/* The microWakeWord v2 inference policy, pure C11 (see wy_mww.h). Host-tested
 * with -std=c11 -Wall -Wextra -Werror; no ESP, TFLM, or allocation. */
#include "wy_mww.h"

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/* ---- manifest ------------------------------------------------------------ */

void wy_mww_params_default(wy_mww_params *out)
{
    if (out == NULL)
        return;
    /* Exactly firmware/esp32/models/okay_nabu.json (the `micro` block of the bundled
     * default model); scripts/test_esp32_wake_scorer.py pins these against the
     * shipped file. */
    out->probability_cutoff = 0.97f;
    out->sliding_window_size = 5u;
    out->feature_step_ms = 10u;
    out->tensor_arena_size = 26080u;
    out->feature_size = WY_MWW_FEATURE_SIZE;
}

/* Find `"key"` in the buffer and return a pointer to the first character of
 * its scalar value (after the ':'), or NULL. Bounded: never reads past
 * json + json_len, and the buffer need not be NUL-terminated. */
static const char *find_value(const char *json, size_t json_len, const char *key)
{
    size_t key_len = strlen(key);
    size_t i;
    if (json == NULL || key_len + 2u > json_len)
        return NULL;
    for (i = 0; i + key_len + 2u <= json_len; i++) {
        if (json[i] != '"')
            continue;
        if (memcmp(json + i + 1u, key, key_len) != 0)
            continue;
        if (json[i + 1u + key_len] != '"')
            continue;
        i += key_len + 2u;
        while (i < json_len && (json[i] == ' ' || json[i] == '\t' || json[i] == '\n' || json[i] == '\r'))
            i++;
        if (i >= json_len || json[i] != ':')
            return NULL;
        i++;
        while (i < json_len && (json[i] == ' ' || json[i] == '\t' || json[i] == '\n' || json[i] == '\r'))
            i++;
        return (i < json_len) ? json + i : NULL;
    }
    return NULL;
}

/* Copy the numeric token at `at` into buf; returns its length, 0 when the
 * first character cannot start a number. */
static size_t number_token(const char *at, size_t avail, char *buf, size_t cap)
{
    size_t n = 0;
    if (avail == 0 || cap < 2u)
        return 0;
    while (n < avail && n + 1u < cap) {
        char c = at[n];
        if ((c >= '0' && c <= '9') || c == '+' || c == '-' || c == '.' || c == 'e' || c == 'E')
            n++;
        else
            break;
    }
    memcpy(buf, at, n);
    buf[n] = '\0';
    return n;
}

static int parse_double(const char *json, size_t json_len, const char *key, double *out)
{
    const char *at = find_value(json, json_len, key);
    char token[32];
    char *end = NULL;
    double v;
    size_t n;
    if (at == NULL)
        return -1;
    n = number_token(at, json_len - (size_t)(at - json), token, sizeof(token));
    if (n == 0)
        return -1;
    v = strtod(token, &end);
    if (end == token || *end != '\0')
        return -1;
    *out = v;
    return 0;
}

/* ---- parameter validation ------------------------------------------------ */

int wy_mww_params_parse(const char *json, size_t json_len, wy_mww_params *out)
{
    wy_mww_params p;
    double cutoff, window, step, arena;

    if (json == NULL || out == NULL)
        return -1;
    if (parse_double(json, json_len, "probability_cutoff", &cutoff) != 0 ||
        parse_double(json, json_len, "sliding_window_size", &window) != 0 ||
        parse_double(json, json_len, "feature_step_size", &step) != 0 ||
        parse_double(json, json_len, "tensor_arena_size", &arena) != 0)
        return -1;
    /* Whole numbers only where the manifest says count/size values. */
    if (window != (double)(unsigned)window || step != (double)(unsigned)step ||
        arena != (double)(unsigned)arena)
        return -1;
    if (!(cutoff > 0.0 && cutoff <= 1.0))
        return -1;
    if ((unsigned)window < 1u || (unsigned)window > WY_MWW_WINDOW_MAX)
        return -1;
    if ((unsigned)step < 1u || (unsigned)step > 100u)
        return -1;
    if ((unsigned)arena < 1024u || (unsigned)arena > 1048576u)
        return -1;

    p.probability_cutoff = (float)cutoff;
    p.sliding_window_size = (unsigned)window;
    p.feature_step_ms = (unsigned)step;
    p.tensor_arena_size = (unsigned)arena;
    p.feature_size = WY_MWW_FEATURE_SIZE;
    *out = p;
    return 0;
}

