"""Shared voice-library implementation for the host and installer, without extra dependencies."""
import sys
from pathlib import Path


def library():
    try:
        from hermes_installer import catalogue
    except ImportError:
        source = Path(__file__).resolve().parents[1] / 'packages/installer/src'
        sys.path.insert(0, str(source))
        from hermes_installer import catalogue
    return catalogue
