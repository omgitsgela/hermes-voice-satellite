#!/usr/bin/env python3
"""Ask the wake shim to open the microphone, as the Gateway does for a follow-up window.

This is the same request the Gateway makes: it sends a `wake-now` event over the satellite connection,
the LED/event service relays it here, and the shim injects a wake detection so the satellite starts
streaming. Running it by hand is how you test that path without speaking to the device.

    python3 inject_wake.py            # on the Pi
"""

from __future__ import annotations

import socket
import sys

CONTROL = ("127.0.0.1", 10402)
TIMEOUT = 5.0


def main() -> int:
    try:
        with socket.create_connection(CONTROL, timeout=TIMEOUT) as sock:
            sock.sendall(b"wake\n")
            reply = sock.recv(64).decode(errors="replace").strip()
    except OSError as exc:
        print(f"could not reach the wake shim at {CONTROL[0]}:{CONTROL[1]}: {exc}")
        return 2
    print(f"shim replied: {reply}")
    return 0 if reply == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
