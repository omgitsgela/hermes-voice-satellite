#include "audio_playback.h"
#include "driver/gpio.h"

#include "wyoming_audio.h"   /* the amp GPO command encoder + XVF I2C/GPO constants (the audio contract) */
#include "wyoming_session.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "esp_err.h"
#include "esp_log.h"
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
/* MCLK feeds the XVF3800 and, through it, the AIC3104: GPIO9 on the XIAO ESP32S3.
 * Seeed publishes this pin as part of the board's I2S pin map. */
#define PLAYBACK_I2S_MCLK_PIN  GPIO_NUM_9

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

/* With CONFIG_FREERTOS_HZ=100 a sub-10 ms delay rounds to 0 ticks, and vTaskDelay(0) is
 * "just forces a reschedule" -- it does not block. A polling loop that relied on it would
 * peg a core (the idle pump is one such loop for the satellite's whole registered life, and
 * TWDT watches the idle task), so every idle wait in this task is floored at one tick
 * (10 ms). The I2S DMA queue (~100 ms of audio) is the real playback buffer, so 10 ms of
 * polling granularity cannot glitch the seam. */
static TickType_t plb_delay_ms(int ms) {
    TickType_t ticks = pdMS_TO_TICKS(ms);
    return ticks ? ticks : 1;
}

static i2s_chan_handle_t tx;
static TaskHandle_t task;
static volatile int stopping;
static void *session;   /* the supervisor's session; the ring's single reader is the pump task */
/* When set, the amplifier stays in whatever state it was forced to and the pump leaves it
 * alone. Without this the pump releases the amp the moment the ring drains, so a manual
 * "AMP 1" lasts about one chime -- which silently invalidated every listen test that tried to
 * hear a non-playback source (the beamformed mic, or the codec's own state). */
static int amp_locked;


/* Keepalive silence: the DMA is fed zeros whenever the ring is empty instead of being left to
 * run dry. An I2S peripheral whose DMA starves can leave the line in an undefined state between
 * bursts, which a downstream codec decodes as ticking rather than silence. Writing real zeros
 * keeps the frame structure continuous and the downstream device in a settled state. */
static const int32_t playback_silence[PLAYBACK_FRAME_BATCH] = { 0 };
/* How long audio can still be playing after the ring drains: i2s_channel_write returns when
 * the DMA has COPIED the chunk, not when it has PLAYED it, so up to the whole DMA queue is
 * still in flight when the ring empties. total_dma_buf_size (bytes) / (16 kHz * 2 ch * 4 B)
 * is that many bytes expressed in realtime -- the tail the amp must survive (finding F4). */
static int32_t playback_drain_ms;

/* How long the amplifier stays armed after the ring goes quiet. This must cover NETWORK CHUNK
 * GAPS, not just the DMA drain: TTS arrives from the gateway in chunks, and if the amplifier
 * rests during every gap then the next chunk re-arms it and replays the settling lead-in -- one
 * ~20ms dropout per chunk, which is exactly the choppiness heard in speech. playback_drain_ms is
 * sized to the DMA (a few hundred ms) and is far too short for this. */
#define PLAYBACK_AMP_HOLD_MS 1500

void audio_playback_set_session(void *s) { session = s; }

/* Read back the live session pointer. Console diagnostics need to enqueue audio through the
 * same session the pump serves, without having to thread that pointer in from elsewhere. */
void *audio_playback_session(void) { return session; }

/* Queue mono16 audio for playback right now.
 *
 * This is the play entry point for diagnostics (chime, tone generators). It keeps the session
 * type and wy_session_local_play inside this module, so callers such as the provisioning console
 * need only audio_playback.h -- which is what lets the console stay testable against its stub
 * harness instead of pulling in the whole session component. */
/* Tri-state the I2S data-out line (a diagnostic: floating it reproduces the failure mode of an
 * undriven line). Lives here so the console needs no ESP-IDF GPIO headers of its own. */
void audio_playback_i2s_float(int enable) {
    gpio_set_direction(PLAYBACK_I2S_TX_PIN, enable ? GPIO_MODE_INPUT : GPIO_MODE_OUTPUT);
}

int audio_playback_play_mono(const int16_t *mono, size_t n) {
    if (!session || !mono || n == 0) return -1;
    return wy_session_local_play((wy_session *)session, mono, n) == WY_OK ? 0 : -1;
}

