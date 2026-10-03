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

/* Saved networks, next to the staged pair in the same namespace: prof_n holds how many, and
 * p<i>_ssid / p<i>_psk hold each one. Kept in RAM-and-rewrite form rather than by moving keys
 * around, because with four slots the shift is smaller to read and far easier to be sure of. */
#define WIFI_PROFILE_KEY_MAX 16
#define WIFI_PROFILE_SLOTS ((int)PROVISION_WIFI_PROFILE_MAX)

static void profile_key(char *out, size_t capacity, int index, const char *field) {
    snprintf(out, capacity, "p%d_%s", index, field);
}

static int count_profiles(void *ctx) {
    if (!*(bool *)ctx) return -1;
    nvs_handle_t handle;
    if (nvs_open("hermes_wifi", NVS_READONLY, &handle) != ESP_OK) return -1;
    uint8_t count = 0;
    esp_err_t result = nvs_get_u8(handle, "prof_n", &count);
    nvs_close(handle);
    if (result == ESP_ERR_NVS_NOT_FOUND) return 0;
    if (result != ESP_OK) return -1;
    /* A count outside the list's size is a corrupt record, not something to round off. */
    return count <= (uint8_t)WIFI_PROFILE_SLOTS ? (int)count : -1;
}

static int get_profile(void *ctx, int index, char *ssid, size_t ssid_capacity,
                       char *psk, size_t psk_capacity) {
    if (!*(bool *)ctx) return -1;
    if (index < 0 || index >= WIFI_PROFILE_SLOTS) return -1;
    nvs_handle_t handle;
    if (nvs_open("hermes_wifi", NVS_READONLY, &handle) != ESP_OK) return -1;
    char key[WIFI_PROFILE_KEY_MAX];
    profile_key(key, sizeof(key), index, "ssid");
    esp_err_t first = nvs_get_str(handle, key, ssid, &ssid_capacity);
    if (first == ESP_ERR_NVS_NOT_FOUND) ssid[0] = 0;
    profile_key(key, sizeof(key), index, "psk");
    esp_err_t second = nvs_get_str(handle, key, psk, &psk_capacity);
    if (second == ESP_ERR_NVS_NOT_FOUND) psk[0] = 0;
    nvs_close(handle);
    if ((first != ESP_OK && first != ESP_ERR_NVS_NOT_FOUND) ||
        (second != ESP_OK && second != ESP_ERR_NVS_NOT_FOUND)) return -1;
    return 0;
}

/* Write the whole list. Slots that are still in use are overwritten in place and only the
 * now-unused tail is erased, so a device that loses power mid-write keeps the list it had:
 * NVS commits atomically, and there is no window with the list erased but not yet rewritten. */
static int write_profiles(bool *storage, char (*ssids)[PROVISION_SSID_MAX + 1],
                          char (*psks)[PROVISION_PSK_MAX + 1], int count) {
    if (!*storage) return -1;
    if (count < 0 || count > WIFI_PROFILE_SLOTS) return -1;
    nvs_handle_t handle;
    if (nvs_open("hermes_wifi", NVS_READWRITE, &handle) != ESP_OK) return -1;
    char key[WIFI_PROFILE_KEY_MAX];
    esp_err_t result = ESP_OK;
    for (int index = 0; index < WIFI_PROFILE_SLOTS && result == ESP_OK; ++index) {
        if (index < count) {
            profile_key(key, sizeof(key), index, "ssid");
            result = nvs_set_str(handle, key, ssids[index]);
            if (result == ESP_OK) {
                profile_key(key, sizeof(key), index, "psk");
                result = nvs_set_str(handle, key, psks[index]);
            }
        } else {
            profile_key(key, sizeof(key), index, "ssid");
            esp_err_t erased = nvs_erase_key(handle, key);
            if (erased != ESP_OK && erased != ESP_ERR_NVS_NOT_FOUND) result = erased;
            profile_key(key, sizeof(key), index, "psk");
            erased = nvs_erase_key(handle, key);
            if (erased != ESP_OK && erased != ESP_ERR_NVS_NOT_FOUND) result = erased;
        }
    }
    if (result == ESP_OK) result = nvs_set_u8(handle, "prof_n", (uint8_t)count);
    if (result == ESP_OK) result = nvs_commit(handle);
    nvs_close(handle);
    return result == ESP_OK ? 0 : -1;
}

