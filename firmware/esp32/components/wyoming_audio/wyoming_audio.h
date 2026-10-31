#ifndef WYOMING_AUDIO_H
#define WYOMING_AUDIO_H
#include <stddef.h>
#include <stdint.h>

/* The audio codec shared by the capture path: building the data field of an `audio-chunk`
 * frame and the sample conversion the XVF3800's 32-bit stereo I2S stream needs to become the
 * 16-bit mono the Wyoming mic contract (16 kHz / S16 / mono) speaks. Pure: no transport, no
 * heap, no RTOS. Proven on the host; the I2S read that feeds it is device-only. */

/* The maximum number of mono samples a single audio-chunk frame may carry (2048 bytes of
 * 16-bit PCM, 64 ms of 16 kHz audio): the bound wy_session_send_audio enforces, sized so an
 * oversized frame can never reach the wire's 16 KB payload limit. The capture path emits
 * smaller 10 ms chunks (see audio_capture.h) for low latency, well under this bound; this is
 * the ceiling, not the cadence. */
#define WY_AUDIO_CHUNK_SAMPLES 1024u

/* The board's I2S link rate, in both directions. This is THE constant for the 48 kHz profile
 * (docs/audio-fidelity.md); audio_bringup.c, audio_capture.c and audio_playback.c each set their
 * channel clocks from it and must agree. A local tone rendered at 16 kHz must be up-sampled to
 * this rate before it reaches the ring, or it plays three times too fast and far too high. */
#define WY_AUDIO_I2S_RATE_HZ 48000u

/* One playback resampler per stream: the bridge's TTS arrives at 22050 Hz (SND_FORMAT) but
 * the board's I2S clock is 16 kHz (shared with the capture), so the device resamples playback
 * down. It keeps an ABSOLUTE input cursor, so the output grid is independent of how the audio
 * is chunked: whether a second of speech arrives as one frame or many, the same input yields
 * the same 16 kHz output, with no step at a seam. Feed chunks in order, one stream per
 * playback session. */
/* Taps of the anti-aliasing FIR applied to the input before interpolation. See the coefficient
 * table in wyoming_audio.c for why it exists (down-sampling without a low-pass aliases every
 * input component above the output's Nyquist limit back into the audible band). */
#define WY_FIR_TAPS 32

typedef struct wy_resampler {
    double position;    /* absolute input offset (22050 Hz samples) of the next output sample */
    double consumed;    /* absolute input offset of the start of the NEXT chunk (samples fed) */
    int32_t prev_value; /* the last FILTERED sample emitted (absolute position consumed-1): it is
                           the lower sample for a seam interpolation, so the resample is continuous */
    int16_t hist[WY_FIR_TAPS - 1];   /* the last TAPS-1 raw input samples, so the FIR is continuous
                                        across chunks and chunking cannot change the output */
} wy_resampler;

/* Feed the next chunk of `n` 22050 Hz mono S16 samples (in[0] is the first new sample of the
 * stream) and emit up to `cap` 16000 Hz mono S16 samples by linear interpolation between the
 * surrounding input samples. Returns how many output samples were written (0 when n or cap is
 * 0). Bounded: never reads past in[n), never writes past out[cap]. Because the cursor is
 * absolute, feeding the same total input in any chunking produces the same output sequence. */
size_t wy_resample_22050_to_16000(wy_resampler *r, const int16_t *in, size_t n,
                                  int16_t *out, size_t cap);
void wy_resampler_reset(wy_resampler *r);   /* fresh stream: cursor and base back to 0 */

/* The 48 kHz profile (docs/audio-fidelity.md): the board's I2S clock runs at 48 kHz in both
 * directions, matching the Seeed reference, so playback no longer has to be crushed into 16 kHz.
 * Two conversions are needed and they are NOT symmetric, which is the whole point of writing
 * both explicitly rather than parameterising one:
 *
 *   wy_resample_22050_to_48000   UP-samples the bridge's TTS. No anti-aliasing filter: up-
 *                                sampling cannot alias, and running the 7400 Hz low-pass that
 *                                the 16 kHz path needs would throw away exactly the 7.4-11 kHz
 *                                content this profile exists to KEEP. Linear interpolation
 *                                already suppresses the interpolation images enough for a
 *                                voice.
 *
 *   wy_decimate_48000_to_16000   DOWN-samples captured audio 3:1 for the wake model and the
 *                                uplink, both of which are 16 kHz contracts. This one MUST
 *                                low-pass first: 48 kHz input carries energy to 24 kHz and
 *                                every component above 8 kHz would fold back into the voice.
 *
 * Both keep wy_resampler's absolute-cursor contract: feeding the same total input in any
 * chunking produces the same output sequence. */