/* Send one raw XVF3800 control transaction over the shared I2C pins, reusing the amp's per-call
 * bus. Returns 0 on success, or the failing esp_err_t (negated) so a caller can report it. */
/* The 3.5 mm output on this board is driven by an AIC3104 codec at 0x18 -- a DIFFERENT chip from
 * the XVF3800 voice processor at 0x2c. It powers up muted, so until it is initialised the jack
 * produces nothing at all regardless of what the XVF3800 routes to it. Register meanings and
 * values follow Seeed's own example for this board (XIAO ESP32S3 + XVF3800). */
/* Codec DAC digital volume: 0.5 dB attenuation steps, 0x00 = 0 dB (max).
 * 0x0C = -6 dB. Raise toward 0x00 for louder, lower for quieter. */
#define DAC_VOLUME_STEP 0x0C

#define AIC3104_ADDR            0x18
#define AIC3104_PAGE_CTRL       0x00
#define AIC3104_LEFT_DAC_VOL    0x2B
#define AIC3104_RIGHT_DAC_VOL   0x2C
#define AIC3104_HPLOUT_LEVEL    0x33
#define AIC3104_HPROUT_LEVEL    0x41
#define AIC3104_LEFT_LOP_LEVEL  0x56
#define AIC3104_RIGHT_LOP_LEVEL 0x5D
#define AIC3104_SER_IF_CTRL     0x09   /* Page 0 reg 9: serial data format + word length */
/* Reg 9 layout (TI SLAA403): D7-D6 transfer mode (00 = I2S), D5-D4 word length (00 = 16,
 * 01 = 20, 10 = 24, 11 = 32 bits). The chip powers up at 16-bit I2S, but the XVF3800 drives
 * its DAC port at 32 bits -- and a word-length mismatch is heard as continuous clicking, not
 * as silence and not as audio. This is the value that matches what actually arrives. */
#define AIC3104_I2S_32BIT       0x30

static int xvf_control_write_at(uint8_t addr, const uint8_t *cmd, size_t len, const char *what) {
    i2c_master_bus_handle_t bus = NULL;
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = (i2c_port_num_t)-1,
        .sda_io_num = XVF_I2C_SDA_PIN,
        .scl_io_num = XVF_I2C_SCL_PIN,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    esp_err_t rc = i2c_new_master_bus(&bus_cfg, &bus);
    if (rc != ESP_OK) { ESP_LOGW("xvf", "%s: bus failed %d (addr=0x%02x)", what, (int)rc, (unsigned)addr);
        return -(int)rc; }
    i2c_master_dev_handle_t dev = NULL;
    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = addr,
        .scl_speed_hz = XVF_I2C_SCL_HZ,
        .flags.disable_ack_check = false,
    };
    rc = i2c_master_bus_add_device(bus, &dev_cfg, &dev);
    if (rc == ESP_OK) rc = i2c_master_transmit(dev, cmd, len, 100);
    if (rc != ESP_OK) {
        ESP_LOGW("xvf", "%s: write failed %d (addr=0x%02x b0=%u b1=%u)", what, (int)rc,
                 (unsigned)addr, (unsigned)cmd[0], (unsigned)cmd[1]);
    } else {
        ESP_LOGI("xvf", "%s: ok (addr=0x%02x b0=%u b1=%u)", what,
                 (unsigned)addr, (unsigned)cmd[0], (unsigned)cmd[1]);
    }
    i2c_master_bus_rm_device(dev);
    i2c_del_master_bus(bus);
    return rc == ESP_OK ? 0 : -(int)rc;
}

/* Set the XVF3800's output mux: which signal the DAC plays. Category 4 (far end / reference)
 * source 0 is the host's own playback arriving over I2S -- what the speaker should be playing. */
/* Write an arbitrary XVF3800 control register: [resid][cmd][count][p0]... in one transaction.
 * For probing the parts of the device that gate what it sends to the DAC. */
