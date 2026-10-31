#!/bin/sh
# Report what this satellite's network looks like, and whether the Gateway is reachable from here.
#
# Run it on the device, whichever network it is on:
#
#     ssh <satellite> 'sh ~/check-network.sh'     # from home
#     sh ~/check-network.sh                        # on the device itself
#
# It answers three questions a person actually has: which network did it join, what address does it have,
# and can it reach the Gateway from here. It does not change anything.
set -u

BRIDGE_HOST="${BRIDGE_HOST:?set BRIDGE_HOST to the Gateway address, e.g. BRIDGE_HOST=10.0.0.5 sh check-network.sh}"
# Ports worth testing from the device's side. 22 is the host's SSH, 8765 is the Gateway's announcement
# endpoint, and 10701 is the registry listener the inversion adds. The device's own 10700 is deliberately
# absent: testing a port the device itself owns proves nothing about the network, which is what the first
# version of this script got wrong.
BRIDGE_PORTS="${BRIDGE_PORTS:-22 8765 10701}"

echo "=== identity ==="
echo "  host:     $(hostname)"
echo "  uptime:   $(uptime -p 2>/dev/null || uptime)"
echo "  boot id:  $(cat /proc/sys/kernel/random/boot_id 2>/dev/null | cut -c1-8)"

echo
echo "=== wi-fi ==="
echo "  active connection(s):"
nmcli -t -f NAME,TYPE,DEVICE,STATE con show --active 2>/dev/null | sed 's/^/    /'
echo "  ssid in use: $(nmcli -t -f ACTIVE,SSID dev wifi 2>/dev/null | grep '^yes' | cut -d: -f2 || echo '<none>')"
echo "  address:"
ip -4 addr show wlan0 2>/dev/null | grep inet | sed 's/^/    /'
echo "  default route: $(ip route show default | head -1)"
echo "  saved profiles and their priorities:"
for f in /etc/NetworkManager/system-connections/*.nmconnection; do
    [ -f "$f" ] || continue
    name=$(basename "$f" .nmconnection)
    # These files are root-only mode 600. If we cannot read one, say so: reporting an empty value would
    # read as "no priority set", which is a different (and misleading) statement.
    if [ -r "$f" ]; then
        priority=$(grep -m1 '^autoconnect-priority=' "$f" 2>/dev/null | cut -d= -f2)
        printf '    %-34s priority=%s\n' "$name" "${priority:-<none>}"
    else
        printf '    %-34s (needs root to read)\n' "$name"
    fi
done

echo
echo "=== can this device reach the Gateway host $BRIDGE_HOST? ==="
echo "  route: $(ip route get "$BRIDGE_HOST" 2>/dev/null | head -1)"
if ping -c 2 -W 2 "$BRIDGE_HOST" >/dev/null 2>&1; then
    echo "  ping:  reachable"
else
    echo "  ping:  no answer (the host may filter ICMP; the port tests below are what count)"
fi
for port in $BRIDGE_PORTS; do
    case "$port" in
        22)    what="host SSH" ;;
        8765)  what="Gateway announcements" ;;
        10701) what="registry listener (the inversion's port)" ;;
        *)     what="" ;;
    esac
    # bash is invoked explicitly, and not for style: /dev/tcp is a bash feature and this device's /bin/sh
    # is dash, so "sh -c '</dev/tcp/...'" fails every time and reports every port as closed.
    if timeout 4 bash -c "exec 3<>/dev/tcp/$BRIDGE_HOST/$port" 2>/dev/null; then
        printf '  port %-6s open        %s\n' "$port" "$what"
    else
        printf '  port %-6s not open    %s\n' "$port" "$what"
    fi
done

echo
echo "=== is the voice link doing anything? ==="
echo "  services:        satellite=$(systemctl is-active wyoming-satellite 2>/dev/null) shim=$(systemctl is-active wake-shim 2>/dev/null) leds=$(systemctl is-active 2mic_leds 2>/dev/null)"
if systemctl list-unit-files 2>/dev/null | grep -q outbound-satellite; then
    echo "  outbound runner: $(systemctl is-active outbound-satellite 2>/dev/null) (the inverted mode)"
else
    echo "  outbound runner: installed on disk, not yet enabled as a service (phase 5)"
fi
echo "  listening:"
ss -lntp 2>/dev/null | grep -E "10700|10400|10500" | sed 's/^/    /' || echo "    nothing on the voice ports"

echo
echo "=== what this means ==="
cat <<'NOTE'
  The device joining a network is step one. For the voice link itself: today the Gateway dials the
  satellite at its home address, so a device sitting behind a travel router cannot be reached and will
  sit idle. The device dialling the Gateway (issue #61) is what makes a remote network work, and that is
  not wired in yet. This report proves the network half only: whichever network it is on, this device
  can reach the Gateway host.
NOTE
