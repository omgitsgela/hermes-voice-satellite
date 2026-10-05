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

/* One playback resampler per stream: the bridge's TTS arrives at 22050 Hz (SND_FORMAT) but
 * the board's I2S clock is 16 kHz (shared with the capture), so the device resamples playback
 * down. It keeps an ABSOLUTE input cursor, so the output grid is independent of how the audio
 * is chunked: whether a second of speech arrives as one frame or many, the same input yields
 * the same 16 kHz output, with no step at a seam. Feed chunks in order, one stream per
 * playback session. */
typedef struct wy_resampler {
    double position;    /* absolute input offset (22050 Hz samples) of the next output sample */
    double consumed;    /* absolute input offset of the start of the NEXT chunk (samples fed) */
    int32_t prev_value; /* the last sample fed (absolute position consumed-1): it is the lower
                           sample for a seam interpolation, so the resample is continuous */
} wy_resampler;

/* Feed the next chunk of `n` 22050 Hz mono S16 samples (in[0] is the first new sample of the
 * stream) and emit up to `cap` 16000 Hz mono S16 samples by linear interpolation between the
 * surrounding input samples. Returns how many output samples were written (0 when n or cap is
 * 0). Bounded: never reads past in[n), never writes past out[cap]. Because the cursor is
 * absolute, feeding the same total input in any chunking produces the same output sequence. */
size_t wy_resample_22050_to_16000(wy_resampler *r, const int16_t *in, size_t n,
                                  int16_t *out, size_t cap);
void wy_resampler_reset(wy_resampler *r);   /* fresh stream: cursor and base back to 0 */

/* Playback volume. The project rule (docs/volume.md): NO PATH MAY TAKE THE DEVICE TO SILENCE.
 * A value of 0, or a value below the floor, becomes the floor -- a small but non-zero gain --
 * so a bad or refused level cannot deafen the device. The floor matches the bridge's
 * conservative floor; the default level is a named constant the live test calibrates. */
#define WY_VOLUME_DEFAULT_PERCENT 50   /* conservative default, below full */
#define WY_VOLUME_FLOOR_PERCENT   25   /* never silent: a real, audible minimum */

/* Scale n mono S16 samples by `percent` in place, clamping percent to
 * [WY_VOLUME_FLOOR_PERCENT, 100] first so no input (0, negative, or >100) yields silence.
 * Returns n. Monotonic: a >= b (both clamped) implies |out_a| >= |out_b| per sample. */
size_t wy_apply_volume(int16_t *pcm, size_t n, int percent);

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