int audio_xvf_write(uint8_t resid, uint8_t cmd, const uint8_t *params, size_t nparams) {
    if (nparams > 16) return -1;
    uint8_t buf[19];
    buf[0] = resid;
    buf[1] = cmd;
    buf[2] = (uint8_t)nparams;
    for (size_t i = 0; i < nparams; ++i) buf[3 + i] = params[i];
    return xvf_control_write_at(XVF_I2C_ADDRESS, buf, 3 + nparams, "xvf write");
}

int audio_xvf_route_output(int right, int category, int source) {
    uint8_t cmd[XVF_I2C_GPO_CMD_LEN];
    wy_output_route_command(cmd, right, category, source);
    return xvf_control_write_at(XVF_I2C_ADDRESS, cmd, sizeof(cmd), right ? "route R" : "route L");
}

static int aic_write(uint8_t reg, uint8_t val) {
    uint8_t cmd[2] = { reg, val };
    return xvf_control_write_at(AIC3104_ADDR, cmd, sizeof(cmd), "aic3104");
}

/* Power up and unmute the AIC3104 that drives the 3.5 mm jack. Idempotent: safe to
 * call on every playback start. Without this the jack is silent no matter what the
 * XVF3800 routes to it. */
/* Read one AIC3104 register back. Verifying a write actually stuck is the difference between
 * "the codec is configured" and "the codec ignored us". Returns 0 and stores the value, or a
 * negative error. */
/* Write one AIC3104 register. Companion to audio_dac_read: write, then read back, is how you
 * know a configuration value actually took effect. */
int audio_dac_write(uint8_t reg, uint8_t value) {
    uint8_t cmd[2] = { reg, value };
    return xvf_control_write_at(AIC3104_ADDR, cmd, sizeof(cmd), "aic write");
}

int audio_dac_read(uint8_t reg, uint8_t *value) {
    if (!value) return -1;
    i2c_master_bus_handle_t bus = NULL;
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = (i2c_port_num_t)-1,
        .sda_io_num = XVF_I2C_SDA_PIN,
        .scl_io_num = XVF_I2C_SCL_PIN,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    esp_err_t rc = i2c_new_master_bus(&bus_cfg, &bus);
    if (rc != ESP_OK) return -(int)rc;
    i2c_master_dev_handle_t dev = NULL;
    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = AIC3104_ADDR,
        .scl_speed_hz = XVF_I2C_SCL_HZ,
        .flags.disable_ack_check = false,
    };
    rc = i2c_master_bus_add_device(bus, &dev_cfg, &dev);
    if (rc == ESP_OK) rc = i2c_master_transmit_receive(dev, &reg, 1, value, 1, 200);
    i2c_master_bus_rm_device(dev);
    i2c_del_master_bus(bus);
    return rc == ESP_OK ? 0 : -(int)rc;
}

