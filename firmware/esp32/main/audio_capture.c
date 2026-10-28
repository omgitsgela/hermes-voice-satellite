#include "audio_capture.h"
#include "esp_log.h"

#include "audio_bringup.h"
#include "wake_detector.h"
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
/* 48 kHz I2S, per docs/audio-fidelity.md. The wake model and the Wyoming uplink are 16 kHz
 * contracts, so audio_capture_send_chunk decimates 3:1 after the downmix; everything downstream
 * of that point is unchanged and still speaks 16 kHz. */
#define CAPTURE_I2S_RATE_HZ    48000
/* CLOCK ROLE: SLAVE. The link rate is not ours to declare -- the XMOS co-processor owns it.
 * Upstream's I2S variants run either as an I2S MASTER at 48 kHz or an I2S SLAVE at 16 kHz, and
 * the two sides "must use complementary roles: connecting two masters causes clock contention,
 * while connecting two slaves leaves the bus without a clock source"
 * (reSpeaker_XVF3800_USB_4MIC_ARRAY, xmos_firmwares/i2s/readme.md). Driving 48 kHz from the
 * ESP32 while the co-processor still held its pre-48 kHz image left the satellite registered,
 * armed and sampling, yet structurally deaf (docs/audio-fidelity.md, open hardware unknown #1).
 * So the ESP32 takes its bit clock and word select FROM the co-processor and only matches its
 * rate. */
#define CAPTURE_I2S_ROLE       I2S_ROLE_SLAVE
/* Same MCLK as the playback side: capture and playback share one I2S port in
 * full-duplex, so there is one master clock for both. */
#define CAPTURE_I2S_MCLK_PIN   GPIO_NUM_9

/* The capture task also runs the wake detector's one-time model + scorer init on its own
 * stack (wake_detector_start -> model_load + scorer_setup -> the TFLM interpreter build,
 * which is several KB of stack by itself). 6 KB was enough when the detector only armed at
 * RUN_REQUESTED; now that a satellite arms at REGISTERED too (responsive the moment it
 * registers), the init runs here more often and a 6 KB stack overflowed into a reboot loop
 * (a stack overflow in task aud_cap). 12 KB gives the interpreter build room to breathe. */
#define CAPTURE_TASK_STACK     12288
/* Capture outranks playback, and both outrank everything cosmetic.
 *
 * Measured on this board: with capture and playback both at the same priority, the mic accounting
 * read exactly 16000 samples/s while idle and then 15,200-15,920 the moment playback started,
 * returning to exactly 16000 when it stopped. Losing up to 5% of the microphone only while
 * transmitting is a scheduling symptom, not a clock one -- the RX DMA overruns while the audio
 * tasks and the TLS/WiFi stack compete for the core. */
#define CAPTURE_TASK_PRIORITY  6
#define CAPTURE_READ_TIMEOUT_MS 10   /* 10 ms of silence is a tick, not a fault */
/* i2s_channel_read takes its timeout in MILLISECONDS and converts to ticks itself, so the
 * call below passes CAPTURE_READ_TIMEOUT_MS (ms), not ticks -- that is already correct. */
/* With CONFIG_FREERTOS_HZ=100 a sub-10 ms vTaskDelay rounds to 0 ticks (a no-op reschedule),
 * so an idle poll must be floored at one tick or the loop busy-spins. */
static TickType_t cap_delay_ms(int ms) {
    TickType_t ticks = pdMS_TO_TICKS(ms);
    return ticks ? ticks : 1;
}

static struct {
    wy_session *session;
    void *user;
    int (*write)(void *, const void *, size_t);
    i2s_chan_handle_t rx;
    SemaphoreHandle_t writer_lock;   /* the one lock every link write goes through */
    TaskHandle_t task;
    volatile int stopping;
} capture;

/* Preserve the original wait bound while diagnosing contention. A timeout is a
 * candidate cause of link loss, not an established diagnosis; log it before tuning. */
#define CAPTURE_WRITER_LOCK_WAIT_MS 100

