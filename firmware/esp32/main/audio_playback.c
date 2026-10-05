#include "audio_playback.h"

#include "wyoming_audio.h"   /* the amp GPO command encoder + XVF I2C/GPO constants (the audio contract) */
#include "wyoming_session.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

/* The playback (bridge -> speaker) glue. The session core (the gateway task) converts each
 * inbound audio-chunk into the board's I2S format and appends the frames to its bounded ring
 * (wyoming_session.h); this file owns the other half: the I2S TX channel, a task that pumps the
 * ring to the I2S at realtime, and the amplifier's I2C GPO enable. The I2S DMA is the pacer: a
 * bounded blocking write pulls each chunk at exactly the rate the speaker plays it, so the
 * bridge's 2x send pace is absorbed by the ring and the speaker never runs ahead of it.
 *
 * The I2S TX is a channel on the SAME controller (I2S_NUM_0) with the SAME clock as the
 * capture RX: same WS (GPIO7), BCLK (GPIO8), rate (16 kHz), slot width (32-bit stereo). The I2S
 * driver constitutes the two as a full-duplex pair -- it matches the channels on ws/bclk/rate/
 * frame-bits, NOT the data pins -- and shares the port clock: the channel created FIRST is the
 * clock master and the second is demoted to the internal full-duplex slave. In this firmware's
 * lifecycle the speaker is armed at registration and the mic at run-satellite, so the TX is
 * created first and is the master; the capture RX then joins as the slave. (If the order were
 * reversed the driver would still pair them and the shared 16 kHz clock would be identical --
 * the master/slave demotion only decides which channel's registers drive the BCLK/WS that come
 * out on GPIO8/7, and both channels are configured to the same rate.) Either way: one clock,
 * two data lines (TX=GPIO44, RX=GPIO43), and the mic and the speaker never fight over the
 * controller. Enabling/stopping one channel touches only that channel's DMA and FIFO, never the
 * shared clock, so the already-running other channel is not disturbed.
 *
 * The amplifier is an XMOS GPO (X0D31, active-low) over the XVF's I2C GPO Servicer, NOT an
 * ESP32 GPIO: the byte-exact command is wy_amp_gpo_command (host-tested), and this file is a
 * thin transmit of it. The pump task drives it from the session state: armed when an answer
 * becomes active (or is still draining), disarmed when the stream has ended AND the ring is
 * empty -- so it amplifies exactly the audio that is playing, and nothing else.
 *
 * Device-only: proven by the firmware build and the ELF symbols. That the speaker actually
 * produces sound at a conservative level is the live-test gate. */

/* The shared XVF3800 I2S topology (audio_capture.c owns the same constants for the RX): one
 * I2S_NUM_0 controller, 16 kHz, 32-bit stereo, BCLK 8 / WS 7. */
#define PLAYBACK_I2S_PORT      I2S_NUM_0
#define PLAYBACK_I2S_BCLK_PIN  GPIO_NUM_8
#define PLAYBACK_I2S_WS_PIN    GPIO_NUM_7
#define PLAYBACK_I2S_TX_PIN    GPIO_NUM_44
#define PLAYBACK_I2S_RATE_HZ   16000

/* The shared XVF3800 I2C GPO Servicer topology (the bring-up probe uses the same bus): SDA 5,
 * SCL 6, 400 kHz. The address and the command encoding come from wyoming_audio.h, the audio
 * contract's single owner of the XVF's I2C facts. */
#define XVF_I2C_SDA_PIN   GPIO_NUM_5
#define XVF_I2C_SCL_PIN   GPIO_NUM_6
#define XVF_I2C_SCL_HZ    400000

#define PLAYBACK_TASK_PRIORITY  4u   /* the capture task's priority: the two share nothing but time */
#define PLAYBACK_TASK_STACK     6144u
#define PLAYBACK_WRITE_MS       20u   /* ~2x the realtime length of a full batch */
#define PLAYBACK_FRAME_BATCH    256u  /* int32 per I2S write = 128 stereo frames = 8 ms of audio */

