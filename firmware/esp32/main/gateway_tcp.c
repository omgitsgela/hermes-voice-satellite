#include "gateway_tcp.h"
#include "gateway_link.h"
#include "gateway_socket.h"
#include <stdio.h>

/* The plaintext transport, kept as a first-class local option rather than a legacy path: the bridge
 * serves plain TCP on its own listener for exactly this case, and a development link between a
 * laptop and a device on a trusted segment has nothing to gain from a certificate. It is never a
 * fallback for a tls:// endpoint - that decision belongs to the console, which refuses rather than
 * downgrading an endpoint the operator marked as TLS. */
#define GATEWAY_DIAL_TIMEOUT_MS 8000
/* The whole session shares one budget, so a peer that accepts a connection and then stalls cannot
 * hold the console for ever. */
#define GATEWAY_RUN_BUDGET_MS 25000

int gateway_tcp_run(const char *host, unsigned port, char *out, size_t capacity) {
    if (!out || capacity == 0) return -1;
    out[0] = 0;
    if (!host || !host[0]) {
        gateway_report_append(out, capacity, "socket=failed reason=no_host");
        return -1;
    }

    char line[160];
    gateway_socket socket = {.descriptor = -1};
    int64_t dial_deadline = gateway_now_us() + (int64_t)GATEWAY_DIAL_TIMEOUT_MS * 1000;
    if (gateway_socket_dial(host, port, dial_deadline, &socket) != 0) {
        snprintf(line, sizeof(line), "socket=failed host=%s port=%u transport=tcp", host, port);
        gateway_report_append(out, capacity, line);
        return -1;
    }
    snprintf(line, sizeof(line), "socket=connected host=%s port=%u transport=tcp", host, port);
    gateway_report_append(out, capacity, line);

    int result = gateway_link_register(gateway_socket_read, gateway_socket_write, &socket,
                                       gateway_now_us() + (int64_t)GATEWAY_RUN_BUDGET_MS * 1000,
                                       out, capacity);
    gateway_socket_close(&socket);
    return result;
}
