#!/usr/bin/env python3
"""Host tests for scripts/bundle-esp32-flash: the one bundle-and-hash script.

Flashing is the one act in this project with teeth, and it waits for a gate. What the
gate needs is a reviewable artifact: the exact set of files that will be written to the
chip, at their exact offsets, with their sizes and hashes, produced from the build's own
flash map (flasher_args.json) rather than a list that drifts from it. bundle-esp32-flash
is that artifact: a manifest, an optional combined bundle (each component placed at its
flash offset, zero-padded between), and a --check that re-hashes the files against a
reviewed manifest. If a byte of the app changed after review, the check must fail; the
gate is a hash comparison, not a memory of what the build produced.
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'bundle-esp32-flash.py'
PY = sys.executable or '/usr/bin/python3'


def make_build(tmp, app=b'APPBYTES'):
    build = Path(tmp) / 'build'
    build.mkdir()
    bootloader = b'\x00' * 64 + b'BOOT'
    ptable = b'\x52\x71' + b'\x00' * 200
    (build / 'bootloader.bin').write_bytes(bootloader)
    (build / 'partition-table.bin').write_bytes(ptable)
    (build / 'app.bin').write_bytes(app)
    # The real shape idf.py writes: a dict of offset to path relative to the build dir.
    map_ = {"flash_files": {"0x0": "bootloader.bin",
                            "0x8000": "partition-table.bin",
                            "0x10000": "app.bin"}}
    (build / 'flasher_args.json').write_text(json.dumps(map_))
    return build, bootloader, ptable, app


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def run_bundle(args, cwd):
    return subprocess.run([PY, str(SCRIPT)] + args, capture_output=True, text=True, cwd=str(cwd))


class ManifestTest(unittest.TestCase):
    def test_manifest_lists_every_file_in_flash_order_with_size_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, bootloader, ptable, app = make_build(tmp)
            ran = run_bundle(['--build', str(build)], tmp)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            lines = [line for line in ran.stdout.splitlines() if line.strip()]
            self.assertEqual(len(lines), 3, ran.stdout)
            self.assertIn('offset=0x0', lines[0])
            self.assertIn(f'size={len(bootloader)}', lines[0])
            self.assertIn(f'sha256={sha256(bootloader)}', lines[0])
            self.assertIn('offset=0x10000', lines[2])
            self.assertIn(f'sha256={sha256(app)}', lines[2])
            # Order follows the flash map, and the app is last.
            self.assertIn('app.bin', lines[2])

    def test_app_hash_anchors_the_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, _b, _p, app = make_build(tmp)
            ran = run_bundle(['--build', str(build)], tmp)
            self.assertEqual(ran.returncode, 0)
            self.assertIn(sha256(app), ran.stdout)
            self.assertIn(f'size={len(app)}', ran.stdout)


class BundleTest(unittest.TestCase):
    def test_bundle_places_each_component_at_its_offset_and_zeros_between(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, bootloader, ptable, app = make_build(tmp)
            bundle = Path(tmp) / 'bundle.bin'
            ran = run_bundle(['--build', str(build), '--bundle', str(bundle)], tmp)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            data = bundle.read_bytes()
            self.assertEqual(len(data), 0x10000 + len(app),
                             'the bundle ends where the last component ends')
            self.assertEqual(data[0x0:0x40], b'\x00' * 64)
            self.assertEqual(data[0x40:0x44], b'BOOT')
            self.assertEqual(data[0x8000:0x8002], b'\x52\x71')
            self.assertEqual(data[0x10000:0x10000 + len(app)], app)
            # Padding is zeros: a flasher can write the bundle straight to 0x0.
            self.assertEqual(data[0x44:0x8000], b'\x00' * (0x8000 - 0x44))
            self.assertIn('bundle', ran.stdout)
            self.assertIn(sha256(data), ran.stdout)

    def test_manifest_and_check_are_the_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, _b, _p, app = make_build(tmp)
            manifest = Path(tmp) / 'manifest.txt'
            first = run_bundle(['--build', str(build), '--out', str(manifest)], tmp)
            self.assertEqual(first.returncode, 0, first.stderr)
            clean = run_bundle(['--build', str(build), '--check', str(manifest)], tmp)
            self.assertEqual(clean.returncode, 0, clean.stderr)
            # Tamper with the app after review: the check must fail and name it.
            (build / 'app.bin').write_bytes(app + b'X')
            tampered = run_bundle(['--build', str(build), '--check', str(manifest)], tmp)
            self.assertNotEqual(tampered.returncode, 0,
                                'a changed file must not pass a reviewed manifest:\n'
                                + tampered.stdout + tampered.stderr)
            self.assertIn('app.bin', tampered.stdout + tampered.stderr)


class BundleVerifyTest(unittest.TestCase):
    """--verify is the prebuilt-bundle gate: it hashes the one file that goes to 0x0 against the
    manifest's bundle_sha256 header, with no build tree in sight."""

    def test_bundle_mode_writes_the_bundle_hash_into_the_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, _b, _p, _a = make_build(tmp)
            manifest = Path(tmp) / 'manifest.txt'
            bundle = Path(tmp) / 'bundle.bin'
            ran = run_bundle(['--build', str(build), '--out', str(manifest),
                              '--bundle', str(bundle)], tmp)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            text = manifest.read_text()
            self.assertIn(f'bundle_sha256={sha256(bundle.read_bytes())}', text)
            # The header is a header, not a component line.
            self.assertEqual(len([l for l in text.splitlines() if l.startswith('offset=')]), 3)

    def test_verify_accepts_a_matching_prebuilt_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, _b, _p, _a = make_build(tmp)
            manifest = Path(tmp) / 'manifest.txt'
            bundle = Path(tmp) / 'bundle.bin'
            run_bundle(['--build', str(build), '--out', str(manifest), '--bundle', str(bundle)], tmp)
            # Verify with only the bundle + manifest present: the prebuilt path needs no build tree.
            verify = run_bundle(['--verify', str(bundle), '--check', str(manifest)], tmp)
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
            self.assertIn('matches the reviewed manifest', verify.stdout)

    def test_independent_pin_refuses_an_old_matching_bundle_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, _b, _p, _a = make_build(tmp)
            manifest = Path(tmp) / 'manifest.txt'
            bundle = Path(tmp) / 'bundle.bin'
            run_bundle(['--build', str(build), '--out', str(manifest), '--bundle', str(bundle)], tmp)
            result = run_bundle(['--verify', str(bundle), '--check', str(manifest),
                                 '--expected-sha256', '0' * 64], tmp)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('selected build', result.stdout + result.stderr)

    def test_independent_pin_accepts_selected_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, _b, _p, _a = make_build(tmp)
            manifest = Path(tmp) / 'manifest.txt'
            bundle = Path(tmp) / 'bundle.bin'
            run_bundle(['--build', str(build), '--out', str(manifest), '--bundle', str(bundle)], tmp)
            result = run_bundle(['--verify', str(bundle), '--check', str(manifest),
                                 '--expected-sha256', sha256(bundle.read_bytes())], tmp)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_malformed_pin_is_refused_without_normalization(self):
        result = run_bundle(['--verify', 'unused.bin', '--check', 'unused.txt',
                             '--expected-sha256', ' ' + '0' * 64], ROOT)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('64 lowercase hexadecimal', result.stdout + result.stderr)

    def test_verify_refuses_a_tampered_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, _b, _p, _a = make_build(tmp)
            manifest = Path(tmp) / 'manifest.txt'
            bundle = Path(tmp) / 'bundle.bin'
            run_bundle(['--build', str(build), '--out', str(manifest), '--bundle', str(bundle)], tmp)
            data = bytearray(bundle.read_bytes())
            data[0] ^= 0xFF
            bundle.write_bytes(bytes(data))
            verify = run_bundle(['--verify', str(bundle), '--check', str(manifest)], tmp)
            self.assertNotEqual(verify.returncode, 0,
                                'a changed bundle must not pass a reviewed manifest')
            self.assertIn('disagrees', verify.stdout + verify.stderr)

    def test_verify_refuses_a_manifest_without_the_bundle_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, _b, _p, _a = make_build(tmp)
            manifest = Path(tmp) / 'manifest.txt'
            run_bundle(['--build', str(build), '--out', str(manifest)], tmp)   # no --bundle
            verify = run_bundle(['--verify', str(Path(tmp) / 'whatever.bin'),
                                 '--check', str(manifest)], tmp)
            self.assertNotEqual(verify.returncode, 0)
            self.assertIn('bundle_sha256', verify.stdout + verify.stderr)