unsigned wy_mww_cutoff_quantized(float probability_cutoff)
{
    if (!(probability_cutoff > 0.0f))
        return 0u;
    if (probability_cutoff > 1.0f)
        probability_cutoff = 1.0f;
    /* Truncating, exactly like the reference's int(cutoff * 255): 0.5 maps to
     * 127, not 128. */
    return (unsigned)(probability_cutoff * 255.0f);
}

/* ---- feature preprocessor settings --------------------------------------- */

void wy_mww_frontend_params(wy_mww_frontend *out, unsigned step_size_ms)
{
    if (out == NULL)
        return;
    out->window_size_ms = 30u;
    out->step_size_ms = step_size_ms;
    out->num_channels = WY_MWW_FEATURE_SIZE;
    out->lower_band_limit = 125.0f;
    out->upper_band_limit = 7500.0f;
    out->smoothing_bits = 10u;
    out->even_smoothing = 0.025f;
    out->odd_smoothing = 0.06f;
    out->min_signal_remaining = 0.05f;
    out->enable_pcan = 1;
    out->pcan_strength = 0.95f;
    out->pcan_offset = 80.0f;
    out->pcan_gain_bits = 21u;
    out->enable_log = 1;
    out->log_scale_shift = 6u;
}

int wy_mww_feature_quantize(uint16_t feature_value)
{
    /* The frontend outputs 16-bit unsigned log-mel values in roughly 0..670.
     * Training divided them by 25.6 (float range ~0..26) and the quantized
     * model maps 0..26 onto -128..127, so the integer form of
     *   input = ((feature / 25.6) / 26.0) * 256 - 128
     * is   input = ((feature * 256) + 333) / 666 - 128
     * with 666 = 25.6 * 26.0 rounded and +333 for round-half-up. This is the
     * exact scaling ESPHome's micro_wake_word uses against the same engine. */
    int32_t value = (((int32_t)feature_value * 256) + 333) / 666;
    value += INT8_MIN;
    if (value < INT8_MIN)
        value = INT8_MIN;
    if (value > INT8_MAX)
        value = INT8_MAX;
    return (int)value;
}

/* ---- streaming bookkeeping ----------------------------------------------- */

int wy_mww_stream_init(wy_mww_stream *s, const wy_mww_params *params, unsigned stride)
{
    if (s == NULL || params == NULL)
        return -1;
    if (params->feature_size != WY_MWW_FEATURE_SIZE)
        return -1;
    if (params->sliding_window_size < 1u || params->sliding_window_size > WY_MWW_WINDOW_MAX)
        return -1;
    if (stride < 1u || stride > WY_MWW_STRIDE_MAX)
        return -1;
    s->cutoff_q = wy_mww_cutoff_quantized(params->probability_cutoff);
    s->window_size = params->sliding_window_size;
    s->detect_sum = s->cutoff_q * s->window_size;
    s->stride = stride;
    wy_mww_stream_reset(s);
    return 0;
}

void wy_mww_stream_reset(wy_mww_stream *s)
{
    if (s == NULL)
        return;
    memset(s->probs, 0, sizeof(s->probs));
    s->head = 0;
    s->slice = 0;
    s->sum = 0;
}

unsigned wy_mww_stream_slot(const wy_mww_stream *s)
{
    return (s == NULL) ? 0u : s->slice;
}

int wy_mww_stream_advance(wy_mww_stream *s)
{
    if (s == NULL)
        return 0;
    s->slice++;
    if (s->slice >= s->stride) {
        s->slice = 0;
        return 1;
    }
    return 0;
}

float wy_mww_stream_push(wy_mww_stream *s, uint8_t probability)
{
    if (s == NULL || s->window_size == 0u)
        return 0.0f;
    s->sum -= s->probs[s->head];
    s->probs[s->head] = probability;
    s->sum += probability;
    s->head = (s->head + 1u) % s->window_size;
    /* The reference decides with integers, strictly above the cutoff:
     * sum > int(cutoff * 255) * window_size. */
    if (s->sum > (unsigned long long)s->detect_sum) {
        float score = (float)s->sum / (float)((unsigned long long)s->window_size * 255ull);
        /* One-shot: consume the window so one utterance yields one crossing. */
        wy_mww_stream_reset(s);
        return score;
    }
    return 0.0f;
}
