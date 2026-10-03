#ifndef GATEWAY_TCP_H
#define GATEWAY_TCP_H
#include <stddef.h>

/* One bounded gateway session over TCP: dial the host and port, run the Wyoming control
 * handshake until the device is registered, then close. The link is not left standing, so a
 * caller does not inherit a socket it did not ask for.
 *
 * Renders one line per step into `out`, newline separated and unprefixed ("socket=...",
 * "session=..."), and returns 0 only when the gateway acknowledged registration. Never
 * writes past `capacity`. */
int gateway_tcp_run(const char *host, unsigned port, char *out, size_t capacity);

#endif