/* The up-sampler keeps its own state rather than sharing wy_resampler: cubic interpolation needs
 * a small history (two samples behind the cursor) that the FIR-based down-sampler's prev_value
 * does not provide, and bolting one onto the other would make both harder to reason about.
 * hist[] holds the last three RAW input samples consumed (hist[0] is the oldest of the three). */
typedef struct wy_upsampler {
    double position;      /* absolute input offset of the next output sample */
    double consumed;      /* absolute input offset of the start of the NEXT chunk */
    int16_t hist[3];      /* the last three raw input samples: k = -3, -2, -1 */
    double ratio;         /* input samples per output sample (in_rate / out_rate) */
} wy_upsampler;

/* Reset for the bridge's TTS: 22050 Hz in, 48000 Hz out. */
void wy_upsampler_reset(wy_upsampler *u);

/* Reset for any other rate pair. The local tones (chime, done tone) are rendered at 16 kHz by
 * the same generator the Pi satellite uses, and the I2S link is 48 kHz, so they need a 16000 -> 48000
 * up-sample or they play three times too fast and an octave and a half too high. */
void wy_upsampler_reset_at(wy_upsampler *u, double in_rate, double out_rate);

size_t wy_resample_22050_to_48000(wy_upsampler *u, const int16_t *in, size_t n,
                                  int16_t *out, size_t cap);

/* A 48 kHz -> 16 kHz decimator: one anti-aliasing FIR history plus the 3:1 phase. The phase is
 * carried across chunks so a chunk boundary cannot shift which samples survive. */
typedef struct wy_decimator {
    int16_t hist[WY_FIR_TAPS - 1];   /* previous raw samples, so the FIR is continuous */
    int phase;                        /* input samples consumed mod 3: 0 emits a sample */
} wy_decimator;

void wy_decimator_reset(wy_decimator *d);

/* Feed the next chunk of `n` 48000 Hz mono S16 samples and emit up to `cap` 16000 Hz mono S16
 * samples (one per three input samples, after the low-pass). Returns how many were written.
 * Bounded: never reads past in[n), never writes past out[cap]. */
size_t wy_decimate_48000_to_16000(wy_decimator *d, const int16_t *in, size_t n,
                                  int16_t *out, size_t cap);

/* Playback volume. The project rule (docs/volume.md): NO PATH MAY TAKE THE DEVICE TO SILENCE.
 * A value of 0, or a value below the floor, becomes the floor -- a small but non-zero gain --
 * so a bad or refused level cannot deafen the device. The floor matches the bridge's
 * conservative floor; the default level is a named constant the live test calibrates. */
#define WY_VOLUME_DEFAULT_PERCENT 50   /* conservative default, below full */
#define WY_VOLUME_FLOOR_PERCENT   25   /* never silent: a real, audible minimum */

/* Map a USER level (1..100, where 1 is the quietest this hardware can be) onto the gain actually
 * applied (floor..100 percent).
 *
 * The fleet's playback_volume is on the USER scale, and the Pi satellite spreads that scale across
 * its whole usable range so every step is a real step. The ESP32 used the number directly as a gain
 * percent, which silently compressed everything below the 25 percent floor into one identical level:
 * asking for 5, 10, 20 or 25 produced exactly the same loudness, and the bottom quarter of the user
 * scale did nothing at all. The number the user says and the number the device applies were only
 * equal by accident in the upper range.
 *
 * This is the same curve the bridge's volume.py applies (FLOOR + (user-1) * span / 99), so a level
 * means the same thing whichever satellite is being talked to. */
int wy_volume_user_to_gain_percent(int user);

/* Scale n mono S16 samples by `percent` in place, clamping percent to
 * [WY_VOLUME_FLOOR_PERCENT, 100] first so no input (0, negative, or >100) yields silence.
 * Returns n. Monotonic: a >= b (both clamped) implies |out_a| >= |out_b| per sample. */
size_t wy_apply_volume(int16_t *pcm, size_t n, int percent);

/* Convert n mono 16 kHz S16 samples to the I2S TX's 32-bit stereo FULL-SCALE frames (two
 * int32 per sample, L=R, the S16 value scaled to 32-bit full-scale: x * 65536 -- a multiply, since a left
 * shift of a negative signed value is undefined). This is the
 * exact inverse of the capture downmix, which is the XVF's own 32-bit full-scale convention.
 * Atomic: returns 2*n when cap >= 2*n (writing all frames), else 0 and writes nothing -- a
 * half-expanded frame would be a corrupted write. */
