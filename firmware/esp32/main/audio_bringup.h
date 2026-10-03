#ifndef AUDIO_BRINGUP_H
#define AUDIO_BRINGUP_H
#include "audio_report.h"

/* One bounded audio bring-up, in the shape of wifi_station_scan: bring the audio path up, prove
 * the codec is there, and leave nothing running. This is the item-6 line in the README's "still
 * pending" list -- the XMOS I2C/I2S adapters -- and it is deliberately the *smallest* thing that
 * proves the audio hardware is wired and alive:
 *
 *   1. the I2S interface is initialized for the XVF3800 (ESP32 master, stereo, 32-bit, 16 kHz,
 *      on the documented pins), and
 *   2. the XVF3800 answers over I2C and reports its firmware version,
 *
 * and then it deletes the I2S channel and tears the I2C bus down. No I2S stream is enabled, no
 * sample is read or written, and the amplifier enable line (X0D31 on the XMOS, not an ESP32 GPIO)
 * is never touched. Enabling a stream would be capture or playback, and this is neither; the
 * report core will not even print the words.
 *
 * Returns 0 when the bring-up ran to the end (even if the codec did not answer) and renders the
 * per-step report into `out`; on a setup failure before any step ran, it writes a single
 * explanatory line and returns -1. Nothing is left on the bus either way. Never writes past
 * `capacity`. */
int audio_bringup_run(char *out, size_t capacity);

#endif
