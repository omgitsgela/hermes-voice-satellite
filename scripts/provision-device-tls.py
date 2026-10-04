#!/usr/bin/env python3
"""Provision a satellite's TLS material into its credential region over the USB download path.

The device needs four things to reach the bridge over TLS: the CA that signed the bridge's
certificate, its own certificate, its own private key, and the fingerprint of the certificate it
should expect from the bridge. It also records the host and port to make the report lines readable.

None of it goes through the console. The console is a line protocol that echoes what it is sent, and
a private key sent through a line protocol is a private key in a log, a scrollback and a terminal
recording. This writes the material straight into flash instead and proves the result by reading it
back.

Two silent failures are checked before anything is written:

  * the device certificate's fingerprint matches the credential the bridge has on file for this
    device, so what is being written is what the bridge will accept rather than a near miss;
  * the region being written is the one the partition table names, read from partitions.csv rather
    than repeated here, and the write does not reach past it.

Nothing else in flash is touched: no erase, no bootloader, no partition table, no fuses.
"""
from pathlib import Path
import argparse
import base64
import hashlib
import json
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from serial_console import resolve_port  # noqa: E402
from tls_credential_blob import REGION_BYTES, build_blob, parse_blob, server_pin  # noqa: E402

PADDING = b'\xff'  # what an unwritten flash byte reads as, so the region's state is unambiguous


def pem_to_der(pem: bytes) -> bytes:
    """The DER form of a PEM block, without shelling out: this is the exact byte string mbedTLS
    hashes, and decoding it here keeps the fingerprint independent of an openssl version."""
    body = b''.join(line for line in pem.splitlines() if not line.startswith(b'-----'))
    return base64.b64decode(body)


def read_pem(path: Path) -> bytes:
    """A PEM block with exactly one terminating NUL, which is what the blob format carries: mbedTLS
    wants C strings, and a length that includes the terminator lets the device hand each block over
    without copying it."""
    pem = path.read_bytes()
    if b'\x00' in pem:
        raise SystemExit(f'{path} already contains a NUL byte; not a plain PEM file')
    if not pem.endswith(b'\n'):
        pem += b'\n'
    return pem + b'\x00'


def storage_offset(partitions_csv: Path) -> int:
    for line in partitions_csv.read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        fields = [field.strip() for field in line.split(',')]
        if fields and fields[0] == 'storage':
            offset = int(fields[3], 16)
            size = int(fields[4], 16)
            if size < REGION_BYTES:
                raise SystemExit('the storage partition is smaller than the credential region')
            return offset
    raise SystemExit(f'no storage partition in {partitions_csv}')


def check_registry(registry: Path, device: str, fingerprint: str) -> None:
    """The bridge's own record of which certificate belongs to which device, as a set of mappings
    from fingerprint to device name. Anything else here means the material on disk and the material
    the bridge will accept have diverged."""
    record = json.loads(registry.read_text())
    if record.get(fingerprint) != device:
        raise SystemExit(f'{registry} does not map {fingerprint} to {device} '
                         f'(it maps it to {record.get(fingerprint)!r})')


def esptool(port: str, arguments: list) -> None:
    command = [sys.executable, '-m', 'esptool', '--chip', 'esp32s3', '--port', port,
               '--after', 'no-reset'] + arguments
    subprocess.run(command, check=True, timeout=300)


