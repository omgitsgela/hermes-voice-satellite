#include "wyoming_audio.h"
#include <math.h>
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

/* The playback resampler's ratio, one constant: 22050 Hz in (the bridge's SND_FORMAT) to
 * 16000 Hz out (the board's I2S clock). A double, because the fractional cursor must not drift
 * on an integer ratio. */
#define WY_RESAMPLE_RATIO  (22050.0 / 16000.0)

void wy_resampler_reset(wy_resampler *r) {
    if (r) { r->position = 0.0; r->consumed = 0.0; r->prev_value = 0; }
}

size_t wy_resample_22050_to_16000(wy_resampler *r, const int16_t *in, size_t n,
                                  int16_t *out, size_t cap) {
    if (!r || !in || !out || n == 0 || cap == 0) return 0;
    /* The cursor is absolute. `base` is where in[0] sits in the stream, so the local index is
     * position - base. One output sample is always interpolated between two ABSOLUTE input
     * samples; at a seam the lower one is the last sample of the previous chunk (prev_value,
     * absolute base-1), which is what keeps the resample continuous across the boundary. */
    double base = r->consumed;
    double prev_pos = base - 1.0;
    size_t written = 0;
    while (written < cap) {
        double p = r->position;
        int32_t a, b;
        double frac;
        if (p < base) {
            /* In the seam region [base-1, base): between prev_value and in[0]. The cursor can
             * be at most one sample behind base (the output rate is lower than the input), so
             * frac is in [0,1). */
            frac = p - prev_pos;
            if (frac < 0.0 || frac >= 1.0) break;   /* cannot happen; guard against drift */
            a = r->prev_value;
            b = in[0];
        } else {
            long i0 = (long)(p - base);
            if ((size_t)i0 + 1 >= n) break;          /* need a sample ahead to interpolate */
            frac = p - (double)(long)p;
            a = in[i0];
            b = in[i0 + 1];
        }
        double sample = (double)a * (1.0 - frac) + (double)b * frac;
        int32_t v = (int32_t)llround(sample);        /* in-range (a lerp of two int16) */
        if (v > 32767) v = 32767;                    /* guard, not the path */
        if (v < -32768) v = -32768;
        out[written++] = (int16_t)v;
        r->position += WY_RESAMPLE_RATIO;
    }
    r->consumed = base + (double)n;   /* the next chunk begins where this one ends */
    r->prev_value = in[n - 1];        /* keep the last sample for the next seam */
    return written;
}

size_t wy_apply_volume(int16_t *pcm, size_t n, int percent) {
    if (!pcm || n == 0) return 0;
    if (percent < WY_VOLUME_FLOOR_PERCENT) percent = WY_VOLUME_FLOOR_PERCENT;
    if (percent > 100) percent = 100;
    for (size_t i = 0; i < n; ++i) {
        /* int64 so the product cannot overflow; llround (half away from zero) keeps the
         * rounding symmetric for the negative half of the range, and the result of scaling an
         * in-range sample by <=100% is in-range, so the clamp is a guard, not the path. */
        int64_t scaled = (int64_t)pcm[i] * (int64_t)percent / 100.0;
        int32_t v = (int32_t)llround((double)scaled);
        if (v > 32767) v = 32767;
        if (v < -32768) v = -32768;
        pcm[i] = (int16_t)v;
    }
    return n;
}
