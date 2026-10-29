#include "audio_playback.h"
#include "driver/gpio.h"

#include "wyoming_audio.h"   /* the amp GPO command encoder + XVF I2C/GPO constants (the audio contract) */
#include "wyoming_session.h"
#include "wy_wake.h"
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
/* 48 kHz I2S, per docs/audio-fidelity.md. This is the profile's real win: the bridge's TTS at
 * 22050 Hz is now UP-sampled to 48 kHz instead of being low-passed at 7400 Hz and crushed into
 * 16 kHz. The voice keeps its full bandwidth. */
#define PLAYBACK_I2S_RATE_HZ   48000
/* CLOCK ROLE: SLAVE, matching the capture RX. Both directions must agree: the SDK pairs them into
 * one full-duplex controller when the frame timing and BCLK/WS settings match, and a mix of roles
 * on the same pins means one side drives the clock the other is trying to receive. The
 * co-processor is the clock owner; we match its rate and consume its BCLK/WS. */
#define PLAYBACK_I2S_ROLE      I2S_ROLE_SLAVE
/* MCLK feeds the XVF3800 and, through it, the AIC3104: GPIO9 on the XIAO ESP32S3.
 * Seeed publishes this pin as part of the board's I2S pin map. */
#define PLAYBACK_I2S_MCLK_PIN  GPIO_NUM_9

/* The shared XVF3800 I2C GPO Servicer topology (the bring-up probe uses the same bus): SDA 5,
 * SCL 6, 400 kHz. The address and the command encoding come from wyoming_audio.h, the audio
 * contract's single owner of the XVF's I2C facts. */
#define XVF_I2C_SDA_PIN   GPIO_NUM_5
#define XVF_I2C_SCL_PIN   GPIO_NUM_6
#define XVF_I2C_SCL_HZ    400000

/* Above the gateway task on the shared core 0, so a TLS burst cannot starve the pump; capture
 * remains above it in importance but lives on the other core, so the two never compete. */
#define PLAYBACK_TASK_PRIORITY  6u
#define PLAYBACK_TASK_STACK     6144u
#define PLAYBACK_WRITE_MS       20u   /* 2x the realtime length of a full batch (10 ms) */
/* One I2S write's worth of audio: 960 int32 = 480 STEREO FRAMES = 10 ms at 48 kHz.
 *
 * This MUST match the TX DMA descriptor size (channel.dma_frame_num, also 480). A write smaller
 * than one descriptor delivers part of a descriptor and then waits for the descriptor cycle to
 * complete, so playback gets paced by the driver's buffering granularity instead of by the audio
 * clock -- the audio still plays, it just takes longer than it should, with no stall or drop to
 * show for it.
 *
 * The previous value (256 int32 = 128 frames) was sized for a 16 kHz link, where 128 frames is
 * 8 ms. At 48 kHz those same 128 frames are only 2.67 ms, a third of a descriptor. Measured with
 * it: a local 3.000 s tone took 6.10 s to drain (2.03x slow) while the pump counters reported
 * 0 stalled and 0 abandoned -- pacing, not loss. */
#define PLAYBACK_FRAME_BATCH    960u

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

/* One lock for the shared I2C pins (SDA 5 / SCL 6), held across an ENTIRE transaction's
 * bus-create-through-bus-delete lifetime. The bus is created per call with the port left to the
 * driver to pick, and the ESP32-S3 has two controllers: separate handles do not serialise two
 * users of the same pins. The amplifier, the codec, the LED ring and the diagnostics all share
 * this bus, so without this a timer-driven LED write can collide with an audio control write and
 * take the sound down with it. */
static SemaphoreHandle_t i2c_bus_lock;

