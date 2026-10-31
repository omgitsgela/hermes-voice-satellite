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

    if state["hermes"] == "none":
        out("  Hermes was not found on this machine.")
        out("")
        out("  Install Hermes first (the desktop app, or the service install), then run this again.")
        out("  Nothing has been changed.")
        return 2

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
        preview = session.install_gateway(
            gateway_address, registry_port=int(registry_port or 10701),
            tls_port=int(tls_port or 10702), skip_hermes=role.startswith("Both"),
        )
        if not preview["ok"]:
            out(f"  Could not continue: {preview['error']}")
            return 1
        out("")
        out("  About to run:")
        for command in preview["commands"]:
            out(f"    {' '.join(command)}")
        if not prompt.yes_no("Run it?", default=True):
            out("  Nothing was changed.")
            return 0
        if session.dry_run:
            out("  (dry run: commands were not executed)")
        else:
            out(preview["log"])
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

    if role.startswith("A satellite") or role.startswith("Both"):
        host = prompt.text("Gateway address for the satellite to dial", default=gateway_address or None)
        port = int(prompt.text("Gateway port", default="10702"))
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
        name = prompt.text("A name for this satellite", default="kitchen")
        paired = session.pair(host, port, words, name, expected_code=code,
                              remember=prompt.yes_no("Remember these words on this machine?",
                                                     default=False))
        if not paired.get("ok"):
            out(f"  Pairing failed: {paired.get('error')}")
            return 1
        out(f"  Paired. {'Remembered in ' + paired['remembered_in'] if paired.get('remembered_in') else 'Nothing was saved.'}")

        devices = state["devices"]
        if not devices:
            out("  No device packages are installed, so there is nothing to flash.")
            return 1
        labels = [f"{d['name']} ({d['vendor']})" for d in devices]
        chosen = prompt.choice("Which device are you setting up?", labels)
        profile = devices[labels.index(chosen)]

        fields = {"device_name": name, "dial_address": paired["dial_address"],
                  "wake_word": profile["wake_default"], "dry_run": session.dry_run}
        if profile["needs_card_reader"]:
            fields["card_mount"] = prompt.text("Where is the card's root filesystem mounted?")
        else:
            fields["port"] = prompt.text("Which USB port is the device on?", default="/dev/ttyACM0")
        fields["wifi_ssid"] = prompt.text("Wi-Fi network it should join (blank to skip)", default="")
        fields["wifi_psk_file"] = prompt.text(
            "Path to a file holding that network's passphrase (blank to skip)", default=""
        ) or None

        result = session.flash(profile["id"], **fields)
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
