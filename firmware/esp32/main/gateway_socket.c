#include "gateway_socket.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/netdb.h"
#include "lwip/sockets.h"
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <unistd.h>

#define GATEWAY_POLL_MS 20

int64_t gateway_now_us(void) {
    return esp_timer_get_time();
}

int64_t gateway_millis_left(int64_t deadline_us) {
    int64_t left = deadline_us - esp_timer_get_time();
    return left > 0 ? left : 0;
}

/* Dead-peer keepalive, matching bridge/net.py (the whole project agrees on these values): probe
 * after 15 s idle, then three probes 5 s apart, and give up. The device's outbound link needs the
 * same timing as the bridge's accepted sockets, or the two ends discover a dead peer at very
 * different times. */
#define GATEWAY_KEEPALIVE_IDLE_S   15
#define GATEWAY_KEEPALIVE_INTERVAL_S 5
#define GATEWAY_KEEPALIVE_PROBES   3

/* Ask the stack to notice a peer that dies without a FIN or an RST. Without this, a bridge that
 * loses power, freezes, or is cut off from the AP leaves the device's persistent read blocked for
 * ever and the supervisor's reconnect never runs: this is the fix for that, not a tuning nicety.
 * Both ends set it (bridge/net.py does the same), so the dead peer is discovered by whichever
 * side probes first.
 *
 * SO_KEEPALIVE arms the probe timer, and the per-socket TCP_KEEPIDLE/INTVL/CNT dial it to the
 * project's 15 s / 5 s / 3 (the setters are compiled in: this build's lwipopts.h defines
 * LWIP_TCP_KEEPALIVE 1). All four calls are best-effort -- a setsockopt failure must not sink the
 * dial, because keepalive is a recovery aid, not a precondition for a working link. */
static void dial_keepalive(int descriptor) {
    int enabled = 1;
    int idle = GATEWAY_KEEPALIVE_IDLE_S;
    int interval = GATEWAY_KEEPALIVE_INTERVAL_S;
    int probes = GATEWAY_KEEPALIVE_PROBES;
    setsockopt(descriptor, SOL_SOCKET, SO_KEEPALIVE, &enabled, sizeof(enabled));
    setsockopt(descriptor, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof(idle));
    setsockopt(descriptor, IPPROTO_TCP, TCP_KEEPINTVL, &interval, sizeof(interval));
    setsockopt(descriptor, IPPROTO_TCP, TCP_KEEPCNT, &probes, sizeof(probes));
}


/* Non-blocking reads on a poll loop, so the deadline is this file's own rather than whatever the
 * stack happened to leave on the descriptor. */
int gateway_socket_read(void *context, void *data, size_t size) {
    gateway_socket *socket = context;
    for (;;) {
        if (gateway_millis_left(socket->deadline_us) == 0) return -1;
        ssize_t count = recv(socket->descriptor, data, size, 0);
        if (count > 0) return (int)count;
        if (count == 0) return -1; /* peer closed: the stream is over, not idle */
        if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) return -1;
        vTaskDelay(pdMS_TO_TICKS(GATEWAY_POLL_MS));
    }
}

int gateway_socket_write(void *context, const void *data, size_t size) {
    gateway_socket *socket = context;
    size_t sent = 0;
    while (sent < size) {
        if (gateway_millis_left(socket->deadline_us) == 0) return -1;
        ssize_t count = send(socket->descriptor, (const char *)data + sent, size - sent, 0);
        if (count > 0) {
            sent += (size_t)count;
            continue;
        }
        if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) {
            vTaskDelay(pdMS_TO_TICKS(GATEWAY_POLL_MS));
            continue;
        }
        return -1;
    }
    return (int)sent;
}

