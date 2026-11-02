#ifndef WY_MWW_H
#define WY_MWW_H
#include <stddef.h>
#include <stdint.h>

/*
 * The microWakeWord v2 inference POLICY (issue #110's host-testable half).
 * Pure C11 with no ESP/TFLM dependency: the exact numbers and decisions the
 * device scorer must make -- the manifest parameters, the frontend
 * configuration the model was trained against, the int8 feature scaling, the
 * stride bookkeeping of the streaming input tensor, and the sliding-window /
 * probability-cutoff detection decision. The device scorer
 * (wy_mww_scorer.cpp: microfrontend + TFLite-Micro) drives THIS, and these
 * sources compile on the host with -std=c11 -Wall -Wextra -Werror and are
 * tested directly (the wy_wake / wy_model pattern).
 *
 * The reference behaviour being matched is ESPHome's micro_wake_word component
 * (the engine the bundled model was trained for):
 *
 *   features  40 log-mel channels per 30 ms window, 10 ms step, from the
 *             audio microfrontend (see wy_mww_frontend_params).
 *   quantize  input = clamp(((feature * 256) + 333) / 666 - 128) -- matching
 *             the TFLite audio-frontend int8 output the model was trained on.
 *   streaming the model's input tensor is [1, stride, 40] int8 (stride comes
 *             from the tensor itself, not from the manifest): one feature
 *             slice is written per 10 ms step and the interpreter is invoked
 *             once every `stride` slices.
 *   output    [1, 1] uint8, a probability on the 0..255 scale.
 *   detect    keep the last `sliding_window_size` probabilities; detect when
 *             sum > int(probability_cutoff * 255) * sliding_window_size, i.e.
 *             the window MEAN crosses the model's own cutoff.
 *
 * The window/cutoff decision is the model's gate. The wy_wake core's
 * `wake_threshold` (config) is a second, independent gate on the returned
 * score: a detection fires only when the model's window crosses its cutoff AND
 * the returned mean clears the engine threshold.
 */

/* The audio contract (wy_wake's frames): 16 kHz mono int16. */
#define WY_MWW_SAMPLE_RATE 16000u

