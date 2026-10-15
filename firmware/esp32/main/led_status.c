/* led_status -- the device glue for the LED ring. See led_status.h and components/wy_led/wy_led.h.
 *
 * ONE WORKER OWNS THE LIGHTS
 * --------------------------
 * Everything that changes the ring is posted to a small queue and applied by a dedicated task.
 * Nothing else touches the LED core or does I2C on the lights' behalf. That is not tidiness: the
 * callers are the capture task (which the link can FORCE-DELETE while it is mid-call), the
 * playback pump (realtime), and the global ESP timer task (shared with every other timer in the
 * system). A synchronous I2C write on any of those either stalls audio or, if the task is deleted
 * while holding the LED mutex, strands it forever -- FreeRTOS does not release a mutex on task
 * deletion. A queue costs microseconds of latency on a light and removes both hazards.
 *
 * The one exception is led_status_set_brightness(), called from the config path on the link task,
 * which is never force-deleted and needs a synchronous answer for the config report. It takes the
 * mutex directly and is bounded, so a wedged bus cannot hold up a config delivery either. */
#include "led_status.h"

#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "audio_playback.h"
#include "wy_led.h"
#include "wyoming_session.h"

static const char *TAG = "led";

#define LED_TICK_MS          WY_LED_TICK_MS_DEFAULT
#define LED_QUEUE_DEPTH      16
#define LED_WORKER_STACK     4096u
#define LED_WORKER_PRIORITY  5u

static wy_led *led;
static SemaphoreHandle_t lock;
static QueueHandle_t queue;
static int64_t last_advance_us;
static int started;
static int boot_shown;       /* the startup look happens once per MCU boot, never per session */
static int have_first_connect;

/* A posted request. Every phase maps to exactly one look, so the payload is just the request. */
enum {
    REQ_BOOT = 1, REQ_CONNECTED_FIRST, REQ_CONNECTED, REQ_DISCONNECTED,
    REQ_WAKE, REQ_LISTENING, REQ_VOICE, REQ_TRANSCRIPT, REQ_THINKING,
    REQ_SPEAKING, REQ_FOLLOWUP, REQ_IDLE, REQ_ERROR
};

/* The XVF control write the core asked for. One transaction: [resid][cmd][count][p0]... */
static int led_emit(void *user, uint8_t resid, uint8_t cmd, const uint8_t *params, size_t nparams)
{
    (void)user;
    int rc = audio_xvf_write(resid, cmd, params, nparams);
    if (rc != 0) {
        /* Never a link fault: a ring that will not light must not take the voice path down. */
        ESP_LOGW(TAG, "control write failed: resid=%u cmd=%u n=%u rc=%d",
                 (unsigned)resid, (unsigned)cmd, (unsigned)nparams, rc);
    }
    return rc;
}

/* Advance the core to *now*, RETAINING the fractional millisecond. Flooring every delta and then
 * assigning `now` threw away up to 999 us per call: a burst of events less than a millisecond
 * apart could credit no elapsed time at all while real time passed, so every timer ran slow. */
static void led_advance_locked(void)
{
    int64_t now = esp_timer_get_time();
    if (last_advance_us <= 0) {
        last_advance_us = now;
        return;
    }
    if (now <= last_advance_us) return;
    uint32_t delta_ms = (uint32_t)((now - last_advance_us) / 1000);
    if (delta_ms == 0) return;                    /* keep the remainder for next time */
    (void)wy_led_tick(led, delta_ms);
    last_advance_us += (int64_t)delta_ms * 1000;  /* NOT `= now`: the fraction survives */
}

static void apply_locked(int req)
{
    led_advance_locked();
    switch (req) {
    case REQ_BOOT:            (void)wy_led_boot(led);          break;
    case REQ_CONNECTED_FIRST: (void)wy_led_connected(led, 1);  break;
    case REQ_CONNECTED:       (void)wy_led_connected(led, 0);  break;
    case REQ_DISCONNECTED:    (void)wy_led_disconnected(led);  break;
    case REQ_WAKE:            (void)wy_led_wake(led);          break;
    case REQ_LISTENING:       (void)wy_led_listening(led);     break;
    case REQ_VOICE:           (void)wy_led_voice_started(led); break;
    case REQ_TRANSCRIPT:      (void)wy_led_transcript(led);    break;
    case REQ_THINKING:        (void)wy_led_thinking(led);      break;
    case REQ_SPEAKING:        (void)wy_led_speaking(led);      break;
    case REQ_FOLLOWUP:        (void)wy_led_followup(led);      break;
    case REQ_IDLE:            (void)wy_led_idle(led);          break;
    case REQ_ERROR:           (void)wy_led_error(led);         break;
    default: break;
    }
}

/* The worker: the only task that touches the LED core. The queue receive's timeout IS the tick, so
 * there is no separate timer task and nothing shared with the rest of the system runs a light. */
static void led_worker(void *arg)
{
    (void)arg;
    for (;;) {
        int req = 0;
        if (xQueueReceive(queue, &req, pdMS_TO_TICKS(LED_TICK_MS)) == pdTRUE) {
            xSemaphoreTake(lock, portMAX_DELAY);
            apply_locked(req);
            xSemaphoreGive(lock);
            /* Drain whatever queued while we worked, so a burst is not paced by the timeout.
             * Bounded by the queue depth by construction. */
            while (xQueueReceive(queue, &req, 0) == pdTRUE) {
                xSemaphoreTake(lock, portMAX_DELAY);
                apply_locked(req);
                xSemaphoreGive(lock);
            }
        } else {
            xSemaphoreTake(lock, portMAX_DELAY);
            led_advance_locked();
            xSemaphoreGive(lock);
        }
    }
}

