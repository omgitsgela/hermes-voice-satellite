/* The local wake detector core. Pure C, one consumer (the mic task), no locks:
 * every input is an injected seam, so this exact source is host-tested under
 * -Werror (the wyoming_config pattern) and links unchanged into the firmware.
 *
 * State: armed (scoring) -> fired (streaming, self-deaf) -> stream_ended
 * (post-stream cooldown) -> armed. A fired core does not re-fire on its own
 * answer: it is deaf until the stream ends, and then deaf again for the
 * configured cooldown. See wy_wake.h for the seams and ownership rules.
 */
#include "wy_wake.h"
#include <math.h>
#include <string.h>

#define WY_WAKE_NAME_MAX 64u
#define WY_WAKE_FRAME_MS 10u /* 160 samples at 16 kHz: the capture chunk cadence */

struct wy_wake {
    double threshold;
    unsigned cooldown_frames;
    char model_name[WY_WAKE_NAME_MAX];
    wy_wake_score_fn score;
    wy_wake_load_fn load;
    wy_wake_chime_fn chime;
    wy_wake_wake_fn wake;
    void *user;
    void *model; /* loader-owned; the core never frees it */
    size_t model_len;
    int has_model;
    int streaming;
    int playback;
    int in_cooldown;
    unsigned cooldown_left;
};

size_t wy_wake_size(void)
{
    return sizeof(struct wy_wake);
}

void wy_wake_set_seams(wy_wake *w, wy_wake_score_fn score, wy_wake_load_fn load,
                       wy_wake_chime_fn chime, wy_wake_wake_fn wake, void *user)
{
    if (w == NULL)
        return;
    w->score = score;
    w->load = load;
    w->chime = chime;
    w->wake = wake;
    w->user = user;
}

int wy_wake_init(wy_wake *w, double threshold, unsigned cooldown_ms, const char *model_name)
{
    if (w == NULL || model_name == NULL)
        return -1;
    /* !(x) is true for NaN, so this rejects NaN as well as out-of-range. */
    if (!(threshold >= 0.0 && threshold <= 1.0))
        return -1;
    /* The seams were installed by set_seams BEFORE init (init loads the model
     * through the load seam), so a full memset would wipe them: preserve them
     * and reset only the state. */
    {
        wy_wake_score_fn score = w->score;
        wy_wake_load_fn load = w->load;
        wy_wake_chime_fn chime = w->chime;
        wy_wake_wake_fn wake = w->wake;
        void *user = w->user;
        memset(w, 0, sizeof(*w));
        w->score = score;
        w->load = load;
        w->chime = chime;
        w->wake = wake;
        w->user = user;
    }
    w->threshold = threshold;
    w->cooldown_frames = (cooldown_ms + WY_WAKE_FRAME_MS - 1u) / WY_WAKE_FRAME_MS;
    strncpy(w->model_name, model_name, WY_WAKE_NAME_MAX - 1u);
    w->model_name[WY_WAKE_NAME_MAX - 1u] = '\0';

    /* A failed load is not an init failure: the core comes up armed but mute
     * (no model, no scorer calls, never fires). */
    if (w->load != NULL) {
        void *model = NULL;
        size_t len = 0;
        if (w->load(w->user, w->model_name, &model, &len) == 0 && model != NULL && len > 0) {
            w->model = model;
            w->model_len = len;
            w->has_model = 1;
        }
    }
    return 0;
}

void wy_wake_destroy(wy_wake *w)
{
    if (w == NULL)
        return;
    /* The core owns no model memory (the loader does); the flags reset only. */
    w->model = NULL;
    w->model_len = 0;
    w->has_model = 0;
}

