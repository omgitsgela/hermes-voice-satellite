#!/usr/bin/env python3
"""Generate the gateway's five-word satellite enrolment token.

One job: pick five words with `secrets.choice` from the bundled word list, store them at
`--out` mode 0600, and print the token ONCE to stdout for the operator to keep. The token is
what a person types when setting up a satellite, so it is words rather than hex; it is also the
key the credential bundle is sealed under, so it is generated with `secrets` and never `random`
(bridge/enroll.py holds the rules; this is only the install-time entry point).

    scripts/generate-enroll-token.py [--words scripts/data/enroll-words.txt] \
        --out state/enroll-token.txt

An existing token file is never overwritten: a regenerated token would strand every satellite
already enrolled under the old one. The bridge itself generates the token lazily on first start
if this script has not run (same shape as the announce token).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.enroll import generate_token, load_wordlist  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--words", default=None,
                        help="the word list (default: scripts/data/enroll-words.txt)")
    parser.add_argument("--out", required=True, help="where to store the token (mode 0600)")
    args = parser.parse_args()

    words_file = (Path(args.words) if args.words
                  else Path(__file__).resolve().parent / "data" / "enroll-words.txt")
    out = Path(args.out)
    if out.is_file() and out.read_text().strip():
        print(f"{out} already holds a token; leaving it untouched", file=sys.stderr)
        return 1
    token = generate_token(load_wordlist(words_file))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(token + "\n")
    out.chmod(0o600)
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