static int writer_lock_acquire(void *user) {
    (void)user;
    SemaphoreHandle_t lock = capture.writer_lock;
    if (!lock) return WY_INVALID;
    TickType_t started = xTaskGetTickCount();
    int ok = xSemaphoreTake(lock, pdMS_TO_TICKS(CAPTURE_WRITER_LOCK_WAIT_MS)) == pdPASS;
    if (!ok) {
        ESP_LOGW("caplock", "writer lock NOT acquired after %d ms; a link write is stalled",
                 CAPTURE_WRITER_LOCK_WAIT_MS);
        return WY_IO;
    }
    TickType_t waited = xTaskGetTickCount() - started;
    if (waited > pdMS_TO_TICKS(100)) {
        ESP_LOGW("caplock", "writer lock took %u ms (a link write was in flight)",
                 (unsigned)(waited * portTICK_PERIOD_MS));
    }
    return WY_OK;
}
static int writer_lock_release(void *user) {
    (void)user;
    SemaphoreHandle_t lock = capture.writer_lock;
    if (!lock) return WY_INVALID;
    return xSemaphoreGive(lock) == pdPASS ? WY_OK : WY_IO;
}

void audio_capture_install_writer_lock(void *session) {
    if (!session) return;
    /* The supervisor owns this buffer for its whole life and re-inits it per link; wy_session_init
     * preserves the lock fields across that re-init (a re-init that wiped them would let the
     * gateway task and this task send unserialized), so installing them once is enough. */
    wy_session *s = session;
    s->lock_writer = writer_lock_acquire;
    s->unlock_writer = writer_lock_release;
    s->writer_lock_user = NULL;
}

/* Accounting for where the microphone audio actually goes.
 *
 * "The device is streaming but the bridge hears nothing" cannot be diagnosed by
 * looking at either end alone: the chunk can be dropped here (stream closed, a
 * writer-lock timeout, a link error) or lost after it leaves. These counters make
 * the boundary visible, so one wake says which side to look at instead of both. */
static uint32_t chunks_sent;
static uint32_t chunks_dropped;
static uint32_t samples_sent;
static uint32_t samples_dropped;

/* The capture path's 48 kHz -> 16 kHz decimator. ONE instance, driven only from the capture
 * task (audio_capture_send_chunk is called from nowhere else), so its phase history is the
 * stream's own and needs no lock. Reset when a fresh stream starts. */
static wy_decimator capture_decim;

void audio_capture_reset_decimator(void) {
    wy_decimator_reset(&capture_decim);
}

