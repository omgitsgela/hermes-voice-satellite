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


class TerminalPrompt:
    def text(self, message: str, default: str | None = None) -> str:
        suffix = f" [{default}]" if default else ""
        while True:
            answer = input(f"{message}{suffix}: ").strip()
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
            answer = input(f"Choose 1-{len(options)}: ").strip()
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return options[int(answer) - 1]
            print("  (a number from the list)")

    def yes_no(self, message: str, default: bool = False) -> bool:
        hint = "Y/n" if default else "y/N"
        answer = input(f"{message} [{hint}]: ").strip().lower()
        if not answer:
            return default
        return answer.startswith("y")

    def secret(self, message: str) -> str:
        import getpass

        return getpass.getpass(f"{message}: ")


class ScriptedPrompt:
    """For tests: a list of answers, and a record of what was asked."""

    def __init__(self, answers: Sequence[str]) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    def _next(self) -> str:
        if not self.answers:
            raise AssertionError(f"ran out of answers after {self.asked}")
        return self.answers.pop(0)

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


def run(session: Session, prompt: Prompt, out=print) -> int:
    """The console flow. Returns a process exit code."""
    state = session.status()
    out("")
    out("  Hermes voice installer")
    out("  ----------------------")
    out("  Sets up a voice gateway, a satellite, or both.")
    out("")

    # A workbench does not need Hermes. Setting up a satellite from a laptop that will never run
    # the gateway is a supported shape (the gateway may be a VPS), so a missing Hermes only blocks
    # the gateway role, not the whole program.
    if state["hermes"] == "none":
        out("  Hermes is not installed on this machine.")
        out("  That only matters if this machine is meant to BE the gateway.")
        out("")
        role = "A satellite, using a gateway that already exists"
    else:
        out(f"  Found: {state['hermes_explained']}")
        out("")
        role = prompt.choice(
            "What are we setting up?",
            ["This machine as the gateway", "A satellite on this machine's card or cable",
             "Both, gateway first"],
        )

    gateway_address = ""
    if role.startswith("This machine") or role.startswith("Both"):
        gateway_address = prompt.text(
            "The address devices will dial (LAN address or hostname, not 127.0.0.1)"
        )
        ports = prompt.text("Ports (registry:tls)", default="10701:10702")
        registry_port, _, tls_port = ports.partition(":")
        # Preview first, always. The old order ran the install and then asked permission, which is
        # not a preview at all: by the time the question appeared the machine had changed.
        preview = session.install_gateway(
            gateway_address, registry_port=int(registry_port or 10701),
            tls_port=int(tls_port or 10702), skip_hermes=role.startswith("Both"),
            dry_run=True,
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
                tls_port=int(tls_port or 10702), skip_hermes=role.startswith("Both"),
                dry_run=False,
            )
            out(result.get("log") or "")
        if not result.get("ok"):
            out(f"  The gateway install failed: {result.get('error') or result.get('log')}")
            return 1
        base = session.repo_root
        cert = base / "state" / "registry-tls" / "server.crt"
        words_file = base / "state" / "enroll-token.txt"
        if cert.exists() and words_file.exists():
            card = session.pairing_card(str(cert), words_file.read_text().strip(), gateway_address)
            if card.get("ok"):
                out("")
                out("  Write this down and keep it somewhere you will find it:")
                for line in card["lines"]:
                    out(f"    {line}")
                out("")
                out("  You will be asked for the address, and to check the identity code,")
                out("  when you set up each satellite.")
        unit = result.get("pairing_unit") if isinstance(result, dict) else None
        if unit:
            out("")
            out("  The pairing service is ready. Start it with:")
            for line in result.get("pairing_unit_instructions", []):
                out(f"    {line}")

    if role.startswith("A satellite") or role.startswith("Both"):
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
            else:
                out(f"  {identity.get('error')}")
                return 1

        host = prompt.text("Gateway address", default=gateway_address or None)
        port = int(prompt.text("Pairing port", default="10704"))
        inspected = session.inspect_gateway(host, port)
        if not inspected.get("ok"):
            out(f"  Could not reach {host}:{port}: {inspected.get('error')}")
            return 1
        code = inspected["identity_code"]
        out("")
        out(f"  That gateway's identity code is  {code}")
        out("  This is how you prove you are talking to your own gateway and not another machine.")
        if not prompt.yes_no("Does it match the code you were given when the gateway was set up?"):
            out("  Stopped. Nothing was sent, and the words were not used.")
            return 1
        words = prompt.secret("Pairing words")
        name = prompt.text("A name for this device", default="") or None
        while not name:
            name = prompt.text("A name for this device (for example kitchen)") or None
        paired = session.pair(host, port, words, name, device_id=fields.get("device_id"),
                              expected_code=code,
                              remember=prompt.yes_no("Remember these words on this machine?",
                                                     default=False))
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

        # Flash and staging are two stages: the software, then the Wi-Fi, dial address, credentials
        # and model where the device looks for them.
        result = session.install_device(profile["id"], **fields)
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
        prog="hermes-installer",
        description="Set up a Hermes voice gateway and its satellites.",
    )
    parser.add_argument("--gui", action="store_true", help="open the graphical installer")
    parser.add_argument("--dry-run", action="store_true", help="show every command, run none of it")
    parser.add_argument("--status", action="store_true", help="print what was detected, as JSON")
    parser.add_argument("--base", default=".", help="the repository checkout to install from")
    args = parser.parse_args(argv)

    session = Session(Path(args.base), dry_run=args.dry_run)

    if args.status:
        print(json.dumps(session.status(), indent=2))
        return 0
    if args.gui:
        from .server import serve

        return serve(session)
    return run(session, TerminalPrompt())


if __name__ == "__main__":
    sys.exit(main())