static int save_profile(void *ctx, const char *ssid, const char *psk) {
    if (!*(bool *)ctx) return -1;
    char kept_ssid[WIFI_PROFILE_SLOTS][PROVISION_SSID_MAX + 1];
    char kept_psk[WIFI_PROFILE_SLOTS][PROVISION_PSK_MAX + 1];
    int kept = 0;
    int replaced = 0;
    int count = count_profiles(ctx);
    if (count < 0) return -1;
    for (int index = 0; index < count; ++index) {
        char entry_ssid[PROVISION_SSID_MAX + 1] = {0};
        char entry_psk[PROVISION_PSK_MAX + 1] = {0};
        if (get_profile(ctx, index, entry_ssid, sizeof(entry_ssid), entry_psk, sizeof(entry_psk)) != 0)
            return -1;
        if (strcmp(entry_ssid, ssid) == 0) {
            /* Replace where it already is: the list keeps its order, and a re-saved network
             * cannot drift to the end. */
            snprintf(kept_ssid[kept], sizeof(kept_ssid[kept]), "%s", ssid);
            snprintf(kept_psk[kept], sizeof(kept_psk[kept]), "%s", psk);
            replaced = 1;
        } else {
            memcpy(kept_ssid[kept], entry_ssid, sizeof(kept_ssid[kept]));
            memcpy(kept_psk[kept], entry_psk, sizeof(kept_psk[kept]));
        }
        ++kept;
    }
    if (!replaced) {
        /* The core refuses a save that would overflow the list, so reaching this is a fault in
         * the caller, not a full list: say so instead of quietly dropping a network. */
        if (kept >= WIFI_PROFILE_SLOTS) return -1;
        snprintf(kept_ssid[kept], sizeof(kept_ssid[kept]), "%s", ssid);
        snprintf(kept_psk[kept], sizeof(kept_psk[kept]), "%s", psk);
        ++kept;
    }
    return write_profiles((bool *)ctx, kept_ssid, kept_psk, kept);
}

static int remove_profile(void *ctx, const char *ssid) {
    if (!*(bool *)ctx) return -1;
    char kept_ssid[WIFI_PROFILE_SLOTS][PROVISION_SSID_MAX + 1];
    char kept_psk[WIFI_PROFILE_SLOTS][PROVISION_PSK_MAX + 1];
    int kept = 0;
    int found = 0;
    int count = count_profiles(ctx);
    if (count < 0) return -1;
    for (int index = 0; index < count; ++index) {
        char entry_ssid[PROVISION_SSID_MAX + 1] = {0};
        char entry_psk[PROVISION_PSK_MAX + 1] = {0};
        if (get_profile(ctx, index, entry_ssid, sizeof(entry_ssid), entry_psk, sizeof(entry_psk)) != 0)
            return -1;
        if (strcmp(entry_ssid, ssid) == 0) { found = 1; continue; }
        memcpy(kept_ssid[kept], entry_ssid, sizeof(kept_ssid[kept]));
        memcpy(kept_psk[kept], entry_psk, sizeof(kept_psk[kept]));
        ++kept;
    }
    if (!found) return -1;
    return write_profiles((bool *)ctx, kept_ssid, kept_psk, kept);
}

static int clear_profiles(void *ctx) {
    char empty_ssid[WIFI_PROFILE_SLOTS][PROVISION_SSID_MAX + 1];
    char empty_psk[WIFI_PROFILE_SLOTS][PROVISION_PSK_MAX + 1];
    memset(empty_ssid, 0, sizeof(empty_ssid));
    memset(empty_psk, 0, sizeof(empty_psk));
    return write_profiles((bool *)ctx, empty_ssid, empty_psk, 0);
}

static const provision_wifi_profiles WIFI_PROFILE_STORE = {
    .save = save_profile,
    .remove = remove_profile,
    .clear = clear_profiles,
    .count = count_profiles,
    .get = get_profile,
};

#define WIFI_CANDIDATE_MAX ((size_t)PROVISION_WIFI_PROFILE_MAX + 1)

/* What a join should try, and which one it picked. Returns 1 with `chosen` set when a network
 * was chosen, 0 when the device holds none at all, -1 when the store could not be read, and -2
 * when it holds networks but none of them is in range. With a single candidate no scan is run:
 * there is nothing to choose between, and that path must stay exactly as reliable as it was. */
