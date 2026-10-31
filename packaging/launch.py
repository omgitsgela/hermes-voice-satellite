"""Entry point for the packaged installer.

PyInstaller needs a script to analyse; this is the smallest honest one. Everything else lives in the
packages, so the packaged build runs exactly the code the checkout runs.
"""
from __future__ import annotations

import sys

from hermes_installer.cli import main

if __name__ == "__main__":
    sys.exit(main())
