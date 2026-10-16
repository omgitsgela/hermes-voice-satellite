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

/* Anti-aliasing filter for the 22050 -> 16000 playback resampler.
 *
 * The resampler interpolates linearly between input samples. That is fine when UP-sampling, but
 * when DOWN-sampling every input component above the output's Nyquist limit (8 kHz) folds back
 * into the audible band as harsh distortion -- the "low quality" character of the speech. Piper's
 * 22050 Hz output carries real energy to ~11 kHz, so roughly 3 kHz was aliasing on top of the
 * voice. Low-pass first, then interpolate.
 *
 * Windowed-sinc FIR: WY_FIR_TAPS taps, Hann window, cutoff 7400 Hz, unity DC gain, Q15.
 * Constant group delay of 15.5 input samples (0.7 ms) -- inaudible, and deterministic: the filter
 * is causal and its history lives in wy_resampler, so f[i] depends only on the raw samples at
 * absolute stream positions. Feeding the same total input in any chunking therefore still yields
 * the same output sequence, which is wy_resampler's documented contract. */
static const int16_t wy_fir_q15[WY_FIR_TAPS] = {
         0,     -5,     -6,     70,   -109,    -35,    332,   -416,
       -78,    912,  -1126,   -120,   2332,  -3314,   -145,  18092,
     18091,   -145,  -3314,   2332,   -120,  -1126,    912,    -78,
      -416,    332,    -35,   -109,     70,     -6,     -5,      0,
};

/* Push one raw input sample through the FIR and return the filtered value (int16).
 * `hist` holds the previous WY_FIR_TAPS-1 raw samples and is updated in place, oldest shifted
 * out. Q15 coefficients with an int32 accumulator: max |tap| is 18092 and the inputs are int16,
 * so the accumulator headroom is ample and the final shift rounds rather than truncates. */
static int16_t wy_fir_step(int16_t *hist, int16_t x) {
    int32_t acc = 0;
    int16_t stage[WY_FIR_TAPS];
    stage[WY_FIR_TAPS - 1] = x;
    for (int i = 0; i < WY_FIR_TAPS - 1; ++i) stage[i] = hist[i];
    for (int i = 0; i < WY_FIR_TAPS; ++i) acc += (int32_t)wy_fir_q15[i] * (int32_t)stage[i];
    for (int i = 0; i < WY_FIR_TAPS - 2; ++i) hist[i] = hist[i + 1];
    hist[WY_FIR_TAPS - 2] = x;
    int32_t v = (acc + 16384) >> 15;
    if (v > 32767) v = 32767;
    if (v < -32768) v = -32768;
    return (int16_t)v;
}

void wy_resampler_reset(wy_resampler *r) {
    if (r) {
        r->position = 0.0; r->consumed = 0.0; r->prev_value = 0;
        for (int i = 0; i < WY_FIR_TAPS - 1; ++i) r->hist[i] = 0;
    }
}

