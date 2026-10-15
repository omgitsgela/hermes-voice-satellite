/* led_status -- the device glue for the LED ring.
 *
 * The policy lives in components/wy_led and is host-tested. This file does exactly two things:
 * turns the policy's control writes into I2C bytes on the XVF3800 bus, and drives wy_led_tick()
 * from a timer so the grace period and the flashes actually elapse.
 *
 * The ring is on the XMOS chip, so this is one more control write on the bus the audio already
 * uses -- no new pins, no new peripheral, and nothing on the USB cable. */
#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Bring up the LED path and show the startup look. Safe to call once, early. */
int led_status_start(void);

/* Lifecycle mirrors. Each is safe to call from any task. */
int led_status_boot(void);
int led_status_connected(int first_this_boot);
int led_status_disconnected(void);
int led_status_wake(void);
int led_status_listening(void);
int led_status_voice_started(void);
int led_status_transcript(void);
int led_status_thinking(void);
int led_status_speaking(void);
int led_status_followup(void);
int led_status_idle(void);
int led_status_error(void);

/* The fleet's `led_brightness` is 1-31; this scales it onto the ring. */
int led_status_set_brightness(unsigned level_1_31);

/* Read the direction the array is currently pointing at, in radians, for diagnostics and for the
 * wake path. Returns 0 on success. The auto-selected beam (index 3) is the one the ring follows. */
int led_status_read_doa(float *radians_out);

/* The presentation callback for the Wyoming session's phase announcements (WY_PHASE_*). Installed
 * once by the gateway task; the session core calls it from the link task, never from the realtime
 * playback pump. An unknown phase is ignored rather than treated as an error. */
void led_status_on_phase(void *user, int phase);

#ifdef __cplusplus
}
#endif
