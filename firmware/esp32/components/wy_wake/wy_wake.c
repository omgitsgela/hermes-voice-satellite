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
#include <math.h>
#include <string.h>

#define WY_WAKE_NAME_MAX 64u
#define WY_WAKE_FRAME_MS 10u /* 160 samples at 16 kHz: the capture chunk cadence */

/* Mean-square in full-scale units -> the fixed 0..100 comparison level
 * (wy_wake.h contract): dB = 20*log10(rms), mapped over [-60 dBFS, 0 dBFS].
 * -1.0 means "nothing measured": silence is a measurement, an unarmed or
 * model-less core that never scored a frame is not. Out-of-range clamps rather
 * than reporting nonsense: a level is an ORDERING signal for arbitration, and
 * a clamped high reading still orders correctly. */
static int level_from_mean_square(double mean_square)
{
    if (mean_square < 0.0)
        return -1;
    if (mean_square <= 0.0)
        return 0; /* exact silence reads the floor, not "no measurement" */
    double db = 10.0 * log10(mean_square); /* 20*log10(rms) == 10*log10(ms) */
    if (db <= -60.0)
        return 0;
    if (db >= 0.0)
        return 100;
    return (int)((db + 60.0) / 60.0 * 100.0 + 0.5);
}

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
    /* Cross-device wake arbitration (issue #32): the exponentially smoothed
     * mean-square of the ARMED frames actually scored, frozen with the crossing
     * score at fire. -1.0 mean-square = nothing measured yet. See wy_wake.h for
     * the scale contract and why only same-utterance comparisons are valid. */
    double mean_square;
    int fired_level;  /* -1 = no measurement; else 0..100 */
    double fired_score; /* <0 = never fired */
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
    /* Fresh measure per init/stream: -1 means "nothing scored yet", which is NOT
     * the same as the silence floor (the memset above would fake silence). */
    w->mean_square = -1.0;
    w->fired_level = -1;
    w->fired_score = -1.0;

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

int wy_wake_set_threshold(wy_wake *w, double threshold) {
    if (!w) return -1;
    /* NaN fails both comparisons, which is exactly the behaviour we want: an unusable value is
     * refused rather than silently making the detector deaf or hair-trigger. */
    if (!(threshold >= 0.0) || !(threshold <= 1.0)) return -1;
    w->threshold = threshold;
    return WY_WAKE_OK;
}

double wy_wake_threshold(const wy_wake *w) {
    return w ? w->threshold : 0.0;
}