size_t wy_resample_22050_to_16000(wy_resampler *r, const int16_t *in, size_t n,
                                  int16_t *out, size_t cap) {
    if (!r || !in || !out || n == 0 || cap == 0) return 0;
    /* The cursor is absolute. `base` is where in[0] sits in the stream, so the local index is
     * position - base. One output sample is always interpolated between two ABSOLUTE input
     * samples; at a seam the lower one is the last FILTERED sample of the previous chunk
     * (prev_value, absolute base-1), which is what keeps the resample continuous across the
     * boundary.
     *
     * Every sample is pushed through the anti-aliasing FIR BEFORE it is interpolated. The FIR is
     * stepped on demand, in order, at most once per sample (filt_done only ever increases), so
     * the whole chunk is filtered by the time we return -- history and prev_value included. */
    double base = r->consumed;
    double prev_pos = base - 1.0;
    size_t written = 0;
    long filt_done = 0;          /* how many samples of THIS chunk have passed the FIR */
    int32_t f_a = r->prev_value; /* f[-1]: the previous chunk's final filtered sample */
    int32_t f_b = 0;             /* f[filt_done-1] once filt_done > 0 */

    while (written < cap) {
        double p = r->position;
        int32_t a, b;
        double frac;
        if (p < base) {
            /* In the seam region [base-1, base): between prev_value and f[0]. The cursor can
             * be at most one sample behind base (the output rate is lower than the input), so
             * frac is in [0,1). */
            frac = p - prev_pos;
            if (frac < 0.0 || frac >= 1.0) break;   /* cannot happen; guard against drift */
            if (filt_done < 1) {
                f_b = wy_fir_step(r->hist, in[0]);
                filt_done = 1;
            }
            a = f_a;
            b = f_b;
        } else {
            long i0 = (long)(p - base);
            if ((size_t)i0 + 1 >= n) break;          /* need a sample ahead to interpolate */
            frac = p - (double)(long)p;
            /* Advance the FIR until f[i0] and f[i0+1] both exist. */
            while (filt_done <= i0 + 1) {
                f_a = f_b;
                f_b = wy_fir_step(r->hist, in[filt_done]);
                ++filt_done;
            }
            a = f_a;
            b = f_b;
        }
        double sample = (double)a * (1.0 - frac) + (double)b * frac;
        int32_t v = (int32_t)llround(sample);        /* in-range (a lerp of two int16) */
        if (v > 32767) v = 32767;                    /* guard, not the path */
        if (v < -32768) v = -32768;
        out[written++] = (int16_t)v;
        r->position += WY_RESAMPLE_RATIO;
    }
    /* Finish filtering any input the cursor did not reach, so the FIR history and prev_value
     * describe the true end of the chunk regardless of where the cursor stopped. */
    while (filt_done < (long)n) {
        f_a = f_b;
        f_b = wy_fir_step(r->hist, in[filt_done]);
        ++filt_done;
    }
    r->consumed = base + (double)n;   /* the next chunk begins where this one ends */
    r->prev_value = (int32_t)f_b;     /* the last FILTERED sample: the next seam's lower value */
    return written;
}

/* The 48 kHz profile's up-sampler: 22050 Hz TTS in, 48000 Hz out. See wyoming_audio.h for why
 * there is deliberately NO anti-aliasing filter here (up-sampling cannot alias, and the 16 kHz
 * path's 7400 Hz low-pass is exactly what this profile exists to stop doing). The cursor,
 * seam handling and chunking contract are wy_resample_22050_to_16000's, minus the FIR: the
 * interpolation reads the RAW input samples. */
#define WY_RESAMPLE_UP_RATIO  (22050.0 / 48000.0)

void wy_upsampler_reset_at(wy_upsampler *u, double in_rate, double out_rate) {
    if (u) {
        u->position = 0.0; u->consumed = 0.0;
        u->hist[0] = u->hist[1] = u->hist[2] = 0;
        /* The ratio is input-samples-per-output-sample, so an UP-sampler has ratio < 1
         * (22050/48000 = 0.459). Do not "helpfully" clamp it up to 1: that silently turns the
         * up-sampler into a pass-through and plays everything one octave down at the wrong
         * speed. Only a non-positive rate is invalid, and unity is the safe fallback there. */
        u->ratio = (out_rate > 0.0 && in_rate > 0.0) ? (in_rate / out_rate) : 1.0;
        if (u->ratio <= 0.0) u->ratio = 1.0;
    }
}

void wy_upsampler_reset(wy_upsampler *u) {
    wy_upsampler_reset_at(u, 22050.0, 48000.0);
}

/* The raw sample at chunk-local index k, where k may be -3..-1 (from the carried history).
 * Returns 0 for anything genuinely unavailable; the caller checks before relying on it. */
static int16_t wy_up_at(const wy_upsampler *u, const int16_t *in, size_t n, long k) {
    if (k >= 0 && (size_t)k < n) return in[k];
    if (k == -1) return u->hist[2];
    if (k == -2) return u->hist[1];
    if (k == -3) return u->hist[0];
    return 0;
}

/* Catmull-Rom cubic through four points. Linear interpolation is a first-order hold and loses
 * 44% of the amplitude of a 9 kHz tone on a 22050 -> 48000 up-sample -- audible dulling, on the
 * exact band this profile exists to preserve. Cubic costs three more multiplies per output
 * sample and keeps that band essentially flat. */
