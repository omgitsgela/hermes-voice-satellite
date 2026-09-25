#!/bin/sh
# Put the device-side files onto a mounted satellite SD card.
#
#   udisksctl mount -b /dev/sdd2          # prints the mount point, e.g. /run/media/<user>/rootfs
#   sh deploy-to-card.sh /run/media/<user>/rootfs
#
# Run this ON THE MACHINE HOLDING THE CARD (a laptop with a reader), not on the bridge host. Idempotent:
# re-running copies the same files again and changes nothing else. It does not switch the device over to
# the new shape - that is a deliberate, separate step (migration phase 5).
#
# Mounting as your own user needs no sudo when the host has a polkit rule allowing udisks2 mount/unmount
# for removable media: an SSH session has no active desktop seat, which is why a plain `udisksctl mount`
# fails without one. The files on the card are owned by uid 1000, which is your uid locally, so the copy
# itself never needs root either.
#
# The files land where the rest of our device-side code lives, a sibling of the wake shim and the LED
# service, so a rebuild from the gold image only needs this script re-run.
set -eu

MOUNT="${1:-}"
if [ -z "$MOUNT" ]; then
    echo "usage: $0 <mountpoint of the card's rootfs partition>" >&2
    exit 2
fi
if [ ! -d "$MOUNT/home/wyoming1/wyoming-satellite/examples" ]; then
    echo "FAIL: $MOUNT does not look like the satellite's rootfs" >&2
    echo "      expected $MOUNT/home/wyoming1/wyoming-satellite/examples to exist" >&2
    exit 1
fi

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEST="$MOUNT/home/wyoming1/wyoming-satellite/examples"

echo "=== source: $HERE"
echo "=== destination: $DEST"

for f in outbound_satellite.py; do
    if [ ! -f "$HERE/$f" ]; then
        echo "FAIL: $HERE/$f is missing" >&2
        exit 1
    fi
    cp "$HERE/$f" "$DEST/$f"
    chmod 0644 "$DEST/$f"
    echo "  copied $f"
done

# A template configuration, so the device has something to read once phase 5 wires the runner into a unit.
# It is NOT read by anything today: nothing starts the runner, and the device still boots in its server
# shape. Placeholders only, and it is deliberately outside the repo so real values never get committed.
ENV_FILE="$MOUNT/home/wyoming1/bridge-link.env"
if [ -f "$ENV_FILE" ]; then
    echo "  kept the existing bridge-link.env"
else
    cat > "$ENV_FILE" <<'EOF'
# Bootstrap configuration for the outbound (device-dials-bridge) shape. Nothing reads this yet.
# Phase 5 of issue #61 wires pi/outbound_satellite.py into a systemd unit that will.
VOICE_DEVICE_ID=wyoming1
VOICE_BRIDGE_HOST=CHANGE-ME
VOICE_BRIDGE_PORT=10700
VOICE_DEVICE_NAME=wyoming1
VOICE_DEVICE_AREA=
# For TLS: VOICE_BRIDGE_TLS_CA, and VOICE_BRIDGE_TLS_CERT/KEY for mutual TLS identity.
EOF
    chmod 0644 "$ENV_FILE"
    echo "  wrote a placeholder bridge-link.env"
fi

echo
echo "=== verification (read back from the card) ==="
for f in outbound_satellite.py; do
    bytes=$(wc -c < "$DEST/$f" | tr -d ' ')
    printf '  %-32s %s bytes  mode %s\n' "$f" "$bytes" "$(stat -c %a "$DEST/$f")"
done
printf '  %-32s %s bytes  mode %s\n' "bridge-link.env" "$(wc -c < "$ENV_FILE" | tr -d ' ')" "$(stat -c %a "$ENV_FILE")"

echo
echo "Done. Unmount before removing the card:  udisksctl unmount -b /dev/sdd2"
echo "Nothing on the device uses these files yet; the device still boots in its server shape."
