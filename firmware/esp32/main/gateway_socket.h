#ifndef GATEWAY_SOCKET_H
#define GATEWAY_SOCKET_H
#include <stddef.h>
#include <stdint.h>

/* One socket implementation for both transports.
 *
 * The plaintext link and the TLS link differ in what travels over the connection, not in how the
 * connection is made or how long it may take. Both therefore dial through this file, and both give
 * a session one absolute deadline measured by our own clock rather than by whatever timeout the
 * stack left on the descriptor: a peer that accepts a connection and then stalls must not be able
 * to hold the console for ever. A link with no deadline of its own (the persistent session) sets
 * deadline_us to this value, so gateway_millis_left reports a number of years rather than zero.
 */
#include <stdint.h>
#define GATEWAY_SOCKET_NO_DEADLINE INT64_MAX
typedef struct {
    int descriptor;
    int64_t deadline_us; /* absolute, from esp_timer_get_time() */
} gateway_socket;

int64_t gateway_now_us(void);

/* Milliseconds of budget left, never negative. */
int64_t gateway_millis_left(int64_t deadline_us);

/* Non-blocking connect driven to completion inside the deadline. Returns 0 and fills `out`, or -1
 * with no descriptor left open. */
int gateway_socket_dial(const char *host, unsigned port, int64_t deadline_us, gateway_socket *out);

/* Deadline-bounded reads and writes, shaped for the Wyoming session callbacks. A read returns the
 * number of bytes read, or -1 when the deadline passed, the peer closed, or an I/O error occurred;
 * it never returns 0. A closed peer is an end of stream, which this bounded path reports as -1,
 * leaving the 0-for-EOF contract to the persistent pair below. A write returns the count written,
 * or -1 on the same conditions. */
int gateway_socket_read(void *context, void *data, size_t size);
int gateway_socket_write(void *context, const void *data, size_t size);

/* Persistent-link reads and writes: block until data or a real end of stream, because an idle
 * registered link is healthy (the bridge sends no application ping). A read returns >0 bytes, 0
 * for a closed stream, or -1 on an I/O error. Used only by the gateway supervisor's persistent
 * link; the one-shot console path above keeps its bounded deadlines.
 *
 * "No deadline of their own" means the link the supervisor sets, not an absence of the field:
 * while the socket's deadline is finite (the registration regime, where a stalled peer must not
 * hold the device), a read that runs out of budget returns -1 like any other bounded read and the
 * supervisor backs off and re-dials; while it is "no deadline", the read blocks until a real end
 * of stream. The write is deadline-blind in both regimes: an idle link is the healthy state and a
 * stuck send will surface as an error or a keepalive RST on the next read. */
int gateway_socket_read_persistent(void *context, void *data, size_t size);
int gateway_socket_write_persistent(void *context, const void *data, size_t size);

void gateway_socket_close(gateway_socket *socket);

#endif