size_t wy_expand_mono16_to_stereo32(const int16_t *mono, size_t n, int32_t *out, size_t cap);

/* The amplifier is an XMOS GPO (X0D31, active-LOW), controlled over the XVF's I2C GPO
 * Servicer, NOT an ESP32 GPIO. GPO_WRITE_VALUE = ResID 20, CmdID 1, 2-byte payload
 * [pin, level]. The I2C write form is one 5-byte transaction [resid, cmd, count, p0, p1]
 * (no 0x80 read-modify, no read-back) -- the XVF3800 I2C protocol from Seeed's guide. */
#define XVF_I2C_ADDRESS       0x2C
#define XVF_I2C_RESID_GPO     20    /* GPO Servicer */
#define XVF_I2C_CMD_GPO_WRITE 1     /* GPO_WRITE_VALUE */
#define XVF_I2C_GPO_PIN_AMP   31    /* X0D31: amplifier enable */
#define XVF_I2C_GPO_CMD_LEN   5

/* Fill out[XVF_I2C_GPO_CMD_LEN] with the GPO_WRITE_VALUE transaction for the amplifier:
 * [20, 1, 2, 31, level], where level = 0 when `enabled` (LOW = amp on, active-low) and
 * 1 when disabled (HIGH = amp off). Pure byte encoder; the device glue transmits it. */
void wy_amp_gpo_command(uint8_t out[XVF_I2C_GPO_CMD_LEN], int enabled);

/* Audio Manager (ResID 35) output-mux routing: AUDIO_MGR_OP_L / AUDIO_MGR_OP_R each take a
 * (category, source) pair choosing what the DAC plays. The board's default routes the beamformed
 * MICROPHONE signal to the speaker, not what the host sends; the host's own playback is
 * "far end (reference) data" = category 4, source 0. Framing matches the GPO write above. */
#define XVF_I2C_RESID_AUDIO_MGR 35   /* Audio Manager */
#define XVF_I2C_CMD_OP_L        15   /* AUDIO_MGR_OP_L: L channel category + source */
#define XVF_I2C_CMD_OP_R        19   /* AUDIO_MGR_OP_R: R channel category + source */
#define XVF_I2C_CMD_OP_UPSAMPLE 14   /* AUDIO_MGR_OP_UPSAMPLE: L/R output upsampling flags */
#define XVF_I2C_CMD_MIC_GAIN     0   /* AUDIO_MGR_MIC_GAIN: float, pre-SHF microphone gain */
#define XVF_I2C_CAT_FAR_END     4    /* far end (reference) data, post rate conversion to 16 kHz */
#define XVF_I2C_CAT_FAR_END_NATIVE 10 /* far end AT NATIVE INTERFACE RATE (no conversion) */
#define XVF_I2C_CAT_USER_CH     8    /* user chosen channels (the beamformed output) */

/* Fill out[XVF_I2C_GPO_CMD_LEN] with an AUDIO_MGR_OP_L/R write. right selects OP_R instead of
 * OP_L. The DAC plays the left channel on both outputs, so OP_L is usually all that is needed. */
void wy_output_route_command(uint8_t out[XVF_I2C_GPO_CMD_LEN], int right, int category, int source);

/* Fill out[XVF_I2C_GPO_CMD_LEN] with an AUDIO_MGR_OP_UPSAMPLE write: one upsampling flag per
 * output channel, payload [L, R]. In a 48 kHz interface configuration the chip defaults these
 * flags to ON, which rate-converts a native-48 kHz far-end signal down to 16 kHz and back before
 * it reaches the speaker. Clearing them, together with selecting category 10 (far end at native
 * rate), is what makes the 48 kHz link actually deliver 48 kHz audio. */
void wy_output_upsample_command(uint8_t out[XVF_I2C_GPO_CMD_LEN], int left_upsample,
                                int right_upsample);

