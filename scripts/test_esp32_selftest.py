#!/usr/bin/env python3
"""Run the boot diagnostic on the host before compiling it for the MCU."""
from pathlib import Path
import ctypes
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / 'firmware/esp32/components'


class BootDiagnosticTests(unittest.TestCase):
    def test_synthetic_boot_diagnostic_succeeds(self):
        diagnostic = COMPONENTS / 'wyoming_diagnostic/wyoming_diagnostic.c'
        self.assertTrue(diagnostic.exists(), 'boot diagnostic missing')
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / 'diagnostic.so'
            wire = COMPONENTS / 'wyoming_wire'
            parser = COMPONENTS / 'wyoming_json'
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-shared', '-fPIC', '-DCJSON_NESTING_LIMIT=16',
                            '-I' + str(wire), '-I' + str(parser),
                            '-I' + str(parser / 'vendor/cjson'),
                            str(diagnostic), str(wire / 'wyoming_wire.c'),
                            str(parser / 'wyoming_json.c'),
                            str(parser / 'vendor/cjson/cJSON.c'), '-lm',
                            '-o', str(library)], check=True)
            lib = ctypes.CDLL(str(library))
            lib.wyoming_diagnostic.restype = ctypes.c_int
            self.assertEqual(lib.wyoming_diagnostic(), 0)
    def test_application_runs_diagnostic_without_hardware_initialization(self):
        app = ROOT / 'firmware/esp32/main/main.c'
        self.assertTrue(app.exists(), 'diagnostic app entry point missing')
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / 'esp_log.h').write_text(
                '#include <stdio.h>\n'
                '#define ESP_LOGI(tag, ...) do { printf(__VA_ARGS__); puts(""); } while(0)\n'
                '#define ESP_LOGE(tag, ...) ESP_LOGI(tag, __VA_ARGS__)\n')
            (tmp / 'esp_psram.h').write_text(
                '#include <stdbool.h>\n#include <stdlib.h>\n'
                'static inline bool esp_psram_is_initialized(void) { return !getenv("MOCK_NO_PSRAM"); }\n'
                'static inline size_t esp_psram_get_size(void) { return 8388608; }\n')
            (tmp / 'esp_heap_caps.h').write_text(
                '#include <stdlib.h>\n#include <stdint.h>\n'
                '#define MALLOC_CAP_SPIRAM 1\n#define MALLOC_CAP_INTERNAL 2\n#define MALLOC_CAP_8BIT 4\n'
                'static inline size_t heap_caps_get_free_size(uint32_t c) { (void)c; return 2000000; }\n'
                'static inline size_t heap_caps_get_minimum_free_size(uint32_t c) { (void)c; return 1900000; }\n'
                'static inline size_t heap_caps_get_largest_free_block(uint32_t c) { (void)c; return 1800000; }\n'
                'static inline void *heap_caps_malloc(size_t n, uint32_t c) { (void)c; return getenv("MOCK_ALLOC_FAIL") ? NULL : malloc(n); }\n'
                'static inline void heap_caps_free(void *p) { free(p); }\n')
            (tmp / 'freertos').mkdir()
            (tmp / 'freertos/FreeRTOS.h').write_text('#include <stddef.h>\n')
            (tmp / 'freertos/task.h').write_text(
                'static inline unsigned uxTaskGetStackHighWaterMark(void *p) { (void)p; return 6000; }\n')
            (tmp / 'runner.c').write_text(
                '#include <stdio.h>\nvoid provisioning_console_run(void) { puts("PROVISIONING_ENTERED"); }\n'
                'void app_main(void); int main(void) { app_main(); return 0; }\n')
            parser = COMPONENTS / 'wyoming_json'
            sources = [app, tmp / 'runner.c',
                       COMPONENTS / 'wyoming_diagnostic/wyoming_diagnostic.c',
                       COMPONENTS / 'wyoming_wire/wyoming_wire.c',
                       parser / 'wyoming_json.c', parser / 'vendor/cjson/cJSON.c',
                       COMPONENTS / 'memory_probe/memory_probe.c']
            includes = [tmp, COMPONENTS / 'wyoming_diagnostic', COMPONENTS / 'memory_probe',
                        COMPONENTS / 'wyoming_wire', parser, parser / 'vendor/cjson']
            executable = tmp / 'boot'
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-DCJSON_NESTING_LIMIT=16',
                            *['-I' + str(p) for p in includes],
                            *[str(p) for p in sources], '-lm', '-o', str(executable)], check=True)
            output = subprocess.check_output([str(executable)], text=True)
            self.assertIn('diagnostic passed', output)
            self.assertIn('audio and network inactive', output)
            self.assertIn('PSRAM pattern test passed: 1048576 bytes', output)
            self.assertIn('stack_min_free_bytes=', output)
            self.assertIn('PROVISIONING_ENTERED', output)
            self.assertIn('internal free=', output)
            for flag, message in [('MOCK_NO_PSRAM', 'PSRAM unavailable'),
                                  ('MOCK_ALLOC_FAIL', 'PSRAM allocation failed')]:
                output = subprocess.check_output([str(executable)], text=True,
                                                 env={**os.environ, flag: '1'})
                self.assertIn(message, output)
                self.assertNotIn('PSRAM pattern test passed', output)


if __name__ == '__main__':
    unittest.main()