static int16_t wy_cubic(int16_t p0, int16_t p1, int16_t p2, int16_t p3, double t) {
    double a = (double)p1;
    double b = 0.5 * ((double)p2 - (double)p0);
    double c = (double)p0 - 2.5 * (double)p1 + 2.0 * (double)p2 - 0.5 * (double)p3;
    double d = -0.5 * (double)p0 + 1.5 * (double)p1 - 1.5 * (double)p2 + 0.5 * (double)p3;
    double v = ((d * t + c) * t + b) * t + a;
    int32_t r = (int32_t)llround(v);
    if (r > 32767) r = 32767;
    if (r < -32768) r = -32768;
    return (int16_t)r;
}

size_t wy_resample_22050_to_48000(wy_upsampler *u, const int16_t *in, size_t n,
                                  int16_t *out, size_t cap) {
    if (!u || !in || !out || n == 0 || cap == 0) return 0;
    double base = u->consumed;
    size_t written = 0;

    while (written < cap) {
        double p = u->position;
        long i0 = (long)floor(p - base);       /* the sample at or just below the cursor */
        double t = (p - base) - (double)i0;    /* fraction between i0 and i0+1 */
        /* Cubic needs i0-1 .. i0+2. History covers -3..-1; the chunk covers 0..n-1. */
        if (i0 + 2 >= (long)n) break;          /* not enough input ahead yet: the next chunk
                                                  will produce these, with the same cursor */
        out[written++] = wy_cubic(wy_up_at(u, in, n, i0 - 1), wy_up_at(u, in, n, i0),
                                  wy_up_at(u, in, n, i0 + 1), wy_up_at(u, in, n, i0 + 2), t);
        u->position += u->ratio;
    }
    /* Carry the last three raw samples so the next chunk's cubic still spans the seam. */
    if (n >= 3) {
        u->hist[0] = in[n - 3]; u->hist[1] = in[n - 2]; u->hist[2] = in[n - 1];
    } else {
        for (size_t k = 0; k < n; ++k) { u->hist[0] = u->hist[1]; u->hist[1] = u->hist[2]; u->hist[2] = in[k]; }
    }
    u->consumed = base + (double)n;
    return written;
}

/* Anti-aliasing filter for the 48000 -> 16000 decimator. 32 taps, Hann window, cutoff 7500 Hz
 * (the output's Nyquist is 8 kHz, so 7.5 kHz leaves a small transition band), unity DC gain.
 * Q15. The accumulator below is int64: 32 taps at max |tap| 9815 over full-scale int16 input
 * reaches ~1.0e10, which a 32-bit accumulator cannot hold. */
static const int16_t wy_decim_fir_q15[WY_FIR_TAPS] = {
         0,       7,      20,     -22,    -135,    -181,      35,     459,
       645,      98,   -1056,   -1789,    -761,    2482,    6766,    9815,
      9815,    6766,    2482,    -761,   -1789,   -1056,      98,     645,
       459,      35,    -181,    -135,     -22,      20,       7,       0
};

void wy_decimator_reset(wy_decimator *d) {
    if (d) {
        for (int i = 0; i < WY_FIR_TAPS - 1; ++i) d->hist[i] = 0;
        d->phase = 0;
    }
}

size_t wy_decimate_48000_to_16000(wy_decimator *d, const int16_t *in, size_t n,
                                  int16_t *out, size_t cap) {
    if (!d || !in || !out || n == 0 || cap == 0) return 0;
    size_t written = 0;
    /* Consume EVERY input sample, even after the output buffer is full. Stopping the loop at
     * `written == cap` (as this once did) leaves the trailing samples out of the FIR history and
     * out of the phase counter, so the NEXT call filters from the wrong state and the two calls
     * together differ from one continuous filter. The production shape makes this real: a 480
     * sample read emits its 160th output at input index 477, and 478-479 must still shift
     * through the history and advance the phase. An output due with no room is dropped rather
     * than corrupting the state -- which cannot happen for correctly sized callers anyway. */
    for (size_t k = 0; k < n; ++k) {
        /* Build the tap window for this input sample: hist holds the previous TAPS-1 raw
         * samples and in[k] is the newest, so the window is [hist[0..TAPS-2], in[k]]. */
        int64_t acc = 0;
        for (int i = 0; i < WY_FIR_TAPS - 1; ++i) {
            acc += (int64_t)wy_decim_fir_q15[i] * (int64_t)d->hist[i];
        }
        acc += (int64_t)wy_decim_fir_q15[WY_FIR_TAPS - 1] * (int64_t)in[k];
        for (int i = 0; i < WY_FIR_TAPS - 2; ++i) d->hist[i] = d->hist[i + 1];
        d->hist[WY_FIR_TAPS - 2] = in[k];

        if (d->phase == 0 && written < cap) {
            int32_t v = (int32_t)((acc + 16384) >> 15);
            if (v > 32767) v = 32767;
            if (v < -32768) v = -32768;
            out[written++] = (int16_t)v;
        }
        d->phase = (d->phase + 1) % 3;   /* carried across chunks: a boundary cannot shift phase */
    }
    return written;
}

