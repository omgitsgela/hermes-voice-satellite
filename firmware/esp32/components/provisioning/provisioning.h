#ifndef PROVISIONING_H
#define PROVISIONING_H
#include <stddef.h>
#define PROVISION_URI_MAX 192u
/* Host length a parser caller must be able to hold: the staging validator allows a name of
 * up to 127 bytes, so a buffer of this size always fits one. */
#define PROVISION_HOST_MAX 128u
#define PROVISION_TRANSPORT_TCP 0
#define PROVISION_TRANSPORT_TLS 1

/* Parse a staged endpoint into its parts. `host` receives the name exactly as staged, so a
 * resolver sees what the user wrote. Any output pointer may be NULL to validate only.
 * Returns 0 on success and -1 when the value is not a valid endpoint, or when the host does
 * not fit the supplied buffer: it refuses rather than truncating, because a shortened host
 * is a different destination. */
int provision_endpoint_parse(const char *value, char *host, size_t host_capacity,
                             unsigned *port, int *transport);
/* LF-only, printable ASCII. Rejected/expired lines drain through the next LF. */
typedef struct { char data[256]; size_t used; int rejected; } provision_line;
int provision_line_feed(provision_line *line, unsigned char byte);
void provision_line_expire(provision_line *line);
typedef int (*provision_save_fn)(void *, const char *);
typedef int (*provision_load_fn)(void *, char *, size_t);
int provision_handle(const char *command, provision_save_fn save, provision_load_fn load,
                     void *ctx, char *response, size_t capacity);

/* Wi-Fi credentials are staged as two independent fields, so a half-written
 * configuration is visible as half-written instead of looking ready. The SSID may
 * contain spaces, which is why it is not one command with two arguments. */
#define PROVISION_SSID_MAX 32u
#define PROVISION_PSK_MAX 64u

/* How many networks a device may hold. Bounded here rather than in the store, so the limit is
 * one rule wherever the list is kept, and so "the list is full" stays distinguishable from
 * "the store failed". Four is enough for a device that moves between home, a travel router
 * and a phone hotspot. */
#define PROVISION_WIFI_PROFILE_MAX 4u

typedef int (*provision_wifi_save_ssid_fn)(void *, const char *);
typedef int (*provision_wifi_save_psk_fn)(void *, const char *);
typedef int (*provision_wifi_clear_fn)(void *);
typedef int (*provision_wifi_load_fn)(void *, char *, size_t, char *, size_t);

/* The saved-network list: the networks a device may join beyond whatever is staged, in the
 * order it holds them. Each call returns 0 on success and -1 on failure; `get` fills both
 * buffers, NUL-terminating each. A store never decides how many networks fit: the core does,
 * and a store asked to keep more than that says so by returning failure. */
typedef int (*provision_wifi_profile_save_fn)(void *, const char *ssid, const char *psk);
typedef int (*provision_wifi_profile_remove_fn)(void *, const char *ssid);
typedef int (*provision_wifi_profile_clear_fn)(void *);
typedef int (*provision_wifi_profile_count_fn)(void *);
typedef int (*provision_wifi_profile_get_fn)(void *, int index, char *ssid, size_t ssid_capacity,
                                            char *psk, size_t psk_capacity);
typedef struct {
    provision_wifi_profile_save_fn save;
    provision_wifi_profile_remove_fn remove;
    provision_wifi_profile_clear_fn clear;
    provision_wifi_profile_count_fn count;
    provision_wifi_profile_get_fn get;
} provision_wifi_profiles;

int provision_wifi_ssid_valid(const char *ssid);
int provision_wifi_psk_valid(const char *psk);
/* `profiles` may be NULL on a device with no saved-network list, in which case the profile
 * commands answer ERR WIFI PROFILE UNSUPPORTED rather than guessing. */
int provision_wifi_handle(const char *command, provision_wifi_save_ssid_fn save_ssid,
                          provision_wifi_save_psk_fn save_psk, provision_wifi_clear_fn clear,
                          provision_wifi_load_fn load, const provision_wifi_profiles *profiles,
                          void *ctx, char *response, size_t capacity);

/* What a join should try, in order: the saved networks as the store holds them, then the
 * staged pair when it is not already one of them, so the single-network flow keeps working
 * unchanged. An entry that could not be joined is skipped rather than offered as a candidate
 * that would fail at association. `ssids` and `psks` are arrays of rows of PROVISION_SSID_MAX+1
 * and PROVISION_PSK_MAX+1 bytes. `saved_split` (when non-NULL) receives how many of the
 * candidates are saved networks, so a caller that labels the chosen one "saved" vs "staged"
 * uses the true boundary rather than the raw length of the saved list (which would count a
 * skipped, now-invalid profile). Returns 0 and writes the count, or -1 when the store could not
 * be read. */
int provision_wifi_candidates(const provision_wifi_profiles *profiles, provision_wifi_load_fn load,
                              void *ctx, char (*ssids)[PROVISION_SSID_MAX + 1],
                              char (*psks)[PROVISION_PSK_MAX + 1], size_t capacity, size_t *count,
                              size_t *saved_split);

/* Whether a freshly booted satellite should register with its gateway now, or stay idle.
 *
 * The device has two independent knobs -- a saved gateway endpoint and a list of networks it may
 * join -- and it should dial only when both are present. This is the whole policy, kept pure so
 * the host can pin down every "idle" case without a board: the device's boot path reads its config,
 * calls this, and either starts the gateway or says why it is idle.
 *
 * `endpoint` is the staged gateway endpoint ("" or NULL when unset). `wifi_candidates` is the
 * number of joinable networks the candidate list reports -- the value `provision_wifi_candidates`
 * writes to its count out, which is negative when the store could not be read at all. The decision
 * is about what is *configured*, not what is in the air: a stored network whose access point is
 * currently off still counts as "configured", because dialing it is what discovers that, and it is
 * the caller that retries the join.
 *
 * The idle reasons follow the console's existing order -- what is wrong with the Wi-Fi is named
 * before what is wrong with the endpoint -- so a boot report and a manual command tell the same
 * story. `reason` receives a short, stable token ("" for GO); it is best-effort and may be empty
 * when the buffer is too small, but the return code is the decision and never depends on it.
 */
enum { BOOT_GO = 0, BOOT_IDLE = 1 };
int provision_boot_decision(const char *endpoint, int wifi_candidates, char *reason, size_t reason_cap);
#endif