static void i2c_bus_take(void) {
    if (!i2c_bus_lock) {
        i2c_bus_lock = xSemaphoreCreateMutex();
        if (!i2c_bus_lock) return;
    }
    xSemaphoreTake(i2c_bus_lock, portMAX_DELAY);
}
static void i2c_bus_give(void) { if (i2c_bus_lock) xSemaphoreGive(i2c_bus_lock); }
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
/* How much audio to buffer before the pump starts consuming.
 *
 * The bridge paces at 2x realtime, but delivery is uneven by up to ~100 ms and occasionally worse.
 * Playing the instant the first chunk lands turns every arrival gap into a hole in the audio. Two
 * seconds of pre-roll absorbs that jitter, and the ring is large enough (6 MB, ~16 s at 48 kHz) that
 * the cost is latency, not memory.
 *
 * The timeout matters as much as the threshold: if the producer is slower than the clock (or the
 * clip is shorter than the pre-roll) the buffer never fills, and without a deadline the device
 * would sit in silence forever. Play anyway after waiting this long. */
#define PLAYBACK_PREROLL_MS      400u
#define PLAYBACK_PREROLL_BYTES   ((size_t)PLAYBACK_PREROLL_MS * 48u * 8u / 1000u)
#define PLAYBACK_PREROLL_MAX_MS  1200u

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
    /* The lock is held across the WHOLE create-transmit-destroy lifetime, not just the transmit:
     * two users allocating and tearing down the same pins concurrently can interfere at any of
     * those steps. */
    i2c_bus_take();
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
        i2c_bus_give(); return -(int)rc; }
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
    i2c_bus_give();
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

/* Send a frame that is already in the chip's wire format.
 *
 * The scalar controls need [resid, cmd, 1, b0, b1, b2, b3]: one value, four bytes. The
 * convenience wrapper above cannot produce that shape, because it derives count from the payload
 * length. Keeping this separate means the odd count field is explicit at the call site instead of
 * being a special case hidden inside a function whose name promises "n params". */
int audio_xvf_write_frame(const uint8_t *frame, size_t len) {
    if (!frame || len < 3 || len > 19) return -1;
    return xvf_control_write_at(XVF_I2C_ADDRESS, frame, len, "xvf write32");
}

/* ---- co-processor audio front end: AGC + microphone gain -------------------------------
 *
 * Measured before this existed, on this board: speech RMS ~219 against a room floor of ~334 --
 * the talker arriving quieter than the room. The wake model crossed its cutoff only intermittently
 * and the bridge's energy gate rejected every speech frame it saw (87 of 87).
 *
 * Both controls live on the co-processor and both were unreachable until the 4-byte scalar
 * framing was fixed (count = 1 with a four-byte payload); a writer that derived count from the
 * payload length sent count = 4, which the chip acknowledges and ignores.
 *
 * Everything set here is VOLATILE on the chip. A co-processor restart -- every power cycle, every
 * firmware write -- restores its defaults, so this must run on every boot rather than once at
 * provisioning, or the device silently reverts to being hard of hearing. */
/* AGC max gain: how far it may lift a quiet talker.
 *
 * AGC TARGET POWER, not gain: 0.003 aimed at about -25 dBFS and the measured result was the
 * opposite of the intent -- the bridge's room floor fell from 334 to 67 and speech medians came in
 * around 275 against a gate threshold of 400, so every utterance was rejected as too quiet. A
 * target that low makes the AGC ATTENUATE towards it rather than lift speech. 0.05 sits near
 * -13 dBFS, which is a normal speech level for this gate. */
#define XVF_AGC_MAX_GAIN   60.0f
#define XVF_AGC_TARGET     0.05f

static int xvf_scalar_i32(uint8_t resid, uint8_t cmd, int32_t value) {
    uint8_t frame[XVF_I2C_SET32_CMD_LEN];
    wy_control_i32_command(frame, resid, cmd, value);
    return audio_xvf_write_frame(frame, sizeof(frame));
}

static int xvf_scalar_f32(uint8_t resid, uint8_t cmd, float value) {
    uint8_t frame[XVF_I2C_SET32_CMD_LEN];
    wy_control_f32_command(frame, resid, cmd, value);
    return audio_xvf_write_frame(frame, sizeof(frame));
}

int audio_xvf_set_mic_gain(int mic_gain_percent) {
    return xvf_scalar_f32(XVF_I2C_RESID_AUDIO_MGR, XVF_I2C_CMD_MIC_GAIN,
                          wy_mic_gain_multiple(mic_gain_percent));
}