/* ---- scalar (4-byte) controls: mic gain, AGC, limiter -------------------------------
 *
 * The XVF3800's float and int32 parameters take ONE value of FOUR bytes, so the I2C frame's
 * count field is 1 while the payload is four bytes: [resid, cmd, 1, b0, b1, b2, b3]. That is
 * NOT the shape of the byte-pair controls above, where every value is one byte and count equals
 * the payload length. Getting it wrong is silent -- the write is ACKed and nothing changes --
 * which is a very likely reason `mic_gain` has never actually moved the microphone.
 *
 * The PP Servicer (ResID 17) holds the co-processor's own AGC. It has never been touched by this
 * firmware. At normal speaking distance the far-field array's output sits far below what the wake
 * model wants, and the bridge's own measurement says so: speech RMS ~220-700 while the room floor
 * is ~300. Enabling AGC is the lever that fixes level without touching the link at all. */
#define XVF_I2C_SET32_CMD_LEN   7    /* [resid, cmd, 1, b0, b1, b2, b3] */
#define XVF_I2C_RESID_AEC       33   /* AEC Servicer: beamformer output taps */
#define XVF_I2C_CMD_ASR_OUT_ONOFF 35 /* AEC_ASROUTONOFF: int32. 0 = AEC residuals (one channel per
                                      * MICROPHONE, unprocessed) 1 = beamformed ASR output (one
                                      * channel per BEAM). Seeed's I2S images route BOTH default
                                      * I2S output channels to ASR beam 0, and XMOS documents that
                                      * this tap is taken AFTER the beamformer and is NOT fed
                                      * through the post-processor -- which is why the PP AGC has
                                      * no effect on the audio we capture. */
#define XVF_I2C_CMD_ASR_OUT_GAIN 36  /* AEC_ASROUTGAIN: float, 0.0-1000.0 linear, applied only
                                      * when ASR_OUT_ONOFF is 1. This is the "configurable fixed
                                      * gain" the datasheet describes for the ASR output, and it is
                                      * the only gain that reaches the stream we capture. */

#define XVF_I2C_RESID_PP        17   /* PP Servicer (AGC / noise / echo) */
#define XVF_I2C_CMD_PP_AGCONOFF      10  /* int32: AGC switch (0,1) */
#define XVF_I2C_CMD_PP_AGCMAXGAIN    11  /* float: AGC max gain [1.0 .. 1000.0] */
#define XVF_I2C_CMD_PP_AGCDESIREDLEVEL 12 /* float: AGC target power [1e-8 .. 1.0] */
#define XVF_I2C_CMD_PP_AGCGAIN       13  /* float: AGC current gain (read-back) */

/* Little-endian 32-bit scalar as the chip expects; the float form is IEEE-754. */
void wy_control_i32_command(uint8_t out[XVF_I2C_SET32_CMD_LEN], uint8_t resid, uint8_t cmd,
                            int32_t value);
void wy_control_f32_command(uint8_t out[XVF_I2C_SET32_CMD_LEN], uint8_t resid, uint8_t cmd,
                            float value);

/* Map the fleet's 0..100 mic_gain percentage onto the co-processor's multiplicative gain.
 *
 * Pure arithmetic, kept here so it is testable: the endpoints are the whole contract. A setting of
 * 0 must NOT produce 0.0 (that would silence the microphone, and a dead microphone is
 * indistinguishable from a hardware fault), and the top of the range must be the compile-time
 * maximum rather than something the caller guessed. */
/* 78 is the fleet default and the setting the device was measured and tuned at, so 78 means
 * UNITY. The bounds are hard limits, not tuning: no value may silence the microphone (0 -> a real,
 * audible floor) and none may drive the capture into clipping. */
#define WY_MIC_GAIN_UNITY_PERCENT 78
#define WY_MIC_GAIN_MAX_MULTIPLE  4.0f
#define WY_MIC_GAIN_MIN_MULTIPLE  0.5f
float wy_mic_gain_multiple(int percent);

/* Serialize the audio-chunk data field for the given format into buffer (capacity bytes,
 * NUL-terminated on success). Returns the number of bytes written (excluding the NUL), or a
 * negative value when the format is not one this device can emit (rate out of range, not
 * 16-bit, or not mono) or the buffer is too small. */
int wy_audio_chunk_data(char *buffer, size_t capacity, int rate, int width, int channels);

/* Convert `frame_count` 32-bit stereo frames (interleaved L,R, as the I2S RX delivers them) to
 * 16-bit mono. Each output sample is the (L+R)/2 average, computed wide so it cannot overflow,
 * and clipped to the int16 range rather than wrapping. Writes at most `out_capacity` samples
 * and returns how many it wrote. */
int wy_audio_downmix_mono16(const int32_t *stereo, size_t frame_count, int16_t *out,
                            size_t out_capacity);

#endif
