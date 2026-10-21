#ifndef WAKE_DETECTOR_H
#define WAKE_DETECTOR_H
#include <stddef.h>
#include <stdint.h>

/* The local wake detector's device glue (increment 4, D1-D5). It owns the portable wy_wake
 * core and wires its four seams to the real hardware/services:
 *
 *   score  -> the placeholder scorer (D2): deterministic, sub-threshold, so it never fires in
 *             normal operation. #110 drops the real scorer + model in here. A force-fire hook
 *             (wake_detector_force_fire) makes the next armed frame cross the threshold so the
 *             live-test gate can exercise the whole chime -> wake path without a trained model.
 *   load   -> the model-region reader (D3): read the `model` data partition, validate its
 *             header (wy_model) and SHA-256 (mbedTLS), hand the bytes to the core. With no
 *             usable real model it hands back a tiny safe placeholder so the deterministic
 *             scorer still runs -- never a garbage scorer.
 *   chime  -> the rendered local tone (D4): wy_chime_render + wy_session_local_play (the same
 *             speaker ring/amp the bridge answer uses).
 *   wake   -> wy_session_wake (the run-satellite flow the bridge already speaks).
 *
 * Single-consumer: the capture task drives the detector on every mic chunk via
 * wake_detector_feed, which ALSO syncs the core's deaf guard (streaming edge -> cooldown, and
 * local/answer playback -> deaf) from the live session state. So the core is only ever touched
 * from that one task; the gateway task does not call into it. This is why there is no separate
 * stream_ended/set_playback entry point -- they are folded into the feed's per-chunk sync.
 *
 * Device-only: nothing here is host-testable (it touches I2S, flash, NVS, mbedTLS). It is
 * proven by a clean esp32s3 build + the symbols/strings in the ELF; that the mic really hears
 * a wake word and the speaker really chimes is the live-test gate.
 */

/* One-time per link: install the seams and bring the core up with the config-plane wake
 * settings (wake_threshold, wake_word) read from the hermes_cfg NVS namespace, falling back to
 * the schema defaults (0.35 / okay_nabu) when absent. `session`, `user` and `write` describe
 * the live link -- the same triple the capture task sends mic audio over, so a wake fires
 * through the very transport the stream will use. Idempotent: a second call re-inits the core
 * (used after a link bounce to reset its state). */
void wake_detector_start(void *session, void *user,
                         int (*write)(void *, const void *, size_t));

/* Feed one downmixed mono16 chunk (16 kHz) to the detector. Called by the capture task on
 * every chunk, always. Before the wake the chunk goes only to the detector; after the wake it
 * goes to the detector and (while the session streams) on to the bridge. The call first syncs
 * the deaf guard from live session state, then scores the frame. */
void wake_detector_feed(const int16_t *mono, size_t n);

/* Tear the detector down (link down). Idempotent; frees the model buffer and resets state. */
void wake_detector_stop(void);

/* Live-test gate: force the next armed frame to fire (chime + wake) so the pipeline can be
 * exercised end to end without a trained model. One-shot: it arms exactly one fire, and stays
 * armed across deaf frames until the device is listening again. */
void wake_detector_force_fire(void);

/* A one-line status for the provisioning console: model/deaf/force state. */
void wake_detector_status(char *out, size_t cap);

#endif

/* Play the chime directly, bypassing the wake detector. Diagnostics: separates an audio-path
 * fault from a trigger-state-machine fault. Returns 0 when queued, -1 otherwise. */
int wake_detector_play_chime(void);