static i2s_chan_handle_t tx;
static TaskHandle_t task;
static volatile int stopping;
static void *session;   /* the supervisor's session; the ring's single reader is the pump task */
/* How long audio can still be playing after the ring drains: i2s_channel_write returns when
 * the DMA has COPIED the chunk, not when it has PLAYED it, so up to the whole DMA queue is
 * still in flight when the ring empties. total_dma_buf_size (bytes) / (16 kHz * 2 ch * 4 B)
 * is that many bytes expressed in realtime -- the tail the amp must survive (finding F4). */
static int32_t playback_drain_ms;

void audio_playback_set_session(void *s) { session = s; }

void audio_playback_amp(int enabled) {
    /* A per-call bus: the amp toggles at most twice per answer, so the re-init cost is
     * negligible, and there is no long-lived bus to manage or to conflict with the bring-up
     * probe's own bus. A failed arm/disarm is a silent (or loud) speaker, not a link fault --
     * the link is left alone. */
    uint8_t cmd[XVF_I2C_GPO_CMD_LEN];
    wy_amp_gpo_command(cmd, enabled);
    i2c_master_bus_handle_t bus = NULL;
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = (i2c_port_num_t)-1,
        .sda_io_num = XVF_I2C_SDA_PIN,
        .scl_io_num = XVF_I2C_SCL_PIN,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    if (i2c_new_master_bus(&bus_cfg, &bus) != ESP_OK) return;
    i2c_master_dev_handle_t dev = NULL;
    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = XVF_I2C_ADDRESS,
        .scl_speed_hz = XVF_I2C_SCL_HZ,
        .flags.disable_ack_check = false,
    };
    esp_err_t rc = i2c_master_bus_add_device(bus, &dev_cfg, &dev);
    if (rc == ESP_OK) rc = i2c_master_transmit(dev, cmd, sizeof(cmd), 100);
    i2c_master_bus_rm_device(dev);
    i2c_del_master_bus(bus);
}

static void playback_task_fn(void *argument) {
    (void)argument;
    int32_t frame[PLAYBACK_FRAME_BATCH];   /* one I2S write's worth of stereo-32 frames */
    int amp_armed = 0;
    TickType_t drain_until = 0;   /* tick the amp rests until, once the stream goes quiet */
    for (;;) {
        if (stopping) break;
        void *s = session;
        if (!s) { vTaskDelay(pdMS_TO_TICKS(5)); continue; }
        /* Drive the amplifier from the session state: on exactly while an answer is active or
         * still draining. The subtlety (finding F4): i2s_channel_write returns when the DMA has
         * COPIED the last chunk, not when the speaker has PLAYED it, so the ring can be empty
         * while up to the whole DMA queue is still sounding. Disarming the instant the ring
         * empties would clip the tail of every answer. So when the stream goes quiet we hold
         * the amp for playback_drain_ms (an upper bound on the in-flight DMA) before resting it,
         * and cancel that rest the moment the next answer becomes active or queues audio. The
         * I2C write happens at most once per transition, so the per-call bus cost is immaterial. */
        int playing = wy_session_playback_active(s) || wy_session_playback_bytes(s) > 0;
        if (playing) {
            drain_until = 0;   /* audio is flowing or queued: no pending rest */
            if (!amp_armed) { audio_playback_amp(1); amp_armed = 1; }
        } else if (amp_armed) {
            if (!drain_until) drain_until = xTaskGetTickCount() + pdMS_TO_TICKS(playback_drain_ms);
            if (xTaskGetTickCount() >= drain_until) {
                drain_until = 0;
                audio_playback_amp(0);
                amp_armed = 0;
                vTaskDelay(pdMS_TO_TICKS(10));   /* the stream is over and the DMA has drained */
                continue;
            }
            vTaskDelay(pdMS_TO_TICKS(2));   /* still discharging the DMA: keep the amp on */
            continue;
        }
        size_t n = wy_session_playback_read(s, frame, sizeof(frame));   /* bytes, <= sizeof(frame) */
        if (n == 0) { vTaskDelay(pdMS_TO_TICKS(1)); continue; }
        size_t written = 0;
        if (tx)
            i2s_channel_write(tx, frame, n, &written, pdMS_TO_TICKS(PLAYBACK_WRITE_MS));
        /* a short write (timeout) means the DMA could not take the chunk in time; the frames
         * were already consumed from the ring, so they are dropped rather than re-sent -- a
         * late answer sample is the same as a dropped one, and re-sending it would repeat audio. */
    }
    /* Clear our own handle before self-deleting (the capture task's discipline): a stop that
     * then reads the handle sees NULL for a fully-exited task, and non-NULL only while the
     * task is still live and force-deletable by handle. */
    task = NULL;
    vTaskDelete(NULL);
}

