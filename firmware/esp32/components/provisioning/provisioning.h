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
typedef int (*provision_wifi_save_ssid_fn)(void *, const char *);
typedef int (*provision_wifi_save_psk_fn)(void *, const char *);
typedef int (*provision_wifi_clear_fn)(void *);
typedef int (*provision_wifi_load_fn)(void *, char *, size_t, char *, size_t);
int provision_wifi_ssid_valid(const char *ssid);
int provision_wifi_psk_valid(const char *psk);
int provision_wifi_handle(const char *command, provision_wifi_save_ssid_fn save_ssid,
                          provision_wifi_save_psk_fn save_psk, provision_wifi_clear_fn clear,
                          provision_wifi_load_fn load, void *ctx,
                          char *response, size_t capacity);
#endif