/* A persistent session (the gateway supervisor) has no deadline of its own: neither end sends an
 * application ping, and both ends set SO_KEEPALIVE (see gateway_socket_dial), so a read that blocks
 * for a long time is the normal, healthy state, not a stall. And a peer that dies without a FIN or
 * RST -- a power loss, a frozen VM, a path failure between the AP and the bridge -- still comes
 * back as a real end of stream, because the stack's keepalive probes stop being answered and it
 * tears the connection down with an RST, which is what unblocks this read. These two block until
 * data or a real end of stream rather than a budget, and they say which end it was: 0 is the peer
 * closing the stream (EOF), -1 is an I/O error (including the keepalive RST from a dead peer). The
 * one-shot path above keeps its own deadline because a stalled peer must not hold the console. */
int gateway_socket_read_persistent(void *context, void *data, size_t size) {
    gateway_socket *socket = context;
    for (;;) {
        /* Finite deadline (the registration regime): a stalled peer ends the read with an error,
         * which the supervisor turns into a backoff and a fresh dial. Under "no deadline" the
         * budget reads as years, so this never fires and the read blocks until data or EOF. */
        if (gateway_millis_left(socket->deadline_us) == 0) return -1;
        ssize_t count = recv(socket->descriptor, data, size, 0);
        if (count > 0) return (int)count;
        if (count == 0) return 0; /* peer closed: the stream is over, not idle */
        if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) return -1;
        vTaskDelay(pdMS_TO_TICKS(GATEWAY_POLL_MS));
    }
}

int gateway_socket_write_persistent(void *context, const void *data, size_t size) {
    gateway_socket *socket = context;
    size_t sent = 0;
    while (sent < size) {
        ssize_t count = send(socket->descriptor, (const char *)data + sent, size - sent, 0);
        if (count > 0) {
            sent += (size_t)count;
            continue;
        }
        if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) {
            vTaskDelay(pdMS_TO_TICKS(GATEWAY_POLL_MS));
            continue;
        }
        return -1;
    }
    return (int)sent;
}

int gateway_socket_dial(const char *host, unsigned port, int64_t deadline_us, gateway_socket *out) {
    if (!host || !host[0] || !out) return -1;
    out->descriptor = -1;
    out->deadline_us = deadline_us;

    char service[8];
    snprintf(service, sizeof(service), "%u", port);
    struct addrinfo hints = {0};
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    struct addrinfo *addresses = NULL;
    if (getaddrinfo(host, service, &hints, &addresses) != 0 || !addresses) return -1;

    int descriptor = socket(addresses->ai_family, addresses->ai_socktype, 0);
    if (descriptor < 0) {
        freeaddrinfo(addresses);
        return -1;
    }
    int flags = fcntl(descriptor, F_GETFL, 0);
    fcntl(descriptor, F_SETFL, flags | O_NONBLOCK);

    int connected = connect(descriptor, addresses->ai_addr, addresses->ai_addrlen);
    if (connected != 0 && errno != EINPROGRESS) {
        freeaddrinfo(addresses);
        close(descriptor);
        return -1;
    }
    freeaddrinfo(addresses);
    if (connected == 0) {
        dial_keepalive(descriptor);
        out->descriptor = descriptor;
        return 0;
    }

    while (gateway_millis_left(deadline_us) > 0) {
        fd_set writable;
        FD_ZERO(&writable);
        FD_SET(descriptor, &writable);
        struct timeval wait = {.tv_sec = 0, .tv_usec = GATEWAY_POLL_MS * 1000};
        int ready = select(descriptor + 1, NULL, &writable, NULL, &wait);
        if (ready > 0) {
            int error = 0;
            socklen_t length = sizeof(error);
            if (getsockopt(descriptor, SOL_SOCKET, SO_ERROR, &error, &length) == 0 && error == 0) {
                dial_keepalive(descriptor);
                out->descriptor = descriptor;
                return 0;
            }
            break;
        }
        if (ready < 0 && errno != EINTR) break;
    }
    close(descriptor);
    return -1;
}

void gateway_socket_close(gateway_socket *socket) {
    if (socket && socket->descriptor >= 0) {
        close(socket->descriptor);
        socket->descriptor = -1;
    }
}
