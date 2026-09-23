#!/usr/bin/env python3
"""Switch the voice satellite's wake word, on the Pi as root, safely and repeatably.

Run this ON wyoming1 (copy it over first: scp scripts/switch-wake-word.py wyoming1:/tmp/).
It defaults to a dry run; nothing is written or restarted without --apply.

  # switch to a built-in model
  sudo python3 switch-wake-word.py --name hey_jarvis

  # install a freshly downloaded model and switch to it (name comes from the file's stem)
  sudo python3 switch-wake-word.py --name hey_hermes --from-file ~/Hey_Hermes_20260707_013715.tflite --apply

  # also move the threshold while switching
  sudo python3 switch-wake-word.py --name hey_jarvis --threshold 0.4 --apply

Why the checks exist:
- The wake service names a custom model after the model file's STEM, and strips a trailing _vN.N.
  The satellite passes --wake-word-name through with no validation, and Describe advertises only
  built-in models, so a name with no matching file is silently skipped and the device quietly keeps
  its old wake word. Nothing errors. Hence the hard refusal to continue on a name/file mismatch.
- Restarting the wake service makes the wake shim churn, and while it churns no microphone audio
  reaches the wake model, so nothing can wake the device. The shim must be restarted after the wake
  service, and the satellite must be STARTED (it does not come back on its own).
"""
import argparse
import datetime
import os
import re
import shutil
import socket
import subprocess
import sys
import time

DROPIN = "/etc/systemd/system/wyoming-openwakeword.service.d/10-threshold.conf"
SATELLITE = "/etc/systemd/system/wyoming-satellite.service"
WAKESVC_UNIT = "wyoming-openwakeword"
SHIM_UNIT = "wake-shim"
SAT_UNIT = "wyoming-satellite"
SCRATCH_PORT = 10499

# The five models this wyoming-openwakeword build actually ships (verified from the Model enum).
BUILTIN = ["alexa", "hey_jarvis", "hey_mycroft", "hey_rhasspy", "okay_nabu"]


def run(cmd, **kw):
    return subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, **kw)


