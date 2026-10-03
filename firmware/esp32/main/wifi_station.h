#ifndef WIFI_STATION_H
#define WIFI_STATION_H
#include <stddef.h>

/* One bounded scan. Brings the station radio up, scans, and leaves the radio off again,
 * so nothing transmits unless a command asked for it. Returns 0 when a scan completed
 * (even if it found nothing) and renders the report into `out`; on failure it writes a
 * single explanatory line into `out` and returns -1. Never writes past `capacity`. */
int wifi_station_scan(char *out, size_t capacity);

/* One bounded join attempt with credentials supplied by the caller. Associates and then
 * releases: the link never outlives the command. Returns 0 when an address was obtained
 * and -1 otherwise, rendering either way into `out` as a single report line that never
 * contains the passphrase. Never writes past `capacity`. */
int wifi_station_connect(const char *ssid, const char *psk, char *out, size_t capacity);

/* Like wifi_station_connect, but leaves the link up on success so a caller can use it, and
 * stops the radio on failure. A caller that gets 0 must call wifi_station_release. */
int wifi_station_associate(const char *ssid, const char *psk, char *out, size_t capacity);

/* Disconnect and stop the radio. Safe to call when nothing is connected. */
void wifi_station_release(void);

#endif
