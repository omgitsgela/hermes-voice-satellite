#include "audio_capture.h"

#include "audio_bringup.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "wyoming_session.h"
#include "wyoming_audio.h"
#include <string.h>

/* The I2S RX configuration is the bring-up's, and it is a constant for the same reason: the
 * XVF3800 presents a 32-bit stereo stream at 16 kHz, and the capture path must read exactly
 * what that hardware delivers. (See audio_bringup.c for the pin map. */
#define CAPTURE_I2S_PORT       I2S_NUM_0
#define CAPTURE_I2S_BCLK_PIN   GPIO_NUM_8
#define CAPTURE_I2S_WS_PIN     GPIO_NUM_7
#define CAPTURE_I2S_RX_PIN     GPIO_NUM_43
#define CAPTURE_I2S_RATE_HZ    16000

#define CAPTURE_TASK_STACK     6144
#define CAPTURE_TASK_PRIORITY  4
#define CAPTURE_READ_TIMEOUT_MS 10   /* 10 ms of silence is a tick, not a fault */

static struct {
    wy_session *session;
    void *user;
    int (*write)(void *, const void *, size_t);
    i2s_chan_handle_t rx;
    SemaphoreHandle_t writer_lock;   /* the one lock every link write goes through */
    TaskHandle_t task;
    volatile int stopping;
} capture;

static int writer_lock_acquire(void *user) {
    (void)user;
    SemaphoreHandle_t lock = capture.writer_lock;
    if (!lock) return WY_INVALID;
    return xSemaphoreTake(lock, pdMS_TO_TICKS(100)) == pdPASS ? WY_OK : WY_IO;
}
static int writer_lock_release(void *user) {
    (void)user;
    SemaphoreHandle_t lock = capture.writer_lock;
    if (!lock) return WY_INVALID;
    return xSemaphoreGive(lock) == pdPASS ? WY_OK : WY_IO;
}

void audio_capture_install_writer_lock(void *session) {
    if (!session) return;
    /* The supervisor owns this buffer for its whole life and re-inits it per link; the lock
     * fields are set once at gateway start and must survive the per-link init, which only
     * touches state, identity and the parse workspace. */
    wy_session *s = session;
    s->lock_writer = writer_lock_acquire;
    s->unlock_writer = writer_lock_release;
    s->writer_lock_user = NULL;
}

void audio_capture_send_chunk(const int32_t *frames, size_t frame_count) {
    if (!frames || !capture.session) return;
    int16_t mono[AUDIO_CAPTURE_FRAMES];
    size_t samples = wy_audio_downmix_mono16(frames, frame_count, mono,
                                             sizeof(mono) / sizeof(mono[0]));
    if (!samples) return;
    /* The session decides whether the microphone is open: a frame sent while the stream is
     * closed is dropped there, which is what keeps the pre-wake chime out of the next
     * utterance. */
    wy_session_send_audio(capture.session, mono, samples * 2, capture.write, capture.user);
}

static void capture_task_fn(void *argument) {
    (void)argument;
    int32_t frames[AUDIO_CAPTURE_FRAMES * 2];   /* stereo: two words per frame */
    for (;;) {
        if (capture.stopping) break;
        size_t bytes_read = 0;
        if (capture.rx) {
            esp_err_t result = i2s_channel_read(capture.rx, frames, sizeof(frames),
                                                &bytes_read, CAPTURE_READ_TIMEOUT_MS);
            if (result != ESP_OK && result != ESP_ERR_TIMEOUT) {
                /* A real I2S error: stop reading. The task exits; the next armed hook will
                 * start it again with a fresh channel. */
                break;
            }
        }
        if (capture.stopping) break;
        if (bytes_read % 8) bytes_read -= bytes_read % 8;   /* whole stereo frames only */
        if (bytes_read) {
            audio_capture_send_chunk(frames, bytes_read / 8);
        } else {
            /* Nothing to send (a 10 ms read with no data, or the channel is gone): a short tick
             * so a channel loss cannot turn this loop into a busy spin. */
            vTaskDelay(pdMS_TO_TICKS(5));
        }
    }
    vTaskDelete(NULL);
}

int audio_capture_start(void *session, void *user,
                        int (*write)(void *, const void *, size_t)) {
    if (!session || !write) return -1;
    /* The link pointers are refreshed on every call: a reconnection hands back the SAME task
     * with a new transport, and the task reads these pointers at send time. (During the
     * reconnect window the session is FAILED, so a stray send is dropped at the session
     * before it touches any transport.) */
    capture.session = session;
    capture.user = user;
    capture.write = write;
    if (capture.task) return 0;   /* already running: the refresh above is all that is needed */

    if (!capture.writer_lock) {
        capture.writer_lock = xSemaphoreCreateMutex();
        if (!capture.writer_lock) return -1;
    }
    audio_capture_install_writer_lock(session);

    i2s_chan_config_t channel = I2S_CHANNEL_DEFAULT_CONFIG(CAPTURE_I2S_PORT, I2S_ROLE_MASTER);
    if (i2s_new_channel(&channel, NULL, &capture.rx) != ESP_OK) {
        capture.rx = NULL;
        return -1;
    }
    i2s_std_config_t std = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(CAPTURE_I2S_RATE_HZ),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT,
                                                        I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = CAPTURE_I2S_BCLK_PIN,
            .ws   = CAPTURE_I2S_WS_PIN,
            .dout = I2S_GPIO_UNUSED,
            .din  = CAPTURE_I2S_RX_PIN,
        },
    };
    if (i2s_channel_init_std_mode(capture.rx, &std) != ESP_OK ||
        i2s_channel_enable(capture.rx) != ESP_OK) {
        i2s_del_channel(capture.rx);
        capture.rx = NULL;
        return -1;
    }

    capture.stopping = 0;
    if (xTaskCreate(capture_task_fn, "aud_cap", CAPTURE_TASK_STACK, NULL,
                    CAPTURE_TASK_PRIORITY, &capture.task) != pdPASS || !capture.task) {
        capture.task = NULL;
        i2s_channel_disable(capture.rx);
        i2s_del_channel(capture.rx);
        capture.rx = NULL;
        return -1;
    }
    return 0;
}

void audio_capture_stop(void) {
    capture.stopping = 1;
    /* Take the channel first: the task's next read (10 ms timeout at most) returns with no data
     * and the loop sees the flag. A bounded join keeps the link-down path from wedging; if the
     * task is somehow still there, delete it outright -- it owns no state the link needs. */
    if (capture.rx) {
        i2s_channel_disable(capture.rx);
        i2s_del_channel(capture.rx);
        capture.rx = NULL;
    }
    if (capture.task) {
        for (int waited = 0; capture.task && waited < 50; ++waited)
            vTaskDelay(pdMS_TO_TICKS(10));
        if (capture.task) vTaskDelete(capture.task);
        capture.task = NULL;
    }
    capture.stopping = 0;
}

bool audio_capture_running(void) { return capture.task != NULL; }
