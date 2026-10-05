#!/usr/bin/env python3
"""Exercise the actual USB adapter with SDK I/O/clock shims, not hardware."""
from pathlib import Path
import io
import os
import subprocess
import tempfile
import unittest
from wyoming.event import Event, read_event, write_event

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / 'firmware/esp32/main'
COMP = ROOT / 'firmware/esp32/components'


class USBLinkTests(unittest.TestCase):
    def test_control_registration_over_fragmented_usb_adapter(self):
        self.assertTrue((MAIN / 'usb_wyoming.c').exists(), 'USB Wyoming adapter missing')
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            (tmp / 'driver').mkdir()
            (tmp / 'freertos').mkdir()
            headers = {
                'esp_err.h': '#pragma once\n#define ESP_OK 0\n',
                'esp_mac.h': '#include <stdint.h>\n#define ESP_MAC_EFUSE_FACTORY 1\nint esp_read_mac(uint8_t *, int);\n',
                'esp_random.h': '#include <stdint.h>\nuint32_t esp_random(void);\n',
                'esp_timer.h': '#include <stdint.h>\nint64_t esp_timer_get_time(void);\n',
                'esp_log.h': '#include <stdarg.h>\ntypedef int (*vprintf_like_t)(const char *, va_list);\nvprintf_like_t esp_log_set_vprintf(vprintf_like_t);\n',
                'esp_heap_caps.h': '#include <stddef.h>\n#define MALLOC_CAP_SPIRAM 1\n#define MALLOC_CAP_8BIT 2\nvoid *heap_caps_malloc(size_t, unsigned);\nvoid heap_caps_free(void *);\n',
                'freertos/FreeRTOS.h': '#pragma once\n#include <stdint.h>\ntypedef uint32_t TickType_t;\n#define pdMS_TO_TICKS(ms) ((ms)/10)\n',
                'driver/usb_serial_jtag.h': '#include <stddef.h>\n#include <stdint.h>\n#include <stdbool.h>\nint usb_serial_jtag_read_bytes(void *, uint32_t, uint32_t);\nint usb_serial_jtag_write_bytes(const void *, size_t, uint32_t);\nint usb_serial_jtag_wait_tx_done(uint32_t);\nbool usb_serial_jtag_is_connected(void);\n',
            }
            for name, content in headers.items():
                (tmp / name).write_text(content)
            (tmp / 'runner.c').write_text(r'''#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_log.h"
#include "esp_timer.h"
#include "usb_wyoming.h"
static int64_t now;
static int muted, ended, writes, flushed, idle;
int esp_read_mac(unsigned char *mac, int type) {
    assert(type == 1); if (getenv("FAIL_MAC")) return -1;
    const unsigned char value[] = {2, 0, 0, 0, 0, 1}; memcpy(mac, value, 6); return 0;
}
unsigned esp_random(void) { return 1; }
int64_t esp_timer_get_time(void) { return now; }
vprintf_like_t esp_log_set_vprintf(vprintf_like_t callback) {
    assert(callback); muted = 1; return NULL;
}
void *heap_caps_malloc(size_t size, unsigned caps) {
    assert(caps == 3); return getenv("FAIL_ALLOC") ? NULL : malloc(size);
}
void heap_caps_free(void *value) { free(value); }
_Bool usb_serial_jtag_is_connected(void) { return !ended; }
int usb_serial_jtag_read_bytes(void *dst, unsigned length, unsigned ticks) {
    assert(muted && length > 0 && ticks > 0 && ticks <= 25);
    if (getenv("IDLE_FIRST") && idle++ < 3) { now += 250000; return 0; }
    int value = getchar();
    if (value == EOF) { ended = 1; return 0; }
    now += getenv("SLOW_INPUT") ? 1100000 : 100;
    *(unsigned char *)dst = (unsigned char)value;
    return 1;
}
int usb_serial_jtag_write_bytes(const void *data, size_t length, unsigned ticks) {
    assert(muted && ticks > 0 && ticks <= 25);
    ++writes;
    if (getenv("FAIL_ACK") && writes == 1) return -1;
    if (getenv("ZERO_WRITE")) return 0;
    size_t chunk = length > 2 ? 2 : length;
    now += flushed && getenv("SLOW_OUTPUT") ? 1100000 : 100;
    return (int)fwrite(data, 1, chunk, stdout);
}
int usb_serial_jtag_wait_tx_done(unsigned ticks) {
    assert(ticks > 0 && ticks <= 25); flushed = 1; return getenv("FAIL_FLUSH") ? -1 : 0;
}
int main(void) {
    int result = usb_wyoming_run();
    fprintf(stderr, "USB_RESULT=%d\n", result);
    return result == -2 ? 0 : 7;
}
''')
            parser = COMP / 'wyoming_json'
            includes = [tmp, MAIN, COMP / 'wyoming_session', COMP / 'wyoming_wire',
                        COMP / 'wyoming_audio', parser, parser / 'vendor/cjson']
            sources = [tmp / 'runner.c', MAIN / 'usb_wyoming.c', MAIN / 'device_identity.c',
                       COMP / 'wyoming_session/wyoming_session.c',
                       COMP / 'wyoming_audio/wyoming_audio.c',
                       COMP / 'wyoming_wire/wyoming_wire.c', parser / 'wyoming_json.c',
                       parser / 'vendor/cjson/cJSON.c']
            binary = tmp / 'usb-link'
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-DCJSON_NESTING_LIMIT=16', *['-I' + str(p) for p in includes],
                            *map(str, sources), '-lm', '-o', str(binary)], check=True)
            input_stream = io.BytesIO()
            for event in [Event(type='describe'), Event(type='hermes.register.ok',
                          data={'device_id': 'esp32-020000000001', 'protocol_version': 1}),
                          Event(type='run-satellite'), Event(type='pause-satellite')]:
                write_event(event, input_stream)
            result = subprocess.run([str(binary)], input=input_stream.getvalue(), capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = io.BytesIO(result.stdout)
            self.assertEqual(output.readline(), b'PROVISION OK WYOMING CONTROL_ONLY RESET_REQUIRED\n')
            info, registration = read_event(output), read_event(output)
            assert info is not None and registration is not None
            self.assertEqual(info.type, 'info')
            self.assertEqual(registration.data['device_id'], 'esp32-020000000001')
            self.assertIsNone(read_event(output), 'no plaintext logs after the mode acknowledgment')
            idle_result = subprocess.run([str(binary)], input=input_stream.getvalue(), capture_output=True,
                                         env={**os.environ, 'IDLE_FIRST': '1'}, timeout=5)
            self.assertEqual(idle_result.returncode, 0, idle_result.stderr)
            self.assertIn(b'hermes.register', idle_result.stdout)
            for fault in ['SLOW_INPUT', 'SLOW_OUTPUT', 'FAIL_ACK', 'ZERO_WRITE', 'FAIL_FLUSH', 'FAIL_MAC', 'FAIL_ALLOC']:
                result = subprocess.run([str(binary)], input=input_stream.getvalue(), capture_output=True,
                                        env={**os.environ, fault: '1'}, timeout=5)
                self.assertNotIn(b'hermes.register', result.stdout)
                self.assertIn(b'USB_RESULT=', result.stderr)


if __name__ == '__main__':
    unittest.main()
