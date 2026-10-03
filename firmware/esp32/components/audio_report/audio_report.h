#ifndef AUDIO_REPORT_H
#define AUDIO_REPORT_H
#include <stddef.h>

/* Rendering for the audio-bring-up report, kept free of ESP-IDF types so it can be tested on a
 * host. The bring-up proves the audio hardware path is initialized and the codec answers. By
 * design it captures nothing and plays nothing, and the report says only what it did: which steps
 * ran, and whether they succeeded.

 * Step codes are small signed integers so the struct stays host-portable:
 *   AUDIO_I2S_OK  / AUDIO_I2S_FAIL
 *   AUDIO_I2C_OK  / AUDIO_I2C_NO_CODEC / AUDIO_I2C_BUS_ERROR
 */
#define AUDIO_I2S_OK        0
#define AUDIO_I2S_FAIL      (-1)
#define AUDIO_I2C_OK        0
#define AUDIO_I2C_NO_CODEC  (-1)
#define AUDIO_I2C_BUS_ERROR (-2)

typedef struct {
    int i2s;             /* AUDIO_I2S_OK or AUDIO_I2S_FAIL */
    int i2c;             /* AUDIO_I2C_OK, AUDIO_I2C_NO_CODEC or AUDIO_I2C_BUS_ERROR */
    int version_present; /* 1 when a version was read from the codec, 0 otherwise */
    unsigned char version[3];  /* major, minor, patch when version_present */
} audio_bringup_result;

/* Append `text` at `used`, never writing at or beyond `capacity` and always leaving the buffer
 * NUL-terminated, even when nothing fits. Returns the new running offset, clamped so the next
 * caller can keep going without rechecking bounds. Same contract as wifi_report_append: the
 * single place bounds are enforced, so it is tested directly as well as through the renderer. */
size_t audio_report_append(char *out, size_t capacity, size_t used, const char *text);

/* Render the bring-up as one self-describing line of field=value pairs, NUL-terminated and
 * bounded by capacity. Returns the number of bytes written excluding the terminator. The codec
 * version is reported only when it was actually read; a codec that answered without one is not
 * given a version to show. The report never claims capture or playback occurred. */
size_t audio_bringup_render(const audio_bringup_result *result, char *out, size_t capacity);

#endif
