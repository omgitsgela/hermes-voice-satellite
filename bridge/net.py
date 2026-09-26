"""Socket-level liveness for the long-lived voice links.

An idle TCP connection has no timeout of its own, and neither end of the voice transport says
anything while it waits for the other. A peer that dies without closing — a power cut, a lost route,
a dropped tunnel — therefore leaves a socket that still reads ESTAB, and the surviving end goes on
believing it is connected to something that is gone.

The protocol cannot settle it: the satellite does not answer application-level pings (measured
2026-09-22; a Ping draws no Pong). Kernel keepalive is the only cheap liveness signal.

Both directions need it, which is why it lives here rather than in either client:

* the device's outbound socket, so a rebooted bridge does not leave the device dialling a corpse;
* the bridge's dialled client, so a dead satellite surfaces instead of hanging;
* the bridge's **accepted** registry sockets, so a satellite that vanishes without closing is noticed
  rather than held as a registration and a lease forever.

Measured 2026-09-26: the Pi was power-cycled and the bridge did not notice at all. The accepted
socket stayed ESTAB for the whole outage and was cleaned up only because the returning device
displaced it.

The values are deliberately shared rather than repeated at each call site: three copies of "15, 5,
3" is three chances for them to drift apart, and a link whose two ends disagree about liveness is
worse than one that never checks.
"""

from __future__ import annotations

import logging
import socket
from typing import Optional

_LOGGER = logging.getLogger("bridge.net")

# 15 s idle, then three probes 5 s apart: roughly 30 s to notice a dead peer.
KEEPALIVE_IDLE_SECONDS = 15
KEEPALIVE_INTERVAL_SECONDS = 5
KEEPALIVE_PROBES = 3


def enable_tcp_keepalive(
    sock: Optional[socket.socket],
    *,
    label: str = "peer",
    logger: Optional[logging.Logger] = None,
) -> bool:
    """Turn on kernel keepalive for *sock*; return True when it was applied.

    Never fatal by design. A socket option must not be the reason a voice link fails to come up, so a
    missing socket or an unsupported option is a warning and a False, not an exception. Options the
    platform does not define are skipped rather than guessed at.
    """
    log = logger or _LOGGER
    if sock is None:
        log.warning("no socket available, so keepalive is off for %s", label)
        return False

    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (
            ("TCP_KEEPIDLE", KEEPALIVE_IDLE_SECONDS),
            ("TCP_KEEPINTVL", KEEPALIVE_INTERVAL_SECONDS),
            ("TCP_KEEPCNT", KEEPALIVE_PROBES),
        ):
            option = getattr(socket, name, None)
            if option is not None:
                sock.setsockopt(socket.IPPROTO_TCP, option, value)
    except OSError as exc:
        log.warning("could not enable tcp keepalive for %s: %s", label, exc)
        return False

    log.info(
        "tcp keepalive on for %s (%ds idle, %d probes %ds apart): a dead peer now surfaces as a "
        "closed socket instead of silence",
        label, KEEPALIVE_IDLE_SECONDS, KEEPALIVE_PROBES, KEEPALIVE_INTERVAL_SECONDS,
    )
    return True


__all__ = [
    "KEEPALIVE_IDLE_SECONDS",
    "KEEPALIVE_INTERVAL_SECONDS",
    "KEEPALIVE_PROBES",
    "enable_tcp_keepalive",
]