static int choose_candidate(bool *storage, char (*ssids)[PROVISION_SSID_MAX + 1],
                            char (*psks)[PROVISION_PSK_MAX + 1], size_t *count, size_t *chosen,
                            bool *from_staged, char *out, size_t capacity) {
    *count = 0;
    *chosen = 0;
    *from_staged = false;
    if (provision_wifi_candidates(&WIFI_PROFILE_STORE, load_wifi, storage, ssids, psks,
                                  WIFI_CANDIDATE_MAX, count) != 0)
        return -1;
    if (*count == 0) return 0;
    int saved = count_profiles(storage);
    if (saved < 0) return -1;
    if (*count == 1) {
        *from_staged = saved == 0;
        return 1;
    }
    const char *names[WIFI_CANDIDATE_MAX];
    int rank[WIFI_CANDIDATE_MAX];
    for (size_t index = 0; index < *count; ++index) names[index] = ssids[index];
    if (wifi_station_scan_match(names, *count, rank, out, capacity) != 0) return -1;
    int best = -1;
    for (size_t index = 0; index < *count; ++index)
        if (rank[index] != WIFI_STATION_NO_SIGNAL && (best < 0 || rank[index] > rank[best]))
            best = (int)index;
    if (best < 0) return -2;  /* every network is out of range: do not dial into the dark */
    *chosen = (size_t)best;
    *from_staged = (int)*chosen >= saved;  /* candidates put the saved networks first */
    return 1;
}

/* Everything START_GATEWAY needs before it dials: the endpoint parsed, and a network chosen.
 * The status codes are the ones the console reports, and the order below keeps the replies what
 * they always were (what is wrong with the Wi-Fi is reported before what is wrong with the
 * endpoint) even though the endpoint is parsed first because it is cheap and radio-free. */
static int prepare_gateway(bool *storage, char *endpoint, size_t endpoint_capacity,
                           char *host, size_t host_capacity, unsigned *port, int *transport,
                           char (*ssids)[PROVISION_SSID_MAX + 1],
                           char (*psks)[PROVISION_PSK_MAX + 1], size_t *chosen, bool *from_staged,
                           char *out, size_t capacity) {
    if (load_endpoint(storage, endpoint, endpoint_capacity) != 0) return -1;
    int endpoint_state = 1;
    if (endpoint[0] == 0) endpoint_state = -3;
    else if (provision_endpoint_parse(endpoint, host, host_capacity, port, transport) != 0)
        endpoint_state = -4;

    size_t count = 0;
    int wifi = choose_candidate(storage, ssids, psks, &count, chosen, from_staged, out, capacity);
    if (wifi == -1) return -1;
    if (wifi == 0) return 0;
    if (endpoint_state != 1) return endpoint_state;
    return wifi;  /* 1 when chosen, -2 when every known network is out of range */
}

static void reply(const char *value) {
    char output[272];
    int length = snprintf(output, sizeof(output), "PROVISION %s\n", value);
    if (length <= 0 || (size_t)length >= sizeof(output)) return;
    /* One writer, finite budget. Never retry a partial response as a new line. */
    usb_serial_jtag_write_bytes(output, (size_t)length, pdMS_TO_TICKS(250));
}

/* Write one finished line. Every emitter goes through here, so the single-writer rule has one
 * implementation rather than one per reply shape. */
static void emit_line(const char *text, size_t length) {
    if (length > 0) usb_serial_jtag_write_bytes(text, length, pdMS_TO_TICKS(250));
}

/* A body whose lines already carry their own label, one prefixed line at a time, so the reader
 * never has to remember where a block started. */