int audio_xvf_apply_frontend(int mic_gain_percent) {
    int rc = 0;
    /* AGC first: it holds the level usable as the talker moves around, which no fixed gain can do
     * in a far-field setup. The fixed gain on top sets where AGC starts from. */
    rc |= xvf_scalar_i32(XVF_I2C_RESID_PP, XVF_I2C_CMD_PP_AGCONOFF, 1);
    rc |= xvf_scalar_f32(XVF_I2C_RESID_PP, XVF_I2C_CMD_PP_AGCMAXGAIN, XVF_AGC_MAX_GAIN);
    rc |= xvf_scalar_f32(XVF_I2C_RESID_PP, XVF_I2C_CMD_PP_AGCDESIREDLEVEL, XVF_AGC_TARGET);
    rc |= audio_xvf_set_mic_gain(mic_gain_percent);
    if (rc != 0)
        ESP_LOGW("plb", "front end: co-processor control write failed (%d); the microphone stays "
                        "quiet and wake detection may only work intermittently", rc);
    else
        ESP_LOGI("plb", "front end: AGC on (max gain %.0f, target %.4f), mic gain %.0f%%",
                 (double)XVF_AGC_MAX_GAIN, (double)XVF_AGC_TARGET, (double)mic_gain_percent);
    return rc;
}

/* Read an arbitrary XVF3800 control register. The caller sends the command bytes already
 * marked as a read (cmd | 0x80) and sized; the reply is [status, ...data]. This is the same
 * shape the bring-up probe uses for VERSION, which is the verified part of the pattern. Used
 * for the direction-of-arrival telemetry the LED ring follows. */
int audio_xvf_read(const uint8_t *cmd, size_t cmdlen, uint8_t *reply, size_t replylen) {
    if (!cmd || !reply || cmdlen == 0 || replylen == 0 || replylen > 32) return -1;
    i2c_master_bus_handle_t bus = NULL;
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = (i2c_port_num_t)-1,
        .sda_io_num = XVF_I2C_SDA_PIN,
        .scl_io_num = XVF_I2C_SCL_PIN,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    i2c_bus_take();
    esp_err_t rc = i2c_new_master_bus(&bus_cfg, &bus);
    if (rc != ESP_OK) { i2c_bus_give(); return -(int)rc; }
    i2c_master_dev_handle_t dev = NULL;
    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = XVF_I2C_ADDRESS,
        .scl_speed_hz = XVF_I2C_SCL_HZ,
        .flags.disable_ack_check = false,
    };
    rc = i2c_master_bus_add_device(bus, &dev_cfg, &dev);
    if (rc == ESP_OK) {
        /* Request and reply are ONE transaction to a caller: the lock covers both halves. */
        rc = i2c_master_transmit_receive(dev, cmd, cmdlen, reply, replylen, 200);
        i2c_master_bus_rm_device(dev);
    }
    i2c_del_master_bus(bus);
    i2c_bus_give();
    return rc == ESP_OK ? 0 : -(int)rc;
}

int audio_xvf_route_output(int right, int category, int source) {
    uint8_t cmd[XVF_I2C_GPO_CMD_LEN];
    wy_output_route_command(cmd, right, category, source);
    return xvf_control_write_at(XVF_I2C_ADDRESS, cmd, sizeof(cmd), right ? "route R" : "route L");
}

int audio_xvf_output_upsample(int left_on, int right_on) {
    uint8_t cmd[XVF_I2C_GPO_CMD_LEN];
    wy_output_upsample_command(cmd, left_on, right_on);
    return xvf_control_write_at(XVF_I2C_ADDRESS, cmd, sizeof(cmd), "route upsample");
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
    i2c_bus_take();   /* the codec is on the same pins as the amp, the LEDs and the XVF */
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
    if (rc != ESP_OK) { i2c_bus_give(); return -(int)rc; }
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
    i2c_bus_give();
    return rc == ESP_OK ? 0 : -(int)rc;
}