void wy_wake_destroy(wy_wake *w) {
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
        /* The frame is ARMED and actually scored: it contributes to the room
         * loudness the detection will report (issue #32). Deaf frames returned
         * above never measure -- the level describes the room, not the device's
         * own speaker. Exponentially smoothed mean-square in full-scale units:
         * one multiply-accumulate per sample, fixed-point-free double math,
         * and alpha 0.1 gives a ~100 ms effective window at the 10 ms frame
         * cadence, which brackets a spoken wake phrase well. */
        double frame_mean_square = 0.0;
        for (size_t i = 0; i < n; i++)
        {
            double sample = (double)mono[i] / 32768.0;
            frame_mean_square += sample * sample;
        }
        frame_mean_square /= (double)n;
        if (w->mean_square < 0.0)
            w->mean_square = frame_mean_square; /* first armed frame seeds the window */
        else
            w->mean_square = 0.9 * w->mean_square + 0.1 * frame_mean_square;

        float s = w->score(w->user, mono, n);
        /* A scorer that misbehaves (NaN, negative, over 1.0) must not fire the
         * device: only a sane reading in [0, 1] that crosses the threshold
         * does. NaN fails both range comparisons, so it can never cross. */
        if (s >= 0.0f && s <= 1.0f && s >= (float)w->threshold) {
            /* Freeze the evidence THIS detection is made of BEFORE any seam
             * runs: the wake seam reads it to put it on the wire, and a seam
             * that re-enters the core (or a stream that starts mid-callback)
             * must not be able to alter what was already heard. */
            w->fired_level = level_from_mean_square(w->mean_square);
            w->fired_score = (double)s;
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

/* How many 10 ms feed frames a queue may go without shrinking before it counts
 * as STALLED rather than draining. A live speaker consumes the ring continuously,
 * so the gap between drains is small even for a full ring; a wedged pump never
 * advances it at all. */
#define WY_WAKE_STALL_FRAMES 50u   /* 500 ms with no movement */

uint32_t wy_wake_playback_stall_frames(void)
{
    return WY_WAKE_STALL_FRAMES;
}

/* Is the device's own output still audible to its microphone?
 *
 * `active` is the bridge's stream flag; `queued_bytes` is what the ring still
 * holds; `frames_since_queue_moved` is how many feed frames have passed since the
 * ring last SHRANK by any amount.
 *
 * The second clause used to read `queued_bytes > 0` on its own, and that is the
 * deadlock: a queue that is filled and never drained keeps the detector deaf for
 * ever, while the room hears nothing because nothing is playing. Silence and
 * permanent deafness from one predicate. Whether audio can be heard is a question
 * about DRAINING, not about the queue being non-empty. */
int wy_wake_playback_sounds(int active, size_t queued_bytes,
                            uint32_t frames_since_queue_moved)
{
    if (active)
        return 1;   /* the bridge says it is sending audio: authoritative */
    if (queued_bytes == 0)
        return 0;
    return frames_since_queue_moved < WY_WAKE_STALL_FRAMES;
}

void wy_wake_stream_ended(wy_wake *w)
{
    if (w == NULL)
        return;
    w->streaming = 0;
    /* A 0 ms cooldown is no cooldown: the core re-arms on the next frame. */
    w->in_cooldown = (w->cooldown_frames > 0) ? 1 : 0;
    w->cooldown_left = w->cooldown_frames;
    /* The next wake measures fresh room audio: the window that included the
     * utterance and whatever fired is done describing detections. */
    w->mean_square = -1.0;
}

int wy_wake_last_level(const wy_wake *w)
{
    if (w == NULL)
        return -1;
    return w->fired_level;
}

float wy_wake_last_score(const wy_wake *w)
{
    if (w == NULL)
        return -1.0f;
    return (float)w->fired_score;
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
#define WY_TONE_ATTACK_MS  15.0   /* raised-cosine attack: a struck note, not a beep */
#define WY_TONE_GAP_MS     18.0   /* between notes, so they read as two distinct strikes */
#define WY_TONE_AMPLITUDE  0.20   /* the reference set's -14 dBFS-ish level */

typedef struct { double freq; double ms; } wy_tone_note;

static size_t wy_tone_note_samples(double ms) {
    return (size_t)(WY_CHIME_RATE * ms / 1000.0);
}

static size_t wy_tone_gap_samples(void) {
    return wy_tone_note_samples(WY_TONE_GAP_MS);
}

/* Render one note into out[0..cap). Returns the sample count. */
static size_t wy_tone_note_render(int16_t *out, size_t cap, double freq, double ms) {
    size_t n = wy_tone_note_samples(ms);
    if (n > cap) n = cap;
    size_t attack = wy_tone_note_samples(WY_TONE_ATTACK_MS);
    if (attack > n) attack = n;
    /* Decay time constant is 0.35 of the note's own duration, so long notes ring longer. */
    double tau = 0.35 * ms / 1000.0;
    if (tau < 1e-6) tau = 1e-6;
    for (size_t i = 0; i < n; ++i) {
        double t = (double)i / WY_CHIME_RATE;
        double env = exp(-t / tau);
        if (attack && i < attack) {
            double x = (double)i / (double)attack;
            env *= 0.5 - 0.5 * cos(3.14159265358979323846 * x);
        }
        double sample = sin(2.0 * 3.14159265358979323846 * freq * t) * env * WY_TONE_AMPLITUDE;
        double scaled = sample * 32767.0;
        if (scaled > 32767.0) scaled = 32767.0;
        if (scaled < -32768.0) scaled = -32768.0;
        out[i] = (int16_t)llround(scaled);
    }
    return n;
}

static size_t wy_tones_render(int16_t *out, size_t cap,
                              const wy_tone_note *notes, size_t count) {
    if (!out) {
        /* NULL is a length query: notes plus the gaps between them. */
        size_t total = 0;
        for (size_t i = 0; i < count; ++i) total += wy_tone_note_samples(notes[i].ms);
        return total + (count > 1 ? (count - 1) * wy_tone_gap_samples() : 0);
    }
    size_t used = 0;
    for (size_t i = 0; i < count; ++i) {
        if (i) {
            size_t gap = wy_tone_gap_samples();
            if (used + gap > cap) gap = cap - used;
            for (size_t g = 0; g < gap; ++g) out[used + g] = 0;
            used += gap;
        }
        if (used >= cap) break;
        used += wy_tone_note_render(out + used, cap - used, notes[i].freq, notes[i].ms);
    }
    return used;
}

/* The wake chime: "I heard you". */
size_t wy_chime_render(int16_t *out, size_t cap)
{
    static const wy_tone_note notes[2] = { {392.0, 120.0}, {523.0, 170.0} };
    return wy_tones_render(out, cap, notes, 2);
}

/* The done tone: "that answer is finished". Same pair, descending, so the two sound of a
 * piece while still telling you which half of the exchange you are in. */
size_t wy_done_render(int16_t *out, size_t cap)
{
    static const wy_tone_note notes[2] = { {523.0, 120.0}, {392.0, 170.0} };
    return wy_tones_render(out, cap, notes, 2);
}