int audio_dac_init(void) {
    int rc = 0;
    rc |= aic_write(AIC3104_PAGE_CTRL, 0x00);

    /* ---- Clocking first: without a locked PLL nothing else means anything. ----------
     * The XVF3800 clocks the AIC3104 with BCLK = 1.024 MHz (16 kHz x 64) and there is no
     * separate MCLK reaching this chip, so the PLL is fed from BCLK. TI's documented
     * relation is fs(ref) = (PLLCLK_IN x K x R) / (2048 x P), which for BCLK 1.024 MHz and
     * J=48, D=0, R=2, P=1 gives exactly 48 kHz; register 2 then selects fs = fs(ref)/3 =
     * 16 kHz. The register sequence follows a known-working AIC3104 setup, rescaled to our
     * BCLK. Register 3: PLL enable (D7), Q (D6-D3), P (D2-D0). */
    rc |= aic_write(0x66, 0xA2);   /* reg 102: CLKDIV_IN and PLLCLK_IN both = BCLK */
    rc |= aic_write(0x65, 0x00);   /* reg 101: CODEC_CLKIN = PLLDIV_OUT */
    rc |= aic_write(0x03, 0x91);   /* reg 3:   PLL on, Q=2, P=1 */
    rc |= aic_write(0x04, 0x30);   /* reg 4:   J = 48 */
    rc |= aic_write(0x05, 0x00);   /* reg 5:   D high = 0 */
    rc |= aic_write(0x06, 0x00);   /* reg 6:   D low  = 0 */
    rc |= aic_write(0x0B, 0x02);   /* reg 11:  R = 2 */
    rc |= aic_write(0x02, 0x44);   /* reg 2:   fs = fs(ref) / 3 -> 16 kHz */

    /* ---- Serial interface format: must match the 32-bit stream the XVF3800 sends. ----- */
    rc |= aic_write(AIC3104_SER_IF_CTRL, AIC3104_I2S_32BIT);

    /* ---- Data path and DAC power-up. ------------------------------------------------ */
    rc |= aic_write(0x07, 0x0A);   /* reg 7:  both DAC data paths connected -- left DAC
                                 * plays the left channel, right DAC the right. 0x08
                                 * routes only the left, leaving the right side dead. */
    rc |= aic_write(0x25, 0xC0);   /* reg 37: power up both DAC channels */
    rc |= aic_write(0x29, 0x00);   /* reg 41: DAC output switching -- left DAC drives the
                                    * left output driver. Without this the DAC runs but its
                                    * output reaches nothing. */
    /* reg 43: left DAC digital volume. 0x00 is 0 dB -- the MAXIMUM, not silence: this
     * register attenuates in 0.5 dB steps down to -63.5 dB. At 0 dB the codec was driven
     * to full scale and speech sounded overmodulated. 0x0C = 12 steps = -6 dB. */
    rc |= aic_write(0x2B, DAC_VOLUME_STEP);
    rc |= aic_write(0x52, 0x80);   /* reg 82: route DAC_L1 to the left line outputs */
    rc |= aic_write(0x5C, 0x80);   /* reg 92: route DAC_R1 to the right line outputs */
    rc |= aic_write(0x2A, 0x00);   /* reg 42: right DAC output switching */
    /* reg 44: right DAC digital volume, matched to the left. */
    rc |= aic_write(0x2C, DAC_VOLUME_STEP);

    rc |= aic_write(AIC3104_HPLOUT_LEVEL, 0x0D);   /* powered up, unmuted */
    rc |= aic_write(AIC3104_HPROUT_LEVEL, 0x0D);
    rc |= aic_write(AIC3104_LEFT_LOP_LEVEL, 0x0B); /* line out up */
    rc |= aic_write(AIC3104_RIGHT_LOP_LEVEL, 0x0B);
    ESP_LOGI("dac", "AIC3104 init %s", rc == 0 ? "ok" : "FAILED");
    return rc;
}

void audio_playback_amp_lock(int locked) {
    amp_locked = locked ? 1 : 0;
    ESP_LOGI("plb_amp", "amp %s", locked ? "LOCKED (pump will not touch it)" : "unlocked");
}

/* Returns 0 when the amplifier really changed state. Callers must NOT assume success:
 * a silently failed arm is a silent speaker, and recording it as armed would leave
 * nothing to retry. */
int audio_playback_amp(int enabled) {
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
    /* Every step is reported now. This path used to fail in silence, and a failed arm is a
     * silent speaker -- which is exactly what a chime that plays as clicks looks like from the
     * outside. One line per attempt is cheap (the amp toggles at most twice per answer). */
    esp_err_t rc = i2c_new_master_bus(&bus_cfg, &bus);
    if (rc != ESP_OK) {
        ESP_LOGW("plb_amp", "amp %d: i2c_new_master_bus failed %d (sda=%d scl=%d)",
                 enabled, (int)rc, (int)XVF_I2C_SDA_PIN, (int)XVF_I2C_SCL_PIN);
        return (int)rc;
    }
    i2c_master_dev_handle_t dev = NULL;
    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = XVF_I2C_ADDRESS,
        .scl_speed_hz = XVF_I2C_SCL_HZ,
        .flags.disable_ack_check = false,
    };
    rc = i2c_master_bus_add_device(bus, &dev_cfg, &dev);
    if (rc == ESP_OK) rc = i2c_master_transmit(dev, cmd, sizeof(cmd), 100);
    if (rc != ESP_OK) {
        ESP_LOGW("plb_amp", "amp %d: i2c write failed %d (addr=0x%02x cmd=0x%02x)",
                 enabled, (int)rc, (unsigned)XVF_I2C_ADDRESS, (unsigned)cmd[0]);
    } else {
        ESP_LOGI("plb_amp", "amp %d: ok (addr=0x%02x cmd=0x%02x)",
                 enabled, (unsigned)XVF_I2C_ADDRESS, (unsigned)cmd[0]);
    }
    i2c_master_bus_rm_device(dev);
    i2c_del_master_bus(bus);
    return (int)rc;
}