/* Post a request. Nonblocking and bounded: if the queue is full the light simply does not change,
 * which is always preferable to stalling a caller that has real work to do. */
static void post(int req)
{
    if (!started || !queue) return;
    if (xQueueSend(queue, &req, 0) != pdTRUE)
        ESP_LOGD(TAG, "LED request %d dropped: queue full", req);
}

/* --- read side: where the array is pointing --- */

/* AEC telemetry lives on its own resource. The resource and command IDs below are PROVISIONAL:
 * Seeed publishes the command NAMES and the LED command IDs, but not these two numbers, so they
 * are inferred from the host_control command map's ordering and must be confirmed with one live
 * probe before they are trusted. The read plumbing is the same pattern the bring-up probe uses for
 * VERSION, which IS verified. */
#define XVF_RESID_AEC              3     /* PROVISIONAL */
#define XVF_CMD_AEC_AZIMUTH_VALUES 24    /* PROVISIONAL */
#define XVF_STATUS_RETRY           1     /* matches the bring-up probe's retry status */

int led_status_read_doa(float *radians_out)
{
    if (!radians_out) return -1;
    /* Four beams: focused 1, focused 2, free-running, auto-selected. The ring follows the fourth. */
    uint8_t cmd[3] = { (uint8_t)XVF_RESID_AEC,
                       (uint8_t)(XVF_CMD_AEC_AZIMUTH_VALUES | 0x80), 16 };
    uint8_t reply[17];
    memset(reply, 0, sizeof(reply));

    /* audio_xvf_read takes the shared bus lock across the whole transaction. It blocks for the
     * duration, so this belongs on a caller that can wait -- not the worker, and not the pump. */
    for (int attempt = 0; attempt < 4; attempt++) {
        if (audio_xvf_read(cmd, sizeof(cmd), reply, sizeof(reply)) != 0) break;
        if (reply[0] == XVF_STATUS_RETRY) continue;
        if (reply[0] == 0) {
            float beams[4];
            memcpy(beams, reply + 1, sizeof(beams));
            *radians_out = beams[3];   /* the auto-selected beam: the one the ring shows */
            return 0;
        }
        break;
    }
    return -1;
}

/* --- lifecycle: every one of these is a post, not a call --- */

void led_status_on_phase(void *user, int phase)
{
    (void)user;
    /* The session says WHAT is happening; this decides what it looks like. */
    switch (phase) {
    case WY_PHASE_LISTENING:  post(REQ_LISTENING);  break;
    case WY_PHASE_TRANSCRIPT: post(REQ_THINKING);   break;
    case WY_PHASE_SPEAKING:   post(REQ_SPEAKING);   break;
    case WY_PHASE_IDLE:       post(REQ_IDLE);       break;
    case WY_PHASE_VOICE:      post(REQ_VOICE);      break;
    case WY_PHASE_FOLLOWUP:   post(REQ_FOLLOWUP);   break;
    case WY_PHASE_ERROR:      post(REQ_ERROR);      break;
    default: break;   /* a phase this build does not know is not a fault */
    }
}

int led_status_boot(void) { post(REQ_BOOT); return 0; }

int led_status_connected(int first_this_boot)
{
    /* The latch decides, and it is read BEFORE it is written. Reading it after setting it made the
     * test a tautology: every caller that said "first" got the green handshake however many times
     * it said it. */
    int first = first_this_boot && !have_first_connect;
    have_first_connect = 1;
    post(first ? REQ_CONNECTED_FIRST : REQ_CONNECTED);
    return 0;
}

int led_status_disconnected(void)  { post(REQ_DISCONNECTED); return 0; }
int led_status_wake(void)          { post(REQ_WAKE);         return 0; }
int led_status_listening(void)     { post(REQ_LISTENING);    return 0; }
int led_status_voice_started(void) { post(REQ_VOICE);        return 0; }
int led_status_transcript(void)    { post(REQ_TRANSCRIPT);   return 0; }
int led_status_thinking(void)      { post(REQ_THINKING);     return 0; }
int led_status_speaking(void)      { post(REQ_SPEAKING);     return 0; }
int led_status_followup(void)      { post(REQ_FOLLOWUP);     return 0; }
int led_status_idle(void)          { post(REQ_IDLE);         return 0; }
int led_status_error(void)         { post(REQ_ERROR);        return 0; }

int led_status_set_brightness(unsigned level_1_31)
{
    /* The synchronous exception. The config plane must know whether the device TOOK the value,
     * and it runs on the link task, which is never force-deleted. Still bounded, so a wedged bus
     * cannot hold up a config delivery. */
    if (!started || !lock || !led) return -1;
    if (xSemaphoreTake(lock, pdMS_TO_TICKS(250)) != pdTRUE) return -1;
    led_advance_locked();
    int rc = wy_led_set_brightness(led, level_1_31);
    xSemaphoreGive(lock);
    return rc;
}

int led_status_start(void)
{
    if (started) return 0;
    lock = xSemaphoreCreateMutex();
    if (!lock) return -1;
    queue = xQueueCreate(LED_QUEUE_DEPTH, sizeof(int));
    if (!queue) return -1;

    led = calloc(1, wy_led_size());
    if (!led) return -1;
    if (wy_led_init(led, led_emit, NULL) != 0) return -1;
    last_advance_us = esp_timer_get_time();

    if (xTaskCreate(led_worker, "led_worker", LED_WORKER_STACK, NULL,
                    LED_WORKER_PRIORITY, NULL) != pdPASS) return -1;

    started = 1;
    ESP_LOGI(TAG, "LED ring ready (XVF3800 GPO servicer, I2C 0x%02x)", WY_LED_XVF_ADDR);
    return 0;
}
