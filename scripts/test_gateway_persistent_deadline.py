#!/usr/bin/env python3
"""The persistent gateway read must honor a finite deadline.

The supervisor's persistent link runs two regimes (gateway_socket.c): while a session is
registering, the socket's deadline is finite and a stalled peer must cut the read with an
error (which the supervisor turns into a backoff and a fresh dial); once registered, the
deadline is "no deadline" and the read blocks until a real end of stream, because an idle
registered link is the healthy state. The whole fix for a wedged bridge -- one that accepts
the connection and then goes silent while its kernel keeps ACKing, the one case keepalive
cannot catch -- lives in the first regime: gateway_socket_read_persistent must check the
deadline each poll.

This test compiles the REAL gateway_socket.c with the device-only bits stubbed (the timer is
the real monotonic clock here, the delay is a real nanosleep) and dials a real loopback pair.
The peer is accepted and then goes completely silent: no data, no FIN, no RST. A read with a
300 ms finite deadline must return -1 in well under a second. Deadline-blind code (the old
behaviour) blocks in recv until this harness kills it.

    .venv/bin/python scripts/test_gateway_persistent_deadline.py
"""
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "firmware/esp32/main/gateway_socket.c"

C_HEADER = r"""
#ifndef GATEWAY_SOCKET_H
#define GATEWAY_SOCKET_H
#include <stddef.h>
typedef struct { int descriptor; long long deadline_us; } gateway_socket;
long long gateway_now_us(void);
long long gateway_millis_left(long long deadline_us);
int gateway_socket_dial(const char *host, unsigned port, long long deadline_us, gateway_socket *out);
int gateway_socket_read_persistent(void *context, void *data, size_t size);
int gateway_socket_write_persistent(void *context, const void *data, size_t size);
void gateway_socket_close(gateway_socket *socket);
#endif
"""

# Host kernel's option numbers, like the keepalive test: the probe's socket is a real host
# kernel TCP socket, so the numbers the firmware code compiles against must be the host's.
LWIP_SOCKETS = r"""
#ifndef LWIP_SOCKETS_STUB_H
#define LWIP_SOCKETS_STUB_H
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/select.h>
#ifndef IPPROTO_TCP
#define IPPROTO_TCP 6
#endif
#define TCP_KEEPIDLE   4
#define TCP_KEEPINTVL  5
#define TCP_KEEPCNT    6
#endif
"""

LWIP_NETDB = r"""
#ifndef LWIP_NETDB_STUB_H
#define LWIP_NETDB_STUB_H
#include <netdb.h>
#endif
"""

FREERTOS = r"""
#ifndef FREERTOS_STUB_H
#define FREERTOS_STUB_H
typedef unsigned long TickType_t;
#define pdMS_TO_TICKS(ms) (ms)
#endif
"""

FREERTOS_TASK = r"""
#ifndef FREERTOS_TASK_STUB_H
#define FREERTOS_TASK_STUB_H
#include "FreeRTOS.h"
void vTaskDelay(TickType_t ticks);
#endif
"""

ESP_TIMER = r"""
#ifndef ESP_TIMER_STUB_H
#define ESP_TIMER_STUB_H
long long esp_timer_get_time(void);
#endif
"""

# Real monotonic clock, real delay: the deadline is measured the way the device measures it.
STUBS_C = r"""
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <time.h>
void vTaskDelay(TickType_t ticks) {
    struct timespec ts = {0, (long)ticks * 1000000L};
    nanosleep(&ts, NULL);
}
long long esp_timer_get_time(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000000LL + ts.tv_nsec / 1000;
}
"""

PROBE = r"""
#include "gateway_socket.h"
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include "lwip/sockets.h"

int main(void) {
    int listener = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in addr; memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = 0;
    if (listener < 0 || bind(listener, (struct sockaddr *)&addr, sizeof(addr)) != 0 ||
        listen(listener, 1) != 0 ||
        getsockname(listener, (struct sockaddr *)&addr, &(socklen_t){sizeof(addr)}) != 0)
        return 97;
    unsigned port = ntohs(addr.sin_port);

    gateway_socket out;
    if (gateway_socket_dial("127.0.0.1", port, gateway_now_us() + 5000000LL, &out) != 0)
        return 98;
    int peer = accept(listener, NULL, NULL);
    if (peer < 0) return 97;
    close(listener);
    /* The peer is accepted and then goes completely silent: no data, no FIN, no RST -- the
     * wedged-bridge shape. Its kernel keeps ACKing, so only a deadline can end the read. */

    char buf[16];
    long long start = gateway_now_us();
    /* Finite deadline, 300 ms: the registration regime. */
    out.deadline_us = gateway_now_us() + 300000LL;
    int result = gateway_socket_read_persistent(&out, buf, sizeof(buf));
    long long elapsed_ms = (gateway_now_us() - start) / 1000;
    fprintf(stderr, "persistent read with a 300 ms deadline: result=%d after %lld ms\n",
            result, elapsed_ms);
    if (result != -1) { fprintf(stderr, "FAIL: the read did not end with an error\n"); return 2; }
    if (elapsed_ms > 1500) { fprintf(stderr, "FAIL: the deadline was not honored\n"); return 3; }
    fprintf(stderr, "OK: a stalled peer is cut at the finite deadline\n");
    close(peer);
    gateway_socket_close(&out);
    return 0;
}
"""


def main() -> int:
    if not SRC.is_file():
        print("FAIL: firmware/esp32/main/gateway_socket.c missing")
        return 1
    with tempfile.TemporaryDirectory() as scratch:
        scratch = pathlib.Path(scratch)
        for name, text in (("lwip/sockets.h", LWIP_SOCKETS), ("lwip/netdb.h", LWIP_NETDB),
                           ("freertos/FreeRTOS.h", FREERTOS), ("freertos/task.h", FREERTOS_TASK),
                           ("esp_timer.h", ESP_TIMER)):
            target = scratch / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        (scratch / "stubs.c").write_text(STUBS_C)
        (scratch / "probe.c").write_text(PROBE)
        (scratch / "gateway_socket.h").write_text(C_HEADER)
        cmd = ["cc", "-std=gnu11", "-D_DEFAULT_SOURCE", "-Wall", "-Wextra", "-Werror",
               f"-I{scratch}", f"-I{SRC.parent}",
               str(SRC), str(scratch / "stubs.c"), str(scratch / "probe.c"),
               "-o", str(scratch / "probe")]
        build = subprocess.run(cmd, capture_output=True, text=True)
        if build.returncode != 0:
            print("FAIL: the real gateway_socket.c would not compile against the host stubs")
            print(build.stderr[-2000:])
            return 1
        result = subprocess.run([str(scratch / "probe")], capture_output=True, text=True, timeout=30)
        for line in result.stderr.strip().splitlines():
            print("  " + line)
        if result.returncode == 0:
            print("OK: the persistent read honors a finite deadline (registration regime)")
            return 0
        print(f"FAIL: the persistent read does not honor a finite deadline (probe exit {result.returncode})")
        return 1


if __name__ == "__main__":
    sys.exit(main())