static int pump_logged;

/* Where to report `played`, and whether this answer has already been reported.
 *
 * `played` is the device saying its speaker finished. Without it the bridge waits
 * after every reply for an event that never arrives, which measured as ~20 s of
 * dead air per turn. The link pointers are refreshed on every bind, exactly like
 * the capture path: a reconnect hands back the same pump with a new transport. */
static wy_session *playback_session;
static void *playback_user;
static int (*playback_write)(void *, const void *, size_t);
static int played_reported = 1;   /* nothing to report until a stream is armed */

void audio_playback_bind(void *session, void *user,
                         int (*write)(void *, const void *, size_t)) {
    playback_session = (wy_session *)session;
    playback_user = user;
    playback_write = write;
}

/* Bounded retry for a stalled I2S write. Each idle attempt costs PLAYBACK_WRITE_MS plus a 1 ms
 * yield; this many in a row means the channel is genuinely not taking data, and the batch must be
 * abandoned rather than spun on until the task watchdog reboots the device. */
#define PLAYBACK_WRITE_MAX_IDLE 50

static void playback_task_fn(void *argument) {
    (void)argument;
    int32_t frame[PLAYBACK_FRAME_BATCH];   /* one I2S write's worth of stereo-32 frames */
    int amp_armed = 0;
    TickType_t drain_start = 0;   /* tick the stream went quiet (0 = not draining) */
    TickType_t drain_dur = 0;     /* ticks to hold the amp after the stream went quiet */
    for (;;) {
        if (stopping) break;
        void *s = session;
        if (!s) { vTaskDelay(plb_delay_ms(5)); continue; }
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
            drain_start = 0;   /* audio is flowing or queued: no pending rest */
            /* Amplifier settling lead-in -- ONLY on a genuine cold start, i.e. when the amp is
             * actually off. Writing the first audio at the same instant the amplifier is enabled
             * clips the leading edge (the first chime was heard cut off). Establish silence, let
             * the amplifier settle, then let the audio follow.
             *
             * It must NOT run on every ring-empty -> ring-full transition. TTS arrives over the
             * network in chunks, so the ring empties and refills constantly, and a lead-in on every
             * gap inserts a ~20ms dropout per chunk: that is the choppiness heard in speech, and
             * it is invisible in a single-shot chime. Gate on the amplifier, not on the gap. */
            if (!amp_armed && !amp_locked) {
                size_t settle_written = 0;
                i2s_channel_write(tx, playback_silence, sizeof(playback_silence),
                                  &settle_written, PLAYBACK_WRITE_MS);
                if (audio_playback_amp(1) == 0) amp_armed = 1;
                vTaskDelay(plb_delay_ms(20));
            }
            if (!amp_armed && !amp_locked) { if (audio_playback_amp(1) == 0) amp_armed = 1; }
        } else if (amp_armed) {
            if (!drain_start) {
                drain_start = xTaskGetTickCount();
                drain_dur = plb_delay_ms(
                    playback_drain_ms > PLAYBACK_AMP_HOLD_MS
                        ? playback_drain_ms : PLAYBACK_AMP_HOLD_MS);
            }
            /* Wrap-safe: elapsed ticks since the stream went quiet, compared to the window.
             * Valid for any window below 2^31 ticks (ours is a few hundred ms), so a tick
             * counter that rolls over ~49.7 days out cannot flip the comparison. */
            if (xTaskGetTickCount() - drain_start >= drain_dur) {
                drain_start = 0;
                if (!amp_locked) { if (audio_playback_amp(0) == 0) amp_armed = 0; }
                else amp_armed = 0;
            }
            /* Deliberately NO continue here. Both arms of this branch used to `continue`
             * before the keepalive, so the amplifier hold window was ALSO a window with no DMA
             * replenishment at all: holding the amp for 1500 ms introduced a measured 1518 ms
             * gap between the last real write and the first zero write. With the SDK's DMA
             * auto-clear flags off, that starved DMA replays stale audio instead of silence --
             * which is exactly the choppiness in speech. Amplifier bookkeeping falls through so
             * the keepalive below runs unconditionally. */
        }
        /* A stream that has ENDED and a ring that has DRAINED means the speaker is
         * finished: say so exactly once. Re-armed by the rising edge of the bridge's
         * playback flag, so the next answer is reported too. */
        if (wy_session_playback_active(s)) {
            played_reported = 0;
        } else if (!played_reported && wy_session_playback_ended(s) &&
                   wy_session_playback_bytes(s) == 0) {
            if (playback_write &&
                wy_session_send_played(s, playback_write, playback_user) == WY_OK) {
                ESP_LOGI("plb", "played: reported to the gateway");
            } else {
                ESP_LOGW("plb", "played: could not be reported (link=%p write=%p)",
                         playback_user, (const void *)playback_write);
            }
            played_reported = 1;
        }
        size_t n = wy_session_playback_read(s, frame, sizeof(frame));   /* bytes, <= sizeof(frame) */
        if (n == 0) {
            /* Keepalive: push one batch of real zeros rather than idling the DMA. The write's
             * own timeout is the pacing, so this yields exactly like the delay did. */
            size_t silence_written = 0;
            i2s_channel_write(tx, playback_silence, sizeof(playback_silence),
                              &silence_written, PLAYBACK_WRITE_MS);
            continue;
        }
        if (!pump_logged) { ESP_LOGI("plb", "pump: first %u frames to I2S", (unsigned)n); pump_logged = 1; }
        size_t written = 0;
        if (tx) {
            /* The driver converts its timeout_ms to ticks itself (xQueueReceive(pdMS_TO_TICKS
             * (timeout_ms))), so pass MILLISECONDS, not ticks -- a double conversion at
             * CONFIG_FREERTOS_HZ=100 would collapse the 20 ms to 0 and every write would
             * "time out" at the first DMA boundary, dropping the rest of the batch. The short
             * write then means "the DMA could not take the chunk in time"; the frames were
             * already consumed from the ring, so they are dropped rather than re-sent -- a
             * late answer sample is the same as a dropped one, and re-sending would repeat
             * audio. */
            /* Write the whole batch. i2s_channel_write may copy fewer bytes than asked (a
             * timeout with the DMA full); dropping the remainder would silently truncate audio
             * and show up as exactly the choppiness we were chasing, so push the unwritten tail
             * rather than losing it. */
            size_t off = 0;
            int idle_tries = 0;
            while (off < n && !stopping) {
                written = 0;
                esp_err_t wrc = i2s_channel_write(tx, (const uint8_t *)frame + off, n - off,
                                                 &written, PLAYBACK_WRITE_MS);
                off += written;
                /* A zero-progress write means "the DMA is full", NOT "discard the rest".
                 * Breaking there silently deleted the tail of every batch that timed out -- a
                 * reproduced loss of half a batch -- and each deletion is a waveform
                 * discontinuity heard as choppiness. So the unwritten tail is retried.
                 *
                 * BUT the retry must be BOUNDED and must honour shutdown. An unbounded loop
                 * spins forever when the channel stops accepting data (DMA wedged, or the
                 * channel torn down during link close), the task watchdog fires, and the whole
                 * device reboots -- which is what was observed: a wake, a transcript and a
                 * started turn, then "Connection reset by peer" as the board came back up.
                 * Retrying is correct; retrying without a limit is a crash. */
                if (written == 0) {
                    if (++idle_tries >= PLAYBACK_WRITE_MAX_IDLE) {
                        ESP_LOGW("plb", "pump: abandoning %u unwritten bytes after %d idle "
                                 "writes (rc=%d); the channel is not accepting data",
                                 (unsigned)(n - off), idle_tries, (int)wrc);
                        break;
                    }
                    vTaskDelay(plb_delay_ms(1));   /* let the DMA drain, then retry the tail */
                    continue;
                }
                idle_tries = 0;
                if (wrc != ESP_OK && wrc != ESP_ERR_TIMEOUT) {
                    /* A real channel fault: stop retrying this batch rather than spinning. */
                    ESP_LOGW("plb", "pump: i2s write failed %d after %u of %u bytes",
                             (int)wrc, (unsigned)off, (unsigned)n);
                    break;
                }
            }
        }
    }
    /* Clear our own handle before self-deleting (the capture task's discipline): a stop that
     * then reads the handle sees NULL for a fully-exited task, and non-NULL only while the
     * task is still live and force-deletable by handle. */
    task = NULL;
    vTaskDelete(NULL);
}

