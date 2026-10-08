#!/usr/bin/env python3
"""Apply the voice-surface keys to the Hermes config without deleting anything else.

`hermes config set` changes one key at a time and rewrites the file, so driving a whole
install through it means N rewrites of a file the user has annotated. This helper loads the
config once, applies every key the voice surface needs in one pass, and refuses to write
unless something actually changes. It is called by scripts/install-gateway.sh with an
explicit list of (dotted.key, value) pairs: the installer owns WHAT gets set, this file owns
HOW the file is edited.

What it does:
  - loads <config.yaml> (default ~/.hermes/config.yaml)
  - sets each dotted key to its value, creating parents as needed
  - reports, key by key, what was added, changed, or already correct
  - writes the file back ONLY if at least one value changed; otherwise it leaves the file
    byte-for-byte untouched
  - never deletes anything: a key the installer does not manage keeps its value

Plain-YAML round-trip: the file's comments are not preserved through a load/dump, so the
installer makes a timestamped backup before calling this. Values the installer does not
manage are preserved; only their comments travel with the backup.
"""
import argparse
import json
import sys
from pathlib import Path

import yaml


def get_path(data: dict, dotted: str):
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def key_present_in_example(example: dict, dotted: str) -> bool:
    """Whether the example config defines this dotted key. The example is the project's own
    declaration of which voice-surface keys are real; a key absent from it is almost certainly a
    typo, and setting it would silently create a key Hermes ignores rather than an error worth
    trusting."""
    node = example
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def set_path(data: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    leaf = parts[-1]
    # A list the installer extends (e.g. plugins.enabled with a new plugin name) is appended to
    # rather than clobbered: replacing the list would silently drop every other plugin the user
    # had enabled.
    existing = node.get(leaf)
    if isinstance(existing, list) and isinstance(value, list):
        for item in value:
            if item not in existing:
                existing.append(item)
    else:
        node[leaf] = value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(Path.home() / ".hermes" / "config.yaml"),
                        help="the Hermes config.yaml to edit (default ~/.hermes/config.yaml)")
    parser.add_argument("--example", required=True,
                        help="the example config shipped in the repo, for the keys' documentation")
    parser.add_argument("pairs", nargs="+",
                        help="dotted.key=value pairs; value is a scalar or JSON")
    args = parser.parse_args()

    config = Path(args.config).expanduser()
    if not config.is_file():
        raise SystemExit(f"no config at {config}; point --config at the Hermes config.yaml")
    data = yaml.safe_load(config.read_text()) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"{config} is not a YAML mapping; refusing to edit it")

    # The example is the project's authoritative list of voice-surface keys. Load it and reject
    # any key not defined in it: a typo'd dotted key would otherwise be silently created as a key
    # Hermes ignores, which is worse than an error.
    example_path = Path(args.example).expanduser()
    if not example_path.is_file():
        raise SystemExit(f"no example config at {example_path}; pass the repo's voice-surface.example.yaml")
    example = yaml.safe_load(example_path.read_text()) or {}
    if not isinstance(example, dict):
        raise SystemExit(f"{example_path} is not a YAML mapping")

    changed = []
    for pair in args.pairs:
        if "=" not in pair:
            raise SystemExit(f"bad pair {pair!r}; expected dotted.key=value")
        key, raw = pair.split("=", 1)
        if not key_present_in_example(example, key):
            raise SystemExit(f"key {key!r} is not in the example {example_path.name}; "
                             f"refusing to create a key Hermes would ignore")
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            value = raw
        current = get_path(data, key)
        if isinstance(current, list) and isinstance(value, list):
            missing = [v for v in value if v not in current]
            if not missing:
                print(f"  ok       {key} (already present)")
                continue
            set_path(data, key, missing)
            changed.append(f"added {missing} to {key}")
        elif current == value:
            print(f"  ok       {key}")
        else:
            set_path(data, key, value)
            changed.append(f"{key} = {value!r}")

    if not changed:
        print("no change: every requested key already holds its value")
        return 0
    for line in changed:
        print(f"  set      {line}")
    config.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False, width=1000))
    print(f"wrote {config}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
