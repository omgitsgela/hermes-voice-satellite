#!/usr/bin/env python3
"""The device's persistent gateway link must come up with the project's short keepalive.

The satellite's half of the dead-peer rule is in gateway_socket.c: the long-lived link (both the
TLS and the plain path dial through gateway_socket_dial) must set SO_KEEPALIVE AND the per-socket
TCP_KEEPIDLE/INTVL/CNT to the same 15 s / 5 s / 3 probes the bridge side sets (bridge/net.py).
SO_KEEPALIVE alone would leave the link at lwIP's own default (first probe ~2 h after the last
segment), which is not the standard the project agrees on -- and it was a real defect: a first
fix set only SO_KEEPALIVE and a reviewer had to catch that the interval was 2 h, not 15 s.

This test compiles the REAL gateway_socket.c with the device-only ESP-IDF/FreeRTOS bits stubbed
out (the timer and the delay, which do not change the dial path) and with a stub lwip/sockets.h
carrying the SAME option numbers as lwIP (IPPROTO_TCP=6 matches the kernel, SO_KEEPALIVE matches
POSIX, and the TCP_KEEPIDLE/INTVL/CNT values are lwIP's). A small probe then dials a real
loopback TCP pair and reads the options back from the returned descriptor. Sockets and the dial
are real; only the timer and the delay are fakes. A missing or mistuned option fails the test.

    .venv/bin/python scripts/test_gateway_link_keepalive.py
"""
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "firmware/esp32/main/gateway_socket.c"

# The exact values the whole project agrees on (bridge/net.py: KEEPALIVE_IDLE_SECONDS=15,
# KEEPALIVE_INTERVAL_SECONDS=5, KEEPALIVE_PROBES=3).
IDLE = 15
INTVL = 5
PROBES = 3

C_HEADER = r"""
#ifndef GATEWAY_SOCKET_H
#define GATEWAY_SOCKET_H
#include <stddef.h>
typedef struct { int descriptor; long long deadline_us; } gateway_socket;
long long gateway_now_us(void);
long long gateway_millis_left(long long deadline_us);
int gateway_socket_dial(const char *host, unsigned port, long long deadline_us, gateway_socket *out);
#endif
"""

# A lwip/sockets.h shaped like the real one, but with the HOST kernel's option numbers: the
# probe's socket is a real host-kernel TCP socket, so the numbers the firmware code compiles
# against must be the ones that host kernel understands, or the values land on the wrong options.
# lwIP itself numbers TCP_KEEPIDLE/INTVL/CNT 3/4/5; the host kernel (where the test socket lives)
# numbers them 4/5/6. On the device (lwIP) the firmware's own numbering is self-consistent; this
# stub only exists so a host test can exercise the real dial code against a real socket.
# SO_KEEPALIVE and SOL_SOCKET come from <sys/socket.h> (the host's 9 and 1), so they are not
# redefined here.
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

STUBS_C = r"""
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
void vTaskDelay(TickType_t ticks) { (void)ticks; }
long long esp_timer_get_time(void) { return 0; }
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

int gateway_socket_dial(const char *host, unsigned port, long long deadline_us, gateway_socket *out);

int main(void) {
    int listener = socket(AF_INET, SOCK_STREAM, 0);
    if (listener < 0) { fprintf(stderr, "socket\n"); return 97; }
    struct sockaddr_in addr; memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = 0;
    if (bind(listener, (struct sockaddr *)&addr, sizeof(addr)) != 0) { fprintf(stderr, "bind\n"); return 97; }
    if (listen(listener, 1) != 0) { fprintf(stderr, "listen\n"); return 97; }
    socklen_t alen = sizeof(addr);
    if (getsockname(listener, (struct sockaddr *)&addr, &alen) != 0) { fprintf(stderr, "getsockname\n"); return 97; }
    unsigned port = ntohs(addr.sin_port);

    gateway_socket out;
    if (gateway_socket_dial("127.0.0.1", port, 5000000LL, &out) != 0) { fprintf(stderr, "dial failed\n"); return 98; }
    if (out.descriptor < 0) { fprintf(stderr, "no descriptor\n"); return 98; }
    int peer = accept(listener, NULL, NULL);
    if (peer < 0) { fprintf(stderr, "accept\n"); return 97; }

    int ka = 0; socklen_t len = sizeof(ka);
    getsockopt(out.descriptor, SOL_SOCKET, SO_KEEPALIVE, &ka, &len);
    int idle = 0, intvl = 0, cnt = 0; len = sizeof(idle);
    getsockopt(out.descriptor, IPPROTO_TCP, TCP_KEEPIDLE, &idle, &len);
    len = sizeof(intvl);
    getsockopt(out.descriptor, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, &len);
    len = sizeof(cnt);
    getsockopt(out.descriptor, IPPROTO_TCP, TCP_KEEPCNT, &cnt, &len);

    fprintf(stderr, "keepalive ka=%d idle=%d intvl=%d cnt=%d\n", ka, idle, intvl, cnt);
    if (ka != 1) { fprintf(stderr, "FAIL: SO_KEEPALIVE not set on the dialled link\n"); return 2; }
    if (idle != 15) { fprintf(stderr, "FAIL: TCP_KEEPIDLE=%d, expected 15 (lwIP's default ~7200000 means "
                   "the interval was never dialed)\n", idle); return 3; }
    if (intvl != 5) { fprintf(stderr, "FAIL: TCP_KEEPINTVL=%d, expected 5\n", intvl); return 4; }
    if (cnt != 3) { fprintf(stderr, "FAIL: TCP_KEEPCNT=%d, expected 3\n", cnt); return 5; }
    fprintf(stderr, "OK: the dialled persistent link carries the project's 15 s / 5 s / 3 keepalive\n");
    close(peer); close(out.descriptor); close(listener);
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
        # The stub directory first: its lwip/ and freertos/ headers stand in for the device's.
        cmd = ["cc", "-std=gnu11", "-D_DEFAULT_SOURCE", "-Wall", "-Wextra", "-Werror",
               f"-I{scratch}", f"-I{SRC.parent}",
               str(SRC), str(scratch / "stubs.c"), str(scratch / "probe.c"),
               "-o", str(scratch / "probe")]
        build = subprocess.run(cmd, capture_output=True, text=True)
        if build.returncode != 0:
            print("FAIL: the real gateway_socket.c would not compile against the host stubs")
            print(build.stderr[-2000:])
            return 1
        result = subprocess.run([str(scratch / "probe")], capture_output=True, text=True, timeout=15)
        for line in result.stderr.strip().splitlines():
            print("  " + line)
        if result.returncode == 0:
            print("OK: the dialled persistent link carries the project's 15 s / 5 s / 3 keepalive")
            return 0
        print(f"FAIL: the dialled persistent link is not keeping the agreed keepalive (probe exit {result.returncode})")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
