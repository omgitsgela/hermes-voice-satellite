#include "wyoming_audio.h"
#include <stdio.h>

int wy_audio_chunk_data(char *buffer, size_t capacity, int rate, int width, int channels) {
    /* Only the formats this hardware can emit: mono, 16-bit, an actual sample rate. Anything
     * wider, multi-channel, or a non-rate is a caller error this device cannot serve, not a
     * format to guess at -- the strictness keeps a wrong chunk from ever reaching the wire. */
    if (rate < 8000 || rate > 96000 || width != 2 || channels != 1) return -1;
    if (!buffer || capacity == 0) return -1;
    int length = snprintf(buffer, capacity, "{\"rate\":%d,\"width\":%d,\"channels\":%d}",
                          rate, width, channels);
    if (length < 0 || (size_t)length >= capacity) return -1;
    return length;
}

int wy_audio_downmix_mono16(const int32_t *stereo, size_t frame_count, int16_t *out,
                            size_t out_capacity) {
    if (!stereo || !out || out_capacity == 0) return 0;
    size_t take = frame_count < out_capacity ? frame_count : out_capacity;
    for (size_t i = 0; i < take; ++i) {
        /* The XVF3800 I2S stream is 32-bit FULL-SCALE (Seeed's TFLM / Edge Impulse guides
         * convert it with `val = i2s_sample >> 16`), not 16-bit samples padded into 32-bit
         * slots. The average is computed in 64 bits -- two 32-bit samples near the extremes
         * would overflow an int32 sum -- then scaled down 16 bits with rounding. The int16
         * clip is a guard for out-of-range input, not the normal path: clipping instead of
         * scaling would turn every real speech sample (well above 2^15 in 32-bit terms) into
         * a sign-only square wave, which is not audio. */
        int64_t average = ((int64_t)stereo[2 * i] + (int64_t)stereo[2 * i + 1]) / 2;
        int64_t scaled = (average + 32768) >> 16;
        if (scaled > 32767) scaled = 32767;
        if (scaled < -32768) scaled = -32768;
        out[i] = (int16_t)scaled;
    }
    return (int)take;
}
