# Transport topology: setup is not the link

Two different things get called "connecting the satellite", and keeping them apart is what makes
the three transports below addable without redesign.

**Setup** is how a satellite is provisioned the first time. It is **USB, always**: the satellite is
cabled into a computer and certificates, wake model, Wi-Fi and the endpoint are written over that
cable. It happens once, and it is the primary setup path for every satellite.

**The link** is how the satellite reaches the gateway afterwards, every time it boots. It is
**whichever transport the installation can actually do**.

| Mode | Endpoint | The right choice when |
|---|---|---|
| Wi-Fi / IP | `tls://host:port`, or `tcp://host:port` on a trusted LAN | the usual case: the satellite is wireless and the gateway is on the network |
| USB | `usb://localhost` | the satellite is cabled straight into the machine running the gateway, so the link is local by definition |
| Bluetooth | `bt://<address>` | there is no usable network path, but the radio is there |

The endpoint names the transport. It is not a host and a port that happen to be prefixed with a
scheme, which is why typing only an IP address is enough for the network case: the installer fills
in `tls://` and the registry port and says what it resolved to. A port given explicitly is always
kept.

## Why the endpoint is a transport

If the endpoint were "an address", then a cabled satellite would have to pretend to be on a network,
and a Bluetooth one would have to pretend to have an IP. Making the scheme mean "how this link is
carried" keeps each mode honest and means adding one is additive.

`guided_normalise_endpoint` in `scripts/lib/guided.sh` is the one place that understands the
shapes. It already produces `usb://localhost` from `usb` and `bt://` from `bluetooth`, so landing a
transport later needs no vocabulary change.

## Status, stated plainly

**Network is carried end to end.** The link's transport enum has `PROVISION_TRANSPORT_TCP` and
`PROVISION_TRANSPORT_TLS` today; the firmware dials the registry and the Gateway accepts it.

**USB is half built.** The device side is real: `firmware/esp32/main/usb_wyoming.c` is a working
Wyoming-over-USB adapter on the same serial channel the provisioning console uses, entered with the
console command `START_WYOMING`, and `scripts/test_esp32_usb_link.py` exercises it against SDK
shims (control registration over a fragmented adapter). The **host side is missing**: the Gateway has
no USB acceptor, so nothing listens for a cabled satellite. The endpoint vocabulary also had no
`usb://` shape, so a cabled satellite could not even be described.

**Bluetooth is absent** in both firmware and bridge.

So `usb://` and `bt://` are named but not fully carried, and an installer asked for one says so and
asks for an IP address instead. That refusal is deliberate: accepting a link that cannot complete
would hand someone a satellite that fails much later looking like a network problem.

The refusal lives in exactly one arm of `guided_valid_endpoint_strict`, plus the corresponding check
on `--endpoint`. For USB, completing the transport is a host-side acceptor plus that deletion; the
device half already speaks the protocol.

**Transport-layer specifics for USB and Bluetooth are deliberately not designed here yet.** The
current work is the network transport and the setup installers. What follows is the shape of the
decisions so they are made on purpose rather than inherited.

## Decisions to make on purpose when a transport lands

**Does a `usb://localhost` link need mutual TLS?** Over the cable to `localhost` there is no network
eavesdropper, so encryption buys little. Identity still matters though: the gateway must know *which*
satellite is on the other end, and today that identity is the client certificate. Deciding to drop
TLS for the USB mode means deciding what replaces it as the identity, not just skipping a handshake.

**How does enrolment work when there is no network path?** The setup code exchange assumes the
satellite-side installer can reach the gateway's enrolment endpoint. On a cabled link that is the
same machine, which simplifies it, but the challenge-response step should not be quietly bypassed
just because the link is short.

**What does Bluetooth actually carry?** Which profile, what framing, what the address in
`bt://<address>` even is. This also has a flash cost: the application image is already most of its
partition, and adding a radio stack may force a chip or partition change.

**How does the gateway advertise a transport it can accept?** A satellite choosing between Wi-Fi,
USB and Bluetooth needs to know which the gateway is actually listening on, rather than trying all
three.

## Where to read next

| For | Read |
|---|---|
| the pipeline, stage by stage | `architecture.md` |
| why the device dials out rather than being dialed | `outbound-link.md` |
| the wire protocol the link carries | `wyoming-protocol.md` |
| the installers that set all of this up | `installation.md` |
