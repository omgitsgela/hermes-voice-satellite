#ifndef AUDIO_PLAYBACK_H
#define AUDIO_PLAYBACK_H

#include <stdint.h>
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
/* Tell the pump where to report `played`. Refreshed per link, like the capture path. */
void audio_playback_bind(void *session, void *user,
                         int (*write)(void *, const void *, size_t));

/* Stop the pump task and release the I2S TX channel. Idempotent: safe to call when nothing is
 * running, and safe to call from the link-down path. The amp is disarmed. */
void audio_playback_stop(void);

bool audio_playback_running(void);

/* Tell the pump task which session (ring) to read. Set once at startup, before the first start. */
void audio_playback_set_session(void *session);

/* Read back the live session pointer. */
void *audio_playback_session(void);

/* Arm (enabled) or rest (disabled) the amplifier over the XVF's I2C GPO Servicer. A thin
 * transmit of wy_amp_gpo_command's byte-exact command (the amp pin is active-low). A failure
 * leaves the link alone: a silent speaker is not a link fault. */
int audio_playback_amp(int enabled);

#endif

/* Set the XVF3800 output mux (which signal the DAC plays). Returns 0 on success. */
int audio_xvf_route_output(int right, int category, int source);

/* Power up and unmute the AIC3104 codec driving the 3.5 mm jack. */
int audio_dac_init(void);

/* Read an AIC3104 register back (verification). */
int audio_dac_read(uint8_t reg, uint8_t *value);

/* Write an arbitrary XVF3800 control register. */
int audio_xvf_write(uint8_t resid, uint8_t cmd, const uint8_t *params, size_t nparams);

/* Write one AIC3104 register. */
int audio_dac_write(uint8_t reg, uint8_t value);

/* Freeze the amplifier state so the pump cannot release it (diagnostics). */
void audio_playback_amp_lock(int locked);

/* Queue mono16 audio for playback now. Returns 0 when queued. */
int audio_playback_play_mono(const int16_t *mono, size_t n);

/* Tri-state (1) or drive (0) the I2S data-out line. Diagnostics only. */
void audio_playback_i2s_float(int enable);