int audio_playback_start(void) {
    if (tx) return 0;   /* already running: a re-start refreshes nothing (the ring already flows) */
    /* The codec that drives the 3.5 mm jack powers up muted. Bring it up before anything plays,
     * or every answer is silent regardless of the routing and the ring state. */
    audio_dac_init();
    i2s_chan_config_t channel = I2S_CHANNEL_DEFAULT_CONFIG(PLAYBACK_I2S_PORT, I2S_ROLE_MASTER);
    /* Clear each TX DMA buffer as soon as it has been played. The SDK default leaves this OFF,
     * which means a buffer that is replayed without being refilled repeats STALE audio instead of
     * silence -- so any momentary underrun becomes an audible glitch rather than a quiet gap.
     * Underrun protection costs one memset per completed buffer. */
    channel.auto_clear_after_cb = true;
    if (i2s_new_channel(&channel, &tx, NULL) != ESP_OK) {
        /* The usual cause is the capture RX already owning I2S_NUM_0 in a state the driver will
         * not pair with. Silent failure here means an answer is produced, sent, and never played,
         * with nothing in the log -- indistinguishable from the device ignoring the reply. */
        ESP_LOGW("plb", "playback: i2s_new_channel failed; no reply can be played");
        tx = NULL; return -1;
    }
    /* Same clock/slot/WS/BCLK as the capture RX, so the driver constitutes the full-duplex pair
     * and the two share the port clock. The data pin is the TX's (GPIO44); the RX keeps 43. */
    i2s_std_config_t std = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(PLAYBACK_I2S_RATE_HZ),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT,
                                                        I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            /* MCLK is deliberately NOT driven here. The XVF3800 GENERATES its own master
             * clock from the I2S reference (its datasheet: "generated internally ... using a
             * PLL that uses the I2S clock as the timing reference"), and on this board that
             * output feeds the AIC3104. Driving GPIO9 from the ESP32 as well puts a second
             * driver on the line: it corrupts the clock, stops the XVF3800's own mic pipeline
             * (the LED beam display goes dead), and leaves the DAC clicking. Verified
             * 2026-10-11 on real hardware. Only a "-extmclk" XVF3800 build, which disables the
             * chip's clock recovery, would want an externally supplied MCLK. */
            .mclk = I2S_GPIO_UNUSED,
            .bclk = PLAYBACK_I2S_BCLK_PIN,
            .ws   = PLAYBACK_I2S_WS_PIN,
            .dout = PLAYBACK_I2S_TX_PIN,
            .din  = I2S_GPIO_UNUSED,
        },
    };
    if (i2s_channel_init_std_mode(tx, &std) != ESP_OK) {
        ESP_LOGW("plb", "playback: i2s init failed; no reply can be played");
        i2s_del_channel(tx);
        tx = NULL;
        return -1;
    }
    if (i2s_channel_enable(tx) != ESP_OK) {
        ESP_LOGW("plb", "playback: i2s enable failed; no reply can be played");
        i2s_del_channel(tx);
        tx = NULL;
        return -1;
    }
    /* Size the amp's drain rest (F4) from the real DMA queue the driver allocated, not a guess.
     * total_dma_buf_size is the whole queue in bytes; at 16 kHz * 2 channels * 4 bytes per
     * word = 128000 bytes/second that is a real-time duration. It is an UPPER bound on how long
     * audio can still be sounding after the ring drains, so resting the amp that long can never
     * clip the tail, and the queue is short (a few hundred ms) so the amp barely overruns its
     * silence. A failed query leaves the drain at 0, i.e. the pre-fix behaviour: still safe
     * (no use-after-free), just a possible clipped tail. */
    i2s_chan_info_t info;
    if (i2s_channel_get_info(tx, &info) == ESP_OK && info.total_dma_buf_size > 0)
        playback_drain_ms = (int32_t)((uint64_t)info.total_dma_buf_size * 1000u / 128000u);
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
    ESP_LOGI("plb", "playback ready (amp drain %ld ms; the TX is armed and the DMA is primed)",
             (long)playback_drain_ms);
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