int wy_wake_feed(wy_wake *w, const int16_t *mono, size_t n)
{
    if (w == NULL || mono == NULL || n == 0)
        return WY_WAKE_OK;
    if (w->in_cooldown) {
        /* The frame is consumed while the cooldown window is open. When this
         * frame closes the window (cooldown_left reaches 0) the core clears
         * the deaf state right here: the detector is armed again the moment
         * the window elapses, so the NEXT frame scores (and this one, still
         * inside the window, was consumed -- the answer's tail / done tone). */
        if (w->cooldown_left > 0)
            w->cooldown_left--;
        if (w->cooldown_left == 0)
            w->in_cooldown = 0;
        return WY_WAKE_OK;
    }
    if (w->streaming || w->playback)
        return WY_WAKE_OK;
    if (w->has_model && w->score != NULL) {
        float s = w->score(w->user, mono, n);
        /* A scorer that misbehaves (NaN, negative, over 1.0) must not fire the
         * device: only a sane reading in [0, 1] that crosses the threshold
         * does. NaN fails both range comparisons, so it can never cross. */
        if (s >= 0.0f && s <= 1.0f && s >= (float)w->threshold) {
            if (w->chime != NULL)
                w->chime(w->user); /* the chime always comes first (the
                                      reference order: chime, then wake) */
            /* A missing wake seam is the header's documented degradation ("no
             * wake"), which is exactly the REJECTED case: no wake ran, so no
             * stream opened and none will ever end -- the core must stay armed,
             * never self-deafen over a stream that does not exist. Treating a
             * NULL seam as accepted (0) wedged the core permanently deaf on its
             * first detection. */
            int wake_result = (w->wake != NULL) ? w->wake(w->user) : -1;
            /* A detection happened (return DETECTED either way). Self-deaf
             * only when the session ACCEPTED the wake: an accepted wake
             * opens the stream, and the device must not re-fire on its own
             * answer until that stream ends. A REJECTED fire (session
             * paused or not armed) opened no stream, so deafening over it
             * would leave the detector silent forever -- no stream will
             * ever end to wake it. Staying armed, the next crossing frame
             * retries the wake. */
            if (wake_result == 0)
                w->streaming = 1;
            return WY_WAKE_DETECTED;
        }
    }
    return WY_WAKE_OK;
}

void wy_wake_set_streaming(wy_wake *w, int streaming)
{
    if (w == NULL)
        return;
    w->streaming = streaming ? 1 : 0;
}

void wy_wake_set_playback_active(wy_wake *w, int active)
{
    if (w == NULL)
        return;
    w->playback = active ? 1 : 0;
}

void wy_wake_stream_ended(wy_wake *w)
{
    if (w == NULL)
        return;
    w->streaming = 0;
    /* A 0 ms cooldown is no cooldown: the core re-arms on the next frame. */
    w->in_cooldown = (w->cooldown_frames > 0) ? 1 : 0;
    w->cooldown_left = w->cooldown_frames;
}

bool wy_wake_deaf(const wy_wake *w)
{
    if (w == NULL)
        return true;
    return (bool)(w->streaming || w->playback || w->in_cooldown);
}

bool wy_wake_has_model(const wy_wake *w)
{
    if (w == NULL)
        return false;
    return w->has_model != 0;
}

/* D4 (increment 4): the "awake" chime as a rendered sample array. A short
 * two-tone tone -- E5 into A5, 80 ms each (2560 samples total at 16 kHz) --
 * each note with a 5 ms (80-sample) linear attack and an exponential decay
 * (tau 100 samples). Pure math, deterministic, no file dependency: the device
 * streams the samples to the I2S TX, and the host test pins the exact bytes. */
#define WY_CHIME_RATE      16000.0
#define WY_CHIME_NOTE_SMP  1280u   /* 80 ms per note */
#define WY_CHIME_ATTACK    80u     /* 5 ms linear attack */
#define WY_CHIME_TAU       100.0   /* exponential decay time constant (samples) */
#define WY_CHIME_PEAK      16384.0 /* half of int16 full scale: headroom, no clipping */

static double wy_chime_note_freq(size_t note)
{
    return (note == 0) ? 659.25 : 880.0;   /* E5 -> A5, a rising "awake" gesture */
}

static int16_t wy_chime_sample(size_t t, double freq)
{
    /* t: sample index within the note; freq: the note's frequency (Hz). */
    double amp;
    if (t < WY_CHIME_ATTACK)
        amp = (double)t / (double)WY_CHIME_ATTACK;   /* linear attack 0 -> 1 */
    else
        amp = exp(-(double)(t - WY_CHIME_ATTACK) / WY_CHIME_TAU);
    return (int16_t)llrint(amp * WY_CHIME_PEAK * sin(2.0 * 3.14159265358979323846 * freq *
                                                    (double)t / WY_CHIME_RATE));
}

size_t wy_chime_render(int16_t *out, size_t cap)
{
    size_t total = 2 * WY_CHIME_NOTE_SMP;
    size_t n = (cap < total) ? cap : total;
    if (out == NULL)
        return total;
    for (size_t note = 0; note < 2; note++) {
        double freq = wy_chime_note_freq(note);
        for (size_t t = 0; t < WY_CHIME_NOTE_SMP; t++) {
            if (note * WY_CHIME_NOTE_SMP + t >= n)
                break;
            out[note * WY_CHIME_NOTE_SMP + t] = wy_chime_sample(t, freq);
        }
    }
    return n;
}
