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
# It installs five things: the outbound runner, the unit that runs it, the device's configuration
# file (satellite.env), the enablement symlink so the device dials the bridge on every boot, and a
# bootstrap env file for the values only a human can fill in. The unit is only enabled when those
# values are real: a card whose env file still says CHANGE-ME is left with the unit installed but
# disabled, rather than booting into a link that is guaranteed to fail.
#
# The second argument is the device user the satellite is installed under (default satellite).
#
# Mounting as your own user needs no sudo when the host has a polkit rule allowing udisks2 mount/unmount
# for removable media: an SSH session has no active desktop seat, which is why a plain `udisksctl mount`
# fails without one. The files on the card are owned by uid 1000, which is your uid locally, so the copy
# itself never needs root either.
set -eu

MOUNT="${1:-}"
DEVICE_USER="${2:-satellite}"
# Keep generated device paths and sed substitutions literal; reject path traversal/metacharacters.
case "$DEVICE_USER" in
    *[!a-zA-Z0-9_-]*|''|-*) echo 'FAIL: invalid device user' >&2; exit 2 ;;
esac
if [ -z "$MOUNT" ]; then
    echo "usage: $0 <mountpoint of the card's rootfs partition> [device-user]" >&2
    exit 2
fi
DEVICE_USER_HOME="/home/$DEVICE_USER"
CARD_USER_HOME="$MOUNT$DEVICE_USER_HOME"
if [ ! -d "$CARD_USER_HOME/wyoming-satellite/examples" ]; then
    echo "FAIL: $MOUNT does not look like the satellite's rootfs" >&2
    echo "      expected $CARD_USER_HOME/wyoming-satellite/examples to exist" >&2
    exit 1
fi

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH= cd -- "$HERE/.." && pwd)
DEST="$CARD_USER_HOME/wyoming-satellite/examples"
ENV_FILE="$CARD_USER_HOME/bridge-link.env"
UNIT_SRC="$REPO/pi/systemd/outbound-satellite.service.in"
UNIT_DEST="$MOUNT/etc/systemd/system/outbound-satellite.service"
WANTS_DIR="$MOUNT/etc/systemd/system/multi-user.target.wants"

# The paths the unit is installed with. These are the card's paths, not the build host's.
DEVICE_PYTHON="$DEVICE_USER_HOME/wyoming-satellite/.venv/bin/python"
DEVICE_RUNNER="$DEVICE_USER_HOME/wyoming-satellite/examples/outbound_satellite.py"

echo "=== source: $HERE"
echo "=== destination: $DEST"
echo "=== device user: $DEVICE_USER"

# The runner lives in the repo at pi/outbound_satellite.py. A staging directory that holds a copy next to
# this script also works, which is how the first hand deployment was done.
if [ -f "$HERE/outbound_satellite.py" ]; then
    RUNNER_DIR="$HERE"
else
    RUNNER_DIR="$REPO/pi"
fi

for f in outbound_satellite.py config_agent.py device_apply.py volume_control.py mic_gain_control.py wake_shim.py 2mic_service.py inject_wake.py led_cmd.py; do
    if [ ! -f "$RUNNER_DIR/$f" ]; then
        echo "FAIL: missing device module $RUNNER_DIR/$f" >&2
        exit 1
    fi
    cp "$RUNNER_DIR/$f" "$DEST/$f"
    chmod 0644 "$DEST/$f"
    echo "  copied $f"
done

# The device's one configuration file. The repo copy is a template with <...> placeholders; if the card
# already has a filled-in file, keep it (it holds this device's real values). If it does not, install the
# template and say so plainly: the units will not work until a person fills it in.
SATENV_DIR="$MOUNT/etc/voice-satellite"
SATENV_DEST="$SATENV_DIR/satellite.env"
if [ -f "$SATENV_DEST" ]; then
    echo "  kept the existing /etc/voice-satellite/satellite.env (it holds the device's real values)"
else
    mkdir -p "$SATENV_DIR"
    cp "$REPO/pi/systemd/satellite.env" "$SATENV_DEST"
    chmod 0644 "$SATENV_DEST"
    echo "  wrote /etc/voice-satellite/satellite.env from the template"
    echo "  FILL IT IN: it still carries <...> placeholders, and the units will not start until it is real"
fi

# The link's bootstrap configuration: identity and where to dial, nothing else. Mode 0600 because a
# deployment may point at certificate material from here.
if [ -f "$ENV_FILE" ]; then
    echo "  kept the existing bridge-link.env (it holds the real values)"
else
    cat > "$ENV_FILE" <<EOF
# Bootstrap configuration for the outbound shape: the device dials the bridge.
# This file is never committed.
VOICE_DEVICE_ID=CHANGE-ME
VOICE_BRIDGE_HOST=CHANGE-ME
VOICE_BRIDGE_PORT=10701
VOICE_DEVICE_NAME=$DEVICE_USER
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
for f in outbound_satellite.py config_agent.py device_apply.py volume_control.py mic_gain_control.py wake_shim.py 2mic_service.py inject_wake.py led_cmd.py; do
    bytes=$(wc -c < "$DEST/$f" | tr -d ' ')
    printf '  %-34s %s bytes  mode %s\n' "$f" "$bytes" "$(stat -c %a "$DEST/$f")"
done
printf '  %-34s %s bytes  mode %s\n' "bridge-link.env" "$(wc -c < "$ENV_FILE" | tr -d ' ')" "$(stat -c %a "$ENV_FILE")"
printf '  %-34s %s bytes  mode %s\n' "satellite.env" "$(wc -c < "$SATENV_DEST" | tr -d ' ')" "$(stat -c %a "$SATENV_DEST")"
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
