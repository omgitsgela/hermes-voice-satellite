/* The #110 device scorer: microfrontend log-mel features -> streaming
 * TFLite-Micro inference -> the portable wy_mww sliding-window decision. See
 * wy_mww_scorer.h for the contract; wy_mww.c for the policy being executed.
 *
 * The engine is espressif/esp-tflite-micro (vendored, see ../VENDORED.md) and
 * the feature frontend is esphome/esp-micro-speech-features -- the same pair
 * ESPHome's micro_wake_word component runs, so the bundled microWakeWord model
 * sees exactly the feature pipeline it was trained against.
 */
#include "wy_mww_scorer.h"

#include "wy_mww.h"

#include <frontend.h>
#include <frontend_util.h>

#include "tensorflow/lite/micro/micro_allocator.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/micro/micro_resource_variable.h"
#include "tensorflow/lite/schema/schema_generated.h"

#include "esp_heap_caps.h"
#include "esp_log.h"

#include <new>
#include <stdlib.h>
#include <string.h>

static const char *TAG = "wy_mww";

/* The variable arena only holds the resource-variable bookkeeping (the
 * variable buffers themselves are persistent allocations of the tensor arena);
 * 1024 bytes with up to 20 variables is the reference layout. */
#define WY_MWW_VAR_ARENA 1024u
#define WY_MWW_VAR_MAX   20

struct wy_mww_scorer {
    wy_mww_params policy;
    wy_mww_stream stream;
    const uint8_t *model; /* caller-owned: the validated model bytes */
    size_t model_len;

    FrontendState frontend;
    int frontend_ready;

    tflite::MicroMutableOpResolver<20> *resolver;
    tflite::MicroAllocator *var_allocator;
    tflite::MicroResourceVariables *resource_vars;
    tflite::MicroInterpreter *interpreter;

    uint8_t *tensor_arena;
    size_t tensor_arena_size;
    uint8_t *var_arena;

    int8_t features[WY_MWW_FEATURE_SIZE];
    int invoke_failed;
};

/* Arenas in PSRAM (8 MB fitted) with an internal-RAM fallback; 16-byte
 * aligned, which TFLM wants for the arena layout. */
