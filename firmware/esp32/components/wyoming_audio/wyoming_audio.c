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
        /* The average is computed in 64 bits: two 32-bit samples near the extremes would
         * overflow an int32 sum, and a wrapped average would turn silence into a click. */
        int64_t average = ((int64_t)stereo[2 * i] + (int64_t)stereo[2 * i + 1]) / 2;
        if (average > 32767) average = 32767;
        if (average < -32768) average = -32768;
        out[i] = (int16_t)average;
    }
    return (int)take;
}
