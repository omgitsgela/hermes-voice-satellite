#!/usr/bin/env python3
"""Offline regression for the voice prompt's mutation policy."""

from check_voice_prompt import check_prompt_clauses


def main() -> int:
    return 1 if check_prompt_clauses() else 0


if __name__ == "__main__":
    raise SystemExit(main())
