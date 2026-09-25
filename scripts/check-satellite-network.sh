#!/bin/sh
# Report what this satellite's network looks like, and whether the bridge is reachable from here.
#
# Run it on the device, whichever network it is on:
#
#     ssh wyoming1 'sh ~/check-network.sh'          # from home
#     sh /home/wyoming1/check-network.sh            # on the device itself
#
# It answers three questions a person actually has: which network did it join, what address does it have,
# and can it reach the bridge from here. It does not change anything.
set -u

BRIDGE_HOST="${BRIDGE_HOST:-192.168.0.142}"
# Ports worth testing from the device's side. 22 is the host's SSH, 8765 is the bridge's announcement
# endpoint (the one service it exposes on the LAN today), and 10700 is the satellite port today and the
# registry listener after issue #61 lands. Testing the satellite's own port from the device proves nothing,
# which is what the first version of this script got wrong.
BRIDGE_PORTS="${BRIDGE_PORTS:-22 8765 10700}"

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
    priority=$(grep -m1 '^autoconnect-priority=' "$f" 2>/dev/null | cut -d= -f2)
    printf '    %-34s priority=%s\n' "$(basename "$f" .nmconnection)" "${priority:-<none>}"
done

echo
echo "=== can this device reach the bridge host $BRIDGE_HOST? ==="
echo "  route: $(ip route get "$BRIDGE_HOST" 2>/dev/null | head -1)"
if ping -c 2 -W 2 "$BRIDGE_HOST" >/dev/null 2>&1; then
    echo "  ping:  reachable"
else
    echo "  ping:  no answer (the host may filter ICMP; the port tests below are what count)"
fi
for port in $BRIDGE_PORTS; do
    case "$port" in
        22)    what="host SSH" ;;
        8765)  what="bridge announcements (today)" ;;
        10700) what="voice port: the satellite's today, the registry listener after #61" ;;
        *)     what="" ;;
    esac
    if timeout 4 sh -c "</dev/tcp/$BRIDGE_HOST/$port" 2>/dev/null; then
        printf '  port %-6s open        %s\n' "$port" "$what"
    else
        printf '  port %-6s not open    %s\n' "$port" "$what"
    fi
done

echo
echo "=== what this means ==="
cat <<'NOTE'
  The device joining a network is step one. For the voice link itself: today the bridge dials the
  satellite at its home address, so a device sitting behind a travel router cannot be reached and will
  sit idle. The device dialling the bridge (issue #61) is what makes a remote network work, and that is
  not wired in yet. This report proves the network half only: whichever network it is on, this device
  can reach the bridge host.
NOTE
