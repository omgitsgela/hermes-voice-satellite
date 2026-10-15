/* led_status -- the device glue for the LED ring. See led_status.h and components/wy_led/wy_led.h.
 *
 * Two jobs and no policy: turn the core's control writes into I2C bytes on the XVF3800 bus, and
 * advance the core's timers so the grace period and the flashes actually elapse. */
#include "led_status.h"

#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#include "audio_playback.h"
#include "wy_led.h"

static const char *TAG = "led";

#define LED_TICK_MS   WY_LED_TICK_MS_DEFAULT

/* The core object is allocated, not overlaid on a byte array. A uint8_t buffer cast to a struct
 * pointer is undefined behaviour twice over: it carries no alignment guarantee, and its declared
 * type is incompatible with the struct actually stored through it. Host tests using ctypes
 * allocator-backed storage never exercise that mistake, so it would only ever bite on the device. */
static wy_led *led;
static SemaphoreHandle_t lock;
static esp_timer_handle_t tick_timer;
static int64_t last_advance_us;
static int started;
static int boot_shown;       /* the startup look happens once per MCU boot, never per session */
static int have_first_connect;

/* The XVF control write the core asked for. One transaction: [resid][cmd][count][p0]... */
static int led_emit(void *user, uint8_t resid, uint8_t cmd, const uint8_t *params, size_t nparams)
{
    (void)user;
    int rc = audio_xvf_write(resid, cmd, params, nparams);
    if (rc != 0) {
        /* The LEDs are feedback, never a link fault. A ring that will not light must not take the
         * voice path down with it, so this is a warning and the caller keeps going. */
        ESP_LOGW(TAG, "control write failed: resid=%u cmd=%u n=%u rc=%d",
                 (unsigned)resid, (unsigned)cmd, (unsigned)nparams, rc);
    }
    return rc;
}

static void led_take(void) { if (lock) xSemaphoreTake(lock, portMAX_DELAY); }
static void led_give(void) { if (lock) xSemaphoreGive(lock); }

/* Advance the core to *now* and remember when. Called before every lifecycle event as well as on
 * the timer, so an action is never handed time that elapsed before it existed. Without this a
 * sweep started just before a tick would be aged by the whole tick period at once, and the 30 s
 * outage grace would arrive early. */
static void led_advance_locked(void)
{
    int64_t now = esp_timer_get_time();
    if (last_advance_us > 0 && now > last_advance_us) {
        uint32_t delta_ms = (uint32_t)((now - last_advance_us) / 1000);
        if (delta_ms > 0) (void)wy_led_tick(led, delta_ms);
    }
    last_advance_us = now;
}

static void tick_cb(void *arg)
{
    (void)arg;
    led_take();
    led_advance_locked();
    led_give();
}

/* --- the read side: where the array is pointing --- */

/* AEC telemetry lives on its own resource. The resource and command IDs below are PROVISIONAL:
 * Seeed publishes the command NAMES and the LED command IDs, but not these two numbers, so they
 * are inferred from the host_control command map's ordering and must be confirmed with one live
 * probe (the console's XVF_WRITE / a read probe) before this is trusted. The read plumbing is the
 * same pattern the bring-up probe uses for VERSION, which IS verified. */
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

    /* The bus is shared with the audio path; take the same lock the LED writes use. */
    int rc = -1;
    for (int attempt = 0; attempt < 4; attempt++) {
        if (audio_xvf_read(cmd, sizeof(cmd), reply, sizeof(reply)) != 0) break;
        if (reply[0] == XVF_STATUS_RETRY) continue;
        if (reply[0] == 0) {
            float beams[4];
            memcpy(beams, reply + 1, sizeof(beams));
            *radians_out = beams[3];   /* the auto-selected beam: the one the ring shows */
            rc = 0;
        }
        break;
    }
    return rc;
}

/* --- lifecycle mirrors --- */

#define MIRROR(name, call)                                      \
    int led_status_##name(void) {                               \
        int rc;                                                 \
        if (!started) return -1;                                \
        led_take(); rc = call; led_give();                      \
        return rc;                                              \
    }

#define MIRROR_T(name, call)                                    \
    int led_status_##name(void) {                               \
        int rc;                                                 \
        if (!started) return -1;                                \
        led_take(); led_advance_locked(); rc = call; led_give();\
        return rc;                                              \
    }

MIRROR_T(disconnected, wy_led_disconnected(led))
MIRROR_T(wake, wy_led_wake(led))
MIRROR_T(listening, wy_led_listening(led))
MIRROR_T(voice_started, wy_led_voice_started(led))
MIRROR_T(transcript, wy_led_transcript(led))
MIRROR_T(thinking, wy_led_thinking(led))
MIRROR_T(speaking, wy_led_speaking(led))
MIRROR_T(followup, wy_led_followup(led))
MIRROR_T(idle, wy_led_idle(led))
MIRROR_T(error, wy_led_error(led))

/* The startup look is once per MCU boot. Reaching it on every gateway-task start would replay the
 * white breath and the ready flash on a session restart that the person never noticed. */
int led_status_boot(void)
{
    int rc = 0;
    if (!started) return -1;
    led_take();
    led_advance_locked();
    if (!boot_shown) {
        boot_shown = 1;
        rc = wy_led_boot(led);
    }
    led_give();
    return rc;
}

int led_status_connected(int first_this_boot)
{
    int rc;
    if (!started) return -1;
    led_take();
    led_advance_locked();
    /* The latch decides, and it is read BEFORE it is written. Reading it after setting it made the
     * test a tautology: `first_this_boot && have_first_connect` always equalled first_this_boot,
     * so every caller that said "first" got the green handshake however many times it said it. */
    int first = first_this_boot && !have_first_connect;
    have_first_connect = 1;
    rc = wy_led_connected(led, first);
    led_give();
    return rc;
}

int led_status_set_brightness(unsigned level_1_31)
{
    int rc;
    if (!started) return -1;
    led_take(); led_advance_locked(); rc = wy_led_set_brightness(led, level_1_31); led_give();
    return rc;
}

int led_status_start(void)
{
    if (started) return 0;
    lock = xSemaphoreCreateMutex();
    if (!lock) return -1;

    led = calloc(1, wy_led_size());
    if (!led) return -1;

    if (wy_led_init(led, led_emit, NULL) != 0) return -1;
    last_advance_us = esp_timer_get_time();

    const esp_timer_create_args_t targs = {
        .callback = tick_cb,
        .arg = NULL,
        .dispatch_method = ESP_TIMER_TASK,
        .name = "led_tick",
    };
    if (esp_timer_create(&targs, &tick_timer) != ESP_OK) return -1;
    if (esp_timer_start_periodic(tick_timer, (uint64_t)LED_TICK_MS * 1000u) != ESP_OK) return -1;

    started = 1;
    ESP_LOGI(TAG, "LED ring ready (XVF3800 GPO servicer, I2C 0x%02x)", WY_LED_XVF_ADDR);
    return 0;
}

