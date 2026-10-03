#include "audio_report.h"
#include <string.h>
#include <stdio.h>

/* The same bounded append the Wi-Fi report uses: one place bounds are enforced, always
 * NUL-terminating, returning the clamped running offset. A report that runs out of room is
 * truncated, never corrupted. */
size_t audio_report_append(char *out, size_t capacity, size_t used, const char *text) {
    if (!out || capacity == 0) return 0;
    if (used >= capacity) used = capacity - 1;
    if (used + 1 >= capacity) {
        out[used] = 0;
        return used;
    }
    size_t room = capacity - 1 - used;
    size_t length = strlen(text);
    size_t take = length < room ? length : room;
    memcpy(out + used, text, take);
    out[used + take] = 0;
    return used + take;
}

size_t audio_bringup_render(const audio_bringup_result *result, char *out, size_t capacity) {
    if (!result || !out || capacity == 0) return 0;
    size_t used = 0;

    /* One line, one field per step, in the order the bring-up ran them. The host can split on
     * whitespace; no field ever carries a secret or a byte that is not printable. */
    used = audio_report_append(out, capacity, used, "i2s=");
    used = audio_report_append(out, capacity, used,
                               result->i2s == AUDIO_I2S_OK ? "initialized" : "init_failed");

    used = audio_report_append(out, capacity, used, " i2c=");
    switch (result->i2c) {
        case AUDIO_I2C_OK:
            used = audio_report_append(out, capacity, used, "codec_ok");
            break;
        case AUDIO_I2C_NO_CODEC:
            used = audio_report_append(out, capacity, used, "no_codec");
            break;
        default:
            used = audio_report_append(out, capacity, used, "bus_error");
            break;
    }

    /* The version is reported only when the codec actually gave one. A codec that answered but
     * returned no version is not invented one: the report says what was read, nothing more. */
    if (result->version_present) {
        char version[24];
        int length = snprintf(version, sizeof(version), " version=%u.%u.%u",
                              (unsigned)result->version[0], (unsigned)result->version[1],
                              (unsigned)result->version[2]);
        if (length > 0) used = audio_report_append(out, capacity, used, version);
    }

    /* The bring-up stops here by design. The report is deliberately silent about capture and
     * playback because they did not happen; saying they were "not armed" would be an extra word
     * that reads as a promise rather than a fact. */
    return used;
}
