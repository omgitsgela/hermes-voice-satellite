"""The console path: the same session, driven by questions.

This is what runs on a headless Linux box or a VPS, and it is also the reference for what the GUI
does, because both call the same `Session`. Nothing here reimplements a rule.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Protocol, Sequence

from .session import Session


class Prompt(Protocol):
    def text(self, message: str, default: str | None = None) -> str: ...

    def choice(self, message: str, options: Sequence[str]) -> str: ...

    def yes_no(self, message: str, default: bool = False) -> bool: ...

    def secret(self, message: str) -> str: ...


class StartOver(Exception):
    """Return to welcome without undoing writes that were already approved."""


def _answer(value: str) -> str:
    if value.strip().lower() == ":start-over":
        raise StartOver
    return value


class TerminalPrompt:
    def text(self, message: str, default: str | None = None) -> str:
        suffix = f" [{default}]" if default else ""
        while True:
            answer = _answer(input(f"{message}{suffix}: ")).strip()
            if answer:
                return answer
            if default is not None:
                return default
            print("  (this one is needed)")

    def choice(self, message: str, options: Sequence[str]) -> str:
        print(message)
        for index, option in enumerate(options, start=1):
            print(f"  {index}. {option}")
        while True:
            answer = _answer(input(f"Choose 1-{len(options)}: ")).strip()
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return options[int(answer) - 1]
            print("  (a number from the list)")

    def yes_no(self, message: str, default: bool = False) -> bool:
        hint = "Y/n" if default else "y/N"
        answer = _answer(input(f"{message} [{hint}]: ")).strip().lower()
        if not answer:
            return default
        return answer.startswith("y")

    def secret(self, message: str) -> str:
        import getpass

        return _answer(getpass.getpass(f"{message}: "))


class ScriptedPrompt:
    """For tests: a list of answers, and a record of what was asked."""

    def __init__(self, answers: Sequence[str]) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    def _next(self) -> str:
        if not self.answers:
            raise AssertionError(f"ran out of answers after {self.asked}")
        return _answer(self.answers.pop(0))

    def text(self, message: str, default: str | None = None) -> str:
        self.asked.append(message)
        answer = self._next()
        return answer or (default or "")

    def choice(self, message: str, options: Sequence[str]) -> str:
        self.asked.append(message)
        answer = self._next()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        return answer

    def yes_no(self, message: str, default: bool = False) -> bool:
        self.asked.append(message)
        answer = self._next().lower()
        if not answer:
            return default
        return answer.startswith("y")

    def secret(self, message: str) -> str:
        return self.text(message)


def configure_speech(session: Session, prompt: Prompt, out, profile: dict, fields: dict) -> bool:
    """Catalogue choices and custom files, shared with the graphical settings pages."""
    if prompt.yes_no("Choose another wake word or a custom model file?", default=False):
        data = session.wake_word_catalogue(profile["wake_format"])
        out(data.get("caveat", ""))
        entries = data.get("wake_words", [])
        labels = [f"{w['word']}: {w['metrics_line']}" for w in entries] + ["Custom model file"]
        choice = prompt.choice("Wake word", labels)
        if choice == "Custom model file":
            fields["wake_model"] = prompt.text("Model file path")
            fields["wake_word"] = prompt.text("Model selector", default=Path(fields["wake_model"]).stem)
        else:
            entry = entries[labels.index(choice)]
            fields["wake_word"] = entry["id"]
            if not entry["first_party"]:
                if not prompt.yes_no("Download this wake model?", default=False):
                    return False
                result = session.prepare_wake_word(profile["wake_format"], entry["id"])
                if not result.get("ok"):
                    out(result.get("error"))
                    return False
                fields["wake_model"] = result.get("path")
    if not prompt.yes_no("Browse voices or preview a custom voice?", default=False):
        return True
    out("  Voices belong to the Satellite Gateway on this computer, not individual devices.")
    out("  Downloads do not select a voice. Remote Satellite Gateway voice changes are not supported.")
    query = prompt.text("Search name, model ID or language (blank for all)", default="")
    data = session.voice_catalogue()
    voices = [v for v in data["voices"]
              if query.lower() in f"{v['name']} {v['id']} {v['language']}".lower()]
    labels = [f"{v['name']} · {v['language']} · {v['size_mb']} MB" for v in voices]
    choice = prompt.choice("Voice catalogue", labels + ["Custom voice file", "Keep current voice"])
    if choice == "Keep current voice":
        return True
    if choice == "Custom voice file":
        path = prompt.text("Piper .onnx path (its .onnx.json must be beside it)")
        result = session.preview_voice_file(path)
        if not result.get("ok") or not result.get("preview_url"):
            out(result.get("error") or result.get("note"))
            return False
        preview_path = (Path.home() / ".cache/hermes-voice-satellite/previews"
                        / result["preview_url"].rsplit("/", 1)[-1])
        out(f"  Listen to the generated WAV: {preview_path}")
        if not prompt.yes_no("Have you listened, and want to install this voice?", default=False):
            return False
        result = session.install_voice_file(path, apply=False)
        out(result.get("apply_note") or result.get("note") or result.get("error"))
        return bool(result.get("ok"))
    voice = voices[labels.index(choice)]
    fields["voice"] = voice["id"]
    sample = voice["sample_url"]
    if sample.startswith("/samples/"):
        sample = str(Path(__file__).resolve().parent / "web" / sample.lstrip("/"))
    out(f"  Listen before selecting: {sample}")
    if not voice.get("installed"):
        if not prompt.yes_no(f"Download {voice['name']} ({voice['size_mb']} MB) on this computer?",
                             default=False):
            return False
        result = session.download_voice(voice["id"], apply=False)
        if not result.get("ok"):
            out(result.get("error"))
            return False
    fields["_apply_voice"] = prompt.yes_no(
        "Select this shared voice for the local Satellite Gateway after its next restart?",
        default=False,
    )
    return True


def run(session: Session, prompt: Prompt, out=print) -> int:
    """The console flow. Returns a process exit code."""
    while True:
        try:
            return _run(session, prompt, out)
        except StartOver:
            session.close()
            out("  Starting over. Carried details cleared; already applied changes are not undone.")


def _run(session: Session, prompt: Prompt, out) -> int:
    state = session.status()
    out("")
    out("  Hermes Voice Satellite")
    out("  ----------------------")
    out("  Connect a microphone and speaker to Hermes, or change an existing satellite.")
    out("  We check Hermes first and show changes before asking permission to write them.")
    out("  Type :start-over at any prompt to return here and clear carried details.")
    out("")

    # A workbench does not need Hermes. Setting up a satellite from a laptop that will never run
    # the gateway is a supported shape (the gateway may be a VPS), so a missing Hermes only blocks
    # the gateway role, not the whole program.
    if state["hermes"] == "none":
        out("  Hermes is not installed on this machine.")
        out("  That only matters if this machine is meant to BE the Satellite Gateway.")
        out("  Official Hermes installation guide: https://hermes-agent.nousresearch.com/docs/")
        out("")
        role = prompt.choice(
            "What are we setting up?",
            ["A satellite, using a gateway that already exists",
             "Change a device I have already set up"],
        )
    else:
        out(f"  Found: {state['hermes_explained']}")
        for note in (state.get("platform") or {}).get("notes", []):
            out(f"  Note: {note}")
        out("")
        role = prompt.choice(
            "What are we setting up?",
            ["This machine as the Satellite Gateway", "A satellite on this machine's card or cable",
             "Both, gateway first", "Change a device I have already set up"],
        )

    gateway_address = ""
    gateway_card = None
    pairing_port = 10704
    if role.startswith("This machine") or role.startswith("Both"):
        out("  This installs the Satellite Gateway on this computer. Hermes must already run here.")
        gateway_address = prompt.text(
            "The address devices will dial (LAN address or hostname, not 127.0.0.1)",
            default=state.get("lan_address") or None,
        )
        ports = prompt.text("Ports (registry:tls)", default="10701:10702")
        registry_port, _, tls_port = ports.partition(":")
        # Preview first, always. The old order ran the install and then asked permission, which is
        # not a preview at all: by the time the question appeared the machine had changed.
        preview = session.install_gateway(
            gateway_address, registry_port=int(registry_port or 10701),
            tls_port=int(tls_port or 10702), dry_run=True,
        )
        if not preview.get("ok"):
            out(f"  Could not continue: {preview.get('error') or preview.get('log')}")
            return 1
        out("")
        out("  About to run:")
        for command in preview["commands"]:
            out(f"    {' '.join(command)}")
        if not prompt.yes_no("Run it?", default=True):
            out("  Nothing was changed.")
            return 0
        if session.dry_run:
            out("  (dry run: nothing was executed)")
            result = preview
        else:
            result = session.install_gateway(
                gateway_address, registry_port=int(registry_port or 10701),
                tls_port=int(tls_port or 10702), dry_run=False,
            )
            out(result.get("log") or "")
        if not result.get("ok"):
            out(f"  The gateway install failed: {result.get('error') or result.get('log')}")
            return 1
        gateway_card = result.get("card")
        pairing_port = result.get("pairing_port", 10704)
        if gateway_card:
            out("")
            out("  Save this record somewhere private. These details are shown once here and")
            out("  cannot be recovered through this installer after starting over or closing it.")
            out(f"    Address: {gateway_address}")
            out(f"    Pairing port: {pairing_port}")
            for line in gateway_card["lines"]:
                out(f"    {line}")
            out("  The identity code proves you are talking to your own Satellite Gateway.")
            out("  The pairing words are the security boundary that lets a device enrol.")
            out("  Anyone with them and access to the pairing service can enrol a device.")
            out("  You will need this record when adding each satellite, here or on another workbench.")
            out("  This run carries it forward for you. Lost words must be replaced on the Satellite Gateway.")
        unit = result.get("pairing_unit") if isinstance(result, dict) else None
        if unit:
            out("")
            out("  The pairing service is ready. Start it with:")
            for line in result.get("pairing_unit_instructions", []):
                out(f"    {line}")

    if role.startswith("This machine") and prompt.yes_no("Add a satellite to this Satellite Gateway now?", default=True):
        role = "Both, gateway first"

    managing = role.startswith("Change")
    if role.startswith("A satellite") or role.startswith("Both") or managing:
        devices = state["devices"]
        if not devices:
            out("  No device packages are installed, so there is nothing to set up.")
            return 1

        # The device comes first, because pairing issues a certificate for the device's own identity
        # and that identity can only be read from the device while it is connected.
        labels = [f"{d['name']} ({d['vendor']})" for d in devices]
        chosen = prompt.choice("Which device are you setting up?", labels)
        profile = devices[labels.index(chosen)]
        card_device = profile["needs_card_reader"]
        fields: dict = {
            "device_name": "",
            "dial_address": "",
            "wake_word": profile["wake_default"],
            "dry_run": session.dry_run,
        }

        if card_device:
            mounts = session.card_mounts()
            if mounts:
                options = [m["mount"] for m in mounts] + ["Type a path"]
                picked = prompt.choice("Which mounted card is it?", options)
                fields["card_mount"] = (prompt.text("Path to the card's root filesystem")
                                        if picked == "Type a path" else picked)
            else:
                fields["card_mount"] = prompt.text("Path to the card's root filesystem")
        else:
            ports = session.usb_ports()
            if ports:
                options = [f"{p['device']}  {p['description']}".strip() for p in ports] + ["Type a port"]
                picked = prompt.choice("Which connection is the device on?", options)
                fields["port"] = (prompt.text("USB port", default="/dev/ttyACM0")
                                  if picked == "Type a port"
                                  else ports[options.index(picked)]["device"])
            else:
                fields["port"] = prompt.text("USB port the device is on", default="/dev/ttyACM0")
            identity = session.device_identity(profile["id"], port=fields["port"])
            if identity.get("ok"):
                fields["device_id"] = identity["device_id"]
                out(f"  This device calls itself {identity['device_id']}.")
                if identity.get("note"):
                    out(f"  {identity['note']}")
            else:
                out(f"  {identity.get('error')}")
                return 1

        if managing:
            # The device already has its gateway, its credentials and its name, so this flow never
            # pairs and never flashes. Only what lives on the card or the board is staged again --
            # which is what "change a device" promised and what used to send people through a flash.
            out("")
            out("  Changing a device that is already set up. Its firmware is not touched.")
            if card_device:
                fields["wifi_ssid"] = prompt.text(
                    "Copy which saved Wi-Fi network onto the card (blank to keep as it is)",
                    default="") or None
            else:
                fields["wifi_ssid"] = prompt.text(
                    "Wi-Fi network it should join (blank to keep as it is)", default="") or None
                if fields["wifi_ssid"]:
                    fields["wifi_password"] = prompt.secret("Wi-Fi password (used once, not stored)")
            change = prompt.choice(
                "What are you changing?",
                ["The wake word", "Only the network above"],
            )
            # Not the profile default until it is actually chosen: staging an unchanged setting
            # would be work the person did not ask for.
            fields["wake_word"] = None
            if change == "The wake word":
                fields["wake_word"] = prompt.text("Wake word", default=profile["wake_default"])
                fields["wake_model"] = prompt.text(
                    "Model file to copy onto the device (blank if it already has this one)",
                    default="") or None
            out("  Cable/card changes: Wi-Fi and wake word. Pairing and firmware stay unchanged.")
            out("  Remote settings require the Satellite Gateway controls; remote editing is not available here.")
            preview = session.manage_device(profile["id"], **{**fields, "dry_run": True})
            if not preview.get("ok"):
                out(preview.get("error") or preview.get("summary"))
                return 1
            for command in preview.get("commands", []):
                out(f"    {command['description']}: {' '.join(command['argv'])}")
            if not prompt.yes_no("Apply these settings (no pairing or firmware write)?", default=False):
                out("  Nothing was changed.")
                return 0
            result = session.manage_device(profile["id"], **fields)
            if not result["ok"]:
                out(f"  {result.get('summary') or result.get('error')}")
                return 1
            for command in result.get("commands", []):
                marker = " (needs elevation)" if command["elevation"] else ""
                out(f"    {command['description']}{marker}")
                out(f"      {' '.join(command['argv'])}")
            out("")
            out(f"  {result['summary']}")
            out("")
            out("  Done.")
            return 0

        host = prompt.text("Gateway address", default=gateway_address or None)
        port = int(prompt.text("Pairing port", default=str(pairing_port)))
        inspected = session.inspect_gateway(host, port)
        if not inspected.get("ok"):
            out(f"  Could not reach {host}:{port}: {inspected.get('error')}")
            return 1
        code = inspected["identity_code"]
        out("")
        out(f"  That gateway's identity code is  {code}")
        out("  This is how you prove you are talking to your own gateway and not another machine.")
        carried = gateway_card if host == gateway_address and port == pairing_port else None
        if carried:
            out(f"  Saved during Satellite Gateway setup: {carried['identity_code']}")
            if code != carried["identity_code"]:
                out("  Stopped: the code does not match the Satellite Gateway just installed. No words were sent.")
                return 1
        if not prompt.yes_no("Does it match the code you were given when the Satellite Gateway was set up?"):
            out("  Stopped. Nothing was sent, and the words were not used.")
            return 1
        words = carried["words"] if carried else prompt.secret("Pairing words")
        name = prompt.text("A name for this device", default="") or None
        while not name:
            name = prompt.text("A name for this device (for example kitchen)") or None
        paired = session.pair(host, port, words, name, device_id=fields.get("device_id"),
                              expected_code=code,
                              remember=prompt.yes_no("Remember these words on this machine?",
                                                     default=False))
        if not paired.get("ok"):
            message = str(paired.get("error") or "")
            if "exists" in message.lower() or "already" in message.lower():
                out(f"  {message}")
                if prompt.yes_no("This device is already known to the gateway. Replace its "
                                 "certificate?", default=True):
                    paired = session.pair(host, port, words, name,
                                          device_id=fields.get("device_id"),
                                          expected_code=code, replace=True,
                                          remember=False)
            if not paired.get("ok"):
                out(f"  Pairing failed: {paired.get('error')}")
                return 1
        out(f"  Paired. {'Remembered in ' + paired['remembered_in'] if paired.get('remembered_in') else 'Nothing was saved.'}")

        # The device was chosen at the top of this block, before its identity could be read.
        fields["device_name"] = name
        fields["dial_address"] = paired["dial_address"]
        fields["certs_dir"] = paired.get("certs_dir")
        if card_device:
            # A card gets a network copied from one this machine already knows. There is no path
            # from a typed passphrase to a card, so the prompt must not pretend there is.
            fields["wifi_ssid"] = prompt.text(
                "Copy which saved Wi-Fi network onto the card (blank to keep as it is)", default=""
            ) or None
        else:
            fields["wifi_ssid"] = prompt.text(
                "Wi-Fi network it should join (blank to keep as it is)", default="") or None
            if fields["wifi_ssid"]:
                fields["wifi_password"] = prompt.secret("Wi-Fi password (used once, not stored)")

        if not configure_speech(session, prompt, out, profile, fields):
            return 1
        apply_voice = fields.pop("_apply_voice", False)
        check = session.preflight(profile["id"], **fields)
        if not check["ok"]:
            out("  Not ready to install:")
            for problem in check["problems"]:
                out(f"    - {problem}")
            out("  Nothing has been written.")
            return 1

        # Flash and staging are two stages: the software, then the Wi-Fi, dial address, credentials
        # and model where the device looks for them.
        preview = session.install_device(profile["id"], **{**fields, "dry_run": True})
        if not preview.get("ok"):
            out(preview.get("error") or preview.get("summary"))
            return 1
        for command in preview.get("commands", []):
            out(f"    {command['description']}: {' '.join(command['argv'])}")
        if not prompt.yes_no("Install the device software and settings shown above?", default=False):
            out("  Nothing was written to the device.")
            return 0
        result = session.install_device(profile["id"], **fields)
        if result.get("ok") and apply_voice:
            selected = session.download_voice(fields["voice"], apply=True)
            out(selected.get("apply_note") or selected.get("note") or selected.get("error"))
        if not result["ok"]:
            out(f"  {result.get('summary') or result.get('error')}")
            return 1
        for command in result["commands"]:
            marker = " (needs elevation)" if command["elevation"] else ""
            out(f"    {command['description']}{marker}")
            out(f"      {' '.join(command['argv'])}")
        out("")
        out(f"  {result['summary']}")

    out("")
    out("  Done.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hermes-voice-satellite",
        description="Hermes Voice Satellite: set up the Gateway and the satellites that talk to it.",
    )
    parser.add_argument("--gui", action="store_true", help="open the graphical installer")
    parser.add_argument("--dry-run", action="store_true", help="show every command, run none of it")
    parser.add_argument("--status", action="store_true", help="print what was detected, as JSON")
    parser.add_argument("--check-assets", action="store_true",
                        help="verify the files an installation runs are present, then exit")
    parser.add_argument("--base", default=".", help="the repository checkout to install from")
    args = parser.parse_args(argv)

    session = Session(Path(args.base), dry_run=args.dry_run)

    if args.check_assets:
        from .session import RUNTIME_ASSETS

        missing = [asset for asset in RUNTIME_ASSETS if not (session.repo_root / asset).exists()]
        for asset in RUNTIME_ASSETS:
            state = "missing" if asset in missing else "present"
            print(f"  {state}: {asset}")
        if missing:
            print(f"  {len(missing)} runtime asset(s) missing from {session.repo_root}")
            return 1
        print(f"  all {len(RUNTIME_ASSETS)} runtime assets present in {session.repo_root}")
        return 0
    if args.status:
        print(json.dumps(session.status(), indent=2))
        return 0
    try:
        if args.gui:
            from .server import serve

            return serve(session)
        return run(session, TerminalPrompt())
    finally:
        # Temporary credential directories hold a private key; they end with the session.
        session.close()


if __name__ == "__main__":
    sys.exit(main())