int audio_dac_init(void) {
    int rc = 0;
    rc |= aic_write(AIC3104_PAGE_CTRL, 0x00);

    /* ---- Clocking first: without a locked PLL nothing else means anything. ----------
     * The XVF3800 clocks the AIC3104 with BCLK = 1.024 MHz (16 kHz x 64) and there is no
     * separate MCLK reaching this chip, so the PLL is fed from BCLK and fs TRACKS BCLK: triple
     * the bit clock and every derived rate triples with it. That is why this sequence is
     * unchanged for the 48 kHz link -- it is not a fixed-rate configuration.
     *
     * CORRECTION, 2026-10-15. This comment used to say "J = 48". The datasheet disagrees: Page 0
     * Register 4 is "PLL Programming Register B", with the PLL J value in D7-D2 (000001 = J 1
     * ... 111111 = J 63) and D1-D0 reserved. So 0x30 = 0b00110000 puts 0b001100 = 12 in the J
     * field, i.e. J = 12, not 48. The VALUES are right -- they are what produces correct-speed
     * audio on real hardware -- but the number written beside them was not the number the chip
     * reads. Do not "correct" the register to 0xC0 on the strength of the old comment: the chip
     * was never reading 48.
     *
     * The one open question is whether fs(ref) (which Register 7 D7 declares as nominally 48 kHz)
     * remains inside the chip's valid range once BCLK has tripled. It is left as-is because the
     * alternative is a speculative register change to a path that demonstrably works and cannot
     * be A/B tested here. If playback is ever flaky at 48 kHz but fine at 16 kHz, that is the
     * first thing to look at. Register 3: PLL enable (D7), Q (D6-D3), P (D2-D0). */
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
    /* Deliberately NO LED work here. The amplifier is armed from the playback pump's realtime
     * path and from the local-tone path, so lighting the ring here did two bad things at once:
     * it blocked the audio pump on three extra I2C transactions, and it overwrote the wake
     * sweep, because the activation chime uses this same amplifier. The chime's amp edges are
     * not speech, and they must not be presented as though they were. */
    i2c_bus_take();
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
        i2c_bus_give();
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
    i2c_bus_give();
    return (int)rc;
    }

static int pump_logged;

/* Playback health counters, per answer.
 *
 * "Stuttering, especially towards the end" is a symptom with several possible mechanisms —
 * the ring running dry, the DMA refusing writes, or the upsampler falling behind realtime — and
 * they are indistinguishable by ear. These counters tell them apart in one playback:
 *
 *   writes      batches accepted by the DMA
 *   empty       times the ring had nothing while the stream was still active: STARVATION
 *   stalled     writes that made no progress (DMA full / channel not accepting): the TX side
 *   abandoned   batches whose tail was given up on after the idle limit: AUDIBLE LOSS
 *
 * A clean answer has empty ~= the count of chunks the bridge had not yet delivered, and
 * stalled/abandoned at zero. Reported once per answer, at the same moment `played` is sent. */
static uint32_t pump_writes, pump_empty, pump_stalled, pump_abandoned;
/* Frames actually removed from the ring, and when the first/last of them went out.
 *
 * This is the number that settles "is playback chopping": frames removed divided by the elapsed
 * time is the EFFECTIVE consumption rate. At a true 48 kHz link it must come out at ~48000 Hz.
 * Anything lower means the pump was not feeding the DMA at the clock rate -- the DMA keeps
 * shifting regardless, transmitting zeros it was given, so no write ever fails and no counter
 * above moves. A downstream fault (codec, co-processor) cannot produce this: I2S has no
 * backpressure, so nothing downstream can slow the ring drain. */
static uint32_t pump_frames;
static TickType_t pump_first_tick, pump_last_tick;
static int pump_active_prev;   /* for the rising-edge reset above */
static int preroll_done;       /* this answer has buffered enough to start */
static TickType_t preroll_start;
static size_t preroll_last_bytes;

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
/* The done tone is the awake pair descending ("that answer is finished"), and it is queued
 * through the SAME ring as the answer so the amplifier policy carries it. Rendered once. */