void audio_capture_send_chunk(const int32_t *frames, size_t frame_count) {
    if (!frames || !capture.session) return;
    /* Two stages, in this order. The downmix runs at the I2S rate (48 kHz); the decimator then
     * takes 3:1 down to the 16 kHz the wake model and the Wyoming uplink both contract for.
     * Decimating IN PLACE is safe because the decimator emits one sample per three read, so its
     * write index never overtakes its read index. */
    int16_t wide[AUDIO_CAPTURE_FRAMES];                 /* 48 kHz, the I2S rate */
    int16_t mono[AUDIO_CAPTURE_CHUNK_SAMPLES];          /* 16 kHz, the contract rate */
    size_t samples = wy_audio_downmix_mono16(frames, frame_count, wide,
                                             sizeof(wide) / sizeof(wide[0]));
    if (!samples) return;
    samples = wy_decimate_48000_to_16000(&capture_decim, wide, samples,
                                         mono, sizeof(mono) / sizeof(mono[0]));
    if (!samples) return;   /* fewer than three input samples since the last chunk: nothing yet */
    /* D1 (increment 4): every downmixed chunk feeds the LOCAL wake detector first, always --
     * before the wake the chunk goes only to the detector (the reference's "audio goes to the
     * wake engine, not to the bridge"). The detector is driven from this task alone (its
     * per-chunk sync keeps the deaf guard current). */
    wake_detector_feed(mono, samples);
    /* The session decides whether the microphone is open: a frame sent while the stream is
     * closed is dropped there, which is what keeps the pre-wake chime out of the next
     * utterance. After a wake opens the stream, the same chunks also go to the bridge. */
    int sent = wy_session_send_audio(capture.session, mono, samples * 2,
                                     capture.write, capture.user);
    if (sent == WY_OK) {
        chunks_sent++;
        samples_sent += (uint32_t)samples;
    } else {
        chunks_dropped++;
        samples_dropped += (uint32_t)samples;
    }
    if (chunks_sent + chunks_dropped >= 100) {
        ESP_LOGI("capacct", "mic audio: sent %u chunks / %u samples, dropped %u / %u "
                 "(dropped means the session refused the frame)",
                 chunks_sent, samples_sent, chunks_dropped, samples_dropped);
        chunks_sent = chunks_dropped = samples_sent = samples_dropped = 0;
    }
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
                 * start it again with a fresh channel.
                 *
                 * This exit used to be SILENT, and that is the worst possible place for silence:
                 * the I2S port is shared with playback (the TX joins the same controller as a
                 * full-duplex pair when a reply is armed), so a capture read failure at exactly
                 * that moment is a live candidate for the link dying during a reply -- and from
                 * the bridge all such failures look identical to a device that reset. Report the
                 * driver error so the two can be told apart. */
                ESP_LOGW("capio", "capture read failed: %s (0x%x); capture task exiting",
                         esp_err_to_name(result), (unsigned)result);
                break;
            }
        }
        if (capture.stopping) break;
        if (bytes_read % 8) bytes_read -= bytes_read % 8;   /* whole stereo frames only */
        if (bytes_read) {
            audio_capture_send_chunk(frames, bytes_read / 8);
        } else {
            /* Nothing to send (a 10 ms read with no data, or the channel is gone): a floored
             * tick (one tick minimum; a sub-10 ms delay would be 0 ticks and thus no wait at
             * CONFIG_FREERTOS_HZ=100) so a channel loss cannot turn this loop into a busy
             * spin. */
            vTaskDelay(cap_delay_ms(5));
        }
    }
    /* Announce the exit BEFORE deleting: the join in audio_capture_stop waits for this write
     * to see capture.task clear, and a stale handle is what makes a later force-delete a
     * delete of a freed (possibly reused) TCB. */
    capture.task = NULL;
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

    /* Release a stale channel left by a task that self-exited on an I2S error: it still owns
     * CAPTURE_I2S_PORT, so creating a second channel would fail, and the old handle would
     * otherwise be leaked (the failure path below used to null it). Cleaning it here is what
     * keeps the microphone alive across a glitch plus a pause/run cycle. */
    if (capture.rx) {
        i2s_channel_disable(capture.rx);
        i2s_del_channel(capture.rx);
        capture.rx = NULL;
    }

    if (!capture.writer_lock) {
        capture.writer_lock = xSemaphoreCreateMutex();
        if (!capture.writer_lock) return -1;
    }
    audio_capture_install_writer_lock(session);

    i2s_chan_config_t channel = I2S_CHANNEL_DEFAULT_CONFIG(CAPTURE_I2S_PORT, CAPTURE_I2S_ROLE);
    /* DMA depth is the real underrun protection, because priority alone cannot beat the WiFi and
     * TLS tasks (which run well above any application task). The defaults give roughly 11 ms of
     * slack at 48 kHz stereo 32-bit -- less than one scheduling hiccup. This is ~46 ms:
     * 12 descriptors x 480 frames x 8 bytes. Dropped microphone samples are permanent (the DMA
     * has already overwritten them), so headroom here is worth the 46 KB. */
    channel.dma_desc_num = 12;
    channel.dma_frame_num = 480;
    if (i2s_new_channel(&channel, NULL, &capture.rx) != ESP_OK) {
        /* The usual cause is the other half of the full-duplex pair still owning the controller
         * (a playback channel that has not finished tearing down). Without this line the
         * microphone simply never comes back and nothing in the log says why. */
        ESP_LOGW("capio", "capture: i2s_new_channel failed; the microphone did not start");
        capture.rx = NULL;   /* nothing was allocated; the stale one was already released above */
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
    /* Start the local wake detector on this exact triple, at the moment its feeder (the capture
     * task) starts: the detector is only ever driven by that task, so its lifetime and link
     * pointers are kept identical to the capture's. This is past the `if (capture.task) return`
     * early-out above, so it runs on a REAL task start, not on a pointer refresh. It reads the
     * config wake settings and loads the model from flash once. */
    wake_detector_start(session, user, write);
    /* Fresh filter state for a fresh hardware stream. capture_decim is file-static and holds the
     * previous stream's FIR history and 3:1 phase; without this a restarted channel opens with
     * stale history and a phase that may not match, so the first few outputs of the new stream
     * are filtered against the OLD one's samples. This is the only place it is reset: the
     * capture task is the sole reader of the decimator, and a task START is the moment a new
     * stream begins. Resetting it per wake or from another task would corrupt a live stream. */
    audio_capture_reset_decimator();
    /* Pinned to core 1. WiFi, lwIP and TLS all run on core 0, and the capture task must not
     * compete with them for the same core: a late I2S read is an unrecoverable lost sample. */
    if (xTaskCreatePinnedToCore(capture_task_fn, "aud_cap", CAPTURE_TASK_STACK, NULL,
                    CAPTURE_TASK_PRIORITY, &capture.task, 1) != pdPASS || !capture.task) {
        capture.task = NULL;
        i2s_channel_disable(capture.rx);
        i2s_del_channel(capture.rx);
        capture.rx = NULL;
        wake_detector_stop();
        return -1;
    }
    return 0;
}

