#ifndef WY_MWW_SCORER_H
#define WY_MWW_SCORER_H
#include <stddef.h>
#include <stdint.h>

/*
 * The #110 device scorer: real inference over a provisioned microWakeWord v2
 * streaming model (the wy_wake_score_fn seam's implementation). One mono16
 * frame at 16 kHz in, one score in [0, 1] out, with all the streaming state
 * (microfrontend, model input ring, model variables, probability window) held
 * in the scorer. The policy it executes is the portable wy_mww module; this
 * component is only the ESP/TFLite-Micro driver around it:
 *
 *   frame -> microfrontend log-mel (40 ch, 30 ms window / manifest step)
 *         -> int8 features (wy_mww_feature_quantize)
 *         -> streaming TFLM interpreter (input [1, stride, 40] int8,
 *            invoked once per `stride` slices; output [1, 1] uint8)
 *         -> sliding window vs the manifest's probability_cutoff
 *         -> score (wy_mww_stream_push).
 *
 * The score is the model's sliding-window MEAN probability on the 0..1 scale
 * at the moment the window crosses the model's own probability_cutoff, and
 * 0.0 otherwise. The crossing consumes the window, so one utterance produces
 * exactly one non-zero score: a one-shot scorer, which is what the wy_wake
 * core's per-frame threshold test expects. The core's `wake_threshold` is a
 * second gate on top (a detection fires only when the score also clears it).
 *
 * Lifecycle: create() loads and validates the model (it copies nothing and
 * keeps the caller's model pointer -- the loader owns the bytes) and refuses
 * to return a scorer for a model whose tensors do not match the expected
 * layout, so a wrong/corrupt model can never run as a garbage scorer.
 * reset() cold-starts all streaming state (frontend, model variables, window)
 * -- the glue calls it when the detector re-arms so the stateful model never
 * resumes from a stale state across a stream gap. destroy() frees everything
 * except the model bytes.
 */

typedef struct wy_mww_scorer wy_mww_scorer;

/* Minimal parameter view (wy_mww_params is portable but keep this header
 * dependency-free for C callers). Pass NULL for the bundled defaults. */
struct wy_mww_scorer_params {
    float probability_cutoff;
    unsigned sliding_window_size;
    unsigned feature_step_ms;
    unsigned tensor_arena_size;
};

#ifdef __cplusplus
extern "C" {
#endif

/* Returns NULL when the model bytes are not a usable streaming microWakeWord
 * model for this pipeline (bad flatbuffer, unsupported schema, tensor layout
 * or dtypes that do not match, or the tensor arena could not be allocated and
 * prepared at up to 2x the manifest size). */
wy_mww_scorer *wy_mww_scorer_create(const void *model, size_t model_len,
                                    const struct wy_mww_scorer_params *params);

/* One frame of 16 kHz mono16 audio (any length; 160-sample 10 ms chunks are
 * the capture task's cadence). Returns the score (see above). */
float wy_mww_scorer_feed(wy_mww_scorer *s, const int16_t *mono, size_t n);

/* Cold-start the streaming state (frontend, model variables, window). */
void wy_mww_scorer_reset(wy_mww_scorer *s);

void wy_mww_scorer_destroy(wy_mww_scorer *s);

/* Diagnostics for the provisioning console: the model's stride (feature
 * slices per invoke) and the tensor arena actually in use. */
unsigned wy_mww_scorer_stride(const wy_mww_scorer *s);
unsigned wy_mww_scorer_arena_bytes(const wy_mww_scorer *s);

#ifdef __cplusplus
}
#endif
#endif