#ifdef __cplusplus
extern "C" {
#endif

/* The log-mel channel count the bundled model's input tensor carries
 * (tflite input dims [1, stride, 40]); every microWakeWord model trains
 * against 40. The device scorer refuses a model whose tensor disagrees. */
#define WY_MWW_FEATURE_SIZE 40u

/* Bounds for the fixed-size sliding window and the stride bookkeeping. */
#define WY_MWW_WINDOW_MAX 32u
#define WY_MWW_STRIDE_MAX 32u

/*
 * The model manifest (models/hey_hermes.json, the `micro` block). The device
 * cannot read the JSON at runtime -- the model partition carries the bare
 * .tflite under the WKM1 header -- so the provisioner copies the manifest's
 * numbers into a version 2 header and the glue passes them to the scorer. These
 * defaults are the fallback for a model provisioned without its manifest
 * (header v1), and scripts/test_esp32_wake_scorer.py pins them to the shipped
 * default model's JSON so the two cannot drift. The parser exists for the same
 * test and for the provisioner's manifest read.
 */
typedef struct wy_mww_params {
    float probability_cutoff;    /* 0..1 (0.50 in the bundled default manifest) */
    unsigned sliding_window_size; /* probabilities per average (5) */
    unsigned feature_step_ms;    /* frontend step size in ms (10) */
    unsigned tensor_arena_size;  /* manifest tensor arena hint (26080) */
    unsigned feature_size;       /* features per slice (40) */
} wy_mww_params;

/* Defaults, exactly the values of firmware/esp32/models/hey_hermes.json (the
 * bundled default model), used only when a provisioned blob carries no policy. */
void wy_mww_params_default(wy_mww_params *out);

/* Parse a manifest JSON (the .json companion of a microWakeWord model).
 * Returns 0 with *out filled when all four `micro` numbers are present and
 * sane, -1 otherwise (*out untouched). Tolerant of arbitrary whitespace and
 * key order; the four keys are unique in the manifest format. */
int wy_mww_params_parse(const char *json, size_t json_len, wy_mww_params *out);

/* The cutoff on the output tensor's 0..255 probability scale, truncating
 * exactly like the reference (int(cutoff * 255)). */
unsigned wy_mww_cutoff_quantized(float probability_cutoff);

/*
 * The feature preprocessor's configuration. Fixed for every microWakeWord
 * model (they are all trained against these settings); only the step size
 * travels in the manifest. Mirrors ESPHome's preprocessor_settings.h.
 */
typedef struct wy_mww_frontend {
    unsigned window_size_ms;    /* 30: each slice is a 30 ms window */
    unsigned step_size_ms;      /* 10: one feature per 10 ms */
    unsigned num_channels;      /* 40: == WY_MWW_FEATURE_SIZE */
    float lower_band_limit;     /* 125.0 Hz */
    float upper_band_limit;     /* 7500.0 Hz */
    unsigned smoothing_bits;    /* 10 */
    float even_smoothing;       /* 0.025 */
    float odd_smoothing;        /* 0.06 */
    float min_signal_remaining; /* 0.05 */
    int enable_pcan;            /* 1 */
    float pcan_strength;        /* 0.95 */
    float pcan_offset;          /* 80.0 */
    unsigned pcan_gain_bits;    /* 21 */
    int enable_log;             /* 1 */
    unsigned log_scale_shift;   /* 6 */
} wy_mww_frontend;

/* Fill the fixed settings, with the manifest's step size. */
void wy_mww_frontend_params(wy_mww_frontend *out, unsigned step_size_ms);

/* The int8 scaling of one frontend feature value (0..65535 in, int8 out):
 *   input = clamp(((feature * 256) + 333) / 666 - 128)
 * (666 = 25.6 * 26.0 rounded; see wy_mww.c for the derivation). Returns the
 * int8 value as an int in [-128, 127]. */
int wy_mww_feature_quantize(uint16_t feature_value);

/*
 * The streaming inference bookkeeping: which input-tensor slice to fill, when
 * to invoke, and the sliding-window / cutoff decision. The device scorer owns
 * the tensor memory; this owns the counts and the decision.
 */
typedef struct wy_mww_stream {
    unsigned cutoff_q;   /* wy_mww_cutoff_quantized(params.probability_cutoff) */
    unsigned window_size;
    unsigned detect_sum; /* cutoff_q * window_size: detect when sum > this */
    unsigned stride;     /* feature slices per invoke (tensor dim 1) */
    unsigned slice;      /* slices filled into the current invoke window */
    unsigned head;       /* ring position of the oldest probability */
    unsigned long long sum;
    uint8_t probs[WY_MWW_WINDOW_MAX];
} wy_mww_stream;

/* Returns 0 on success, -1 on a bad argument (NULL, window/stride out of
 * bounds). The window starts zeroed, exactly like the reference. */
int wy_mww_stream_init(wy_mww_stream *s, const wy_mww_params *params, unsigned stride);

/* Zero the window (after a detection or on a cold start). */
void wy_mww_stream_reset(wy_mww_stream *s);

/* The input-tensor slice index (in units of WY_MWW_FEATURE_SIZE int8s) the
 * next feature slice belongs at: 0 .. stride-1. */
unsigned wy_mww_stream_slot(const wy_mww_stream *s);

/* Record that a slice was written. Returns 1 when the invoke window is full
 * (the caller invokes the interpreter now), 0 otherwise. */
int wy_mww_stream_advance(wy_mww_stream *s);

/* Record one invoke's output probability (the raw 0..255 tensor byte). Returns
 * the SCORE for this step: the window mean on the 0..1 scale when the window
 * crosses the model's cutoff (sum > cutoff_q * window_size), and 0.0 the rest
 * of the time. A crossing consumes the window (it is zeroed) so one utterance
 * yields one crossing -- the one-shot property the wy_wake core expects from a
 * scorer (a fired detection must not re-fire every frame). */
float wy_mww_stream_push(wy_mww_stream *s, uint8_t probability);

#ifdef __cplusplus
}
#endif
#endif
