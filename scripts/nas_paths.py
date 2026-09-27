"""Shared explicit target selection for the mirror and its destructive prune step."""
import argparse
import os
from pathlib import Path


def mirror_root(*, allow_apply=False):
    parser = argparse.ArgumentParser(description='Use an explicitly mounted NAS parent directory')
    parser.add_argument('mount', nargs='?', help='parent of hermes-voice-satellite on the mounted share')
    if allow_apply:
        parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    target = (args.mount or os.environ.get('VOICE_NAS_MOUNT', '').strip()
              or os.environ.get('VOICE_NAS_MIRROR_ROOT', '').strip())
    # Never discover an arbitrary share, or silently fall back to a local backup directory.
    if not target:
        return None
    return Path(target) / 'hermes-voice-satellite'
