#include "provisioning_console.h"
#include "usb_wyoming.h"
#include "provisioning.h"
#include "wifi_station.h"
#include "gateway_tcp.h"
#include "gateway_tls.h"
#include "tls_credentials_store.h"
#include "driver/usb_serial_jtag.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

static int save_endpoint(void *ctx, const char *value) {
    if (!*(bool *)ctx) return -1;
    nvs_handle_t handle;
    if (nvs_open("hermes_dev", NVS_READWRITE, &handle) != ESP_OK) return -1;
    esp_err_t result = nvs_set_str(handle, "endpoint_v1", value);
    if (result == ESP_OK) result = nvs_commit(handle);
    nvs_close(handle);
    return result == ESP_OK ? 0 : -1;
}

static int load_endpoint(void *ctx, char *value, size_t capacity) {
    if (!*(bool *)ctx) return -1;
    nvs_handle_t handle;
    esp_err_t result = nvs_open("hermes_dev", NVS_READONLY, &handle);
    if (result == ESP_ERR_NVS_NOT_FOUND) { value[0] = 0; return 0; }
    if (result != ESP_OK) return -1;
    result = nvs_get_str(handle, "endpoint_v1", value, &capacity);
    nvs_close(handle);
    if (result == ESP_ERR_NVS_NOT_FOUND) { value[0] = 0; return 0; }
    return result == ESP_OK ? 0 : -1;
}

static int save_wifi_ssid(void *ctx, const char *value) {
    if (!*(bool *)ctx) return -1;
    nvs_handle_t handle;
    if (nvs_open("hermes_wifi", NVS_READWRITE, &handle) != ESP_OK) return -1;
    esp_err_t result = nvs_set_str(handle, "ssid_v1", value);
    if (result == ESP_OK) result = nvs_commit(handle);
    nvs_close(handle);
    return result == ESP_OK ? 0 : -1;
}

static int save_wifi_psk(void *ctx, const char *value) {
    if (!*(bool *)ctx) return -1;
    nvs_handle_t handle;
    if (nvs_open("hermes_wifi", NVS_READWRITE, &handle) != ESP_OK) return -1;
    esp_err_t result = nvs_set_str(handle, "psk_v1", value);
    if (result == ESP_OK) result = nvs_commit(handle);
    nvs_close(handle);
    return result == ESP_OK ? 0 : -1;
}

static int clear_wifi(void *ctx) {
    if (!*(bool *)ctx) return -1;
    nvs_handle_t handle;
    if (nvs_open("hermes_wifi", NVS_READWRITE, &handle) != ESP_OK) return -1;
    esp_err_t first = nvs_erase_key(handle, "ssid_v1");
    esp_err_t second = nvs_erase_key(handle, "psk_v1");
    esp_err_t result = nvs_commit(handle);
    nvs_close(handle);
    if (result != ESP_OK) return -1;
    if (first != ESP_OK && first != ESP_ERR_NVS_NOT_FOUND) return -1;
    if (second != ESP_OK && second != ESP_ERR_NVS_NOT_FOUND) return -1;
    return 0;
}

static int load_wifi(void *ctx, char *ssid, size_t ssid_capacity, char *psk, size_t psk_capacity) {
    if (!*(bool *)ctx) return -1;
    nvs_handle_t handle;
    esp_err_t opened = nvs_open("hermes_wifi", NVS_READONLY, &handle);
    if (opened == ESP_ERR_NVS_NOT_FOUND) { ssid[0] = 0; psk[0] = 0; return 0; }
    if (opened != ESP_OK) return -1;
    esp_err_t first = nvs_get_str(handle, "ssid_v1", ssid, &ssid_capacity);
    if (first == ESP_ERR_NVS_NOT_FOUND) ssid[0] = 0;
    esp_err_t second = nvs_get_str(handle, "psk_v1", psk, &psk_capacity);
    if (second == ESP_ERR_NVS_NOT_FOUND) psk[0] = 0;
    nvs_close(handle);
    if ((first != ESP_OK && first != ESP_ERR_NVS_NOT_FOUND) ||
        (second != ESP_OK && second != ESP_ERR_NVS_NOT_FOUND)) return -1;
    return 0;
}

