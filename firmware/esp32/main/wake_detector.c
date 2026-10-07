#include "wake_detector.h"

#include "wy_model.h"
#include "wy_wake.h"
#include "wyoming_session.h"

#include "esp_heap_caps.h"
#include "esp_partition.h"
#include "mbedtls/md.h"
#include "nvs.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* The config-plane wake settings, staged to NVS by inc3's cfg_apply (cfg_stage_wake). The keys
 * live in the hermes_cfg namespace next to the agent's state blob. When a key is absent (first
 * boot, or the bridge never sent the setting) the schema defaults below are used -- the same
 * defaults wyoming_config.c validates against. wake_threshold is stored as a "%f" string and
 * wake_word as the model SELECTOR string (underscore form, e.g. hey_hermes), NOT the detection
 * name (WYAKE_WORD_NAME). See the plan's D5. */
#define WAKE_NVS_NAMESPACE "hermes_cfg"
#define WAKE_NVS_THRESHOLD "wake_threshold"
#define WAKE_NVS_WORD      "wake_word"
#define WAKE_DEFAULT_THRESHOLD 0.35
#define WAKE_DEFAULT_WORD      "hey_hermes"

/* The post-stream deaf window (the plan's "deaf lead-in after speech"): after a stream ends the
 * detector stays deaf this long so it does not re-fire on the answer's tail or the done tone. The
 * reference uses a 1.2 s lead-in; 1200 ms matches it. It is a device constant (not config) -- the
 * config plane exposes threshold and word, and the cooldown is part of the detector's guard. */
#define WAKE_COOLDOWN_MS 1200u

static struct {
    wy_wake *core;          /* the portable core (opaque; heap-sized via wy_wake_size) */
    int core_ready;         /* seams installed + init done */
    void *session;          /* the live link triple -- the transport a wake fires through */
    void *user;
    int (*write)(void *, const void *, size_t);

    uint8_t *model;         /* loader-owned PSRAM buffer: header + model bytes (real model only) */
    size_t model_len;
    int have_real_model;    /* 1 = a validated real model loaded, 0 = the safe placeholder */

    int16_t *chime;         /* pre-rendered chime samples (mono16), played on a detection */
    size_t chime_samples;
    size_t chime_left;      /* sample-time countdown while the tone sounds (self-trigger guard) */

    int prev_streaming;     /* to detect the 1->0 stream-end edge that starts the cooldown */
    /* live-test: one-shot forced detection (D2's force-fire hook). Set from the console task,
     * read/cleared in the capture task -- a single aligned int flag, so `volatile` keeps the
     * compiler from caching it across the task boundary (the same discipline as
     * capture.stopping). */
    volatile int force_fire;
} wd;

/* ---- config ------------------------------------------------------------------- */

static void read_wake_config(double *threshold, char *word, size_t word_cap) {
    *threshold = WAKE_DEFAULT_THRESHOLD;
    snprintf(word, word_cap, "%s", WAKE_DEFAULT_WORD);
    nvs_handle_t handle;
    if (nvs_open(WAKE_NVS_NAMESPACE, NVS_READONLY, &handle) != ESP_OK) return;
    char buf[32];
    size_t len = sizeof(buf);
    if (nvs_get_str(handle, WAKE_NVS_THRESHOLD, buf, &len) == ESP_OK && buf[0]) {
        char *end = NULL;
        double v = strtod(buf, &end);
        /* The schema bounds it to [0.05, 0.99]; clamp anything out of band to the default so a
         * torn or hostile stored value cannot make the detector un-triggerable or hair-trigger. */
        if (end != buf && v >= 0.05 && v <= 0.99) *threshold = v;
    }
    len = word_cap;
    if (nvs_get_str(handle, WAKE_NVS_WORD, word, &len) != ESP_OK || !word[0])
        snprintf(word, word_cap, "%s", WAKE_DEFAULT_WORD);
    nvs_close(handle);
}

/* ---- seams --------------------------------------------------------------------- */

/* The #110 scorer's placeholder (D2): deterministic and sub-threshold, so the detector never
 * fires on real audio in normal operation. force_fire makes exactly one frame cross the
 * threshold so the live-test gate can drive the whole chime -> wake path with no trained model.
 * #110 replaces this with the real inference over `model`. */
static float placeholder_score(void *user, const int16_t *mono, size_t n) {
    (void)user;
    (void)mono;
    (void)n;
    if (wd.force_fire) {
        wd.force_fire = 0;
        return 1.0f;   /* crosses any threshold in [0, 1]: a forced detection */
    }
    return 0.0f;
}