class PreserveOtherPartitionsTest(unittest.TestCase):
    """Flashing must write ONLY the regions the manifest names.

    The bundled image is contiguous from 0x0 to the end of the app, with zero
    padding between components. Writing it as one blob therefore zeroes every
    partition that lives in those gaps -- and on this board the settings store
    (nvs at 0x9000) and the PHY calibration (phy_init at 0xF000) both sit between
    the partition table and the app. That is why every firmware update cost a full
    re-provision: the flash was erasing the settings itself. This is not an
    ESP32 constraint -- images that write only their own regions keep NVS intact.
    """

    def test_extraction_works_without_a_build_tree(self):
        """The region-wise fallback in the install guide exists for machines with no build.

        It used to demand `--build` anyway, so the documented command failed for exactly the reader
        it was written for.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            build, _boot, _ptable, app_bytes = make_build(tmp)
            bundle = Path(tmp) / "flash.bundle.bin"
            manifest = Path(tmp) / "flash.manifest.txt"
            made = run_bundle(["--build", str(build), "--bundle", str(bundle), "--out", str(manifest)], tmp)
            self.assertEqual(made.returncode, 0, made.stderr)

            out = Path(tmp) / "extracted"
            got = run_bundle(
                ["--extract", str(out), "--bundle", str(bundle), "--check", str(manifest)], tmp
            )
            self.assertEqual(got.returncode, 0, got.stderr)
            self.assertEqual((out / "app.bin").read_bytes(), app_bytes)

            # Still a verified extraction: a tampered bundle must not be unpacked.
            bundle.write_bytes(bundle.read_bytes() + b"tamper")
            refused = run_bundle(
                ["--extract", str(out), "--bundle", str(bundle), "--check", str(manifest)], tmp
            )
            self.assertNotEqual(refused.returncode, 0)

    def test_extract_recovers_each_component_from_the_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, bootloader, ptable, app = make_build(tmp)
            manifest = Path(tmp) / 'manifest.txt'
            bundle = Path(tmp) / 'bundle.bin'
            run_bundle(['--build', str(build), '--out', str(manifest),
                        '--bundle', str(bundle)], tmp)
            out = Path(tmp) / 'extracted'
            ran = run_bundle(['--verify', str(bundle), '--check', str(manifest),
                              '--extract', str(out)], tmp)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertEqual((out / 'bootloader.bin').read_bytes(), bootloader)
            self.assertEqual((out / 'partition-table.bin').read_bytes(), ptable)
            self.assertEqual((out / 'app.bin').read_bytes(), app)

    def test_written_regions_never_overlap_a_partition_outside_the_manifest(self):
        """The real guard: given this board's actual partition table, the ranges a
        flash writes must not touch any partition the manifest does not name.

        The negative control matters as much as the assertion: if the contiguous
        blob write did NOT overlap a protected partition, this test would pass
        while proving nothing. It asserts both halves."""
        import struct
        table = ROOT / 'firmware/esp32/build/partition_table/partition-table.bin'
        if not table.exists():
            self.skipTest('no built partition table; run --build first')
        data = table.read_bytes()
        partitions = []
        for i in range(0, len(data), 32):
            ent = data[i:i + 32]
            if len(ent) < 32 or ent[0] != 0xAA or ent[1] != 0x50:
                continue
            # ESP-IDF partition entry: magic u16, type u8, subtype u8,
            # offset u32, size u32, name[16], flags u32. Read them where they are.
            offset, size = struct.unpack('<II', ent[4:12])
            name = ent[12:28].split(b'\x00')[0].decode('utf-8', 'replace')
            partitions.append((offset, size, name))

        # What the bundle contains: the three regions the flash map names.
        written = [(0x0, 21168, 'bootloader'),
                   (0x8000, 3072, 'partition-table'),
                   (0x10000, 1281440, 'app')]

        def overlaps(a, b):
            return a[0] < b[0] + b[1] and b[0] < a[0] + a[1]

        protected = [(o, s, n) for o, s, n in partitions
                     if n not in ('factory',)]
        self.assertTrue(protected, 'the partition table has nothing to protect')

        # Negative control: the merged blob MUST overlap the protected partitions,
        # or the guard below is vacuous and proves nothing.
        blob = (0x0, max(o + s for o, s, _ in written))
        self.assertTrue([n for o, s, n in protected if overlaps(blob, (o, s, n))],
                        'expected the contiguous write to hit a protected partition')

        # The assertion: region-wise writes leave every protected partition alone.
        for region in written:
            hit = [n for o, s, n in protected if overlaps(region, (o, s, n))]
            self.assertEqual(hit, [],
                             f'{region[2]} would overwrite {hit}: '
                             'flash regions must stay inside the manifest components')


class ErrorsTest(unittest.TestCase):
    def test_a_missing_flash_map_is_refused_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = Path(tmp) / 'build'
            build.mkdir()
            ran = run_bundle(['--build', str(build)], tmp)
            self.assertNotEqual(ran.returncode, 0)
            self.assertIn('flasher_args', ran.stdout + ran.stderr)

    def test_a_map_entry_pointing_at_a_missing_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, _b, _p, _a = make_build(tmp)
            raw = json.loads((build / 'flasher_args.json').read_text())
            raw['flash_files']['0x20000'] = 'gone.bin'
            (build / 'flasher_args.json').write_text(json.dumps(raw))
            ran = run_bundle(['--build', str(build)], tmp)
            self.assertNotEqual(ran.returncode, 0)
            self.assertIn('gone.bin', ran.stdout + ran.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