static void reply_prefixed(const char *block) {
    const char *start = block;
    while (*start) {
        const char *end = strchr(start, '\n');
        size_t length = end ? (size_t)(end - start) : strlen(start);
        if (length > 0 && length < 200) {
            char output[272];
            int written = snprintf(output, sizeof(output), "PROVISION %.*s\n", (int)length, start);
            if (written > 0 && (size_t)written < sizeof(output)) emit_line(output, (size_t)written);
        }
        if (!end) break;
        start = end + 1;
    }
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
                static char ssids[WIFI_CANDIDATE_MAX][PROVISION_SSID_MAX + 1];
                static char psks[WIFI_CANDIDATE_MAX][PROVISION_PSK_MAX + 1];
                static char report[192];
                char framed[224];
                size_t count = 0;
                size_t chosen = 0;
                bool from_staged = false;
                int picked = choose_candidate(&storage_ready, ssids, psks, &count, &chosen,
                                              &from_staged, report, sizeof(report));
                if (picked == -1) {
                    reply("ERR WIFI STORAGE");
                } else if (picked == 0) {
                    reply("ERR WIFI NOT_STAGED");
                } else if (picked == -2) {
                    reply("ERR WIFI NO_KNOWN_NETWORK");
                } else {
                    reply("WIFI CONNECT BEGIN");
                    /* Which network was used, before the outcome: a device holding several
                     * networks has to be able to say which one it joined. */
                    snprintf(framed, sizeof(framed), "profile source=%s index=%u ssid=%s\n",
                             from_staged ? "staged" : "saved", (unsigned)chosen, ssids[chosen]);
                    reply_block(framed);
                    wifi_station_connect(ssids[chosen], psks[chosen], report, sizeof(report));
                    snprintf(framed, sizeof(framed), "%s\n", report);
                    reply_block(framed);
                    reply("WIFI CONNECT END");
                }
                /* The passphrases are not needed after the attempt and are not kept. */
                memset(psks, 0, sizeof(psks));
                memset(report, 0, sizeof(report));
                continue;  /* this branch replies for itself */
            } else if (!strcmp(line.data, "WIFI_PROFILES")) {
                /* Multi-line, so it frames itself line by line rather than through the
                 * single-line response buffer. */
                static char listing[600];
                if (provision_wifi_handle(line.data, save_wifi_ssid, save_wifi_psk, clear_wifi,
                                          load_wifi, &WIFI_PROFILE_STORE, &storage_ready,
                                          listing, sizeof(listing)) == 0)
                    reply_prefixed(listing);
                else
                    reply(listing);  /* the module writes a one-line reason */
                continue;  /* this branch replies for itself */
            } else if (!strcmp(line.data, "START_GATEWAY")) {
                /* Join over Wi-Fi and register with the staged gateway. Unlike START_WYOMING
                 * this does not latch the console: the protocol runs on a socket, so the
                 * console stays usable for diagnostics while the link is up. */
                static char ssids[WIFI_CANDIDATE_MAX][PROVISION_SSID_MAX + 1];
                static char psks[WIFI_CANDIDATE_MAX][PROVISION_PSK_MAX + 1];
                static char endpoint[PROVISION_URI_MAX];
                static char report[512];
                char framed[224];
                char host[PROVISION_HOST_MAX];
                unsigned port = 0;
                int transport = 0;
                size_t chosen = 0;
                bool from_staged = false;
                endpoint[0] = 0;
                int picked = prepare_gateway(&storage_ready, endpoint, sizeof(endpoint), host,
                                             sizeof(host), &port, &transport, ssids, psks,
                                             &chosen, &from_staged, report, sizeof(report));
                if (picked == -1) {
                    reply("ERR GATEWAY STORAGE");
                } else if (picked == 0) {
                    reply("ERR GATEWAY WIFI_NOT_STAGED");
                } else if (picked == -3) {
                    reply("ERR GATEWAY NO_ENDPOINT");
                } else if (picked == -4) {
                    reply("ERR GATEWAY BAD_ENDPOINT");
                } else if (picked == -2) {
                    reply("ERR GATEWAY NO_KNOWN_NETWORK");
                } else {
                    reply("GATEWAY BEGIN");
                    /* Which network was used, before the outcome. */
                    snprintf(framed, sizeof(framed), "profile source=%s index=%u ssid=%s\n",
                             from_staged ? "staged" : "saved", (unsigned)chosen, ssids[chosen]);
                    reply_block(framed);
                    if (wifi_station_associate(ssids[chosen], psks[chosen], report, sizeof(report)) == 0) {
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
                memset(psks, 0, sizeof(psks));
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
                       !strcmp(line.data, "WIFI_STATUS") ||
                       !strcmp(line.data, "WIFI_PROFILE_SAVE") ||
                       !strcmp(line.data, "WIFI_PROFILE_CLEAR") ||
                       !strncmp(line.data, "WIFI_PROFILE_REMOVE ", 20))
                provision_wifi_handle(line.data, save_wifi_ssid, save_wifi_psk, clear_wifi, load_wifi,
                                      &WIFI_PROFILE_STORE, &storage_ready, response, sizeof(response));
            else
                provision_handle(line.data, save_endpoint, load_endpoint, &storage_ready,
                                 response, sizeof(response));
            reply(response);
        }
    }
}
