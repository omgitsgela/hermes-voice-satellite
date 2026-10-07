#ifndef WY_WAKE_H
#define WY_WAKE_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/*
 * The local wake detector core (increment 4). One question per frame of
 * microphone audio: does this frame's score cross the configured wake
 * threshold, and is the device in a state where a detection is allowed?
 *
 * The core is pure C with no ESP/RTOS/socket dependency: every input is an
 * injected seam, so this exact source compiles on the host with -Werror and is
 * tested directly (the wyoming_config pattern). The seams:
 *
 *   score  -- one mono16 frame (16 kHz) -> a score in [0, 1]. #110 supplies the
 *             real scorer; until then the device glue installs a placeholder
 *             that never crosses the threshold, and the host tests a stub.
 *   load   -- fetch the named model's bytes from the model partition. Called
 *             once, at init, with the configured model name. The LOADER owns
 *             the returned memory (a PSRAM buffer in the glue); the core stores
 *             the pointer for tracking and never frees it.
 *   chime  -- play the local "awake" tone (the device's own speaker).
 *   wake   -- fire the run-satellite sequence (the glue's wy_session_wake).
 *
 * Install the seams (wy_wake_set_seams) before wy_wake_init: init loads the
 * model through the load seam. A core with no usable model (no load seam, a
 * failed load, or a corrupt model) is armed but mute: it consumes every frame
 * and never fires, and does not even call the scorer. That is the pre-#110
 * device state and a corrupt-model fallback, not an error condition.
 *
 * The core's own logic (what increment 4 is, apart from the #110 scorer): the
 * threshold decision, the deaf guard (no detection while the mic is forwarded
 * to the bridge, while a local sound is playing, or during the post-stream
 * cooldown), the chime-then-wake ordering, and the single-shot state: a fired
 * detection makes the core deaf until the stream ends, so the device cannot
 * re-wake on its own answer (the reference's "deaf lead-in after speech").
 */

#define WY_WAKE_OK        0
#define WY_WAKE_DETECTED  1

typedef struct wy_wake wy_wake;

/* The seams. All take the core's user pointer (a NULL seam is allowed on every
 * one: the core degrades -- no model, no chime, no wake -- rather than faulting).
 * The wake seam's return value matters: 0 means the session accepted the wake
 * (it opened, or will open, the stream) and the core self-deafens until the
 * stream ends; a non-zero return means the wake was rejected (the session is
 * paused or not armed) and the core STAYS ARMED -- a rejected fire does not
 * start a stream, so the detector must not go deaf over one it never got. */
typedef float (*wy_wake_score_fn)(void *user, const int16_t *mono, size_t n);
typedef int   (*wy_wake_load_fn)(void *user, const char *model_name,
                                 void **out_model, size_t *out_len);
typedef int   (*wy_wake_chime_fn)(void *user);
typedef int   (*wy_wake_wake_fn)(void *user);

size_t wy_wake_size(void);

/* Install the seams before init. Pass NULL for any seam the caller does not
 * provide. `user` is passed back to every seam. */
void wy_wake_set_seams(wy_wake *w, wy_wake_score_fn score, wy_wake_load_fn load,
                       wy_wake_chime_fn chime, wy_wake_wake_fn wake, void *user);

/* Init the core from the config plane. `threshold` is wake_threshold (the
 * config bounds it to [0.05, 0.99]; the core accepts any value in [0, 1] and
 * rejects anything else, including NaN). `cooldown_ms` is the post-stream deaf
 * window (the bridge's transcript is followed by the device's own "done" tone
 * and the tail of an answer; 0 = none). `model_name` is the config wake_word:
 * the model file name in the model partition -- the model SELECTOR, not the
 * detection name (that is the session's WYAKE_WORD_NAME). A NULL name is a bad
 * argument. Returns 0 on success, -1 on a bad argument. A load failure is NOT
 * an init failure: the core comes up armed-but-mute (see the header). */
int wy_wake_init(wy_wake *w, double threshold, unsigned cooldown_ms, const char *model_name);

/* Reset the model flags. The core owns no model memory (the loader does), so
 * destroy frees nothing. */
void wy_wake_destroy(wy_wake *w);

/* One frame of 16 kHz mono16 microphone audio (typically 160 samples = 10 ms,
 * the capture task's chunk cadence). WY_WAKE_DETECTED when a detection fired
 * on this frame -- the chime and the wake have ALREADY been requested, in
 * that order. When the wake seam ACCEPTS the wake (returns 0) the core is now
 * deaf until the stream ends; when it REJECTS it (non-zero), the core stays
 * armed so the next crossing frame retries (see the seam contract above).
 * WY_WAKE_OK when the frame was consumed (below threshold, deaf, or
 * model-less). Null/empty frames are consumed silently. */
int wy_wake_feed(wy_wake *w, const int16_t *mono, size_t n);

/* The glue reports the stream state: 1 when the microphone is being forwarded
 * to the bridge (the core also sets it itself on a fire, so a fire is
 * single-shot even if the glue is slow), 0 otherwise. */
void wy_wake_set_streaming(wy_wake *w, int streaming);

/* The glue reports local playback (the chime, an announcement): 1 while a
 * local sound is playing. A high score during local playback is a
 * self-trigger, not a person, and is consumed. The core does not manage this
 * flag itself -- the glue owns it, because only the glue knows how long a
 * rendered sound lasts. */
void wy_wake_set_playback_active(wy_wake *w, int active);

/* The stream stopped (the bridge's transcript or a pause). The core leaves
 * the deaf state and starts the post-stream cooldown, during which high
 * frames are still consumed (the answer's tail and the done tone can land
 * right here). */
void wy_wake_stream_ended(wy_wake *w);

/* Is the core deaf right now (streaming, playing, or in the post-stream
 * cooldown)? The glue uses this to keep a new chime from clashing with a
 * stream and to report the detector state on the provisioning console. */
bool wy_wake_deaf(const wy_wake *w);

/* Is a usable model loaded? False with no load seam, on a failed load, or on
 * first boot with an empty model partition. A model-less core never fires and
 * never calls the scorer. */
bool wy_wake_has_model(const wy_wake *w);

/* The "awake" chime as a sample array -- pure math, no file dependency. A short
 * two-tone tone (E5 into A5), each note with a 5 ms linear attack and an
 * exponential decay, at 16 kHz / 16-bit, so it is host-testable and the device
 * never needs a tone file. `out == NULL` returns the number of samples the chime
 * needs; otherwise it renders min(cap, needed) samples into `out` and returns the
 * number written. The device glue streams the samples to the I2S TX. */
size_t wy_chime_render(int16_t *out, size_t cap);
#endif
