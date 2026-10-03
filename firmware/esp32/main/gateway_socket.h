#ifndef GATEWAY_SOCKET_H
#define GATEWAY_SOCKET_H
#include <stddef.h>
#include <stdint.h>

/* One socket implementation for both transports.
 *
 * The plaintext link and the TLS link differ in what travels over the connection, not in how the
 * connection is made or how long it may take. Both therefore dial through this file, and both give
 * a session one absolute deadline measured by our own clock rather than by whatever timeout the
 * stack left on the descriptor: a peer that accepts a connection and then stalls must not be able to
 * hold the console for ever.
 */
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
 * count, 0 on deadline, or -1 when the peer closed or the deadline passed. */
int gateway_socket_read(void *context, void *data, size_t size);
int gateway_socket_write(void *context, const void *data, size_t size);

void gateway_socket_close(gateway_socket *socket);

#endif
