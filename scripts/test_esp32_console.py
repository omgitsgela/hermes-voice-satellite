#!/usr/bin/env python3
"""Compile the actual ESP-IDF adapter with host driver/NVS fault shims."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / 'firmware/esp32/main'
CORE = ROOT / 'firmware/esp32/components/provisioning'

#: A real-shaped passphrase used only to prove it never appears in console output.
SECRET = 'SuperSecret123'

RUNNER = r'''#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "nvs.h"
#include "driver/usb_serial_jtag.h"
#include "provisioning_console.h"
#include "tls_credentials.h"

/* Two namespaces (hermes_dev, hermes_wifi) with staged/committed separation, so the
 * tests can prove a value is only visible after a commit and is verified by readback. */
#define MAX_ENTRIES 8
typedef struct { char ns[24], key[24], value[200]; } entry;
static entry committed[MAX_ENTRIES], staged[MAX_ENTRIES];
static int committed_count, staged_count;

static const char *ns_for(nvs_handle_t handle) { return handle == 1 ? "hermes_dev" : "hermes_wifi"; }

static void copy_string(char *target, size_t capacity, const char *source) {
    size_t index = 0;
    for (; source[index] && index + 1 < capacity; ++index) target[index] = source[index];
    target[index] = 0;
}

static entry *find(entry *table, int count, const char *ns, const char *key) {
    for (int i = 0; i < count; ++i)
        if (!strcmp(table[i].ns, ns) && !strcmp(table[i].key, key)) return &table[i];
    return NULL;
}

static void put(entry *table, int *count, const char *ns, const char *key, const char *value) {
    entry *slot = find(table, *count, ns, key);
    if (!slot) {
        assert(*count < MAX_ENTRIES);
        slot = &table[(*count)++];
        copy_string(slot->ns, sizeof(slot->ns), ns);
        copy_string(slot->key, sizeof(slot->key), key);
    }
    copy_string(slot->value, sizeof(slot->value), value);
}

static void drop(entry *table, int *count, const char *ns, const char *key) {
    entry *slot = find(table, *count, ns, key);
    if (slot) *slot = table[--(*count)];
}

int nvs_flash_init(void) { return getenv("FAIL_INIT") ? -1 : 0; }

int nvs_open(const char *name, int mode, nvs_handle_t *handle) {
    (void)mode;
    if (!strcmp(name, "hermes_dev")) *handle = 1;
    else if (!strcmp(name, "hermes_wifi")) *handle = 2;
    else assert(0);
    return 0;
}

void nvs_close(nvs_handle_t handle) { (void)handle; }

int nvs_set_str(nvs_handle_t handle, const char *key, const char *value) {
    if (getenv("FAIL_SET")) return -1;
    put(staged, &staged_count, ns_for(handle), key, value);
    return 0;
}

int nvs_get_str(nvs_handle_t handle, const char *key, char *value, size_t *capacity) {
    entry *slot = find(committed, committed_count, ns_for(handle), key);
    if (!slot) return ESP_ERR_NVS_NOT_FOUND;
    assert(*capacity > strlen(slot->value));
    strcpy(value, slot->value);
    return 0;
}

int nvs_commit(nvs_handle_t handle) {
    if (getenv("FAIL_COMMIT")) return -1;
    const char *ns = ns_for(handle);
    for (int i = 0; i < staged_count; ) {
        if (!strcmp(staged[i].ns, ns)) {
            put(committed, &committed_count, staged[i].ns, staged[i].key, staged[i].value);
            staged[i] = staged[--staged_count];
        } else ++i;
    }
    return 0;
}

int nvs_erase_key(nvs_handle_t handle, const char *key) {
    const char *ns = ns_for(handle);
    drop(staged, &staged_count, ns, key);
    drop(committed, &committed_count, ns, key);
    return ESP_OK;
}

unsigned xTaskGetTickCount(void) { return 1; }

int usb_serial_jtag_driver_install(usb_serial_jtag_driver_config_t *config) {
    assert(config->rx_buffer_size > 0 && config->tx_buffer_size > 0);
    return 0;
}

int usb_serial_jtag_read_bytes(void *buf, uint32_t length, uint32_t timeout) {
    assert(length == 1 && timeout > 0);
    const char *input = getenv("INPUT_SCRIPT");
    if (!input) input = "STATUS\nSET_ENDPOINT tcp://192.0.2.1:10701\nSTATUS\n";
    static size_t index;
    if (!input[index]) exit(0);
    *(char *)buf = input[index++];
    return 1;
}

int usb_serial_jtag_write_bytes(const void *src, size_t length, uint32_t timeout) {
    assert(timeout > 0);
    return (int)fwrite(src, 1, length, stdout);
}

int usb_wyoming_run(void) { puts("USB_WYOMING_ENTERED"); return -2; }

/* Stands in for the real radio: the console's job is only to route and frame the report. */
int wifi_station_scan(char *out, size_t capacity) {
    if (getenv("SCAN_FAIL")) {
        snprintf(out, capacity, "ERROR wifi init\n");
        return -1;
    }
    const char *report = getenv("SCAN_REPORT");
    if (!report) report = "GL-Test-AP ch=6 rssi=-42 auth=WPA2\n(hidden) ch=11 rssi=-70 auth=WPA3\n";
    snprintf(out, capacity, "%s", report);
    return 0;
}

/* The join attempt. It records what it was handed to a file so the test can prove the
 * staged passphrase reached it without the passphrase ever appearing in captured output. */
int wifi_station_connect(const char *ssid, const char *psk, char *out, size_t capacity) {
    if (!ssid || !psk) {
        snprintf(out, capacity, "status=failed reason=none");
        return -1;
    }
    const char *record = getenv("CONNECT_RECORD");
    if (record) {
        const char *expected = getenv("EXPECTED_PSK");
        FILE *log = fopen(record, "w");
        if (log) {
            fprintf(log, "ssid=%s psk_matches=%d psk_len=%u\n", ssid,
                    expected && !strcmp(psk, expected), (unsigned)strlen(psk));
            fclose(log);
        }
    }
    if (getenv("CONNECT_FAIL")) {
        snprintf(out, capacity, "status=failed reason=15:4WAY_HANDSHAKE_TIMEOUT");
        return -1;
    }
    snprintf(out, capacity, "status=connected ssid=%s ip=192.0.2.51 channel=11 rssi=-40", ssid);
    return 0;
}

/* Holding the link open is what START_GATEWAY needs: it registers over a socket while the
 * association stays up, and releases afterwards. */
int wifi_station_associate(const char *ssid, const char *psk, char *out, size_t capacity) {
    if (getenv("ASSOCIATE_FAIL")) {
        snprintf(out, capacity, "status=failed reason=201:NO_AP_FOUND");
        return -1;
    }
    return wifi_station_connect(ssid, psk, out, capacity);
}

static int released;
void wifi_station_release(void) { released += 1; puts("GATEWAY_RELEASED"); }

/* The socket leg: reports what it was asked to dial, so the console's routing and framing
 * are what the test checks rather than the network. */
int gateway_tcp_run(const char *host, unsigned port, char *out, size_t capacity) {
    const char *record = getenv("GATEWAY_RECORD");
    if (record) {
        FILE *log = fopen(record, "w");
        if (log) {
            fprintf(log, "host=%s port=%u\n", host, port);
            fclose(log);
        }
    }
    if (getenv("GATEWAY_FAIL")) {
        snprintf(out, capacity, "socket=failed host=%s port=%u\n", host, port);
        return -1;
    }
    snprintf(out, capacity, "socket=connected host=%s port=%u transport=tcp\n"
                            "session=registered result=ok device_id=esp32-68ee8f4b5988\n", host, port);
    return 0;
}

/* The TLS leg records its own dial separately from the plaintext one, so a test can prove the
 * endpoint's scheme chose the carrier instead of both paths happening to be reachable. */
int gateway_tls_run(const char *host, unsigned port, char *out, size_t capacity) {
    const char *record = getenv("TLS_RECORD");
    if (record) {
        FILE *log = fopen(record, "w");
        if (log) {
            fprintf(log, "host=%s port=%u\n", host, port);
            fclose(log);
        }
    }
    if (getenv("TLS_FAIL")) {
        snprintf(out, capacity, "clock=unix=1780000000 synced=yes\ntls=failed reason=handshake\n");
        return -1;
    }
    snprintf(out, capacity, "socket=connected host=%s port=%u transport=tls\n"
                            "tls=server_pin matched\n"
                            "session=registered result=ok device_id=esp32-68ee8f4b5988\n", host, port);
    return 0;
}

/* The credential region as the console sees it: an intact blob, or nothing provisioned. */
int tls_credentials_probe(tls_credentials *out, char *reason, size_t reason_capacity) {
    if (getenv("TLS_PROBE_ABSENT")) {
        snprintf(reason, reason_capacity, "none provisioned");
        return -1;
    }
    for (size_t index = 0; index < TLS_SERVER_PIN_LEN; index++)
        out->server_pin[index] = (unsigned char)index;
    snprintf(out->server_host, sizeof(out->server_host), "bridge.example");
    out->server_port = 10702;
    return 0;
}
void vTaskDelay(unsigned ticks) { assert(ticks > 0); puts("RESET_LATCH_ENTERED"); exit(0); }
int main(void) { provisioning_console_run(); return 0; }
'''


class ConsoleTests(unittest.TestCase):
    def build(self, tmp):
        tmp = Path(tmp)
        (tmp / 'driver').mkdir()
        (tmp / 'freertos').mkdir()
        (tmp / 'esp_err.h').write_text('#pragma once\ntypedef int esp_err_t;\n#define ESP_OK 0\n#define ESP_ERR_NVS_NOT_FOUND 1\n')
        (tmp / 'nvs.h').write_text('''#pragma once
#include <stddef.h>
#include "esp_err.h"
typedef unsigned nvs_handle_t;
#define NVS_READONLY 0
#define NVS_READWRITE 1
int nvs_open(const char *, int, nvs_handle_t *);
void nvs_close(nvs_handle_t);
int nvs_set_str(nvs_handle_t, const char *, const char *);
int nvs_get_str(nvs_handle_t, const char *, char *, size_t *);
int nvs_commit(nvs_handle_t);
int nvs_erase_key(nvs_handle_t, const char *);
''')
        (tmp / 'nvs_flash.h').write_text('#include "esp_err.h"\nint nvs_flash_init(void);\n')
        (tmp / 'freertos/FreeRTOS.h').write_text('#pragma once\n#include <stdint.h>\ntypedef uint32_t TickType_t;\n#define pdMS_TO_TICKS(x) (x)\n')
        (tmp / 'freertos/task.h').write_text('#include "FreeRTOS.h"\nTickType_t xTaskGetTickCount(void);\nvoid vTaskDelay(TickType_t);\n')
        (tmp / 'driver/usb_serial_jtag.h').write_text('''#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"
typedef struct { uint32_t tx_buffer_size, rx_buffer_size; } usb_serial_jtag_driver_config_t;
int usb_serial_jtag_driver_install(usb_serial_jtag_driver_config_t *);
int usb_serial_jtag_read_bytes(void *, uint32_t, uint32_t);
int usb_serial_jtag_write_bytes(const void *, size_t, uint32_t);
''')
        (tmp / 'runner.c').write_text(RUNNER)
        executable = tmp / 'console'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                        '-I' + str(tmp), '-I' + str(CORE), '-I' + str(MAIN),
                        str(tmp / 'runner.c'), str(MAIN / 'provisioning_console.c'),
                        str(CORE / 'provisioning.c'), '-o', str(executable)], check=True)
        return executable

    def console(self, executable, script, **environment):
        return subprocess.check_output([str(executable)], text=True, timeout=5,
                                       env={**os.environ, 'INPUT_SCRIPT': script, **environment})

    def test_usb_adapter_requires_commit_and_never_erases_nvs(self):
        self.assertTrue((MAIN / 'provisioning_console.c').exists(), 'USB console adapter missing')
        with tempfile.TemporaryDirectory() as tmp:
            executable = self.build(tmp)
            output = self.console(executable, 'STATUS\nSET_ENDPOINT tcp://192.0.2.1:10701\nSTATUS\n')
            self.assertIn('PROVISION OK SAVED STAGED', output)
            self.assertIn('PROVISION OK ENDPOINT tcp://192.0.2.1:10701 STAGED', output)
            self.assertNotIn('USB_WYOMING_ENTERED', output, 'saving must not activate transport')
            activated = self.console(executable, 'START_WYOMING\nSET_ENDPOINT tcp://192.0.2.2:10701\n')
            self.assertIn('USB_WYOMING_ENTERED', activated)
            self.assertIn('RESET_LATCH_ENTERED', activated)
            self.assertNotIn('OK SAVED', activated, 'trailing protocol bytes must not resume commands')
            rejected = self.console(executable, 'START_WYOMING suffix\nSTATUS\n')
            self.assertNotIn('USB_WYOMING_ENTERED', rejected)
            for flag in ['FAIL_COMMIT', 'FAIL_INIT']:
                output = self.console(executable, 'STATUS\nSET_ENDPOINT tcp://192.0.2.1:10701\nSTATUS\n',
                                      **{flag: '1'})
                self.assertIn('PROVISION ERR STORAGE', output)
                self.assertNotIn('OK SAVED', output)

    def test_wifi_credentials_stage_through_the_console(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = self.build(tmp)
            output = self.console(executable, 'WIFI_STATUS\nSET_WIFI_SSID GL-Test-AP\nWIFI_STATUS\n'
                                              f'SET_WIFI_PSK {SECRET}\nWIFI_STATUS\n')
            self.assertIn('PROVISION OK WIFI UNCONFIGURED', output)
            self.assertIn('PROVISION OK WIFI SSID STAGED', output)
            self.assertIn('PROVISION OK WIFI ssid=GL-Test-AP psk=missing', output,
                          'half a configuration must not read as ready')
            self.assertIn('PROVISION OK WIFI PSK STAGED', output)
            self.assertIn('PROVISION OK WIFI ssid=GL-Test-AP psk=staged', output)
            self.assertNotIn('USB_WYOMING_ENTERED', output, 'staging must not bring up a radio')

    def test_the_passphrase_never_appears_in_console_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = self.build(tmp)
            output = self.console(executable, f'SET_WIFI_SSID GL-Test-AP\nSET_WIFI_PSK {SECRET}\n'
                                              'WIFI_STATUS\nSTATUS\n')
            self.assertIn('OK WIFI PSK STAGED', output)
            self.assertNotIn(SECRET, output, 'the passphrase must never be echoed or reported')

    def test_wifi_staging_requires_a_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = self.build(tmp)
            output = self.console(executable, f'SET_WIFI_SSID GL-Test-AP\nSET_WIFI_PSK {SECRET}\n'
                                              'WIFI_STATUS\n', FAIL_COMMIT='1')
            self.assertIn('PROVISION ERR STORAGE', output)
            self.assertIn('PROVISION OK WIFI UNCONFIGURED', output, 'nothing may survive a failed commit')
            self.assertNotIn(SECRET, output)

    def test_clearing_wifi_removes_both_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = self.build(tmp)
            output = self.console(executable, f'SET_WIFI_SSID GL-Test-AP\nSET_WIFI_PSK {SECRET}\n'
                                              'CLEAR_WIFI\nWIFI_STATUS\n')
            self.assertIn('PROVISION OK WIFI CLEARED', output)
            self.assertIn('PROVISION OK WIFI UNCONFIGURED', output)
            self.assertNotIn(SECRET, output)

    def test_wifi_and_endpoint_staging_do_not_interfere(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = self.build(tmp)
            output = self.console(executable, 'SET_ENDPOINT tcp://192.0.2.7:10701\n'
                                              'SET_WIFI_SSID GL-Test-AP\n'
                                              f'SET_WIFI_PSK {SECRET}\n'
                                              'STATUS\nWIFI_STATUS\nCLEAR_WIFI\nSTATUS\n')
            self.assertIn('PROVISION OK SAVED STAGED', output)
            self.assertIn('PROVISION OK ENDPOINT tcp://192.0.2.7:10701 STAGED', output)
            self.assertIn('PROVISION OK WIFI ssid=GL-Test-AP psk=staged', output)
            self.assertIn('PROVISION OK WIFI CLEARED', output)
            self.assertIn('PROVISION OK ENDPOINT tcp://192.0.2.7:10701 STAGED', output.split('CLEARED')[1],
                          'clearing Wi-Fi credentials must not touch the endpoint')


    def test_scan_command_frames_the_report_and_does_not_activate_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = self.build(tmp)
            output = self.console(executable, 'WIFI_SCAN\n')
            self.assertIn('PROVISION WIFI SCAN BEGIN', output)
            self.assertIn('PROVISION WIFI SCAN END', output)
            self.assertIn('GL-Test-AP ch=6 rssi=-42 auth=WPA2', output)
            self.assertIn('(hidden) ch=11 rssi=-70 auth=WPA3', output)
            self.assertNotIn('USB_WYOMING_ENTERED', output)
            begin, end = output.index('SCAN BEGIN'), output.index('SCAN END')
            self.assertLess(begin, output.index('GL-Test-AP'), 'the marker must come before the report')
            self.assertLess(output.index('GL-Test-AP'), end, 'the report must sit inside the markers')

    def test_a_long_report_is_passed_through_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = self.build(tmp)
            report = '\n'.join(f'network-{i:02d} ch={i + 1} rssi=-{30 + i} auth=WPA2' for i in range(18))
            output = self.console(executable, 'WIFI_SCAN\n', SCAN_REPORT=report + '\n')
            self.assertIn('network-00', output)
            self.assertIn('network-17', output, 'a report inside the cap must not be clipped')
            self.assertIn('SCAN END', output)

    def test_a_failed_scan_reports_the_reason_without_open_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = self.build(tmp)
            output = self.console(executable, 'WIFI_SCAN\nSTATUS\n', SCAN_FAIL='1')
            self.assertIn('ERROR wifi init', output)
            self.assertNotIn('SCAN BEGIN', output, 'a failed scan must not open a report block')
            self.assertNotIn('SCAN END', output)

    def test_scanning_never_prints_the_passphrase(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = self.build(tmp)
            output = self.console(executable, f'SET_WIFI_SSID GL-Test-AP\nSET_WIFI_PSK {SECRET}\n'
                                              'WIFI_SCAN\nWIFI_STATUS\n')
            self.assertIn('SCAN END', output)
            self.assertNotIn(SECRET, output, 'a scan must not leak the staged passphrase')


    def test_a_scan_does_not_emit_a_spurious_reply_after_the_report(self):
        """The scan branch replies for itself, so nothing more may follow it.

        Printing a second, uninitialized response buffer is how a stray line gets onto
        the console, and reading uninitialized stack memory is worse than cosmetic.
        """
        with tempfile.TemporaryDirectory() as tmp:
            executable = self.build(tmp)
            output = self.console(executable, 'WIFI_SCAN\nSTATUS\n')
            lines = [line for line in output.splitlines() if line.strip()]
            self.assertEqual(lines[-1], 'PROVISION OK UNCONFIGURED')
            framed = [line for line in lines if line.startswith('PROVISION') and 'SCAN' in line]
            self.assertEqual(framed, ['PROVISION WIFI SCAN BEGIN', 'PROVISION WIFI SCAN END'])
            between = lines[lines.index('PROVISION WIFI SCAN BEGIN'):]
            self.assertEqual(between[1], 'GL-Test-AP ch=6 rssi=-42 auth=WPA2')
            self.assertEqual(between[2], '(hidden) ch=11 rssi=-70 auth=WPA3')
            self.assertEqual(between[3], 'PROVISION WIFI SCAN END')
            self.assertEqual(between[4], 'PROVISION OK UNCONFIGURED',
                             'exactly one reply may follow the scan block')


    def test_a_connect_attempt_is_framed_and_reports_where_it_landed(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = self.build(tmp)
            record = Path(tmp) / 'connect-record.txt'
            output = self.console(executable,
                                  f'SET_WIFI_SSID GL-Test-AP\nSET_WIFI_PSK {SECRET}\nWIFI_CONNECT\n',
                                  CONNECT_RECORD=str(record), EXPECTED_PSK=SECRET)
            self.assertIn('PROVISION WIFI CONNECT BEGIN', output)
            self.assertIn('status=connected ssid=GL-Test-AP ip=192.0.2.51 channel=11 rssi=-40\n',
                          output, 'the report must stand on its own line, not merge with the terminator')
            self.assertIn('PROVISION WIFI CONNECT END', output)
            self.assertEqual(output.count('PROVISION WIFI CONNECT END'), 1,
                             'exactly one reply may follow the join block')
            self.assertNotIn('PROVISION \n', output,
                             'a bare PROVISION line means the block fell through to a shared reply')
            lines = [line for line in output.splitlines() if line.strip()]
            self.assertEqual(lines[-1], 'PROVISION WIFI CONNECT END',
                             'nothing may be emitted after the join block: a fall-through reply '
                             'repeats whatever the previous command left in the shared buffer')
            self.assertNotIn(SECRET, output, 'the passphrase must never reach the console output')
            self.assertEqual(record.read_text(),
                             f'ssid=GL-Test-AP psk_matches=1 psk_len={len(SECRET)}\n',
                             'the staged pair is what must be handed to the join attempt')

    def test_a_connect_without_staged_credentials_is_refused(self):
        """Half-staged is not staged: no attempt may reach the radio."""
        with tempfile.TemporaryDirectory() as tmp:
            executable = self.build(tmp)
            record = Path(tmp) / 'connect-record.txt'
            output = self.console(executable,
                                  'WIFI_CONNECT\nSET_WIFI_SSID GL-Test-AP\nWIFI_CONNECT\n',
                                  CONNECT_RECORD=str(record), EXPECTED_PSK=SECRET)
            self.assertEqual(output.count('PROVISION ERR WIFI NOT_STAGED'), 2)
            self.assertNotIn('WIFI CONNECT BEGIN', output)
            self.assertFalse(record.exists(), 'no attempt may be made without both fields')

    def test_a_failed_join_reports_its_reason_instead_of_pretending(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = self.build(tmp)
            output = self.console(executable,
                                  f'SET_WIFI_SSID GL-Test-AP\nSET_WIFI_PSK {SECRET}\nWIFI_CONNECT\n',
                                  CONNECT_FAIL='1')
            self.assertIn('status=failed reason=15:4WAY_HANDSHAKE_TIMEOUT', output)
            self.assertNotIn('status=connected', output)
            self.assertNotIn(SECRET, output, 'a failure must not leak the passphrase either')


    def test_a_gateway_run_reports_the_link_the_socket_and_the_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = self.build(tmp)
            record = Path(tmp) / 'gateway-record.txt'
            output = self.console(executable,
                                  f'SET_WIFI_SSID GL-Test-AP\nSET_WIFI_PSK {SECRET}\n'
                                  'SET_ENDPOINT tcp://192.0.2.1:10701\nSTART_GATEWAY\n',
                                  GATEWAY_RECORD=str(record))
            self.assertIn('PROVISION GATEWAY BEGIN', output)
            self.assertIn('status=connected ssid=GL-Test-AP', output)
            self.assertIn('socket=connected host=192.0.2.1 port=10701', output)
            self.assertIn('session=registered result=ok device_id=esp32-68ee8f4b5988', output)
            self.assertIn('PROVISION GATEWAY END', output)
            self.assertIn('GATEWAY_RELEASED', output)
            self.assertEqual(output.count('PROVISION GATEWAY END'), 1,
                             'exactly one terminator for one run')
            self.assertNotIn(SECRET, output)
            lines = [line for line in output.splitlines() if line.strip()]
            self.assertEqual(lines[-1], 'PROVISION GATEWAY END',
                             'the run must end the block and nothing may follow it')
            self.assertEqual(record.read_text(), 'host=192.0.2.1 port=10701\n',
                             'the parsed host and port are what the socket layer is handed')
            self.assertLess(output.index('GATEWAY_RELEASED'), output.index('PROVISION GATEWAY END'),
                            'the link is released before the block is closed')

    def test_a_gateway_run_without_staged_configuration_is_refused(self):
        """Nothing staged, half staged, and then no endpoint: none of them may dial."""
        with tempfile.TemporaryDirectory() as tmp:
            executable = self.build(tmp)
            record = Path(tmp) / 'gateway-record.txt'
            output = self.console(executable,
                                  'START_GATEWAY\n'
                                  'SET_WIFI_SSID GL-Test-AP\n'
                                  'START_GATEWAY\n'
                                  f'SET_WIFI_PSK {SECRET}\n'
                                  'START_GATEWAY\n',
                                  GATEWAY_RECORD=str(record))
            self.assertEqual(output.count('PROVISION ERR GATEWAY WIFI_NOT_STAGED'), 2,
                             'nothing staged and half staged are both refused')
            self.assertEqual(output.count('PROVISION ERR GATEWAY NO_ENDPOINT'), 1)
            self.assertNotIn('GATEWAY BEGIN', output)
            self.assertFalse(record.exists(), 'nothing may be dialled without an endpoint')
            self.assertNotIn(SECRET, output)

    def test_a_tls_endpoint_is_dialled_over_tls_and_never_in_plaintext(self):
        """The scheme decides the carrier. A tls:// endpoint reaches the TLS transport, and the
        plaintext transport is not called for it at all: that is what keeps the earlier refusal from
        turning into a silent downgrade now that TLS is implemented."""
        with tempfile.TemporaryDirectory() as tmp:
            executable = self.build(tmp)
            tcp_record = Path(tmp) / 'tcp-record.txt'
            tls_record = Path(tmp) / 'tls-record.txt'
            output = self.console(executable,
                                  f'SET_WIFI_SSID GL-Test-AP\nSET_WIFI_PSK {SECRET}\n'
                                  'SET_ENDPOINT tls://bridge.example:10702\nSTART_GATEWAY\n',
                                  GATEWAY_RECORD=str(tcp_record), TLS_RECORD=str(tls_record))
            self.assertIn('PROVISION GATEWAY BEGIN', output)
            self.assertIn('transport=tls', output)
            self.assertIn('PROVISION GATEWAY END', output)
            self.assertIn('GATEWAY_RELEASED', output)
            self.assertTrue(tls_record.exists(), 'the TLS transport must be the one that dialled')
            self.assertEqual(tls_record.read_text().strip(), 'host=bridge.example port=10702')
            self.assertFalse(tcp_record.exists(), 'a TLS endpoint must never be dialled in plaintext')
            self.assertNotIn(SECRET, output)

    def test_a_tcp_endpoint_still_reaches_the_plaintext_transport(self):
        """Plain TCP remains a first-class local transport, and it does not silently become TLS."""
        with tempfile.TemporaryDirectory() as tmp:
            executable = self.build(tmp)
            tcp_record = Path(tmp) / 'tcp-record.txt'
            tls_record = Path(tmp) / 'tls-record.txt'
            output = self.console(executable,
                                  f'SET_WIFI_SSID GL-Test-AP\nSET_WIFI_PSK {SECRET}\n'
                                  'SET_ENDPOINT tcp://192.0.2.1:10701\nSTART_GATEWAY\n',
                                  GATEWAY_RECORD=str(tcp_record), TLS_RECORD=str(tls_record))
            self.assertIn('transport=tcp', output)
            self.assertIn('PROVISION GATEWAY END', output)
            self.assertTrue(tcp_record.exists())
            self.assertFalse(tls_record.exists(), 'a tcp:// endpoint must not be sent over TLS')

    def test_a_failed_tls_run_is_reported_and_the_radio_is_still_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = self.build(tmp)
            output = self.console(executable,
                                  f'SET_WIFI_SSID GL-Test-AP\nSET_WIFI_PSK {SECRET}\n'
                                  'SET_ENDPOINT tls://bridge.example:10702\nSTART_GATEWAY\n',
                                  TLS_FAIL='1')
            self.assertIn('clock=unix=1780000000 synced=yes', output,
                          'the clock state belongs next to the result, not only on success')
            self.assertIn('tls=failed reason=handshake', output)
            self.assertIn('PROVISION GATEWAY END', output)
            self.assertIn('GATEWAY_RELEASED', output)
            self.assertNotIn(SECRET, output)

    def test_tls_status_reports_the_provisioned_material_without_dialling(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = self.build(tmp)
            staged = self.console(executable, 'TLS_STATUS\n')
            self.assertIn('PROVISION OK TLS STAGED host=bridge.example port=10702 pin=', staged)
            self.assertIn('pin=' + ''.join(f'{index:02x}' for index in range(32)), staged)
            self.assertNotIn('GATEWAY BEGIN', staged, 'a status query must not dial anything')
            absent = self.console(executable, 'TLS_STATUS\n', TLS_PROBE_ABSENT='1')
            self.assertIn('PROVISION OK TLS ABSENT reason=none provisioned', absent)

    def test_a_failed_link_stops_before_the_socket_is_dialled(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = self.build(tmp)
            record = Path(tmp) / 'gateway-record.txt'
            output = self.console(executable,
                                  f'SET_WIFI_SSID GL-Test-AP\nSET_WIFI_PSK {SECRET}\n'
                                  'SET_ENDPOINT tcp://192.0.2.1:10701\nSTART_GATEWAY\n',
                                  GATEWAY_RECORD=str(record), ASSOCIATE_FAIL='1')
            self.assertIn('PROVISION GATEWAY BEGIN', output)
            self.assertIn('status=failed reason=201:NO_AP_FOUND', output)
            self.assertNotIn('socket=', output, 'no socket may be dialled without a link')
            self.assertIn('PROVISION GATEWAY END', output)
            self.assertIn('GATEWAY_RELEASED', output)
            self.assertFalse(record.exists())
            self.assertNotIn(SECRET, output)


if __name__ == '__main__':
    unittest.main()
