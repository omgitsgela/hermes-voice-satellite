#!/usr/bin/env python3
"""Live checks for the google plugin's `places_search` action.

Angela's requirement, 2026-09-22: the voice surface must be able to call the Places API, through the
same gog-backed plugin as her tasks and calendar, and it must actually work. The failure this fixes:
asked for the hours of a nearby Las Delicias, the voice surface searched the web and invented a
place ("Las Delicias North on Conifer Road") with invented hours, because it had no way to reach
Places at all.

These are live checks against the real API, because the failure mode was a plausible-looking answer,
which only a real lookup can rule out. Nothing here writes anything.

    python3 /home/angela/.hermes/plugins/google/test_places_live.py
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

PLUGIN = Path("/home/angela/.hermes/plugins/google/google_tool.py")
TRUTH_NAME = "Las Delicias Glendale"
TRUTH_ADDRESS = "4301 E Kentucky Ave"


def load_plugin():
    spec = importlib.util.spec_from_file_location("google_tool", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def call(module, args: dict) -> dict:
    raw = module.handle(args)
    return json.loads(raw)


def main() -> int:
    module = load_plugin()
    problems = 0
    print("=== PLACES ACTION (live API) ===")

    # 1. The exact question that failed, through the real entry point.
    result = call(module, {"action": "places_search", "query": "Las Delicias"})
    print(f"\n1. places_search 'Las Delicias'  (ok={result.get('ok')}, near={result.get('near')})")
    if not result.get("ok"):
        print(f"   FAIL: {result.get('error')}")
        return 1
    places = result.get("places") or []
    for p in places[:3]:
        print(f"     {p.get('distance_miles')} mi  {p.get('name')}  [{p.get('status')}]")
        print(f"              {p.get('address')}")
    if not places:
        print("   FAIL: no places returned")
        problems += 1
    else:
        nearest = places[0]
        print(f"   nearest: {nearest.get('name')} in {nearest.get('distance_miles')} mi, "
              f"{len(nearest.get('hours') or [])} hour lines")
        checks = [
            ("returned hours", bool(nearest.get("hours"))),
            ("within a mile of home", (nearest.get("distance_miles") or 99) < 1.0),
            ("is the real Glendale one", TRUTH_NAME.lower() in str(nearest.get("name", "")).lower()),
            ("has its real street address", TRUTH_ADDRESS.lower() in str(nearest.get("address", "")).lower()),
            ("has a place id for follow-ups", bool(nearest.get("place_id"))),
        ]
        for label, ok in checks:
            print(f"     {'OK  ' if ok else 'FAIL'} {label}")
            if not ok:
                problems += 1

    # 2. Hours must be real weekday text, not a made-up summary.
    if places:
        hours = places[0].get("hours") or []
        real = sum(1 for line in hours if any(d in line for d in
                                              ("Monday", "Tuesday", "Wednesday", "Thursday",
                                               "Friday", "Saturday", "Sunday")))
        print(f"\n2. hours are weekday lines: {real}/7")
        if real < 7:
            print("   FAIL: opening hours are incomplete")
            problems += 1
        else:
            print(f"   OK: e.g. {hours[0]}")

    # 3. `near` must actually move the origin.
    golden = call(module, {"action": "places_search", "query": "Starbucks", "near": "Golden"})
    home = call(module, {"action": "places_search", "query": "Starbucks"})
    print(f"\n3. near='Golden' vs default home")
    if not (golden.get("ok") and home.get("ok")):
        print(f"   FAIL: {golden.get('error') or home.get('error')}")
        problems += 1
    else:
        g0 = (golden.get("places") or [{}])[0]
        h0 = (home.get("places") or [{}])[0]
        print(f"   near Golden : {g0.get('distance_miles')} mi  {g0.get('name')}")
        print(f"   near home   : {h0.get('distance_miles')} mi  {h0.get('name')}")
        differ = (g0.get("name") != h0.get("name")) or (g0.get("distance_miles") != h0.get("distance_miles"))
        print(f"     {'OK  ' if differ else 'FAIL'} the origin changes the answer")
        if not differ:
            problems += 1

    # 4. A bad origin is a clean refusal, not a crash.
    bad = call(module, {"action": "places_search", "query": "Starbucks",
                        "near": "zzz not a real place 99999"})
    print(f"\n4. bad origin refused cleanly: ok={bad.get('ok')} error={str(bad.get('error'))[:60]!r}")
    if bad.get("ok"):
        print("   FAIL: it accepted an origin it cannot locate")
        problems += 1
    else:
        print("   OK: clean error, no traceback")

    # 5. The key must never appear in anything this tool returns.
    key = Path("/home/angela/google-places-api-key.txt").read_text().strip()
    blob = json.dumps(result) + json.dumps(golden) + json.dumps(home) + json.dumps(bad)
    print(f"\n5. key absent from all output: {key not in blob}")
    if key in blob:
        print("   FAIL: the API key leaked into a tool result")
        problems += 1

    # 6. The action is reachable by name, and unknown actions still refuse.
    unknown = call(module, {"action": "places_delete"})
    print(f"\n6. unknown action refused: {not unknown.get('ok')}")
    print(f"   known actions: {len(module._ACTIONS)}  includes places_search: "
          f"{'places_search' in module._ACTIONS}")
    if unknown.get("ok") or "places_search" not in module._ACTIONS:
        problems += 1

    print(f"\n=== {'ALL CHECKS PASSED' if problems == 0 else f'{problems} PROBLEM(S)'} ===")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