size_t wy_apply_volume(int16_t *pcm, size_t n, int percent) {
    if (!pcm || n == 0) return 0;
    if (percent < WY_VOLUME_FLOOR_PERCENT) percent = WY_VOLUME_FLOOR_PERCENT;
    if (percent > 100) percent = 100;
    for (size_t i = 0; i < n; ++i) {
        /* Scale in double and round once: an intermediate integer cast would truncate the
         * fraction (1 * 50 / 100 = 0, not 0.5 -> 1), which both under-scales quiet samples and
         * rounds asymmetrically. llround (half away from zero) keeps the rounding symmetric for
         * the negative half of the range; the product (|sample| <= 32767, percent <= 100) is far
         * below the double's exact-integer range, so this is exact. */
        double scaled = (double)pcm[i] * (double)percent / 100.0;
        int32_t v = (int32_t)llround(scaled);
        if (v > 32767) v = 32767;              /* in-range by construction; a guard, not the path */
        if (v < -32768) v = -32768;
        pcm[i] = (int16_t)v;
    }
    return n;
}

size_t wy_expand_mono16_to_stereo32(const int16_t *mono, size_t n, int32_t *out, size_t cap) {
    if (!mono || !out || n == 0 || cap < 2 * n) return 0;   /* atomic: all or nothing */
    for (size_t i = 0; i < n; ++i) {
        /* 32-bit FULL-SCALE (the XVF's I2S convention, the downmix's inverse): a 16-bit value
         * lives in the top 16 bits. <<16 of an int16-promoted value is exact and in range. */
        /* Multiply, do NOT shift. Shifting a negative signed value left is undefined behaviour
         * in C and in C++ before C++20, and int16_t audio is negative for roughly half of every
         * waveform -- so `(int32_t)mono[i] << 16` was UB on half our samples. The multiply is the
         * same scaling and is well defined. */
        int32_t v = (int32_t)mono[i] * 65536;
        out[2 * i] = v;
        out[2 * i + 1] = v;
    }
    return 2 * n;
}

void wy_output_route_command(uint8_t out[XVF_I2C_GPO_CMD_LEN], int right, int category, int source) {
    if (!out) return;
    /* [resid, cmd, count, category, source] in one transaction, same shape as the GPO write. */
    out[0] = XVF_I2C_RESID_AUDIO_MGR;
    out[1] = (uint8_t)(right ? XVF_I2C_CMD_OP_R : XVF_I2C_CMD_OP_L);
    out[2] = 2;                          /* two payload bytes: category, source */
    out[3] = (uint8_t)category;
    out[4] = (uint8_t)source;
}

void wy_amp_gpo_command(uint8_t out[XVF_I2C_GPO_CMD_LEN], int enabled) {
    if (!out) return;
    /* The XVF3800 I2C GPO write form: [resid, cmd, count, p0, p1] in one transaction. The amp
     * pin X0D31 is active-LOW, so ENABLING the amplifier (to play) is level 0 and disabling it
     * is level 1. A swapped mapping would mute every answer, so this is pinned by test. */
    out[0] = XVF_I2C_RESID_GPO;
    out[1] = XVF_I2C_CMD_GPO_WRITE;
    out[2] = 2;                     /* two payload bytes: pin, level */
    out[3] = XVF_I2C_GPO_PIN_AMP;
    out[4] = enabled ? 0 : 1;
}