def read(path):
    with open(path) as fh:
        return fh.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="wake word to use (file stem for a custom model)")
    ap.add_argument("--model-dir", default="/home/wyoming1/wake-models")
    ap.add_argument("--from-file", help="a .tflite to install into the model dir under --name")
    ap.add_argument("--threshold", type=float, help="set the wake threshold (default: leave unchanged)")
    ap.add_argument("--apply", action="store_true", help="actually write and restart (default: dry run)")
    args = ap.parse_args()

    name = args.name
    custom = name not in BUILTIN
    model_path = os.path.join(args.model_dir, name + ".tflite")
    print(f"target wake word: {name!r}  ({'custom model' if custom else 'built-in model'})")

    if custom and args.from_file:
        src = os.path.expanduser(args.from_file)
        if not os.path.isfile(src):
            sys.exit(f"  no such file: {src}")
        with open(src, "rb") as fh:
            head = fh.read(8)
        size = os.path.getsize(src)
        if head[4:8] != b"TFL3":
            sys.exit(f"  {src} is not a TFLite model (no TFL3 identifier at offset 4)")
        print(f"  source file OK: {size} bytes, TFLite " + ("(as expected)" if size else ""))
        print(f"  would install to {model_path}")
        if args.apply:
            os.makedirs(args.model_dir, exist_ok=True)
            shutil.copy2(src, model_path)
            print(f"  installed: {model_path}")

    if custom and not os.path.isfile(model_path):
        if args.from_file and not args.apply:
            # A dry run must not fail just because --apply has not copied the file yet.
            print(f"  {model_path} does not exist yet; --apply would create it from {args.from_file}")
        else:
            sys.exit(
                f"  refusing: {model_path} does not exist.\n"
                f"  A custom wake word is named after the model file's stem, so the file must be "
                f"{name}.tflite (a trailing _v1.2 is stripped). Use --from-file to install it, or pick "
                f"one of the built-ins: {', '.join(BUILTIN)}"
            )

    if not os.path.exists(DROPIN) or not os.path.exists(SATELLITE):
        sys.exit("  refusing: expected systemd files are missing; is this wyoming1?")

    dropin = read(DROPIN)
    sat = read(SATELLITE)

    # --- wake service: ExecStart carries uri + threshold + custom model dir together, on purpose ---
    m = re.search(r"^ExecStart=(/home/wyoming1/wyoming-openwakeword/script/run.*)$", dropin, re.M)
    if not m:
        sys.exit("  refusing: could not find the wake service ExecStart line")
    current = m.group(1)
    new_exec = current
    if custom:
        if "--custom-model-dir" not in new_exec:
            new_exec += f" --custom-model-dir {args.model_dir}"
    if args.threshold is not None:
        if "--threshold" in new_exec:
            new_exec = re.sub(r"--threshold [0-9.]+", f"--threshold {args.threshold}", new_exec)
        else:
            new_exec += f" --threshold {args.threshold}"
    new_dropin = dropin.replace(f"ExecStart={current}", f"ExecStart={new_exec}")

    # --- satellite: the name it asks the wake service for ---
    m = re.search(r"--wake-word-name\s+'([^']*)'", sat)
    if not m:
        sys.exit("  refusing: could not find --wake-word-name in the satellite unit")
    old_name = m.group(1)
    new_sat = sat.replace(f"--wake-word-name '{old_name}'", f"--wake-word-name '{name}'")

    print()
    print(f"wake word:   {old_name!r} -> {name!r}")
    if new_exec != current:
        print(f"wake service ExecStart: {new_exec}")
    else:
        print("wake service ExecStart: unchanged")

    if not args.apply:
        print()
        print("DRY RUN. Nothing written, nothing restarted. Re-run with --apply to do it.")
        return

    if new_dropin == dropin and new_sat == sat:
        print("nothing to change; already in this state")
    else:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        for path in (DROPIN, SATELLITE):
            shutil.copy2(path, f"{path}.bak-wakeword-{stamp}")
            print(f"backed up {path} -> {os.path.basename(path)}.bak-wakeword-{stamp}")
        with open(DROPIN, "w") as fh:
            fh.write(new_dropin)
        with open(SATELLITE, "w") as fh:
            fh.write(new_sat)
        print("written")

    # Pre-flight: prove the service accepts this command line before touching the live service.
    if custom:
        pf = run(["timeout", "8", "/home/wyoming1/wyoming-openwakeword/script/run",
                  "--uri", f"tcp://127.0.0.1:{SCRATCH_PORT}", "--custom-model-dir", args.model_dir,
                  "--debug"])
        out = pf.stdout + pf.stderr
        if f"Found custom model {name}" in out:
            print(f"pre-flight: service registers {name!r}")
        else:
            print("pre-flight FAILED: the service did not report the custom model:")
            print("  " + "\n  ".join(out.strip().splitlines()[-6:]))
            sys.exit("  refusing to restart live services after a failed pre-flight")

    print()
    print("restarting, in the order that works:")
    run(f"systemctl daemon-reload && systemctl restart {WAKESVC_UNIT}")
    time.sleep(3)
    print(f"  {WAKESVC_UNIT}: {run(f'systemctl is-active {WAKESVC_UNIT}').stdout.strip()}")
    run(f"systemctl restart {SHIM_UNIT}")
    time.sleep(2)
    run(f"systemctl start {SAT_UNIT}")
    time.sleep(8)

    # Verify: services up, both relay legs established, shim quiet, satellite waiting.
    print()
    print("verification:")
    for unit in (WAKESVC_UNIT, SHIM_UNIT, SAT_UNIT, "2mic_leds"):
        print(f"  {unit}: {run(f'systemctl is-active {unit}').stdout.strip()}")
    sockets = run("ss -tn").stdout
    legs = [ln for ln in sockets.splitlines() if "10400" in ln or "10401" in ln]
    est = [ln for ln in legs if ln.split()[0] == "ESTAB"]
    print(f"  established relay legs (expect 2): {len(est)}")
    for ln in est[:4]:
        print("    " + ln.strip())
    churn = run(f"journalctl -u {SHIM_UNIT} --since '20 seconds ago'").stdout
    print("  shim churning: " + ("YES (bad)" if "reconnecting" in churn else "no"))
    satlog = run(f"journalctl -u {SAT_UNIT} -n 15").stdout
    print("  satellite waiting for wake word: "
          + ("yes" if "Waiting for wake word" in satlog else "NOT SEEN (check the journal)"))
    print()
    print(f"Now speak it. Confirm in the bridge log on autumn: journalctl --user -u hermes-voice-bridge"
          f"  -> 'wake word: {name}'")
    print("A synthesised voice cannot test this; only a real person can.")


if __name__ == "__main__":
    main()
