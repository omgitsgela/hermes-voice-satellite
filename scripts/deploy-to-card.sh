#!/bin/sh
# Put the device-side files onto a mounted satellite SD card.
#
#   udisksctl mount -b /dev/sdd2          # prints the mount point, e.g. /run/media/<user>/rootfs
#   sh deploy-to-card.sh /run/media/<user>/rootfs
#
# Run this ON THE MACHINE HOLDING THE CARD (a laptop with a reader), not on the bridge host. Idempotent:
# re-running copies the same files again and changes nothing else. It never overwrites an existing
# bridge-link.env, because that file holds the real values a person had to supply.
#
# It installs four things: the outbound runner, the unit that runs it, the enablement symlink so the device
# dials the bridge on every boot, and a bootstrap env file for the values only a human can fill in. The unit
# is only enabled when those values are real: a card whose env file still says CHANGE-ME is left with the
# unit installed but disabled, rather than booting into a link that is guaranteed to fail.
#
# Mounting as your own user needs no sudo when the host has a polkit rule allowing udisks2 mount/unmount
# for removable media: an SSH session has no active desktop seat, which is why a plain `udisksctl mount`
# fails without one. The files on the card are owned by uid 1000, which is your uid locally, so the copy
# itself never needs root either.
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
REPO=$(CDPATH= cd -- "$HERE/.." && pwd)
DEST="$MOUNT/home/wyoming1/wyoming-satellite/examples"
ENV_FILE="$MOUNT/home/wyoming1/bridge-link.env"
UNIT_SRC="$REPO/pi/systemd/outbound-satellite.service.in"
UNIT_DEST="$MOUNT/etc/systemd/system/outbound-satellite.service"
WANTS_DIR="$MOUNT/etc/systemd/system/multi-user.target.wants"

# The paths the unit is installed with. These are the card's paths, not the build host's.
DEVICE_USER_HOME="/home/wyoming1"
DEVICE_PYTHON="$DEVICE_USER_HOME/wyoming-satellite/.venv/bin/python"
DEVICE_RUNNER="$DEVICE_USER_HOME/wyoming-satellite/examples/outbound_satellite.py"

echo "=== source: $HERE"
echo "=== destination: $DEST"

# The runner lives in the repo at pi/outbound_satellite.py. A staging directory that holds a copy next to
# this script also works, which is how the first hand deployment was done.
if [ -f "$HERE/outbound_satellite.py" ]; then
    RUNNER_SRC="$HERE/outbound_satellite.py"
else
    RUNNER_SRC="$REPO/pi/outbound_satellite.py"
fi

for f in outbound_satellite.py; do
    if [ ! -f "$RUNNER_SRC" ]; then
        echo "FAIL: no outbound_satellite.py beside this script, and none at $RUNNER_SRC" >&2
        exit 1
    fi
    cp "$RUNNER_SRC" "$DEST/$f"
    chmod 0644 "$DEST/$f"
    echo "  copied $f"
done

# The device's bootstrap configuration: identity and where to dial, nothing else. Mode 0600 because a
# deployment may point at certificate material from here.
if [ -f "$ENV_FILE" ]; then
    echo "  kept the existing bridge-link.env (it holds the real values)"
else
    cat > "$ENV_FILE" <<'EOF'
# Bootstrap configuration for the outbound shape: the device dials the bridge.
# This file is the only place real values live, and it is never committed.
VOICE_DEVICE_ID=CHANGE-ME
VOICE_BRIDGE_HOST=CHANGE-ME
VOICE_BRIDGE_PORT=10701
VOICE_DEVICE_NAME=
VOICE_DEVICE_AREA=
VOICE_CAPABILITIES=led,speaker,microphone
# TLS, when the bridge terminates it: VOICE_BRIDGE_TLS_CA, and VOICE_BRIDGE_TLS_CERT/KEY for mutual TLS.
EOF
    chmod 0600 "$ENV_FILE"
    echo "  wrote a placeholder bridge-link.env (fill in VOICE_DEVICE_ID and VOICE_BRIDGE_HOST)"
fi

# The unit, from the repo template, with the card's paths substituted in.
if [ ! -f "$UNIT_SRC" ]; then
    echo "FAIL: $UNIT_SRC is missing" >&2
    exit 1
fi
mkdir -p "$MOUNT/etc/systemd/system"
ENV_FILE_ON_DEVICE="$DEVICE_USER_HOME/bridge-link.env"
sed -e "s|__ENV_FILE__|$ENV_FILE_ON_DEVICE|g" \
    -e "s|__PYTHON__|$DEVICE_PYTHON|g" \
    -e "s|__RUNNER__|$DEVICE_RUNNER|g" \
    "$UNIT_SRC" > "$UNIT_DEST"
chmod 0644 "$UNIT_DEST"
echo "  wrote outbound-satellite.service"

# Enable it only when the env file has been filled in, so a card never boots dialling CHANGE-ME.
PLACEHOLDER=no
grep -q '^VOICE_BRIDGE_HOST=CHANGE-ME' "$ENV_FILE" 2>/dev/null && PLACEHOLDER=yes
if [ "$PLACEHOLDER" = no ]; then
    mkdir -p "$WANTS_DIR"
    ln -sf ../outbound-satellite.service "$WANTS_DIR/outbound-satellite.service"
    echo "  enabled outbound-satellite.service"
else
    echo "  NOT enabled: bridge-link.env still says CHANGE-ME, so the link would fail on every boot"
    echo "              fill it in, re-run this script, or enable it on the device with systemctl"
fi

echo
echo "=== verification (read back from the card) ==="
for f in outbound_satellite.py; do
    bytes=$(wc -c < "$DEST/$f" | tr -d ' ')
    printf '  %-34s %s bytes  mode %s\n' "$f" "$bytes" "$(stat -c %a "$DEST/$f")"
done
printf '  %-34s %s bytes  mode %s\n' "bridge-link.env" "$(wc -c < "$ENV_FILE" | tr -d ' ')" "$(stat -c %a "$ENV_FILE")"
printf '  %-34s %s bytes\n' "outbound-satellite.service" "$(wc -c < "$UNIT_DEST" | tr -d ' ')"
if [ -e "$WANTS_DIR/outbound-satellite.service" ]; then
    echo "  enablement: linked (the device will dial the bridge on boot)"
else
    echo "  enablement: not linked (see above)"
fi
if [ -x "$MOUNT$DEVICE_PYTHON" ]; then
    echo "  interpreter: present on the card"
else
    echo "  interpreter: MISSING at $DEVICE_PYTHON - the unit will fail to start"
fi

echo
echo "Done. Unmount before removing the card:  udisksctl unmount -b /dev/sdd2"
