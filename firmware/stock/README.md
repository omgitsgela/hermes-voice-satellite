# Stock firmware: what a device shipped with, and how to put it back

This directory holds the images that return an XVF3800 four-mic satellite to the state it had when
it left the shelf. It exists because flashing is reversible only if the before-picture is kept:
development on a device is safe precisely because a verified restore is one command away.

Read [`../esp32/README.md`](../esp32/README.md) first for how the project firmware is built and
flashed. This file covers the other direction.

## The two images

| File | Size | SHA-256 | What it is |
|---|---|---|---|
| `esp32-full-flash-8mb.bin` | 8,388,608 B | `afe7b55ef757e56434c6f20add6db9604ffe951c1e24742cb6def5248b6c6362` | Full-chip image of the ESP32-S3, read from a stock device before any development flash. Contains the Seeed vendor application, bootloader, partition table, and empty NVS/PHY regions. |
| `xmos-official-v1.0.4-dfu.bin` | 888,832 B | `6c66525116ae085ca7bf80f7e343ca1a879d190152edb064c408139f044ab2c3` | The official ReSpeaker XMOS/XVF3800 co-processor image (i2s DFU v1.0.4), byte-identical to what the stock devices shipped with, and byte-identical to the image read out of the chip on 2026-10-11 before development touched it. |
| `xmos-official-v1.0.9-48k-master-dfu.bin` | 888,832 B | `6380851ac59b3c2efc9799264bdd0a35b15e9112e5ae5e7b6af8b2b05bb02f23` | The official ReSpeaker 48 kHz **master** co-processor image (v1.0.9). Not a stock image — this is the co-processor state the project's *current* ESP32 firmware is built against. Included because a fresh install onto a stock device needs it; see "Co-processor coupling" below. |

Provenance of the ESP32 dump: taken 2026-10-02 from the board whose Wi-Fi MAC was
`68:ee:8f:4b:59:88` (the MAC is derived by the SDK at runtime from the eFuse block and is *not*
stored in the application image). It was read twice, without rebooting between reads; both reads
are exactly 8,388,608 bytes and byte-identical. An independent copy lives in the project's
evidence archive on the NAS. Secure boot and flash encryption were verified off before the read
(`SPI_BOOT_CRYPT_CNT` = 0), and no eFuse was ever written.

A byte-level scan of the dump found no Wi-Fi credentials, no TLS material, and no device-name
strings: NVS at `0x9000` is empty except two small ESP-IDF metadata records, the PHY-calibration
region at `0x1100000` is blank, and the application region holds only compiled strings. It is safe
to distribute with the repository.

Upstream provenance of both co-processor images: ReSpeaker's official firmware repository,
`reSpeaker_XVF3800_USB_4MIC_ARRAY/xmos_firmwares/i2s/` — `old_firmwares/respeaker_xvf3800_i2s_dfu_firmware_v1.0.4.bin`
(v1.0.4: verified byte-identical to a fresh upstream download on 2026-11-02, and byte-identical to
the image read out of a stock device's chip before development) and
`respeaker_xvf3800_i2s_master_v1.0.9_48k.bin` (v1.0.9: fresh upstream download 2026-11-02; the same
image written to a real device via DFU on 2026-10-28, after which the chip's own version readout
went `01 00 04` → `01 00 09`). See
[`docs/hardware/seeed-wiki-references.md`](../../docs/hardware/seeed-wiki-references.md) for the
attribution trail.

## What "stock" means, and why both chips matter

The device is two processors: the **ESP32-S3** (Wi-Fi, Wyoming client, wake scoring) and the
**XVF3800 co-processor** on XMOS (beamforming, echo cancellation, noise suppression, AGC). Each is
flashed separately, and each restore is independent.

- **Restore the ESP32 alone** (`esp32-full-flash-8mb.bin` at `0x0`): the board goes back to the
  Seeed vendor application. It stops talking to the Satellite Gateway entirely and becomes
  inert as a network device. This is the right move when handing a board to someone else or
  when you want a blank slate before a fresh project install.
- **The XMOS is not required for the ESP32 restore to "work"**, because the vendor ESP32 image
  predates any XMOS change we made and boots fine against whatever the co-processor holds. But
  a *fully* stock device — identical to the out-of-box state on both chips — needs both writes,
  because development (the 48 kHz line) also wrote the co-processor once, to the official
  v1.0.9 48 kHz master image via DFU.
- **To run the project**, only the ESP32 side is normally touched: application region at
  `0x10000` plus the model partition, never a chip erase. A project-converted device therefore
  usually has a stock XMOS unless someone deliberately updated it; `PROVISION`'s bring-up line at
  boot reports the co-processor version (`version=1.0.4` stock, `version=1.0.9` updated), and
  that line is how you tell which state you are in without opening anything.

So the honest answer: **reflashing to stock is one ESP32 write for a silent, hand-it-over device;
it is two writes (ESP32 + co-processor) for a verifiably out-of-box device.**

## Co-processor coupling: what the project firmware actually needs

The two chips' I2S roles are **complementary and rate-matched**, and the pairing is not optional:

| Co-processor image | Role/rate | Project ESP32 line that works against it |
|---|---|---|
| v1.0.4 (stock) | 16 kHz **slave** | the historical 16 kHz line (superseded; do not start there) |
| v1.0.9 48k master (`xmos-official-v1.0.9-48k-master-dfu.bin`) | 48 kHz **master** | **the current firmware** — the ESP32 runs I2S slave at 48 kHz, capture is native 48 kHz, decimated once on-device to the 16 kHz the wake model and uplink use |

Driving the current 48 kHz ESP32 against a stock v1.0.4 co-processor leaves the microphone pipeline
dead: the clock roles collide and the link never runs (`i2s=init_failed` on bring-up, silent capture
after). This was found the hard way on 2026-10-28 and is the reason a fresh install onto a
**stock** device is a two-write operation, not one: flash the ESP32 project regions, *then* do the
Safe Mode DFU write of `xmos-official-v1.0.9-48k-master-dfu.bin` exactly as the restore section
above describes (same procedure, other image). The device's own boot bring-up line reports the
co-processor version (`version=1.0.4` stock vs `version=1.0.9` project pairing) — read that line
before troubleshooting anything else; it tells you which side of the pairing you are on.

Conversely: a device restored to stock on both chips can never talk to the Satellite Gateway, and
a device whose ESP32 was restored to stock with the co-processor left at v1.0.9 boots the vendor
demo application — the co-processor version is only observable from *our* firmware's bring-up
line, and the vendor application's compatibility with the 48 kHz master image has never been
tested. If you want a board truly out-of-box, restore both chips.

## Restore procedure: project → stock

You need: the ESP32's USB-C port cabled to a host with `esptool` (the laptop flow in
[`docs/installation.md`](../../docs/installation.md) works; no button sequence is needed for the
ESP32 side).