static void reply(const char *value) {
    char output[272];
    int length = snprintf(output, sizeof(output), "PROVISION %s\n", value);
    if (length <= 0 || (size_t)length >= sizeof(output)) return;
    /* One writer, finite budget. Never retry a partial response as a new line. */
    usb_serial_jtag_write_bytes(output, (size_t)length, pdMS_TO_TICKS(250));
}

/* Multi-line payloads (a scan report) go out under the same single-writer rule, with a
 * hard cap so a large result cannot stall the console. */
static void reply_block(const char *value) {
    size_t length = strlen(value);
    if (length == 0 || length > 1200) return;
    usb_serial_jtag_write_bytes(value, length, pdMS_TO_TICKS(500));
}

/* Emit a newline-separated block one prefixed line at a time, so every line the host sees
 * is self-describing and nothing relies on the parser knowing where a block began. */
static void reply_lines(const char *block) {
    const char *start = block;
    while (*start) {
        const char *end = strchr(start, '\n');
        size_t length = end ? (size_t)(end - start) : strlen(start);
        if (length > 0 && length < 200) {
            char output[272];
            int written = snprintf(output, sizeof(output), "GATEWAY %.*s", (int)length, start);
            if (written > 0 && (size_t)written < sizeof(output)) reply(output);
        }
        if (!end) break;
        start = end + 1;
    }
}

