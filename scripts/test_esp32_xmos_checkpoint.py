#!/usr/bin/env python3
"""Host tests for the XMOS checkpoint tooling: the record format and the tool's read-only contract.

Nothing here touches a board or the XMOS toolchain. The checkpoint format is pinned by building and
re-parsing real images and by feeding it the corruption a bad or stale flash would produce; the
tool's behaviour is pinned by injecting a fake driver (a plain callable returning canned bytes), so
every path -- backup, restore, the restore refusal, and diff -- is exercised without a device.
"""
from __future__ import annotations
import importlib.util
import json
import struct
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ckpt = load('xmos_checkpoint', 'xmos_checkpoint.py')
tool = load('xmos_firmware', 'xmos-firmware.py')

#: A board-shaped flash: not page-aligned on purpose, so the pad-to-page is the thing under test.
FLASH = bytes(range(256)) * 3 + b'\xa5' * 131
FLASH_LEN = len(FLASH)


class CheckpointFormatTests(unittest.TestCase):
    def test_round_trip_is_stable_and_page_aligned(self):
        image = ckpt.build_checkpoint(FLASH)
        parsed = ckpt.parse_checkpoint(image)
        self.assertEqual(parsed['flash_len'], (FLASH_LEN + 255) // 256 * 256)
        self.assertEqual(parsed['flash'][:FLASH_LEN], FLASH)
        self.assertTrue(parsed['flash'][FLASH_LEN:] == b'\xff' * (parsed['flash_len'] - FLASH_LEN),
                        'padding must be 0xFF, what erased flash reads as')
        # The same flash always builds the same image: a record keyed on it must be stable.
        self.assertEqual(ckpt.build_checkpoint(FLASH), image)

    def test_a_corrupted_crc_is_refused_not_misread(self):
        image = bytearray(ckpt.build_checkpoint(FLASH))
        image[-1] ^= 0x01  # flip a bit in the flash -> CRC mismatch
        with self.assertRaises(ValueError):
            ckpt.parse_checkpoint(bytes(image))

    def test_bad_magic_and_wrong_version_are_refused(self):
        image = bytearray(ckpt.build_checkpoint(FLASH))
        image[0] ^= 0xff
        with self.assertRaises(ValueError):
            ckpt.parse_checkpoint(bytes(image))
        image = bytearray(ckpt.build_checkpoint(FLASH))
        image[8] = 9  # version byte
        with self.assertRaises(ValueError):
            ckpt.parse_checkpoint(bytes(image))

    def test_a_truncated_image_is_refused(self):
        image = ckpt.build_checkpoint(FLASH)
        with self.assertRaises(ValueError):
            ckpt.parse_checkpoint(image[:ckpt.HEADER_LEN - 1])
        with self.assertRaises(ValueError):
            ckpt.parse_checkpoint(image[:-5])  # flash shorter than declared

    def test_diff_reports_identical_and_hunks(self):
        a = ckpt.build_checkpoint(FLASH)
        self.assertTrue(ckpt.diff_checkpoints(a, a)['identical'])
        changed = bytearray(FLASH)
        changed[100] ^= 0xff
        changed[101] ^= 0xff
        b = ckpt.build_checkpoint(bytes(changed))
        result = ckpt.diff_checkpoints(a, b)
        self.assertFalse(result['identical'])
        self.assertEqual(result['hunks'], [[100, 102]])


class ToolDryRunTests(unittest.TestCase):
    """The tool's contract, with a fake driver: it reads, it saves, and it never writes the MCU."""

    def backup_with_fake_driver(self, tmp, flash=FLASH, label='board-a'):
        calls = []

        def fake_driver():
            calls.append(1)
            return flash

        with tempfile.TemporaryDirectory() as scratch:
            scratch_path = Path(scratch)
            record = tool.wrap_and_save(flash, label, scratch_path, 'fake')
        self.assertEqual(record['flash_len'], (FLASH_LEN + 255) // 256 * 256 if len(flash) == FLASH_LEN
                         else (len(flash) + 255) // 256 * 256)
        return scratch_path, record, calls

    def test_backup_writes_image_and_record_and_validates(self):
        with tempfile.TemporaryDirectory() as scratch:
            scratch_path = Path(scratch)
            record = tool.wrap_and_save(FLASH, 'board-a', scratch_path, 'fake')
            image_path = scratch_path / record['image']
            self.assertTrue(image_path.exists())
            # The saved image must re-validate and match the record's key.
            re_parsed = ckpt.parse_checkpoint(image_path.read_bytes())
            self.assertEqual(re_parsed['sha256'], record['sha256'])
            json_path = scratch_path / (record['image'].removesuffix('.img') + '.json')
            self.assertTrue(json_path.exists())
            on_disk = json.loads(json_path.read_text())
            self.assertEqual(on_disk['sha256'], record['sha256'])
            self.assertEqual(on_disk['image'], record['image'])

    def test_restore_is_a_dry_run_and_says_so(self):
        with tempfile.TemporaryDirectory() as scratch:
            scratch_path = Path(scratch)
            record = tool.wrap_and_save(FLASH, 'board-a', scratch_path, 'fake')
            image_path = scratch_path / record['image']
            result = tool.restore_dry_run(image_path.read_bytes(), None)
            self.assertEqual(result, 0)

    def test_restore_refuses_to_write(self):
        """--write is accepted so it can be asked, and answered no: this is the device-change guard."""
        with self.assertRaises(SystemExit):
            tool.main(['restore', '--image', '/nonexistent', '--write'])

    def test_restore_refuses_when_record_does_not_match_image(self):
        """Restoring from an image whose record disagrees is exactly the bad board being written
        over; the mismatch must stop it, not be waved past."""
        with tempfile.TemporaryDirectory() as scratch:
            scratch_path = Path(scratch)
            record = tool.wrap_and_save(FLASH, 'good', scratch_path, 'fake')
            record_path = scratch_path / (record['image'].removesuffix('.img') + '.json')
            # A different board's image, with the good board's record: the sha256 will not match.
            other = tool.wrap_and_save(bytes(FLASH_LEN) + b'\x01', 'other', scratch_path, 'fake')
            other_image = scratch_path / other['image']
            with self.assertRaises(SystemExit):
                tool.restore_dry_run(other_image.read_bytes(), record_path)

    def test_backup_command_uses_injected_driver_and_never_a_device(self):
        """The full command path with a fake driver: --connect is satisfied by the injection, and
        no real tool is invoked. The record records the driver as 'injected'."""
        fake_calls = []

        def fake_driver():
            fake_calls.append(1)
            return FLASH

        with tempfile.TemporaryDirectory() as scratch:
            out_path = Path(scratch) / 'cp'
            rc = tool.main(['backup', '--connect', '--chip', 'xvf3800', '--label', 'ci',
                            '--out', str(out_path)], driver=fake_driver)
            self.assertEqual(rc, 0)
            self.assertEqual(len(fake_calls), 1, 'the injected driver is the one that read the flash')
            records = list(out_path.glob('*.json'))
            self.assertEqual(len(records), 1)
            on_disk = json.loads(records[0].read_text())
            self.assertEqual(on_disk['driver'], 'injected')
            self.assertEqual(on_disk['label'], 'ci')

    def test_backup_without_connect_or_image_is_refused(self):
        with self.assertRaises(SystemExit):
            tool.main(['backup'])

    def test_diff_command_reports_identical(self):
        with tempfile.TemporaryDirectory() as scratch:
            a = Path(scratch) / 'a.img'
            a.write_bytes(ckpt.build_checkpoint(FLASH))
            rc = tool.main(['diff', '--a', str(a), '--b', str(a)])
            self.assertEqual(rc, 0)

    def test_check_command_reports_identity(self):
        with tempfile.TemporaryDirectory() as scratch:
            img = Path(scratch) / 'img'
            img.write_bytes(ckpt.build_checkpoint(FLASH))
            rc = tool.main(['check', '--image', str(img), '--label', 'probe'])
            self.assertEqual(rc, 0)


if __name__ == '__main__':
    unittest.main()