static void *scorer_alloc(size_t bytes)
{
    void *p = heap_caps_aligned_alloc(16, bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (p == NULL)
        p = heap_caps_aligned_alloc(16, bytes, MALLOC_CAP_8BIT);
    return p;
}

static bool prepare_var_state(wy_mww_scorer *s)
{
    if (s->var_arena == NULL) {
        s->var_arena = (uint8_t *)scorer_alloc(WY_MWW_VAR_ARENA);
        if (s->var_arena == NULL)
            return false;
    }
    /* Zeroed so a re-prepare cannot inherit a previous run's bookkeeping. */
    memset(s->var_arena, 0, WY_MWW_VAR_ARENA);
    s->var_allocator = tflite::MicroAllocator::Create(s->var_arena, WY_MWW_VAR_ARENA);
    if (s->var_allocator == NULL)
        return false;
    s->resource_vars = tflite::MicroResourceVariables::Create(s->var_allocator, WY_MWW_VAR_MAX);
    return s->resource_vars != NULL;
}

static void teardown_interpreter(wy_mww_scorer *s)
{
    /* The interpreter is ours (new/delete); the allocator and the resource
     * variables live INSIDE the arenas and are freed with them. */
    delete s->interpreter;
    s->interpreter = NULL;
    s->var_allocator = NULL;
    s->resource_vars = NULL;
    if (s->tensor_arena != NULL) {
        heap_caps_free(s->tensor_arena);
        s->tensor_arena = NULL;
        s->tensor_arena_size = 0;
    }
}

/* Allocate + prepare the interpreter, escalating the manifest's arena hint:
 * x1, x1.5, x2 (the reference does the same -- different esp-nn versions need
 * different amounts). Returns false with everything torn down on failure. */
static bool prepare_interpreter(wy_mww_scorer *s, const tflite::Model *model)
{
    const size_t base = s->policy.tensor_arena_size;
    const size_t attempts[3] = { base, base * 3u / 2u, base * 2u };

    for (size_t attempt = 0; attempt < 3; attempt++) {
        size_t size = (attempts[attempt] + 15u) & ~((size_t)15u);
        s->tensor_arena = (uint8_t *)scorer_alloc(size);
        if (s->tensor_arena == NULL)
            continue;
        s->tensor_arena_size = size;
        if (!prepare_var_state(s)) {
            teardown_interpreter(s);
            return false;
        }
        s->interpreter = new (std::nothrow)
            tflite::MicroInterpreter(model, *s->resolver, s->tensor_arena, size, s->resource_vars);
        if (s->interpreter == NULL) {
            teardown_interpreter(s);
            return false;
        }
        if (s->interpreter->AllocateTensors() == kTfLiteOk) {
            ESP_LOGI(TAG, "interpreter ready: tensor arena %u bytes (manifest hint %u)",
                     (unsigned)size, (unsigned)base);
            return true;
        }
        ESP_LOGW(TAG, "AllocateTensors failed at %u bytes", (unsigned)size);
        /* A failed attempt may have consumed persistent state: re-prepare. */
        teardown_interpreter(s);
        if (!prepare_var_state(s))
            return false;
    }
    return false;
}

static bool register_ops(tflite::MicroMutableOpResolver<20> *r)
{
    /* The superset ESPHome registers for streaming microWakeWord models: the
     * bundled model uses 13 of these (conv/depthwise/fully-connected, the
     * variable ops for its streaming state, concat/reshape/strided-slice/
     * split-v/quantize/logistic); the rest keep a custom provisioned model
     * working without a firmware change. */
    return r->AddCallOnce() == kTfLiteOk && r->AddVarHandle() == kTfLiteOk &&
           r->AddReshape() == kTfLiteOk && r->AddReadVariable() == kTfLiteOk &&
           r->AddStridedSlice() == kTfLiteOk && r->AddConcatenation() == kTfLiteOk &&
           r->AddAssignVariable() == kTfLiteOk && r->AddConv2D() == kTfLiteOk &&
           r->AddMul() == kTfLiteOk && r->AddAdd() == kTfLiteOk && r->AddMean() == kTfLiteOk &&
           r->AddFullyConnected() == kTfLiteOk && r->AddLogistic() == kTfLiteOk &&
           r->AddQuantize() == kTfLiteOk && r->AddDepthwiseConv2D() == kTfLiteOk &&
           r->AddAveragePool2D() == kTfLiteOk && r->AddMaxPool2D() == kTfLiteOk &&
           r->AddPad() == kTfLiteOk && r->AddPack() == kTfLiteOk && r->AddSplitV() == kTfLiteOk;
}

extern "C" wy_mww_scorer *wy_mww_scorer_create(const void *model, size_t model_len,
                                               const struct wy_mww_scorer_params *params)
{
    wy_mww_params policy;

    if (model == NULL || model_len == 0) {
        ESP_LOGE(TAG, "no model bytes");
        return NULL;
    }
    const tflite::Model *tfl_model = tflite::GetModel(model);
    if (tfl_model == NULL || tfl_model->version() != TFLITE_SCHEMA_VERSION) {
        ESP_LOGE(TAG, "model is not a supported tflite flatbuffer");
        return NULL;
    }

    wy_mww_params_default(&policy);
    if (params != NULL) {
        if (!(params->probability_cutoff > 0.0f && params->probability_cutoff <= 1.0f) ||
            params->sliding_window_size < 1u || params->sliding_window_size > WY_MWW_WINDOW_MAX ||
            params->feature_step_ms < 1u || params->feature_step_ms > 100u ||
            params->tensor_arena_size < 1024u || params->tensor_arena_size > 1048576u) {
            ESP_LOGE(TAG, "bad scorer parameters");
            return NULL;
        }
        policy.probability_cutoff = params->probability_cutoff;
        policy.sliding_window_size = params->sliding_window_size;
        policy.feature_step_ms = params->feature_step_ms;
        policy.tensor_arena_size = params->tensor_arena_size;
    }

    wy_mww_scorer *s = new (std::nothrow) wy_mww_scorer{};
    if (s == NULL)
        return NULL;
    s->policy = policy;
    s->model = (const uint8_t *)model;
    s->model_len = model_len;

    /* The feature frontend: the fixed microWakeWord preprocessor settings. */
    wy_mww_frontend fp;
    wy_mww_frontend_params(&fp, policy.feature_step_ms);
    struct FrontendConfig fc;
    FrontendFillConfigWithDefaults(&fc);
    fc.window.size_ms = (uint16_t)fp.window_size_ms;
    fc.window.step_size_ms = (uint16_t)fp.step_size_ms;
    fc.filterbank.num_channels = (int)fp.num_channels;
    fc.filterbank.lower_band_limit = fp.lower_band_limit;
    fc.filterbank.upper_band_limit = fp.upper_band_limit;
    fc.noise_reduction.smoothing_bits = (int)fp.smoothing_bits;
    fc.noise_reduction.even_smoothing = fp.even_smoothing;
    fc.noise_reduction.odd_smoothing = fp.odd_smoothing;
    fc.noise_reduction.min_signal_remaining = fp.min_signal_remaining;
    fc.pcan_gain_control.enable_pcan = fp.enable_pcan;
    fc.pcan_gain_control.strength = fp.pcan_strength;
    fc.pcan_gain_control.offset = fp.pcan_offset;
    fc.pcan_gain_control.gain_bits = (int)fp.pcan_gain_bits;
    fc.log_scale.enable_log = fp.enable_log;
    fc.log_scale.scale_shift = (int)fp.log_scale_shift;
    if (!FrontendPopulateState(&fc, &s->frontend, (int)WY_MWW_SAMPLE_RATE)) {
        ESP_LOGE(TAG, "feature frontend init failed");
        delete s;
        return NULL;
    }
    s->frontend_ready = 1;

    s->resolver = new (std::nothrow) tflite::MicroMutableOpResolver<20>();
    if (s->resolver == NULL || !register_ops(s->resolver)) {
        ESP_LOGE(TAG, "op resolver registration failed");
        wy_mww_scorer_destroy(s);
        return NULL;
    }

    if (!prepare_interpreter(s, tfl_model)) {
        ESP_LOGE(TAG, "tensor arena / interpreter setup failed");
        wy_mww_scorer_destroy(s);
        return NULL;
    }

    /* Tensor layout: input [1, stride, 40] int8, output [1, 1] uint8. Anything
     * else is a different model family and must not run through this
     * pipeline -- refuse instead of scoring garbage. */
    TfLiteTensor *input = s->interpreter->input(0);
    TfLiteTensor *output = s->interpreter->output(0);
    if (input == NULL || output == NULL || input->dims == NULL || output->dims == NULL ||
        input->dims->size != 3 || input->dims->data[0] != 1 ||
        input->dims->data[2] != (int)policy.feature_size ||
        input->dims->data[1] < 1 || input->dims->data[1] > (int)WY_MWW_STRIDE_MAX ||
        input->type != kTfLiteInt8 || output->dims->size != 2 ||
        output->dims->data[0] != 1 || output->dims->data[1] != 1 || output->type != kTfLiteUInt8) {
        ESP_LOGE(TAG, "model tensor layout mismatch (want in [1, stride, %u] int8, out [1, 1] uint8)",
                 (unsigned)policy.feature_size);
        wy_mww_scorer_destroy(s);
        return NULL;
    }
    unsigned stride = (unsigned)input->dims->data[1];
    if (wy_mww_stream_init(&s->stream, &policy, stride) != 0) {
        wy_mww_scorer_destroy(s);
        return NULL;
    }

    ESP_LOGI(TAG, "model ready: input [1, %u, %u] int8, cutoff %.2f, window %u, arena %u",
             stride, (unsigned)policy.feature_size, (double)policy.probability_cutoff,
             policy.sliding_window_size, (unsigned)s->tensor_arena_size);
    return s;
}

extern "C" float wy_mww_scorer_feed(wy_mww_scorer *s, const int16_t *mono, size_t n)
{
    if (s == NULL || mono == NULL || n == 0 || s->interpreter == NULL || s->invoke_failed)
        return 0.0f;

    float score = 0.0f;
    size_t consumed_total = 0;
    while (consumed_total < n) {
        size_t processed = 0;
        struct FrontendOutput out =
            FrontendProcessSamples(&s->frontend, mono + consumed_total, n - consumed_total, &processed);
        /* The frontend consumes at least one sample per call, so this loop
         * terminates; a 0-consume call (only possible on an empty feed) stops
         * it defensively. */
        if (processed == 0)
            break;
        consumed_total += processed;
        if (out.size == 0)
            continue; /* buffered internally: no feature slice yet */
        if (out.size != s->policy.feature_size)
            continue; /* defensive: the config pins num_channels == feature_size */

        for (size_t i = 0; i < out.size; i++)
            s->features[i] = (int8_t)wy_mww_feature_quantize(out.values[i]);

        TfLiteTensor *input = s->interpreter->input(0);
        int8_t *slot = input->data.int8 + (size_t)WY_MWW_FEATURE_SIZE * wy_mww_stream_slot(&s->stream);
        memcpy(slot, s->features, sizeof(s->features));
        if (wy_mww_stream_advance(&s->stream)) {
            if (s->interpreter->Invoke() != kTfLiteOk) {
                /* Fail silent-but-safe: never score again rather than emit
                 * garbage probabilities. */
                ESP_LOGE(TAG, "invoke failed; scoring disabled until reset");
                s->invoke_failed = 1;
                return 0.0f;
            }
            TfLiteTensor *output = s->interpreter->output(0);
            float step = wy_mww_stream_push(&s->stream, output->data.uint8[0]);
            if (step > score)
                score = step;
        }
    }
    return score;
}

extern "C" void wy_mww_scorer_reset(wy_mww_scorer *s)
{
    if (s == NULL)
        return;
    if (s->frontend_ready)
        FrontendReset(&s->frontend);
    wy_mww_stream_reset(&s->stream);
    s->invoke_failed = 0;
    /* Re-create the interpreter so the model's streaming state (its resource
     * variables) starts clean: a stateful model resuming after a stream gap
     * would otherwise score the new audio against the old audio's history. */
    if (s->interpreter != NULL && s->model != NULL) {
        const tflite::Model *tfl_model = tflite::GetModel(s->model);
        teardown_interpreter(s);
        if (tfl_model == NULL || !prepare_interpreter(s, tfl_model)) {
            ESP_LOGE(TAG, "scorer reset: interpreter re-init failed");
            s->invoke_failed = 1;
        }
    }
}

extern "C" void wy_mww_scorer_destroy(wy_mww_scorer *s)
{
    if (s == NULL)
        return;
    teardown_interpreter(s);
    if (s->var_arena != NULL) {
        heap_caps_free(s->var_arena);
        s->var_arena = NULL;
        s->var_allocator = NULL;
        s->resource_vars = NULL;
    }
    delete s->resolver;
    s->resolver = NULL;
    if (s->frontend_ready) {
        FrontendFreeStateContents(&s->frontend);
        s->frontend_ready = 0;
    }
    delete s;
}

extern "C" unsigned wy_mww_scorer_stride(const wy_mww_scorer *s)
{
    return (s != NULL) ? s->stream.stride : 0u;
}

extern "C" unsigned wy_mww_scorer_arena_bytes(const wy_mww_scorer *s)
{
    return (s != NULL) ? (unsigned)s->tensor_arena_size : 0u;
}