def _port(value: str) -> int:
    """A TCP port: 1-65535. The device's blob validator refuses port 0 and anything above
    65535 (tls_credentials_parse), so an out-of-range value is refused here, before a byte is
    written, rather than read back clean and reported PROVISIONED for a blob the device will
    never load."""
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f'{value} is not a TCP port (1-65535)')
    return port


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--certs', type=Path, required=True,
                        help='directory holding client-ca.crt, <device>.crt, <device>.key and the '
                             'bridge server certificate')
    parser.add_argument('--device', required=True, help='device name, as the bridge knows it')
    parser.add_argument('--server-cert', default='server.crt',
                        help='file in --certs holding the bridge certificate to pin')
    parser.add_argument('--host', required=True, help='the address the device should dial')
    parser.add_argument('--port', type=_port, required=True, help='the port the device should dial (1-65535)')
    parser.add_argument('--partitions', type=Path,
                        default=Path(__file__).resolve().parent.parent / 'firmware/esp32/partitions.csv')
    parser.add_argument('--registry', type=Path,
                        help='state/device-credentials.json, to check the certificate is the one the '
                             'bridge has on file for this device')
    parser.add_argument('--port-device',
                        help='ESP32 USB serial device for --write; or set HERMES_SATELLITE_SERIAL')
    parser.add_argument('--out', type=Path, default=Path('tls-provisioning'),
                        help='where to write the blob and the record of what was done')
    parser.add_argument('--write', action='store_true',
                        help='actually write to the device; without this the run only builds and '
                             'checks, and says what it would write')
    arguments = parser.parse_args()

    certs = arguments.certs
    ca_pem = read_pem(certs / 'client-ca.crt')
    cert_pem = read_pem(certs / f'{arguments.device}.crt')
    key_pem = read_pem(certs / f'{arguments.device}.key')
    server_pem = read_pem(certs / arguments.server_cert)

    fingerprint = 'sha256:' + hashlib.sha256(pem_to_der(cert_pem)).hexdigest()
    pin = server_pin(pem_to_der(server_pem))
    if arguments.registry:
        check_registry(arguments.registry, arguments.device, fingerprint)
        print(f'certificate matches the bridge registry entry for {arguments.device}')

    blob = build_blob(ca_pem, cert_pem, key_pem, pin, arguments.host, arguments.port)
    region = blob.ljust(REGION_BYTES, PADDING)
    # Round-trip through the device's own validator before anything is written: a blob that the
    # device would reject must never reach flash.
    parsed = parse_blob(region)
    assert parsed['pin'] == pin and parsed['host'] == arguments.host and parsed['port'] == arguments.port

    offset = storage_offset(arguments.partitions)
    if offset + REGION_BYTES > 8 * 1024 * 1024:
        raise SystemExit('the credential region does not fit the 8 MB of fitted flash')

    arguments.out.mkdir(parents=True, exist_ok=True)
    region_path = arguments.out / 'credential-region.bin'
    region_path.write_bytes(region)
    record = {
        'device': arguments.device,
        'certificate_fingerprint': fingerprint,
        'server_pin': pin.hex(),
        'host': arguments.host,
        'port': arguments.port,
        'region_offset': hex(offset),
        'region_bytes': REGION_BYTES,
        'blob_bytes': len(blob),
        'region_sha256': hashlib.sha256(region).hexdigest(),
    }
    print(json.dumps(record, indent=2))
    print(f'blob built: {len(blob)} bytes, region padded to {REGION_BYTES} with 0xFF')
    print(f'region image: {region_path}')

    if not arguments.write:
        print('dry run: nothing written. Re-run with --write to provision the device.')
        return 0

    # The device port is needed only now: a dry run never opens it, and its source is an argument
    # or the environment, never a path baked into this tree.
    device_port = resolve_port(arguments.port_device)

    # A window one 4 KB sector wider than the region, so the readback can show both that the region
    # holds what was written and that the byte after it was never part of the write.
    window = REGION_BYTES + 4096
    before_path = arguments.out / 'before-region.bin'
    before_path.unlink(missing_ok=True)
    esptool(device_port, ['read-flash', '--no-progress', hex(offset), str(window),
                          str(before_path)])
    before = before_path.read_bytes()
    print(f'replaced region: sha256={hashlib.sha256(before[:REGION_BYTES]).hexdigest()[:16]} '
          f'(first bytes {before[:8].hex()})')

    esptool(device_port, ['write-flash', '--no-progress', '--flash-mode', 'keep',
                          '--flash-freq', 'keep', '--flash-size', 'keep',
                          hex(offset), str(region_path)])

    after_path = arguments.out / 'after-region.bin'
    esptool(device_port, ['read-flash', '--no-progress', hex(offset), str(window),
                          str(after_path)])
    after = after_path.read_bytes()
    if after[:REGION_BYTES] != region:
        raise SystemExit('readback of the credential region differs from what was written')
    if after[REGION_BYTES:] != before[REGION_BYTES:]:
        raise SystemExit('the bytes past the credential region changed, so the write ran past it')
    print('readback byte-identical to the region image, and the 4 KB beyond it is unchanged')

    if arguments.registry:
        # The device validates its own blob at load; this repeats that check on the bytes that are
        # now in flash rather than on the ones that were staged.
        parse_blob(after[:REGION_BYTES])
        print('the region in flash parses as a valid credential blob')

    (arguments.out / 'provision-record.json').write_text(json.dumps(
        {**record, 'written': True,
         'replaced_region_sha256': hashlib.sha256(before[:REGION_BYTES]).hexdigest(),
         'readback_sha256': hashlib.sha256(after[:REGION_BYTES]).hexdigest()}, indent=2) + '\n')
    print(f'PROVISIONED {arguments.device} at {hex(offset)}: {REGION_BYTES} bytes, '
          f'pin={pin.hex()[:16]}...')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
