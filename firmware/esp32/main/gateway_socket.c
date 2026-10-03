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

/* A persistent session (the gateway supervisor) has no deadline of its own: the bridge keeps an
 * idle link alive with TCP keepalive and sends no application ping, so a read that blocks for a
 * long time is the normal, healthy state, not a stall. These two block until data or a real end
 * of stream rather than a budget, and they say which end it was: 0 is the peer closing the stream
 * (EOF), -1 is an I/O error. The one-shot path above keeps its own deadline because a stalled
 * peer must not hold the console. */
int gateway_socket_read_persistent(void *context, void *data, size_t size) {
    gateway_socket *socket = context;
    for (;;) {
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
