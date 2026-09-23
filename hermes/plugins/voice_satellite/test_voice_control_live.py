#!/usr/bin/env python3
"""Live checks for the voice_satellite control path.

Angela, 2026-09-22: "'turn on follow up mode' should work too. if you give the agent a tool call
script, it should be able to execute that on its own smartly." So the assistant gets a tool, and
this proves the tool can actually change the device.

Checks the whole chain: the plugin's handler -> the bridge's authenticated /settings endpoint -> the
loop's persisted state, and back out again. It leaves follow-up mode OFF, which is the default.

    python3 /home/angela/.hermes/plugins/voice_satellite/test_voice_control_live.py
"""

from __future__ import annotations

import importlib.util
import json
import urllib.error
import urllib.request
from pathlib import Path

PLUGIN = Path("/home/angela/.hermes/plugins/voice_satellite/voice_tool.py")
STATE = Path("/home/angela/hermes-voice-bridge/state/follow-up-on")
PROBLEMS = 0


def load():
    spec = importlib.util.spec_from_file_location("voice_tool", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(label: str, ok: bool, detail: str = "") -> None:
    global PROBLEMS
    print(f"   {'OK  ' if ok else 'FAIL'} {label}{(' -- ' + str(detail)) if detail else ''}")
    if not ok:
        PROBLEMS += 1


def call(module, action: str) -> dict:
    return json.loads(module.handle({"action": action}))


def raw(module, method: str, path: str, payload=None, token: str | None = "auto"):
    """A request straight at the bridge, bypassing the plugin, to check it independently."""
    if token == "auto":
        token = module._token()
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{module.BASE_URL}{path}", data=body, headers=headers,
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return {"status": response.status, "body": json.loads(response.read().decode())}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "body": json.loads(exc.read().decode() or "{}")}


def main() -> int:
    module = load()
    print("=== VOICE CONTROL (live, through the bridge) ===")

    # 1. Status reads the real state.
    result = call(module, "follow_up_status")
    check("status answers with the device's state",
          result.get("ok") and isinstance(result.get("follow_up"), bool), result)

    # 2. Independent read of the same endpoint, not through the plugin.
    direct = raw(module, "GET", "/settings")
    check("the endpoint agrees, read independently",
          direct["status"] == 200 and direct["body"].get("follow_up") == result.get("follow_up"),
          direct)

    # 3. Turn it on, and prove it on disk as well as over the wire.
    result = call(module, "follow_up_on")
    check("follow_up_on reports success and verification",
          result.get("ok") and result.get("verified") and result.get("follow_up") is True, result)
    check("the bridge persisted it to its state file",
          STATE.is_file() and STATE.read_text().strip() == "on",
          STATE.read_text().strip() if STATE.is_file() else "missing")
    check("a fresh read sees it on", raw(module, "GET", "/settings")["body"].get("follow_up") is True)

    # 4. Turn it back off, the state Angela would normally leave it in.
    result = call(module, "follow_up_off")
    check("follow_up_off reports success and verification",
          result.get("ok") and result.get("verified") and result.get("follow_up") is False, result)
    check("the state file says off again",
          STATE.read_text().strip() == "off", STATE.read_text().strip())
    check("a fresh read sees it off", raw(module, "GET", "/settings")["body"].get("follow_up") is False)

    # 5. Refusals: no token, bad body, unknown action.
    unauth = raw(module, "GET", "/settings", token=None)
    check("an unauthenticated read is refused with 401",
          unauth["status"] == 401, f"status={unauth['status']}")
    check("an unauthenticated change is refused with 401",
          raw(module, "POST", "/settings", {"follow_up": True}, token=None)["status"] == 401)
    bad = raw(module, "POST", "/settings", {"follow_up": "yes please"})
    check("a non-boolean value is a 400, not a silent change",
          bad["status"] == 400, f"status={bad['status']}")
    missing = raw(module, "POST", "/settings", {})
    check("a body with no setting is a 400", missing["status"] == 400)
    unknown = call(module, "follow_up_sideways")
    check("an unknown action is refused by the plugin", not unknown.get("ok"), unknown)

    # 6. The token must never appear in anything a caller sees.
    token = module._token()
    blob = json.dumps([result, direct, unauth, bad, missing, unknown])
    check("the token never appears in any output", token not in blob)

    # 7. Nothing was left turned on.
    check("follow-up mode is left OFF",
          raw(module, "GET", "/settings")["body"].get("follow_up") is False)

    print(f"\n=== {'ALL CHECKS PASSED' if PROBLEMS == 0 else f'{PROBLEMS} PROBLEM(S)'} ===")
    return 1 if PROBLEMS else 0


if __name__ == "__main__":
    raise SystemExit(main())
