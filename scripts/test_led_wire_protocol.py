#!/usr/bin/env python3
"""Execute real wy_led.c writes against the vendor's packed RGB word contract (#161, #162).

Seeed's XVF3800 I2C command list specifies GPO resource 20 command 16 (LED_COLOR) as ONE
uint32 and command 17 (LED_DOA_COLOR) as TWO uint32 values. Colours are packed 0x00RRGGBB
and serialize little-endian (B, G, R, 0), not as an R,G,B byte triplet. The vendor's own
python_control/xvf_host.py packs each value with struct.pack('<I'-equivalent 'I') and the
read length counts those 4-byte words.

Compile the ENTIRE production core against a capture boundary at emit(), so the assertions
run on the real serializer rather than a re-implementation of it. This is an offline wire
test: physical channel appearance still requires on-device acceptance after deployment.

    .venv/bin/python -B scripts/test_led_wire_protocol.py
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LED_CORE = ROOT / "firmware/esp32/components/wy_led"

PRELUDE = r'''
#include "wy_led.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include "wy_led.c"

static int capture(void *user, uint8_t resid, uint8_t cmd,
                   const uint8_t *params, size_t nparams) {
    (void)user;
    printf("%u %u %zu ", (unsigned)resid, (unsigned)cmd, nparams);
    for (size_t i = 0; i < nparams; ++i) printf("%02x", (unsigned)params[i]);
    putchar('\n');
    return 0;
}
'''


class LedWireProtocolTest(unittest.TestCase):
    def commands(self, body):
        """Compile the entire production core; capture only its emit boundary."""
        with tempfile.TemporaryDirectory(prefix="led-wire-") as tmp:
            source = Path(tmp) / "test.c"
            binary = Path(tmp) / "test"
            source.write_text(PRELUDE + r'''
int main(void) {
    wy_led *led = calloc(1, wy_led_size());
    assert(led);
    assert(wy_led_init(led, capture, NULL) == 0);
''' + body + r'''
    free(led);
    return 0;
}
''', encoding="utf-8")
            built = subprocess.run(
                ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                 "-I", str(LED_CORE), str(source), "-o", str(binary)],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(0, built.returncode, built.stdout + built.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15)
        writes = []
        for line in ran.stdout.splitlines():
            resid, cmd, size, hex_params = line.split()
            params = bytes.fromhex(hex_params)
            self.assertEqual(int(size), len(params), "capture must retain every byte")
            writes.append((int(resid), int(cmd), params))
        return writes

    def test_color_is_one_complete_little_endian_packed_rgb_word(self):
        writes = self.commands(r'''
    assert(wy_led_thinking(led) == 0);
    assert(wy_led_error(led) == 0);
''')
        colors = [w for w in writes if w[1] == 16]
        # Packed 0x009646ff (the working violet) and 0x00ff0000 (red), each little-endian,
        # NOT an R,G,B triplet: the vendor consumes one uint32 per colour.
        self.assertEqual(colors, [(20, 16, bytes.fromhex("ff469600")),
                                  (20, 16, bytes.fromhex("0000ff00"))])

    def test_doa_is_two_complete_little_endian_packed_rgb_words(self):
        # Include the whole production core to exercise its private serializer with unequal
        # channels; the public black/white palette alone cannot expose a byte swap.
        writes = self.commands(r'''
    assert(set_doa_colors(led, 0x12, 0x34, 0x56, 0x9a, 0xbc, 0xde) == 0);
''')
        colors = [w for w in writes if w[1] == 17]
        self.assertEqual(colors, [(20, 17, bytes.fromhex("56341200debc9a00"))])

    def test_listening_keeps_the_moving_direction_marker(self):
        # docs/leds.md has always described this phase as "black ring + white marker: the
        # bright point is where the person is". A solid colour contradicts the docs and the
        # operator's field verdict (#163); the moving marker is the confirmation that the
        # satellite is hearing the speaker.
        writes = self.commands(r'''
    assert(wy_led_wake(led) == 0);
    assert(wy_led_listening(led) == 0);
    assert(wy_led_state(led) == WY_LED_STATE_LISTENING);
''')
        doa = [w for w in writes if w[1] == 17]
        self.assertEqual(doa, [(20, 17, bytes.fromhex("00000000ffffff00"))],
                         "listening must write the documented black base and white marker")
        self.assertEqual(writes[-1], (20, 12, b"\x04"),
                         "listening must select the DOA effect (4), not a solid colour (3)")


if __name__ == "__main__":
    unittest.main()