void provisioning_console_run(void) {
    /* Preserve existing flash: incompatible/full NVS is an error, never an erase. */
    bool storage_ready = nvs_flash_init() == ESP_OK;
    usb_serial_jtag_driver_config_t config = {.tx_buffer_size = 512, .rx_buffer_size = 512};
    if (usb_serial_jtag_driver_install(&config) != ESP_OK) return;
    reply("READY 1 NETWORK_INACTIVE AUDIO_INACTIVE");
    provision_line line = {0};
    char response[256] = {0};  /* every path that replies must fill it: never print stack */
    TickType_t started = 0;
    for (;;) {
        if (line.used && (TickType_t)(xTaskGetTickCount() - started) >= pdMS_TO_TICKS(5000))
            provision_line_expire(&line);
        unsigned char byte;
        if (usb_serial_jtag_read_bytes(&byte, 1, pdMS_TO_TICKS(250)) != 1) continue;
        if (!line.used && !line.rejected) started = xTaskGetTickCount();
        int result = provision_line_feed(&line, byte);
        if (result < 0) reply("ERR LINE");
        else if (result > 0) {
            if (!strcmp(line.data, "START_WYOMING")) {
                usb_wyoming_run();
                /* Terminal latch even on allocation/ack/protocol failure. Never
                 * reinterpret queued Wyoming bytes as provisioning commands. */
                for (;;) vTaskDelay(pdMS_TO_TICKS(1000));
            }
            /* The received line is never echoed and never logged, so a passphrase
             * typed here exists only in RAM and in NVS; only the response is sent. */
            if (!strcmp(line.data, "WIFI_SCAN")) {
                static char report[1400];
                if (wifi_station_scan(report, sizeof(report)) == 0) {
                    reply("WIFI SCAN BEGIN");
                    reply_block(report);
                    reply("WIFI SCAN END");
                } else {
                    reply_block(report);  /* the module writes a one-line reason */
                }
                continue;  /* this branch replies for itself; response[] was never filled */
            } else if (!strcmp(line.data, "WIFI_CONNECT")) {
                static char ssid[PROVISION_SSID_MAX + 1];
                static char psk[PROVISION_PSK_MAX + 1];
                static char report[192];
                char framed[200];
                ssid[0] = 0;
                psk[0] = 0;
                if (load_wifi(&storage_ready, ssid, sizeof(ssid), psk, sizeof(psk)) != 0) {
                    reply("ERR WIFI STORAGE");
                } else if (ssid[0] == 0 || psk[0] == 0) {
                    reply("ERR WIFI NOT_STAGED");
                } else {
                    reply("WIFI CONNECT BEGIN");
                    wifi_station_connect(ssid, psk, report, sizeof(report));
                    snprintf(framed, sizeof(framed), "%s\n", report);
                    reply_block(framed);
                    reply("WIFI CONNECT END");
                }
                /* The passphrase is not needed after the attempt and is not kept. */
                memset(psk, 0, sizeof(psk));
                memset(report, 0, sizeof(report));
                continue;  /* this branch replies for itself */
            } else if (!strcmp(line.data, "START_GATEWAY")) {
                /* Join over Wi-Fi and register with the staged gateway. Unlike START_WYOMING
                 * this does not latch the console: the protocol runs on a socket, so the
                 * console stays usable for diagnostics while the link is up. */
                static char ssid[PROVISION_SSID_MAX + 1];
                static char psk[PROVISION_PSK_MAX + 1];
                static char endpoint[PROVISION_URI_MAX];
                static char report[512];
                char host[PROVISION_HOST_MAX];
                unsigned port = 0;
                int transport = 0;
                ssid[0] = 0;
                psk[0] = 0;
                endpoint[0] = 0;
                if (load_wifi(&storage_ready, ssid, sizeof(ssid), psk, sizeof(psk)) != 0 ||
                    load_endpoint(&storage_ready, endpoint, sizeof(endpoint)) != 0) {
                    reply("ERR GATEWAY STORAGE");
                } else if (ssid[0] == 0 || psk[0] == 0) {
                    reply("ERR GATEWAY WIFI_NOT_STAGED");
                } else if (endpoint[0] == 0) {
                    reply("ERR GATEWAY NO_ENDPOINT");
                } else if (provision_endpoint_parse(endpoint, host, sizeof(host), &port,
                                                     &transport) != 0) {
                    reply("ERR GATEWAY BAD_ENDPOINT");
                } else {
                    reply("GATEWAY BEGIN");
                    if (wifi_station_associate(ssid, psk, report, sizeof(report)) == 0) {
                        reply_lines(report);
                        /* The endpoint's scheme decides the carrier, and only a scheme the parser
                         * actually recognised reaches here. TLS is never downgraded to plaintext:
                         * an unrecognised transport refuses and dials nothing. */
                        if (transport == PROVISION_TRANSPORT_TLS) {
                            gateway_tls_run(host, port, report, sizeof(report));
                        } else if (transport == PROVISION_TRANSPORT_TCP) {
                            gateway_tcp_run(host, port, report, sizeof(report));
                        } else {
                            reply("ERR GATEWAY BAD_TRANSPORT");
                        }
                        reply_lines(report);
                    } else {
                        reply_lines(report);  /* the failed link line, and no socket attempt */
                    }
                    wifi_station_release();
                    reply("GATEWAY END");
                }
                memset(psk, 0, sizeof(psk));
                memset(report, 0, sizeof(report));
                continue;  /* this branch replies for itself */
            } else if (!strcmp(line.data, "TLS_STATUS")) {
                /* What is provisioned, without dialling anything: the full fingerprint is printed
                 * because comparing it against the bridge's own value is the whole check, and a
                 * truncated one would make that comparison guesswork. */
                static tls_credentials credentials;
                static char reason[96];
                char pin[TLS_SERVER_PIN_LEN * 2 + 1];
                if (tls_credentials_probe(&credentials, reason, sizeof(reason)) == 0) {
                    for (size_t index = 0; index < TLS_SERVER_PIN_LEN; index++)
                        snprintf(pin + index * 2, 3, "%02x", credentials.server_pin[index]);
                    snprintf(response, sizeof(response), "OK TLS STAGED host=%s port=%u pin=%s",
                             credentials.server_host, (unsigned)credentials.server_port, pin);
                } else {
                    snprintf(response, sizeof(response), "OK TLS ABSENT reason=%s", reason);
                }
                /* No continue here: like STATUS and the SET_* branches, this one fills the response
                 * buffer and lets the reply at the end of the loop send it. Only the branches that
                 * send several lines of their own reply for themselves. */
            } else if (!strncmp(line.data, "SET_WIFI", 8) || !strcmp(line.data, "CLEAR_WIFI") ||
                       !strcmp(line.data, "WIFI_STATUS"))
                provision_wifi_handle(line.data, save_wifi_ssid, save_wifi_psk, clear_wifi, load_wifi,
                                      &storage_ready, response, sizeof(response));
            else
                provision_handle(line.data, save_endpoint, load_endpoint, &storage_ready,
                                 response, sizeof(response));
            reply(response);
        }
    }
}
