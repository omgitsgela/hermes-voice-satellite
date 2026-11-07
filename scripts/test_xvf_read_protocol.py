#!/usr/bin/env python3
"""Offline native regression for XVF_READ's status-inclusive wire length (#164).

Compile the unmodified XVF_READ branch and reply() extracted from the live production
console, alongside the complete production provisioning.c line parser. Only audio_xvf_read
and the USB output are shimmed; no device, bus, or service is opened. This is bounded
branch/parser coverage, NOT a complete console/ESP-IDF build.

Wire contract (Seeed's XVF3800 I2C list, vendor xvf_host.py read()):
    read header = [resid, cmd | 0x80, data_length + 1]
the count INCLUDES the one-byte status the chip always returns first. The production
handler asked the bus for nbytes + 1 bytes but counted only nbytes in the header: the
co-processor is told to return one byte fewer than the buffer can hold, so a multi-byte
read (DOA_VALUE, VERSION) comes back short against the documented contract.

    .venv/bin/python -B scripts/test_xvf_read_protocol.py
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "firmware/esp32/main"
PROVISIONING = ROOT / "firmware/esp32/components/provisioning"


def extract_function(source, signature):
    """One whole top-level function, by brace matching, exactly as written in production."""
    start = source.index(signature)
    depth = 0
    i = source.index("{", start)
    for j in range(i, len(source)):
        if source[j] == "{":
            depth += 1
        elif source[j] == "}":
            depth -= 1
            if depth == 0:
                return source[start:j + 1]
    raise AssertionError("unbalanced braces after " + signature)


class XvfReadProtocolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="xvf-read-native-")
        cls.addClassCleanup(cls.tmp.cleanup)
        source = (MAIN / "provisioning_console.c").read_text()
        start = source.index('if (!strncmp(line.data, "XVF_READ ", 9)) {')
        end = source.index(' else if (!strncmp(line.data, "XVF_SET32 ",', start)
        branch = source[start:end]
        reply = extract_function(source, "static void reply(")
        harness = Path(cls.tmp.name) / "test.c"
        cls.binary = Path(cls.tmp.name) / "test"
        harness.write_text(r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "provisioning.h"
#define pdMS_TO_TICKS(x) (x)
static unsigned reads, writes;
static size_t command_length, read_length;
static uint8_t header[3];
static int bus_result;
static uint8_t bus_status;
static char output[272];
static int audio_xvf_read(const uint8_t *cmd, size_t cmd_len,
                          uint8_t *data, size_t data_len) {
    ++reads;
    command_length = cmd_len;
    read_length = data_len;
    assert(cmd_len == sizeof(header));
    assert(data_len >= 2 && data_len <= 17);
    memcpy(header, cmd, cmd_len);
    if (bus_result) return bus_result;
    data[0] = bus_status;
    for (size_t i = 1; i < data_len; ++i) data[i] = (uint8_t)(0xa0u + i - 1u);
    return 0;
}
static int usb_serial_jtag_write_bytes(const void *data, size_t count, int ticks) {
    ++writes;
    assert(ticks == 250);
    assert(count < sizeof(output));
    memcpy(output, data, count);
    output[count] = 0;
    return (int)count;
}
''' + reply + r'''
static void dispatch(const char *input) {
    provision_line line = {0};
    char response[256] = {0};
    for (size_t i = 0; input[i]; ++i)
        assert(provision_line_feed(&line, (unsigned char)input[i]) == 0);
    if (provision_line_feed(&line, '\n') != 1) {
        reply("ERR LINE");
        return;
    }
''' + branch + r''' else {
        snprintf(response, sizeof(response), "ERR COMMAND");
    }
    reply(response);
}
int main(int argc, char **argv) {
    assert(argc == 4);
    bus_status = (uint8_t)strtoul(argv[2], NULL, 10);
    bus_result = (int)strtol(argv[3], NULL, 10);
    dispatch(argv[1]);
    printf("reads=%u writes=%u cmdlen=%zu readlen=%zu header=%02x,%02x,%02x\n",
           reads, writes, command_length, read_length, header[0], header[1], header[2]);
    fputs(output, stdout);
    return 0;
}
''')
        result = subprocess.run(
            ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
             "-I", str(PROVISIONING), str(harness), str(PROVISIONING / "provisioning.c"),
             "-o", str(cls.binary)],
            capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise RuntimeError("Native harness compilation failed (not behavioral RED):\n"
                               + result.stdout + result.stderr)

    def request(self, command, status=0, bus_result=0):
        result = subprocess.run(
            [str(self.binary), command, str(status), str(bus_result)],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 2, result.stdout)
        fields = dict(field.split("=", 1) for field in lines[0].split())
        self.assertEqual(fields["writes"], "1", "exactly one diagnostic reply")
        return fields, lines[1]

    def assert_request(self, fields, resource, command, size):
        self.assertEqual(fields["reads"], "1", "one read, no retry/extra device action")
        self.assertEqual(fields["cmdlen"], "3", "read header only, no state-write payload")
        self.assertEqual(fields["readlen"], str(size + 1))
        self.assertEqual(fields["header"], f"{resource:02x},{command | 0x80:02x},{size + 1:02x}",
                         "read header count must include the status byte")

    def test_status_inclusive_count_for_led_and_maximum_reads(self):
        for name, resource, command, size in (
            ("LED effect", 20, 12, 1),
            ("LED color", 20, 16, 4),
            ("LED DoA colors", 20, 17, 8),
            ("maximum 16-byte payload", 20, 19, 16),
        ):
            with self.subTest(name=name, size=size):
                fields, response = self.request(f"XVF_READ {resource} {command} {size}")
                self.assert_request(fields, resource, command, size)
                payload = "".join(f" {0xa0 + i:02x}" for i in range(size))
                self.assertEqual(response, f"PROVISION OK XVF {resource}/{command} st=0{payload}")

    def test_default_payload_length_is_one(self):
        fields, response = self.request("XVF_READ 20 12")
        self.assert_request(fields, 20, 12, 1)
        self.assertEqual(response, "PROVISION OK XVF 20/12 st=0 a0")

    def test_raw_success_retry_and_unknown_status_remain_visible_without_retry(self):
        for status in (0, 64, 66, 255):
            with self.subTest(status=status):
                fields, response = self.request("XVF_READ 20 12 1", status=status)
                self.assertEqual(fields["reads"], "1")
                self.assertEqual(response, f"PROVISION OK XVF 20/12 st={status} a0")

    def test_bus_error_is_not_replaced_by_status_or_payload(self):
        fields, response = self.request("XVF_READ 20 16 4", status=66, bus_result=-37)
        self.assertEqual(fields["reads"], "1")
        self.assertEqual(response, "PROVISION ERR XVF_READ 20 16 bus=-37")

    def test_invalid_payload_lengths_do_not_touch_the_bus(self):
        for command in ("XVF_READ 20 12 0", "XVF_READ 20 12 17", "XVF_READ 20"):
            with self.subTest(command=command):
                fields, response = self.request(command)
                self.assertEqual(fields["reads"], "0")
                self.assertEqual(response,
                                 "PROVISION ERR XVF_READ usage: XVF_READ <resid> <cmd> [bytes 1-16]")

    def test_real_line_parser_rejects_control_bytes_before_dispatch(self):
        fields, response = self.request("XVF_READ 20 12 1\r")
        self.assertEqual(fields["reads"], "0")
        self.assertEqual(response, "PROVISION ERR LINE")


if __name__ == "__main__":
    unittest.main()