/* D3: read the `model` data partition and hand the core its bytes. A validated real model is
 * preferred; anything else (absent partition, never provisioned, bad header, name mismatch, or a
 * failed SHA-256) falls back to a tiny safe placeholder so the deterministic scorer still runs
 * and the force-fire live-test hook still works. The corrupt/absent real model is DISCARDED, never
 * used -- so we never run a garbage scorer (the plan's safety property), and the placeholder is
 * itself sub-threshold so the detector is "armed but mute" on real audio exactly like the core's
 * no-model state. */
static int model_load(void *user, const char *model_name, void **out_model, size_t *out_len) {
    (void)user;
    /* Non-const on purpose: the seam returns void *, and casting away const on a static would
     * be a needless const violation. It is never written -- the placeholder scorer ignores it.
     * This is the only thing the loader hands back with no real model. */
    static uint8_t placeholder[4] = { 'P', 'L', 'C', 'H' };
    if (!out_model || !out_len) return -1;
    *out_model = NULL;
    *out_len = 0;

    const esp_partition_t *part =
        esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY, "model");
    if (part && part->size >= WY_MODEL_HEADER_SIZE) {
        /* Read the header first to learn the model's real length before allocating. This is a
         * header-ONLY read, so it must go through wy_model_header_peek (fixed fields, no
         * whole-blob length check) -- NOT wy_model_header_parse, whose "does the blob hold
         * data_len bytes" check can never pass against a buffer holding only the header. Using
         * parse here made the entire real-model branch unreachable and silently discarded every
         * valid provisioned model. The authoritative whole-blob parse happens below on the full
         * read, where that length check is exactly right. */
        uint8_t header_buf[WY_MODEL_HEADER_SIZE];
        if (esp_partition_read(part, 0, header_buf, sizeof(header_buf)) == ESP_OK) {
            wy_model_header header;
            if (wy_model_header_peek(header_buf, sizeof(header_buf), &header) == 0 &&
                header.data_len > 0 &&
                (uint64_t)WY_MODEL_HEADER_SIZE + header.data_len <= part->size &&
                model_name && strcmp(header.name, model_name) == 0) {
                size_t total = (size_t)WY_MODEL_HEADER_SIZE + header.data_len;
                uint8_t *buf = heap_caps_malloc(total, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
                if (buf && esp_partition_read(part, 0, buf, total) == ESP_OK) {
                    /* Re-parse on the full buffer (authoritative) and verify SHA-256 over the
                     * model bytes: a model that fails either check is corrupt and is dropped. */
                    wy_model_header full;
                    unsigned char digest[WY_MODEL_SHA256_LEN];
                    const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
                    if (wy_model_header_parse(buf, total, &full) == 0 && info &&
                        mbedtls_md(info, buf + WY_MODEL_HEADER_SIZE, full.data_len, digest) == 0 &&
                        memcmp(digest, full.sha256, WY_MODEL_SHA256_LEN) == 0) {
                        if (wd.model) heap_caps_free(wd.model);
                        wd.model = buf;
                        wd.model_len = total;
                        wd.have_real_model = 1;
                        /* The scorer consumes the model bytes that follow the header. */
                        *out_model = buf + WY_MODEL_HEADER_SIZE;
                        *out_len = full.data_len;
                        return 0;
                    }
                }
                if (buf) heap_caps_free(buf);
            }
        }
    }
    /* No usable real model: hand back a tiny safe placeholder. It is never scored against (the
     * placeholder scorer ignores it); it exists so has_model is set and the deterministic scorer
     * runs, keeping the force-fire gate usable with no trained model on the device. A model
     * buffer held from an earlier link is freed here so a re-init that loses its model cannot
     * leak the old one (and have_real_model / wd.model cannot disagree). */
    if (wd.model) {
        heap_caps_free(wd.model);
        wd.model = NULL;
        wd.model_len = 0;
    }
    wd.have_real_model = 0;
    *out_model = (void *)placeholder;
    *out_len = sizeof(placeholder);
    return 0;
}

/* D4: play the rendered "awake" tone through the speaker ring (the same path a bridge answer
 * takes, so the amp policy -- armed while the ring is non-empty -- carries it). The chime is
 * counted down in the feed so the detector stays deaf while its own tone is sounding. */
static int chime_play(void *user) {
    (void)user;
    if (!wd.session || !wd.chime || !wd.chime_samples) return -1;
    if (wy_session_local_play(wd.session, wd.chime, wd.chime_samples) != WY_OK) return -1;
    wd.chime_left = wd.chime_samples;
    return 0;
}

