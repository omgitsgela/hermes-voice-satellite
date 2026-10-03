#ifndef WIFI_REPORT_H
#define WIFI_REPORT_H
#include <stddef.h>

/* Rendering for scan results, kept free of ESP-IDF types so it can be tested on a host.
 * An SSID is arbitrary bytes, while the console protocol is printable ASCII lines, so
 * every network name is escaped: bytes outside 0x21..0x7E become \xNN. That also keeps
 * spaces out of field values, so each line can be split on whitespace naively. */
#define WIFI_REPORT_SSID_MAX 32u
#define WIFI_REPORT_MAX_APS 32u

#define WIFI_REPORT_AUTH_OPEN 0u
#define WIFI_REPORT_AUTH_WEP 1u
#define WIFI_REPORT_AUTH_WPA 2u
#define WIFI_REPORT_AUTH_WPA2 3u
#define WIFI_REPORT_AUTH_WPA3 4u
#define WIFI_REPORT_AUTH_ENTERPRISE 5u

typedef struct {
    char ssid[WIFI_REPORT_SSID_MAX];  /* raw bytes, not necessarily NUL-terminated */
    unsigned char ssid_length;
    unsigned char channel;
    signed char rssi;
    unsigned char auth;
} wifi_report_ap;

/* Append `text` at `used`, never writing at or beyond `capacity` and always leaving the
 * buffer NUL-terminated. Returns the length actually stored, so a caller can tell when
 * it has run out of room. Exposed because it is the single place bounds are enforced,
 * and a one-byte mistake there is the difference between a truncated report and memory
 * corruption, so it is tested directly as well as through the renderer. */
size_t wifi_report_append(char *out, size_t capacity, size_t used, const char *text);

/* Sort strongest first (ties by name) and render one line per network. Always
 * NUL-terminates and never writes more than capacity bytes, including the terminator.
 * Returns the number of bytes written excluding the terminator. When networks do not
 * fit, the last line says how many were left out rather than silently dropping them. */
size_t wifi_report_render(wifi_report_ap *access_points, size_t count,
                          char *out, size_t capacity);

/* One join attempt. `reason` is the Wi-Fi disconnect reason code, which is what separates
 * "the passphrase is wrong" from "that network is not there"; 0 means no disconnect was
 * reported at all. `ip` is a dotted quad, and `channel`/`rssi` describe the access point
 * that was joined (-1 when unknown). No field ever carries key material. */
typedef struct {
    int connected;
    const char *ssid;
    const char *ip;
    int channel;
    int rssi;
    int reason;
} wifi_connect_outcome;

/* Name for the disconnect reasons worth telling apart, or NULL when this build does not
 * know the code. An unknown code is reported as a bare number: inventing a plausible
 * description for a reason we cannot interpret would be worse than the number itself. */
const char *wifi_connect_reason_name(int reason);

/* One line of space-separated field=value pairs, NUL-terminated, bounded by capacity.
 * Returns the number of bytes written excluding the terminator. */
size_t wifi_connect_report_render(const wifi_connect_outcome *outcome, char *out, size_t capacity);

#endif
