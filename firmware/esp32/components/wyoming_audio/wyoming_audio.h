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
