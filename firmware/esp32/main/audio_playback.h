#ifndef AUDIO_PLAYBACK_H
#define AUDIO_PLAYBACK_H
#include <stdbool.h>
#include <stddef.h>

/* The speaker playback task: owns the I2S TX on the XVF3800 (the same controller and clock as
 * the capture RX, which the I2S driver pairs into full duplex), pumps the session's playback
 * ring to the I2S at realtime (the DMA is the pacer), and arms the amplifier -- an XMOS GPO,
 * X0D31, over the XVF's I2C GPO Servicer, NOT an ESP32 GPIO -- for the life of the answer it
 * plays. The session core (the gateway task) is the ring's single writer; this task is its
 * single reader. Started on registration, stopped on link-down.
 *
 * Device-only: nothing here is host-testable. It is proven by the firmware build and the
 * symbols in the ELF; that the speaker actually produces audio at a conservative level is the
 * live-test gate. */

/* Create the I2S TX channel (full-duplex with the capture RX when it is running) and start the
 * pump task. Idempotent. Returns 0 when running, -1 when the channel could not be created. The
 * ring is the session's, configured by wy_session_playback_configure. */
int audio_playback_start(void);

/* Stop the pump task and release the I2S TX channel. Idempotent: safe to call when nothing is
 * running, and safe to call from the link-down path. The amp is disarmed. */
void audio_playback_stop(void);

bool audio_playback_running(void);

/* Tell the pump task which session (ring) to read. Set once at startup, before the first start. */
void audio_playback_set_session(void *session);

/* Arm (enabled) or rest (disabled) the amplifier over the XVF's I2C GPO Servicer. A thin
 * transmit of wy_amp_gpo_command's byte-exact command (the amp pin is active-low). A failure
 * leaves the link alone: a silent speaker is not a link fault. */
void audio_playback_amp(int enabled);

#endif