void audio_capture_stop(void) {
    capture.stopping = 1;
    /* Disable the channel first: the task's in-flight read returns with an error (the read has
     * a 10 ms timeout at most) and the loop exits. The channel is not DELETED yet, because the
     * task may still be inside the read, and deleting a channel mid-read is a use-after-free. */
    if (capture.rx)
        i2s_channel_disable(capture.rx);
    /* The join waits for the task to clear its own handle (it does so before self-deleting),
     * so a successful join never touches a freed TCB. The bounded wait keeps the link-down path
     * from wedging; at this point the transport is already closed (the on_link_down hook runs
     * after teardown), so any blocked write has already errored out -- the only things the task
     * can still be doing are the 10 ms read and the 5 ms tick, both safe to delete. */
    if (capture.task) {
        for (int waited = 0; capture.task && waited < 50; ++waited)
            vTaskDelay(pdMS_TO_TICKS(10));
        if (capture.task) vTaskDelete(capture.task);   /* still a live task: the clear above is pending */
        capture.task = NULL;
    }
    if (capture.rx) {
        i2s_del_channel(capture.rx);
        capture.rx = NULL;
    }
    /* The detector is stopped after the capture task is joined and the channel released, so no
     * feed can be in flight: its link pointers are dropped exactly when its feeder is gone. */
    wake_detector_stop();
    capture.stopping = 0;
}

/* Bounded confirmation that no write is in flight: the writer lock is free. The TLS teardown
 * calls this between stopping the capture task and freeing the mbedTLS contexts, because an
 * in-flight write still references the contexts through the socket callbacks. In practice the
 * join inside audio_capture_stop is what makes this succeed on the first poll (a task that has
 * exited cannot hold the lock, and the socket close that preceded the stop turns any write in
 * flight into an EBADF that unwinds it); the bound exists so a path that somehow held the lock
 * past the join cannot wedge the teardown. It PEEKS at the lock (a zero-timeout take, given
 * straight back) rather than taking-and-holding it, so a real writer never has to wait on the
 * check. Returns 1 when the lock was found free within the bound, 0 when it was not -- the
 * caller then defers the free to the next establish, where the capture task is provably absent. */
bool audio_capture_wait_writer_idle(void) {
    SemaphoreHandle_t lock = capture.writer_lock;
    if (!lock) return true;   /* no lock installed: nothing is being serialized, nothing to wait */
    /* 400 x one tick, bounded (1 tick at CONFIG_FREERTOS_HZ=100 is 10 ms, so the bound is up to
     * 4 s; the cap_delay_ms floor is what makes the wait real at all). */
    for (int waited = 0; waited < 400; ++waited) {
        if (xSemaphoreTake(lock, 0) == pdPASS) {
            xSemaphoreGive(lock);
            return true;
        }
        vTaskDelay(cap_delay_ms(5));   /* floored: at HZ=100 a 5 ms delay would be 0 ticks,
                                         * turning this 2 s bound into a 400-iteration burst */
    }
    return false;
}

bool audio_capture_running(void) { return capture.task != NULL; }