/* The run-satellite flow: fire wy_session_wake through the live link. A 0 return tells the core
 * the session ACCEPTED the wake (it self-deafens until the stream ends); non-zero is a rejection
 * (paused / not armed), and the core stays armed to retry. */
static int wake_fire(void *user) {
    (void)user;
    if (!wd.session || !wd.write) return -1;
    return wy_session_wake(wd.session, WYAKE_WORD_NAME, wd.write, wd.user) == WY_OK ? 0 : -1;
}

/* ---- lifecycle ------------------------------------------------------------------ */

void wake_detector_start(void *session, void *user,
                         int (*write)(void *, const void *, size_t)) {
    if (!session || !write) return;
    wd.session = session;
    wd.user = user;
    wd.write = write;

    if (!wd.core) wd.core = calloc(1, wy_wake_size());
    if (!wd.core) return;

    /* Pre-render the chime once (deterministic pure math) so the detection path only streams it,
     * never synthesizes it mid-callback. */
    if (!wd.chime) {
        size_t need = wy_chime_render(NULL, 0);
        if (need > 0) {
            wd.chime = heap_caps_malloc(need * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
            if (wd.chime) {
                wd.chime_samples = wy_chime_render(wd.chime, need);
            }
        }
    }

    double threshold;
    char word[WY_MODEL_NAME_MAX];
    read_wake_config(&threshold, word, sizeof(word));

    /* Seams first, then init: init loads the model through the load seam. */
    wy_wake_set_seams(wd.core, placeholder_score, model_load, chime_play, wake_fire, NULL);
    wd.prev_streaming = 0;
    wd.chime_left = 0;
    wd.core_ready = (wy_wake_init(wd.core, threshold, WAKE_COOLDOWN_MS, word) == 0);
}

void wake_detector_feed(const int16_t *mono, size_t n) {
    if (!wd.core_ready || !wd.core || !wd.session || !mono || n == 0) return;

    /* Sync the deaf guard from live session state -- single-consumer, only ever from the capture
     * task. The chime's sample-time countdown keeps the core deaf while its own tone sounds. */
    if (wd.chime_left > 0) {
        wd.chime_left = (wd.chime_left > n) ? wd.chime_left - n : 0;
    }
    int streaming = wy_session_streaming(wd.session);
    /* "The speaker is sounding" is the ring being non-empty (a chime or an answer still
     * draining) OR the bridge's playback flag: either way the mic can hear the device's own
     * output, so the detector must be deaf against a self-trigger. */
    int playing = wy_session_playback_active(wd.session) ||
                  wy_session_playback_bytes(wd.session) > 0;
    if (wd.prev_streaming && !streaming) wy_wake_stream_ended(wd.core);   /* 1->0: cooldown */
    wd.prev_streaming = streaming;
    wy_wake_set_streaming(wd.core, streaming);
    wy_wake_set_playback_active(wd.core, playing || wd.chime_left > 0);

    /* Score only while armed: a wake can only succeed when the host has run-satellite'd, so
     * feeding the scorer at any other time would only ever produce a rejected fire. The core
     * itself then self-gates on its deaf guard. */
    if (wy_session_state(wd.session) == WY_SESSION_RUN_REQUESTED)
        wy_wake_feed(wd.core, mono, n);
}

void wake_detector_stop(void) {
    if (wd.core) wy_wake_destroy(wd.core);
    if (wd.model) {
        heap_caps_free(wd.model);
        wd.model = NULL;
        wd.model_len = 0;
    }
    wd.have_real_model = 0;
    wd.prev_streaming = 0;
    wd.chime_left = 0;
    wd.force_fire = 0;
    wd.session = NULL;
    wd.user = NULL;
    wd.write = NULL;
    /* The core, the chime buffer, and the seams are kept across links (a re-start re-inits the
     * core with the same seams); only per-link state is cleared here. */
}

void wake_detector_force_fire(void) {
    wd.force_fire = 1;
}

void wake_detector_status(char *out, size_t cap) {
    if (!out || cap == 0) return;
    snprintf(out, cap, "model=%s deaf=%d force=%d threshold_armed=%d",
             wd.have_real_model ? "real" : "placeholder",
             wd.core_ready && wy_wake_deaf(wd.core) ? 1 : 0,
             wd.force_fire ? 1 : 0,
             wd.core_ready ? 1 : 0);
}
