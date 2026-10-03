#include "wifi_report.h"
#include <stdio.h>
#include <string.h>

#define WIFI_REPORT_LINE_MAX 160u

static const char *auth_name(unsigned char auth) {
    switch (auth) {
        case WIFI_REPORT_AUTH_OPEN: return "OPEN";
        case WIFI_REPORT_AUTH_WEP: return "WEP";
        case WIFI_REPORT_AUTH_WPA: return "WPA";
        case WIFI_REPORT_AUTH_WPA2: return "WPA2";
        case WIFI_REPORT_AUTH_WPA3: return "WPA3";
        case WIFI_REPORT_AUTH_ENTERPRISE: return "ENT";
        default: return "UNKNOWN";
    }
}

/* Append text at `used`, never writing past capacity - 1 and always NUL-terminating.
 * Returns the new length, so the caller can see when it has run out of room. */
size_t wifi_report_append(char *out, size_t capacity, size_t used, const char *text) {
    if (!out || capacity == 0) return 0;
    if (used >= capacity) used = capacity - 1;   /* never write at or past the end */
    if (used + 1 >= capacity) {
        out[used] = 0;                            /* guaranteed terminated even on a no-op */
        return used;
    }
    size_t room = capacity - 1 - used;
    size_t length = strlen(text);
    size_t take = length < room ? length : room;
    memcpy(out + used, text, take);
    out[used + take] = 0;
    return used + take;
}

static size_t append_char(char *out, size_t capacity, size_t used, char value) {
    if (!out || capacity == 0) return 0;
    if (used >= capacity) used = capacity - 1;
    if (used + 1 >= capacity) {
        out[used] = 0;
        return used;
    }
    out[used] = value;
    out[used + 1] = 0;
    return used + 1;
}

/* Escape every byte outside 0x21..0x7E as \xNN, and never emit a partial escape token:
 * a truncated "\x2" would look like part of the name and break naive field parsing. */
static size_t append_escaped(char *out, size_t capacity, size_t used,
                             const char *bytes, size_t length) {
    static const char *hex = "0123456789abcdef";
    for (size_t i = 0; i < length; ++i) {
        unsigned char byte = (unsigned char)bytes[i];
        if (byte > 0x20 && byte < 0x7f) {
            used = append_char(out, capacity, used, (char)byte);
        } else {
            if (used + 5 > capacity) break;   /* room for \xNN plus the terminator, or nothing */
            char escaped[5] = {'\\', 'x', hex[(byte >> 4) & 0xf], hex[byte & 0xf], 0};
            used = wifi_report_append(out, capacity, used, escaped);
        }
    }
    return used;
}

/* Write the escaped network name into dest. An empty name means the network is hidden:
 * it is still listed, because a hidden network that is present is exactly what a scan
 * needs to reveal, and `(hidden)` is unambiguous. */
static void render_name(char *dest, size_t capacity, const wifi_report_ap *ap) {
    if (capacity == 0) return;
    dest[0] = 0;
    if (ap->ssid_length == 0) {
        wifi_report_append(dest, capacity, 0, "(hidden)");
        return;
    }
    size_t length = ap->ssid_length < WIFI_REPORT_SSID_MAX ? ap->ssid_length : WIFI_REPORT_SSID_MAX;
    append_escaped(dest, capacity, 0, ap->ssid, length);
}

/* Strongest first, then by name so equal-strength networks never reorder between runs. */
static int precedes(const wifi_report_ap *left, const wifi_report_ap *right) {
    int left_rssi = (int)(signed char)left->rssi;
    int right_rssi = (int)(signed char)right->rssi;
    if (left_rssi != right_rssi) return left_rssi > right_rssi;
    unsigned shared = left->ssid_length < right->ssid_length ? left->ssid_length : right->ssid_length;
    int order = memcmp(left->ssid, right->ssid, shared);
    if (order != 0) return order < 0;
    return left->ssid_length < right->ssid_length;
}