int audio_playback_start(void) {
    if (tx) return 0;   /* already running: a re-start refreshes nothing (the ring already flows) */
    i2s_chan_config_t channel = I2S_CHANNEL_DEFAULT_CONFIG(PLAYBACK_I2S_PORT, I2S_ROLE_MASTER);
    if (i2s_new_channel(&channel, &tx, NULL) != ESP_OK) { tx = NULL; return -1; }
    /* Same clock/slot/WS/BCLK as the capture RX, so the driver constitutes the full-duplex pair
     * and the two share the port clock. The data pin is the TX's (GPIO44); the RX keeps 43. */
    i2s_std_config_t std = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(PLAYBACK_I2S_RATE_HZ),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT,
                                                        I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = PLAYBACK_I2S_BCLK_PIN,
            .ws   = PLAYBACK_I2S_WS_PIN,
            .dout = PLAYBACK_I2S_TX_PIN,
            .din  = I2S_GPIO_UNUSED,
        },
    };
    if (i2s_channel_init_std_mode(tx, &std) != ESP_OK) {
        i2s_del_channel(tx);
        tx = NULL;
        return -1;
    }
    if (i2s_channel_enable(tx) != ESP_OK) {
        i2s_del_channel(tx);
        tx = NULL;
        return -1;
    }
    /* Size the amp's drain rest (F4) from the real DMA queue the driver allocated, not a guess.
     * total_dma_buf_size is the whole queue in bytes; at 16 kHz * 2 ch * 4 B = 64000 B/s that is
     * a real-time duration. It is an UPPER bound on how long audio can still be sounding after the
     * ring drains, so resting the amp exactly that long can never clip the tail, and it is short
     * enough (a few hundred ms) that an idle amp barely overruns its silence. A failed query
     * leaves the drain at 0, i.e. the pre-fix behaviour, which is still safe (no UAF), just a
     * possible clipped tail. */
    i2s_chan_info_t info;
    if (i2s_channel_get_info(tx, &info) == ESP_OK && info.total_dma_buf_size > 0)
        playback_drain_ms = (int32_t)((uint64_t)info.total_dma_buf_size * 1000u / 64000u);
    else
        playback_drain_ms = 0;
    /* The TX clocks out silence until the first write, and the amp stays off until the session
     * says an answer is playing, so starting the channel early (at registration, before the
     * first wake) is what makes the first answer start clean: the DMA is already primed. */
    stopping = 0;
    if (xTaskCreate(playback_task_fn, "plb_task", PLAYBACK_TASK_STACK, NULL,
                    PLAYBACK_TASK_PRIORITY, &task) != pdPASS) {
        i2s_channel_disable(tx);
        i2s_del_channel(tx);
        tx = NULL;
        return -1;
    }
    return 0;
}

void audio_playback_stop(void) {
    stopping = 1;
    if (task) {
        for (int waited = 0; task && waited < 50; ++waited)
            vTaskDelay(pdMS_TO_TICKS(10));
        if (task) vTaskDelete(task);   /* still a live task after the bounded wait: force it */
        task = NULL;
    }
    if (tx) {
        i2s_channel_disable(tx);
        i2s_del_channel(tx);
        tx = NULL;
    }
    audio_playback_amp(0);   /* the link is going down: the speaker rests, even mid-answer */
    stopping = 0;
}

bool audio_playback_running(void) { return tx != NULL; }
