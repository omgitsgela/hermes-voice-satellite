#!/usr/bin/env python3
"""Send one LED command to the local 2mic LED service.

The satellite's command hooks (--tts-start-command, --tts-played-command) run this so the
end of a turn can change the LEDs without adding a second listener or exposing anything to
the LAN. Keep it tiny: the satellite waits for this process to exit, so it is on the path
of every spoken answer.
"""
import json
import socket
import sys

EVENT_TYPES = {"busy": "led-busy", "idle": "led-idle", "followup": "led-followup"}
CONTROL_ADDRESS = ("127.0.0.1", 10500)


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1].lower() not in EVENT_TYPES:
        print("usage: led_cmd.py {idle|busy|followup}", file=sys.stderr)
        return 2

    event_type = EVENT_TYPES[sys.argv[1].lower()]

    try:
        with socket.create_connection(CONTROL_ADDRESS, timeout=2.0) as sock:
            sock.sendall(json.dumps({"type": event_type, "data": {}}).encode("utf-8") + b"\n")
    except OSError as exc:
        # Never block a turn over the LEDs.
        print(f"led_cmd: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