size_t wifi_report_render(wifi_report_ap *access_points, size_t count,
                          char *out, size_t capacity) {
    if (!out || capacity == 0) return 0;
    out[0] = 0;
    if (!access_points) count = 0;

    for (size_t i = 1; i < count; ++i) {
        wifi_report_ap key = access_points[i];
        size_t j = i;
        while (j > 0 && precedes(&key, &access_points[j - 1])) {
            access_points[j] = access_points[j - 1];
            --j;
        }
        access_points[j] = key;
    }

    size_t used = 0;
    size_t rendered = 0;
    for (size_t i = 0; i < count; ++i) {
        char name[WIFI_REPORT_SSID_MAX * 4 + 1];
        char line[WIFI_REPORT_LINE_MAX];
        render_name(name, sizeof(name), &access_points[i]);
        int written = snprintf(line, sizeof(line), "%s ch=%u rssi=%d auth=%s", name,
                               (unsigned)access_points[i].channel,
                               (int)(signed char)access_points[i].rssi,
                               auth_name(access_points[i].auth));
        if (written < 0 || (size_t)written >= sizeof(line)) {
            rendered = i + 1;  /* unrenderable, but it must not be reported as omitted */
            continue;
        }
        /* Keep room for the terminator and, if needed, for the truncation line. */
        if (used + (size_t)written + 24 > capacity) break;
        used = wifi_report_append(out, capacity, used, line);
        used = append_char(out, capacity, used, '\n');
        rendered = i + 1;
    }
    if (rendered < count) {
        char suffix[48];
        snprintf(suffix, sizeof(suffix), "TRUNCATED %u\n", (unsigned)(count - rendered));
        wifi_report_append(out, capacity, used, suffix);
    } else if (count == 0) {
        wifi_report_append(out, capacity, used, "NO NETWORKS\n");
    }
    return strlen(out);
}

const char *wifi_connect_reason_name(int reason) {
    switch (reason) {
        case 15: return "4WAY_HANDSHAKE_TIMEOUT";  /* usually a wrong passphrase */
        case 16: return "GROUP_KEY_UPDATE_TIMEOUT";
        case 23: return "802_1X_AUTH_FAILED";
        case 24: return "CIPHER_SUITE_REJECTED";
        case 200: return "BEACON_TIMEOUT";
        case 201: return "NO_AP_FOUND";            /* wrong name, or out of range */
        case 202: return "AUTH_FAIL";
        case 203: return "ASSOC_FAIL";
        case 204: return "HANDSHAKE_TIMEOUT";
        case 205: return "CONNECTION_FAIL";
        default: return NULL;
    }
}

size_t wifi_connect_report_render(const wifi_connect_outcome *outcome, char *out, size_t capacity) {
    if (!out || capacity == 0) return 0;
    out[0] = 0;
    if (!outcome) return wifi_report_append(out, capacity, 0, "status=failed reason=none");

    size_t used = 0;
    if (outcome->connected) {
        char tail[48];
        used = wifi_report_append(out, capacity, used, "status=connected ssid=");
        if (outcome->ssid) {
            used = append_escaped(out, capacity, used, outcome->ssid, strlen(outcome->ssid));
        } else {
            used = wifi_report_append(out, capacity, used, "(none)");
        }
        used = wifi_report_append(out, capacity, used, " ip=");
        used = wifi_report_append(out, capacity, used, outcome->ip ? outcome->ip : "(none)");
        snprintf(tail, sizeof(tail), " channel=%d rssi=%d", outcome->channel, outcome->rssi);
        used = wifi_report_append(out, capacity, used, tail);
        return strlen(out);
    }

    used = wifi_report_append(out, capacity, used, "status=failed reason=");
    if (outcome->reason == 0) {
        wifi_report_append(out, capacity, used, "none");
        return strlen(out);
    }
    const char *name = wifi_connect_reason_name(outcome->reason);
    char code[32];
    if (name) snprintf(code, sizeof(code), "%d:%s", outcome->reason, name);
    else snprintf(code, sizeof(code), "%d", outcome->reason);
    wifi_report_append(out, capacity, used, code);
    return strlen(out);
}
