#ifndef WIFI_STATION_H
#define WIFI_STATION_H
#include <stddef.h>

/* Reported for a network that was not seen in the scan. A real RSSI is never this low, so a
 * caller can rank candidates without a second "found" array to keep in step. */
#define WIFI_STATION_NO_SIGNAL (-1000)

/* One size, shared by the scan report buffer and the console's reply budget. The renderer fills
 * its buffer up to capacity-1 bytes (including its own TRUNCATED line) when many networks are
 * visible, so the console must be willing to send a full buffer: any smaller cap would silently
 * drop a large-but-valid report, and the operator would read an empty scan where there were
 * networks. Sized for 32 full-escape lines (the driver's AP ceiling) plus the truncation note. */
#define WIFI_STATION_SCAN_REPORT_MAX 1400u

/* One bounded scan. Brings the station radio up, scans, and leaves the radio off again,
 * so nothing transmits unless a command asked for it. Returns 0 when a scan completed
 * (even if it found nothing) and renders the report into `out`; on failure it writes a
 * single explanatory line into `out` and returns -1. Never writes past `capacity`. */
int wifi_station_scan(char *out, size_t capacity);

/* One bounded scan that answers "which of these networks can I see, and how well", so a device
 * holding several networks can pick one instead of trying them in the dark. `rank[i]` receives
 * the strongest RSSI seen for `ssids[i]`, or WIFI_STATION_NO_SIGNAL when it was not seen (a
 * hidden network cannot be matched by name). Leaves the radio off, like the report scan. Returns
 * 0 when the scan completed and -1 on failure, writing a one-line reason into `out`. */
int wifi_station_scan_match(const char *const *ssids, size_t count, int *rank,
                            char *out, size_t capacity);

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