```sh
# 1. Capture the current state first, so the restore itself is reversible.
esptool --chip esp32s3 -p /dev/ttyACM0 -b 460800 \
    read-flash 0 8388608 board-current-state.bin

# 2. Verify the image you are about to write.
sha256sum esp32-full-flash-8mb.bin
# expect afe7b55ef757e56434c6f20add6db9604ffe951c1e24742cb6def5248b6c6362

# 3. Write the full image at 0x0. This is the one case where writing the whole chip
#    (covering the partition table) is correct: it restores the factory layout.
esptool --chip esp32s3 -p /dev/ttyACM0 -b 460800 \
    write-flash 0 esp32-full-flash-8mb.bin

# 4. Verify by an independent read-back, never the writer's word alone.
esptool --chip esp32s3 -p /dev/ttyACM0 -b 460800 \
    read-flash 0 8388608 board-after.bin
sha256sum board-after.bin   # must equal the image hash above
```

This procedure was exercised on a real device on 2026-11-02: current state captured
(`b15b341b...`), full image written, independent read-back byte-identical to the factory dump,
and the device confirmed silent to the gateway afterward.

Restore the co-processor only if the bring-up line says `version=1.0.9` and you want true
out-of-box state. This one needs the **XVF3800's own USB-C port** (the one next to the 3.5 mm
jack) and a physical Safe Mode entry: power off, hold Mute, re-apply power, keep holding until the
red LED blinks. Then:

```sh
dfu-util -l   # expect vendor 2886 product 001a, alt 1 = "reSpeaker DFU Upgrade"
sudo dfu-util -R -e -a 1 -D xmos-official-v1.0.4-dfu.bin
```

The `Invalid DFU suffix signature` warning is expected for this raw image and harmless; success
ends at `dfuMANIFEST` then `dfuIDLE` with `status(0) = No error condition is present`. Verify from
the ESP32 console afterwards: the bring-up line should read `version=1.0.4`. This write was
exercised in both directions on a real device (v1.0.4 → v1.0.9 on 2026-10-28, and the rollback
pair staged and hash-matched against exactly this file).

## Stock → project

There is no special "unlock": the stock ESP32 application is just an image at the same offsets the
project uses. Provision a fresh board through the USB console exactly as documented in
[`docs/installation.md`](../../docs/installation.md) — the provisioning console is the setup
surface for a bare board — then flash the reviewed release bundle's regions per
[`firmware/esp32/README.md`](../esp32/README.md). Then do the co-processor write: the current
firmware needs the 48 kHz master pairing (see "Co-processor coupling" above), so from a stock
device this is the Safe Mode + `dfu-util -a 1` procedure with
`xmos-official-v1.0.9-48k-master-dfu.bin`. Verify at first boot: the bring-up line should read
`version=1.0.9`, and a `version=1.0.4` there means the mic path will stay dead.

## Distribution note

These binaries are committed to the repository on purpose: a device image is the device's
undo-history, and an undo that lives only on one person's laptop is not an undo. The full-chip ESP32
dump passed the byte-level secret scan described above before it was committed, and
`scripts/check-no-personal-data.sh` keeps scanning every tracked file for installation values. The
co-processor image is redistributed from ReSpeaker's repository under its terms (see
`THIRD-PARTY.md`).