static int16_t done_tone[5120];
static size_t done_tone_samples;
static int done_queued;

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
        /* Edge-triggered, deliberately. This block used to run on EVERY iteration while the stream
         * was active, which reset the counters continuously: the summary then described the last
         * loop pass rather than the answer, and reported 31 writes / 210 ms for a 16 s clip. A
         * measurement that silently describes the wrong window is worse than no measurement. */
        int active_now = wy_session_playback_active(s) ? 1 : 0;
        if (active_now && !pump_active_prev) {
            played_reported = 0;
            done_queued = 0;
            pump_writes = pump_empty = pump_stalled = pump_abandoned = 0;
            pump_frames = 0;
            pump_first_tick = pump_last_tick = 0;
            pump_logged = 0;
            preroll_done = 0;
            preroll_start = 0;
            preroll_last_bytes = 0;
        }
        pump_active_prev = active_now;
        if (active_now) {
        } else if (!played_reported && wy_session_playback_ended(s) &&
                   wy_session_playback_bytes(s) == 0 && !done_queued) {
            /* The answer has finished sounding: mark it with the done tone BEFORE saying
             * `played`, so the bridge's wait ends after the room has heard the closing
             * gesture too. One tone per answer, never per chunk. */
            if (!done_tone_samples) {
                size_t need = wy_done_render(NULL, 0);
                if (need <= sizeof(done_tone) / sizeof(done_tone[0]))
                    done_tone_samples = wy_done_render(done_tone, need);
            }
            if (done_tone_samples &&
                wy_session_local_play(s, done_tone, done_tone_samples) == WY_OK) {
                ESP_LOGI("plb", "done tone queued (%u samples)", (unsigned)done_tone_samples);
            }
            done_queued = 1;
        } else if (!played_reported && done_queued && wy_session_playback_ended(s) &&
                   wy_session_playback_bytes(s) == 0) {
            {
                /* The decisive figure: frames removed / elapsed. A true 48 kHz link must show
                 * ~48000 Hz here. Lower means the pump was late and the DMA played zeros in the
                 * meantime -- which no write-failure counter can see. */
                unsigned elapsed_ms = 0;
                if (pump_last_tick > pump_first_tick)
                    elapsed_ms = (unsigned)((pump_last_tick - pump_first_tick) * 1000u
                                            / configTICK_RATE_HZ);
                unsigned rate = elapsed_ms ? (unsigned)(((uint64_t)pump_frames * 1000u) / elapsed_ms)
                                           : 0u;
                ESP_LOGI("plb", "playback stats: %u writes, %u frames, %u ms, effective %u Hz "
                                 "(48 kHz expected), %u empty-ring, %u stalled, %u abandoned",
                         (unsigned)pump_writes, (unsigned)pump_frames, elapsed_ms, rate,
                         (unsigned)pump_empty, (unsigned)pump_stalled, (unsigned)pump_abandoned);
            }
            if (playback_write &&
                wy_session_send_played(s, playback_write, playback_user) == WY_OK) {
                ESP_LOGI("plb", "played: reported to the gateway");
            } else {
                ESP_LOGW("plb", "played: could not be reported (link=%p write=%p)",
                         playback_user, (const void *)playback_write);
            }
            played_reported = 1;
        }
        /* Pre-roll: buffer before playing, so arrival jitter is absorbed instead of heard. */
        /* Never pre-roll a device-generated tone: it is already complete and in the ring, and
         * buffering it just delays and chops the feedback the user is listening for. */
        if (active_now && !preroll_done && !wy_session_local_pending(s)) {
            size_t have = wy_session_playback_bytes(s);
            preroll_last_bytes = have;
            TickType_t now_tick = xTaskGetTickCount();
            if (preroll_start == 0) preroll_start = now_tick;
            unsigned waited_ms = (unsigned)((now_tick - preroll_start) * 1000u / configTICK_RATE_HZ);
            if (have >= PLAYBACK_PREROLL_BYTES || waited_ms >= PLAYBACK_PREROLL_MAX_MS) {
                preroll_done = 1;
                ESP_LOGI("plb", "pre-roll done after %u ms: %u bytes buffered (%.2f s)%s",
                         waited_ms, (unsigned)have, (double)have / (48000.0 * 8.0),
                         have < PLAYBACK_PREROLL_BYTES ? " -- TIMED OUT, producer behind the clock" : "");
            } else {
                /* Keep the DMA fed with real silence while the buffer fills. */
                size_t silence_written = 0;
                i2s_channel_write(tx, playback_silence, sizeof(playback_silence),
                                  &silence_written, PLAYBACK_WRITE_MS);
                continue;
            }
        }
        size_t n = wy_session_playback_read(s, frame, sizeof(frame));   /* bytes, <= sizeof(frame) */
        if (n == 0) {
            /* The ring is empty, so no local tone can still be sounding. */
            wy_session_local_done();
            /* An empty ring WHILE A STREAM IS ACTIVE is starvation: the room hears a gap. An
             * empty ring after the stream ended is just the tail of a finished answer and is
             * not counted, or every answer would report a false fault. */
            if (wy_session_playback_active(s)) pump_empty++;
            /* Keepalive: push one batch of real zeros rather than idling the DMA. The write's
             * own timeout is the pacing, so this yields exactly like the delay did. */
            size_t silence_written = 0;
            i2s_channel_write(tx, playback_silence, sizeof(playback_silence),
                              &silence_written, PLAYBACK_WRITE_MS);
            continue;
        }
        if (!pump_logged) { ESP_LOGI("plb", "pump: first %u frames to I2S", (unsigned)n); pump_logged = 1; }
        pump_writes++;
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
                /* 8 bytes per stereo-32 frame: this is the only place that counts what the
                 * speaker actually consumed, as opposed to what was queued. */
                pump_frames += (uint32_t)(written / 8u);
                if (pump_writes == 1 && pump_first_tick == 0) pump_first_tick = xTaskGetTickCount();
                pump_last_tick = xTaskGetTickCount();
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
                    pump_stalled++;
                    if (++idle_tries >= PLAYBACK_WRITE_MAX_IDLE) {
                        pump_abandoned++;
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
    /* The co-processor's AGC and mic gain are volatile chip state: a co-processor restart wipes
     * them, so they are re-armed here on every boot. Without this the device comes up hard of
     * hearing and the wake word only fires sometimes, with nothing in the log to say why. */
    audio_xvf_apply_frontend(XVF_MIC_GAIN_DEFAULT_PERCENT);

    /* ---- Arm the full-band playback path: upsampler OFF, far end at its NATIVE rate. ----
     *
     * These two settings are ONE decision and the order matters.
     *
     * The XMOS Programming Guide's own host-to-host loopback test is explicit about the pairing: it
     * runs `AUDIO_MGR_OP_UPSAMPLE 0 0` first, then routes category 10, explaining that the signals
     * "are by definition at the I2S or USB sample rate, [so] they do not require the use of the
     * upsampler in the case of a 48 kHz connection, and therefore we explicitly unset the
     * AUDIO_MGR_OP_UPSAMPLE flags (set to 1 by default in a 48 kHz configuration)". Category 10 is
     * the same idea on the mux side: far-end data passed straight out of the I2S logical core, and
     * "all sources carry useful data if the external interface rate is 48 kHz".
     *
     * Left at the defaults, our careful 22050 -> 48000 up-sample is undone on the way to the DAC:
     * the far end is rate-converted down to the 16 kHz voice-DSP rate and back up again,
     * band-limiting every answer to 8 kHz. That quietly cancels the entire reason the 48 kHz profile
     * exists, and nothing in the log would ever show it.
     *
     * The pair is atomic for a reason: category 4 is the far end AFTER conversion to 16 kHz, so a
     * category-4 tap with the upsampler cleared would hand a 16 kHz signal to a 48 kHz output stage
     * and play it at a third of the right speed. The safe fallback therefore restores the flags to
     * their default before selecting the converted tap, rather than mixing one half of each. */
    int up_rc = audio_xvf_output_upsample(0, 0);
    if (up_rc == 0) {
        int native_rc = audio_xvf_route_output(0, XVF_I2C_CAT_FAR_END_NATIVE, 0);
        if (native_rc != 0) {
            /* The native tap was not accepted: put the upsampler back before choosing the converted
             * tap, because the converted tap is a 16 kHz signal and needs re-upsampling to reach a
             * 48 kHz output at the right speed. */
            ESP_LOGW("plb", "playback: native-rate far end rejected (%d); restoring the upsampler "
                            "and falling back to the rate-converted far end (answers band-limited "
                            "to 8 kHz)", native_rc);
            audio_xvf_output_upsample(1, 1);
            audio_xvf_route_output(0, XVF_I2C_CAT_FAR_END, 0);
        }
    } else {
        /* Flags could not be cleared, so the converted tap is the consistent choice. */
        ESP_LOGW("plb", "playback: could not clear the output upsampling flags (%d); "
                        "selecting the rate-converted far end, so answers stay band-limited to "
                        "8 kHz even on the 48 kHz link", up_rc);
        audio_xvf_route_output(0, XVF_I2C_CAT_FAR_END, 0);
    }
    i2s_chan_config_t channel = I2S_CHANNEL_DEFAULT_CONFIG(PLAYBACK_I2S_PORT, PLAYBACK_I2S_ROLE);
    /* Clear each TX DMA buffer as soon as it has been played. The SDK default leaves this OFF,
     * which means a buffer that is replayed without being refilled repeats STALE audio instead of
     * silence -- so any momentary underrun becomes an audible glitch rather than a quiet gap.
     * Underrun protection costs one memset per completed buffer. */
    channel.auto_clear_after_cb = true;
    /* Same reasoning as capture, on the transmit side: ~38 ms of DMA depth (10 descriptors x 480
     * frames x 8 bytes) so a scheduling hiccup becomes a slightly late write instead of an audible
     * gap. With auto_clear on, an underrun that does happen is silence rather than a replayed
     * stale buffer, which is the difference between a small dropout and a machine-gun glitch. */
    channel.dma_desc_num = 10;
    channel.dma_frame_num = 480;
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
     * total_dma_buf_size is the whole queue in bytes; at the link rate * 2 channels * 4 bytes
     * per word that is a real-time duration. The rate is DERIVED, not retyped: the old code
     * kept dividing by 128000 (the 16 kHz byte rate) after the comment above it had already
     * been updated to 48 kHz, so the reported drain duration was three times the truth. It is an UPPER bound on how long
     * audio can still be sounding after the ring drains, so resting the amp that long can never
     * clip the tail, and the queue is short (a few hundred ms) so the amp barely overruns its
     * silence. A failed query leaves the drain at 0, i.e. the pre-fix behaviour: still safe
     * (no use-after-free), just a possible clipped tail. */
    i2s_chan_info_t info;
    if (i2s_channel_get_info(tx, &info) == ESP_OK && info.total_dma_buf_size > 0) {
        /* Bytes per second of I2S wire: link rate * 2 channels * 4 bytes per 32-bit word.
         * Derived from the rate constant so this cannot silently disagree with the clock again.
         * The division rounds UP: this is an upper bound on how long audio can still be sounding,
         * and under-estimating it would let the amplifier be cut while the ring is still draining. */
        const uint64_t bytes_per_second =
            (uint64_t)PLAYBACK_I2S_RATE_HZ * 2u * 4u;
        playback_drain_ms = (int32_t)(((uint64_t)info.total_dma_buf_size * 1000u
                                       + bytes_per_second - 1u) / bytes_per_second);
    } else {
        playback_drain_ms = 0;
    }
    /* The TX clocks out silence until the first write, and the amp stays off until the session
     * says an answer is playing, so starting the channel early (at registration, before the
     * first wake) is what makes the first answer start clean: the DMA is already primed. */
    stopping = 0;
    /* Core 0, NOT the capture core.
     *
     * The capture task runs the wake-model inference on every 10 ms frame at priority 6. Pinning the
     * pump to the same core (priority 5, i.e. below it) starved playback catastrophically: a 0.31 s
     * chime took 4.6 s to drain, about 7% of realtime, heard as a chime arriving late and then
     * stuttering. The two must not share a core.
     *
     * So: capture + inference own core 1; the pump joins the network/UI core at a priority above the
     * gateway task, so a TLS burst cannot starve it either. The pump blocks on the DMA most of its
     * life, so sharing with the radio costs the radio little. */
    if (xTaskCreatePinnedToCore(playback_task_fn, "plb_task", PLAYBACK_TASK_STACK, NULL,
                    PLAYBACK_TASK_PRIORITY, &task, 0) != pdPASS) {
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
