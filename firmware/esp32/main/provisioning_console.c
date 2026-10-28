#include "provisioning_console.h"
#include "usb_wyoming.h"
#include "provisioning.h"
#include "device_identity.h"
#include "wifi_station.h"
#include "audio_bringup.h"
#include "audio_playback.h"
#include "wyoming_audio.h"   /* the scalar-control frame encoders (wy_control_f32/i32_command) */
#include <stdlib.h>
#include <string.h>
#include <math.h>
#ifndef M_PI
#define M_PI 3.14159265358979323846   /* not in strict C11 */
#endif
#include "report_tail.h"
#include "gateway_tcp.h"
#include "gateway_tls.h"
#include "gateway_task.h"
#include "tls_credentials_store.h"
#include "wake_detector.h"
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

/* The device identity fields (the Pi reads these from env: VOICE_DEVICE_NAME /
 * VOICE_DEVICE_AREA / VOICE_FIRMWARE_VERSION). Staged to the hermes_cfg namespace so the
 * registration and describe/info report the real name, area and firmware version instead of
 * the placeholders. A failure is reported as ERR STORAGE, never silently dropped. */
static int save_identity_field(void *ctx, const char *key, const char *value) {
    if (!*(bool *)ctx) return -1;
    nvs_handle_t handle;
    if (nvs_open("hermes_cfg", NVS_READWRITE, &handle) != ESP_OK) return -1;
    esp_err_t result = nvs_set_str(handle, key, value);
    if (result == ESP_OK) result = nvs_commit(handle);
    nvs_close(handle);
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
 * was chosen, 0 when the device holds none at all, -1 when the store could not be read, -2 when
 * it holds networks but none of them is in range, and -3 when the scan itself failed (a radio
 * or transport error, not a statement about which networks exist). With a single candidate no
 * scan is run: there is nothing to choose between, and that path must stay exactly as reliable
 * as it was. */
static int choose_candidate(bool *storage, char (*ssids)[PROVISION_SSID_MAX + 1],
                            char (*psks)[PROVISION_PSK_MAX + 1], size_t *count, size_t *chosen,
                            bool *from_staged, char *out, size_t capacity) {
    *count = 0;
    *chosen = 0;
    *from_staged = false;
    size_t saved_split = 0;
    if (provision_wifi_candidates(&WIFI_PROFILE_STORE, load_wifi, storage, ssids, psks,
                                  WIFI_CANDIDATE_MAX, count, &saved_split) != 0)
        return -1;
    if (*count == 0) return 0;
    if (*count == 1) {
        /* The lone candidate is the staged pair only when no saved network made it into the list:
         * saved_split counts the saved candidates that survived, so a skipped (now-invalid) saved
         * profile does not flip this label. */
        *from_staged = saved_split == 0;
        return 1;
    }
    const char *names[WIFI_CANDIDATE_MAX];
    int rank[WIFI_CANDIDATE_MAX];
    for (size_t index = 0; index < *count; ++index) names[index] = ssids[index];
    /* A scan that could not run is a transient radio condition, not "no joinable network": the
     * caller decides what to do with a network it simply could not see. */
    if (wifi_station_scan_match(names, *count, rank, out, capacity) != 0) return -3;
    int best = -1;
    for (size_t index = 0; index < *count; ++index)
        if (rank[index] != WIFI_STATION_NO_SIGNAL && (best < 0 || rank[index] > rank[best]))
            best = (int)index;
    if (best < 0) return -2;  /* every network is out of range: do not dial into the dark */
    *chosen = (size_t)best;
    *from_staged = (size_t)best >= saved_split;  /* candidates put the saved networks first */
    return 1;
}

/* Everything START_GATEWAY needs before it dials: the endpoint parsed, and a network chosen.
 * The status codes are the ones the console reports, and the order below keeps the replies what
 * they always were (what is wrong with the Wi-Fi is reported before what is wrong with the
 * endpoint) even though the endpoint is parsed first because it is cheap and radio-free. The
 * codes: 1 chosen; 0 no network held; -1 the store could not be read; -2 every known network is
 * out of range; -3 no endpoint staged; -4 the endpoint is malformed; -5 the scan could not run
 * (a radio fault -- distinct from a missing store, which would be -1). */
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
    if (wifi == -3) return -5;  /* the scan could not run: a radio fault, distinct from a missing store */
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

/* The framed actions (scan, connect, audio bring-up) refuse with a `PROVISION ERR ...` line,
 * the same vocabulary a successful `exchange` surfaces. That is what makes a device-side refusal
 * a complete, immediately-recognizable answer for the provisioning tool: a raw, unprefixed
 * `ERROR ...` line is not a `PROVISION` reply at all, so a block reader waiting for the action's
 * END marker would stall until its deadline and then discard the reason the device already gave.
 * The radio module writes its reason as `ERROR <reason>\n`; strip that prefix and any trailing
 * whitespace so the line reads `ERR WIFI SCAN <reason>` rather than carrying the module's own
 * prefix twice. */
static void reply_scan_refusal(const char *report, const char *action) {
    const char *reason = report ? report : "";
    if (strncmp(reason, "ERROR ", 6) == 0) reason += 6;
    char line[160];
    size_t length = strlen(reason);
    while (length > 0 && (reason[length - 1] == '\n' || reason[length - 1] == '\r' ||
                          reason[length - 1] == ' '))
        --length;
    line[0] = 0;
    snprintf(line, sizeof(line), "%.*s", (int)length, reason);
    char full[192];
    snprintf(full, sizeof(full), "ERR %s %s", action, line);
    reply(full);
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
 * hard cap so a large result cannot stall the console. The cap is the scan report's own size
 * (the buffer and the budget are one constant), so a report the renderer can honestly produce
 * is always delivered in full, truncation line and all. */
static void reply_block(const char *value) {
    size_t length = strlen(value);
    if (length == 0 || length > WIFI_STATION_SCAN_REPORT_MAX) return;
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

/* Register-on-boot: once, after the READY banner and before the command loop. It reads the saved
 * endpoint and the networks the device holds, asks the pure decision whether that is "dial now" or
 * "idle", and acts. On GO it picks a network and hands it to the persistent gateway task (item 1),
 * which then owns the session for the life of the device -- the radio, the dial, and the reconnect
 * all happen off this thread. On idle it says why, in the same words a manual START_GATEWAY would.
 *
 * A configured network whose access point is off at this moment is still a GO: the decision is
 * about what is configured, and the task's backoff is the retry that finds the AP when it returns.
 * The scan here only ranks which configured network is strongest right now; it never makes the
 * idle/GO call, which the pure decision already made. */
static void provisioning_boot_gate(bool *storage) {
    static char endpoint[PROVISION_URI_MAX];
    static char ssids[WIFI_CANDIDATE_MAX][PROVISION_SSID_MAX + 1];
    static char psks[WIFI_CANDIDATE_MAX][PROVISION_PSK_MAX + 1];
    static char report[256];
    char host[PROVISION_HOST_MAX];
    char reason[24];
    unsigned port = 0;
    int transport = 0;
    size_t count = 0;
    size_t chosen = 0;
    bool from_staged = false;

    if (load_endpoint(storage, endpoint, sizeof(endpoint)) != 0) {
        reply("GATEWAY BOOT IDLE reason=STORAGE");
        return;
    }
    int stored = 0;
    if (provision_wifi_candidates(&WIFI_PROFILE_STORE, load_wifi, storage, ssids, psks,
                                  WIFI_CANDIDATE_MAX, &count, NULL) != 0)
        stored = -1;
    else
        stored = (int)count;
    if (provision_boot_decision(endpoint, stored, reason, sizeof(reason)) == BOOT_IDLE) {
        char framed[272];
        snprintf(framed, sizeof(framed), "GATEWAY BOOT IDLE reason=%s", reason);
        reply(framed);
        return;
    }

    provision_endpoint_parse(endpoint, host, sizeof(host), &port, &transport);
    int pick = choose_candidate(storage, ssids, psks, &count, &chosen, &from_staged, report,
                                sizeof(report));
    if (pick == -2 || pick == -3) {
        /* -2: every configured network is out of range right now. -3: the scan itself could not
         * run (a transient radio error), so we do not even know which are in range. Either way the
         * networks are configured and the answer is the same: start the task on the first one. It
         * keeps re-associating on its backoff and will register the moment an AP appears (or the
         * radio recovers). A -2 can only be reached with two or more candidates, and the saved
         * networks come first, so candidate 0 is a saved one; with -3 the ranking is unknown but
         * candidate 0 is still the first saved network the store offers. */
        chosen = 0;
        from_staged = false;
    } else if (pick <= 0) {
        /* -1: the store could not be read, or 0: the device holds no networks at all. Idle is the
         * honest answer to both; the reason above already said which. */
        reply("GATEWAY BOOT IDLE reason=NO_JOINABLE");
        memset(psks, 0, sizeof(psks));
        return;
    }
    reply("GATEWAY BOOT");
    char framed[224];
    snprintf(framed, sizeof(framed), "profile source=%s index=%u ssid=%s",
             from_staged ? "staged" : "saved", (unsigned)chosen, ssids[chosen]);
    reply(framed);
    if (gateway_task_start(host, port, transport, ssids[chosen], psks[chosen], report,
                           sizeof(report)) == 0)
        reply("GATEWAY BOOT RUNNING");
    else
        reply("GATEWAY BOOT FAILED reason=NO_TASK");
    memset(psks, 0, sizeof(psks));
    memset(report, 0, sizeof(report));
}

void provisioning_console_run(void) {
    /* Preserve existing flash: incompatible/full NVS is an error, never an erase. */
    bool storage_ready = nvs_flash_init() == ESP_OK;
    usb_serial_jtag_driver_config_t config = {.tx_buffer_size = 512, .rx_buffer_size = 512};
    if (usb_serial_jtag_driver_install(&config) != ESP_OK) return;
    reply("READY 1 NETWORK_INACTIVE AUDIO_INACTIVE");
    /* A provisioned device now registers itself at boot instead of waiting for a command; an
     * unprovisioned one says why it is idle and leaves the console free for setup. */
    provisioning_boot_gate(&storage_ready);
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
                static char report[WIFI_STATION_SCAN_REPORT_MAX];
                if (gateway_task_active()) {
                    reply("ERR WIFI BUSY");  /* the gateway holds the radio; a scan would drop it */
                } else if (wifi_station_scan(report, sizeof(report)) == 0) {
                    reply("WIFI SCAN BEGIN");
                    reply_block(report);
                    reply("WIFI SCAN END");
                } else {
                    /* The module wrote a one-line reason. A failed scan answers with that reason
                     * and then closes the frame it promised: the provisioning tool's scan block
                     * waits for its END marker, and a bare reason line would leave the block
                     * open until the reader's deadline, discarding the very fault that was
                     * reported. */
                    reply_scan_refusal(report, "WIFI SCAN");
                    reply("WIFI SCAN END");
                }
                continue;  /* this branch replies for itself; response[] was never filled */
            } else if (!strcmp(line.data, "AUDIO_BRINGUP")) {
                /* Item-6 audio bring-up: initialize the I2S interface and confirm the XVF3800
                 * codec answers over I2C, then tear both down. No stream is started and the
                 * amplifier is never armed, so it does not conflict with the gateway (which uses
                 * the radio, not I2S/I2C). The report is one self-describing line. */
                static char report[256];
                if (audio_bringup_run(report, sizeof(report)) == 0) {
                    reply("AUDIO BRINGUP BEGIN");
                    reply(report);
                    reply("AUDIO BRINGUP END");
                } else {
                    reply_block(report);  /* the module wrote a one-line reason */
                }
                continue;  /* this branch replies for itself */
            } else if (!strcmp(line.data, "WIFI_CONNECT")) {
                static char ssids[WIFI_CANDIDATE_MAX][PROVISION_SSID_MAX + 1];
                static char psks[WIFI_CANDIDATE_MAX][PROVISION_PSK_MAX + 1];
                static char report[192];
                char framed[224];
                size_t count = 0;
                size_t chosen = 0;
                bool from_staged = false;
                if (gateway_task_active()) {
                    reply("ERR WIFI BUSY");  /* the gateway holds the radio; a join would drop it */
                    continue;  /* this branch replies for itself */
                }
                int picked = choose_candidate(&storage_ready, ssids, psks, &count, &chosen,
                                              &from_staged, report, sizeof(report));
                if (picked == -1) {
                    reply("ERR WIFI STORAGE");
                    reply("WIFI CONNECT END");  /* close the frame the action promised */
                } else if (picked == 0) {
                    reply("ERR WIFI NOT_STAGED");
                    reply("WIFI CONNECT END");
                } else if (picked == -3) {
                    /* A scan that could not run is a radio fault, not a missing configuration: a
                     * store error would have been -1, above. */
                    reply("ERR WIFI RADIO");
                    reply("WIFI CONNECT END");
                } else if (picked == -2) {
                    reply("ERR WIFI NO_KNOWN_NETWORK");
                    reply("WIFI CONNECT END");
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
                 * console stays usable for diagnostics while the link is up. A persistent gateway
                 * already running (auto-started at boot) owns the radio, so a manual run is refused
                 * rather than racing it. */
                if (gateway_task_active()) {
                    reply("ERR GATEWAY ALREADY_RUNNING");
                    continue;  /* this branch replies for itself */
                }
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
                } else if (picked == -5) {
                    /* The radio could not run its scan: a transient fault, not a configuration
                     * problem, so the label must not say the store is missing. */
                    reply("ERR GATEWAY RADIO");
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
            } else if (!strcmp(line.data, "GATEWAY_STATUS")) {
                /* A read-only query: what is the persistent link doing right now? The soak
                 * harness (and a person at the console) reads this while the link runs, so it
                 * must name the link's last action without dialling, associating, or touching
                 * the radio. An empty report while running is a real answer, not an error. */
                static char status_report[256];
                static char status_line[128];
                if (gateway_task_active()) {
                    gateway_task_report(status_report, sizeof(status_report));
                    /* The task's report is line-terminated (every line ends with '\n'), and it
                     * grows for the life of the session, so the last line's TEXT is what the
                     * status must carry: report_tail_last_line would return the empty string
                     * after the final newline, and a prefix copy of a long report would show
                     * the oldest lines. report_tail_last_complete_line takes the newest
                     * complete line from the END of the report; the prefix is a constant that
                     * fits with room, and the line is bounded against whatever room it left. */
                    int used = snprintf(status_line, sizeof(status_line),
                                        "OK GATEWAY RUNNING last=");
                    if (used > 0)
                        report_tail_last_complete_line(status_report, status_line + (size_t)used,
                                                       sizeof(status_line) - (size_t)used);
                    reply(status_line);
                } else {
                    reply("OK GATEWAY IDLE");
                }
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
            } else if (!strcmp(line.data, "ENROLL_STATUS")) {
                /* The enrollment contract's honest-reporting surface (increment 5). The device has
                 * no authenticated enrollment flow -- its identity is a provisioned client
                 * certificate written out of band over USB -- and it must never claim otherwise.
                 * This reports that real state, so a bring-up can confirm the device is not
                 * pretending to be enrolled. */
                snprintf(response, sizeof(response),
                         "OK ENROLL ABSENT authenticated=no detail=out_of_band");
            } else if (!strcmp(line.data, "DEVICE_ID")) {
                /* Who this board is, read-only: the identity it derives from its own factory
                 * MAC -- the same one the gateway and USB registration paths use -- so a host
                 * tool can read it off the board instead of being told it. Nothing is staged,
                 * dialed or changed; an unreadable MAC is reported as an error, because the
                 * identity is then unprovable rather than merely different. */
                char device_id[32];
                char boot_id[40];
                if (device_identity(device_id, sizeof(device_id), boot_id, sizeof(boot_id)) == 0)
                    snprintf(response, sizeof(response), "OK DEVICE_ID %s", device_id);
                else
                    snprintf(response, sizeof(response), "ERR DEVICE_ID UNREADABLE");
            } else if (!strncmp(line.data, "XVF_UPSAMPLE ", 13)) {
                /* "XVF_UPSAMPLE <L> <R>" -- set the Audio Manager's per-channel output upsampling
                 * flags. In a 48 kHz interface configuration the chip defaults these to 1, which
                 * rate-converts our already-48 kHz playback down to 16 kHz and back before it
                 * reaches the speaker, band-limiting answers to 8 kHz. Try:
                 *   XVF_UPSAMPLE 1 1   (the chip's default)
                 *   XVF_UPSAMPLE 0 0   (keep the native 48 kHz all the way to the DAC)
                 * paired with "XVF_OP 4 0" versus "XVF_OP 10 0", and compare the tone test by ear:
                 * the difference between the two pairs is the top octave of the voice. */
                int lo = 0, ro = 0;
                if (sscanf(line.data, "XVF_UPSAMPLE %d %d", &lo, &ro) != 2 ||
                    (lo != 0 && lo != 1) || (ro != 0 && ro != 1)) {
                    snprintf(response, sizeof(response),
                             "ERR XVF_UPSAMPLE usage: XVF_UPSAMPLE <0|1> <0|1>");
                } else {
                    int rc = audio_xvf_output_upsample(lo, ro);
                    snprintf(response, sizeof(response), "%s XVF_UPSAMPLE L=%d R=%d",
                             rc == 0 ? "OK" : "ERR", lo, ro);
                }
            } else if (!strncmp(line.data, "XVF_READ ", 9)) {
                /* "XVF_READ <resid> <cmd> [bytes]" -- query any XVF3800 control and print the
                 * raw reply. Byte 0 of the reply is the status (0 = ok, 64 = the chip asked to be
                 * retried). Two reads matter for the 48 kHz work:
                 *   XVF_READ 35 24 1   -> I2S_INACTIVE: 0 = the co-processor's I2S path is ALIVE,
                 *                         1 = inactive. This is the direct answer to "is the link
                 *                         running at all", with no logic analyzer involved.
                 *   XVF_READ 20 18 4   -> DOA_VALUE: DOA angle (0-359) plus a speech-detection
                 *                         flag, computed by the co-processor from its own microphones.
                 *                         If it reports speech while our wake scorer never crosses its
                 *                         cutoff, the fault is in the link or the samples; if it reports
                 *                         silence, the microphone front end is the suspect.
                 * Command IDs 18/24 are version-dependent (DOA_VALUE and LED_RING_COLOR swapped at
                 * image v1.0.8), so read the version first: XVF_READ 48 0 4. */
                unsigned r = 0, cm = 0, nbytes = 1;
                int got = sscanf(line.data, "XVF_READ %u %u %u", &r, &cm, &nbytes);
                if (got < 2 || nbytes < 1 || nbytes > 16) {
                    snprintf(response, sizeof(response),
                             "ERR XVF_READ usage: XVF_READ <resid> <cmd> [bytes 1-16]");
                } else {
                    uint8_t cmd[3] = { (uint8_t)r, (uint8_t)(cm | 0x80), (uint8_t)nbytes };
                    uint8_t reply[17] = {0};
                    int rc = audio_xvf_read(cmd, sizeof(cmd), reply, (size_t)nbytes + 1u);
                    if (rc != 0) {
                        snprintf(response, sizeof(response), "ERR XVF_READ %u %u bus=%d", r, cm, rc);
                    } else {
                        /* Status first, then the data, so a retry (64) is not misread as a value. */
                        int used = snprintf(response, sizeof(response), "OK XVF %u/%u st=%u",
                                            r, cm, reply[0]);
                        for (int i = 1; i <= (int)nbytes && used < (int)sizeof(response) - 4; ++i)
                            used += snprintf(response + used, sizeof(response) - (size_t)used,
                                             " %02x", reply[i]);
                    }
                }
            } else if (!strncmp(line.data, "XVF_SET32 ", 10) || !strncmp(line.data, "XVF_SETF ", 9)) {
                /* "XVF_SET32 <resid> <cmd> <value>"  -> int32 scalar
                 * "XVF_SETF  <resid> <cmd> <value>"  -> IEEE-754 float scalar
                 *
                 * Both send [resid, cmd, 1, b0, b1, b2, b3]: ONE value, FOUR bytes. That count
                 * field is what the chip requires for its float and int32 parameters, and it is
                 * exactly what XVF_WRITE cannot produce (it always sets count = the number of
                 * bytes supplied), so until now these controls were unreachable from the console.
                 *
                 * The PP Servicer (ResID 17) holds the co-processor's own AGC, which this
                 * firmware has never enabled. Measured on this board, speech arrives at RMS
                 * ~220-700 while the room floor is ~300 -- quieter than the room -- which is why
                 * the wake word only fires sometimes. The live levers:
                 *   XVF_SET32 17 10 1       AGC on
                 *   XVF_SETF  17 11 30.0    AGC max gain  [1.0 .. 1000.0]
                 *   XVF_SETF  17 12 0.001   AGC target power [1e-8 .. 1.0]
                 * Audio Manager mic gain is the same shape:
                 *   XVF_SETF  35 0 4.0
                 * The reply echoes the bytes actually sent, so a malformed frame is visible. */
                unsigned r = 0, cm = 0;
                double v = 0.0;
                int is_float = !strncmp(line.data, "XVF_SETF ", 9);
                int got = is_float ? sscanf(line.data, "XVF_SETF %u %u %lf", &r, &cm, &v)
                                   : sscanf(line.data, "XVF_SET32 %u %u %lf", &r, &cm, &v);
                if (got != 3 || r > 255 || cm > 255) {
                    snprintf(response, sizeof(response), "ERR usage: %s <resid> <cmd> <value>",
                             is_float ? "XVF_SETF" : "XVF_SET32");
                } else {
                    uint8_t frame[XVF_I2C_SET32_CMD_LEN];
                    if (is_float)
                        wy_control_f32_command(frame, (uint8_t)r, (uint8_t)cm, (float)v);
                    else
                        wy_control_i32_command(frame, (uint8_t)r, (uint8_t)cm, (int32_t)v);
                    int rc = audio_xvf_write_frame(frame, sizeof(frame));
                    snprintf(response, sizeof(response),
                             "%s %s %u/%u = %g  [%02x %02x %02x %02x %02x %02x %02x]",
                             rc == 0 ? "OK" : "ERR", is_float ? "SETF" : "SET32", r, cm, v,
                             frame[0], frame[1], frame[2], frame[3], frame[4], frame[5], frame[6]);
                }
            } else if (!strncmp(line.data, "XVF_BYTES ", 10)) {
                /* "XVF_BYTES <resid> <cmd> <b0> [b1 ... b15]" -- write a raw byte payload.
                 *
                 * Needed for AUDIO_MGR_OP_ALL (ResID 35, CmdID 23, 12 bytes), which is how the
                 * XMOS native-rate far end is ACTUALLY configured. The Programming Guide's own
                 * native loopback test sets all six selections at once:
                 *   XVF_BYTES 35 23 10 0 10 2 10 4 10 1 10 3 10 5
                 * Setting only OP_L/OP_R changes two of the six; with native-rate output selected
                 * the untouched selections still carry their old categories, so unrelated or
                 * stale signals interleave into the output. That is a candidate explanation for
                 * the native 48 kHz tap being far rougher than the band-limited one. */
                unsigned r = 0, cm = 0;
                int consumed = 0;
                if (sscanf(line.data, "XVF_BYTES %u %u%n", &r, &cm, &consumed) != 2 ||
                    r > 255 || cm > 255) {
                    snprintf(response, sizeof(response),
                             "ERR usage: XVF_BYTES <resid> <cmd> <b0> [b1 ... b15]");
                } else {
                    uint8_t frame[19];
                    int nb = 0;
                    const char *p = line.data + consumed;
                    while (*p && nb < 16) {
                        int v = 0, span = 0;
                        if (sscanf(p, " %d%n", &v, &span) != 1) break;
                        if (v < 0 || v > 255) break;
                        frame[3 + nb] = (uint8_t)v;
                        nb++;
                        p += span;
                    }
                    if (nb == 0) {
                        snprintf(response, sizeof(response),
                                 "ERR XVF_BYTES needs at least one payload byte");
                    } else {
                        frame[0] = (uint8_t)r;
                        frame[1] = (uint8_t)cm;
                        frame[2] = (uint8_t)nb;
                        int rc = audio_xvf_write_frame(frame, (size_t)(3 + nb));
                        int used = snprintf(response, sizeof(response),
                                            "%s XVF_BYTES %u/%u (%d bytes)%s",
                                            rc == 0 ? "OK" : "ERR", r, cm, nb, nb ? ":" : "");
                        for (int i = 0; i < nb && used < (int)sizeof(response) - 4; ++i)
                            used += snprintf(response + used,
                                             sizeof(response) - (size_t)used, " %02x",
                                             frame[3 + i]);
                    }
                }
            } else if (!strncmp(line.data, "XVF_WRITE ", 10)) {
                /* "XVF_WRITE <resid> <cmd> <p0> [p1 ...]" -- probe any XVF3800 control.
                 * ResID 35 (Audio Manager) cmd 27 is I2S_DAC_DSP_ENABLE, the switch that
                 * gates whether far-end audio is forwarded to the DAC at all. */
                unsigned r = 0, cm = 0;
                int p[8], np = 0;
                int got = sscanf(line.data, "XVF_WRITE %u %u %d %d %d %d",
                                 &r, &cm, &p[0], &p[1], &p[2], &p[3]);
                if (got < 3) {
                    snprintf(response, sizeof(response),
                             "ERR XVF_WRITE usage: XVF_WRITE <resid> <cmd> <p0> [p1 ...]");
                } else {
                    np = got - 2;
                    uint8_t params[8];
                    for (int i = 0; i < np; ++i) params[i] = (uint8_t)p[i];
                    int rc = audio_xvf_write((uint8_t)r, (uint8_t)cm, params, (size_t)np);
                    snprintf(response, sizeof(response), "%s XVF_WRITE %u %u (%d params)",
                             rc == 0 ? "OK" : "ERR", r, cm, np);
                }
            } else if (!strncmp(line.data, "AIC_WRITE ", 10)) {
                /* "AIC_WRITE <reg> <val>" -- set any AIC3104 register live. Register 7 is the
                 * interesting one: 0x00 disconnects both DAC data paths (the DACs stay powered,
                 * the output drivers stay live), 0x0A connects left->left and right->right.
                 * Comparing those two isolates whether noise enters through the digital data
                 * path or from the analog side. */
                unsigned reg = 0, val = 0;
                if (sscanf(line.data, "AIC_WRITE %u %u", &reg, &val) != 2 || reg > 255 || val > 255) {
                    snprintf(response, sizeof(response), "ERR AIC_WRITE usage: AIC_WRITE <reg 0-255> <val 0-255>");
                } else {
                    int wc = audio_dac_write((uint8_t)reg, (uint8_t)val);
                    uint8_t v = 0;
                    int rd = audio_dac_read((uint8_t)reg, &v);
                    snprintf(response, sizeof(response), "%s AIC_WRITE %u=%u (read back %u)",
                             (wc == 0 && rd == 0) ? "OK" : "ERR", reg, val, (unsigned)v);
                }
            } else if (!strncmp(line.data, "AIC_READ", 8)) {
                /* "AIC_READ" with no argument dumps the registers that decide whether the
                 * codec is actually running: clocking, format, DAC power and output routing.
                 * A write that was ACKed can still have been ignored; this is the check. */
                static const uint8_t regs[] = { 0x66, 0x65, 0x03, 0x04, 0x0B, 0x02,
                                                0x09, 0x07, 0x25, 0x29, 0x2B, 0x52, 0x56 };
                char out[128];
                size_t used = 0;
                for (size_t i = 0; i < sizeof(regs); ++i) {
                    uint8_t v = 0xEE;
                    int rc = audio_dac_read(regs[i], &v);
                    used += (size_t)snprintf(out + used, sizeof(out) - used, "%s%02x=%02x",
                                             i ? " " : "", (unsigned)regs[i],
                                             rc == 0 ? (unsigned)v : 0xFF);
                    if (used >= sizeof(out) - 1) break;
                }
                snprintf(response, sizeof(response), "OK AIC %s", out);
            } else if (!strcmp(line.data, "DAC_INIT")) {
                /* Re-run the AIC3104 codec init (power up + unmute) on demand. */
                int rc = audio_dac_init();
                snprintf(response, sizeof(response), "%s DAC_INIT", rc == 0 ? "OK" : "ERR");
            } else if (!strncmp(line.data, "TONE ", 5)) {
                /* "TONE <count> <ms>" -- play <count> sustained tones of <ms> milliseconds each,
                 * separated by short silences. A chime is ~160 ms, far too short to exercise a
                 * sustained stream; this holds real audio on the wire for seconds at a time so
                 * DMA starvation or stale-buffer replay shows up as clipping mid-tone rather than
                 * at a burst boundary. Count the tones you hear and note WHERE any are cut. */
                int count = 0, ms = 0;
                if (sscanf(line.data, "TONE %d %d", &count, &ms) != 2 || count < 1 || count > 12
                    || ms < 50 || ms > 4000) {
                    snprintf(response, sizeof(response),
                             "ERR TONE usage: TONE <count 1-12> <ms 50-4000>");
                } else {
                    const int rate = 16000, gap_ms = 300;
                    size_t tone_n = (size_t)ms * rate / 1000u;
                    size_t gap_n = (size_t)gap_ms * rate / 1000u;
                    size_t total = (size_t)count * tone_n + (size_t)(count - 1) * gap_n;
                    int16_t *pcm = (int16_t *)malloc(total * sizeof(int16_t));
                    if (!pcm) {
                        snprintf(response, sizeof(response), "ERR TONE out of memory (%u samples)",
                                 (unsigned)total);
                    } else {
                        memset(pcm, 0, total * sizeof(int16_t));
                        for (int i = 0; i < count; i++) {
                            /* alternate 440 / 660 Hz so consecutive tones are distinguishable */
                            double hz = (i % 2) ? 660.0 : 440.0;
                            size_t off = (size_t)i * (tone_n + gap_n);
                            for (size_t s = 0; s < tone_n; s++) {
                                double env = 1.0;
                                if (s < 400) env = (double)s / 400.0;                 /* fade in  */
                                else if (s + 400 >= tone_n) env = (double)(tone_n - s) / 400.0;
                                pcm[off + s] = (int16_t)(12000.0 * env *
                                                         sin(2.0 * M_PI * hz * (double)s / rate));
                            }
                        }
                        int pr = audio_playback_play_mono(pcm, total);
                        snprintf(response, sizeof(response),
                                 "%s TONE %d x %dms queued (%u samples, %u KB)",
                                 pr == 0 ? "OK" : "ERR", count, ms,
                                 (unsigned)total, (unsigned)(total * sizeof(int16_t) / 1024u));
                        free(pcm);
                    }
                }
            } else if (!strncmp(line.data, "CHIME", 5) && (line.data[5] == '\0' || line.data[5] == ' ')) {
                /* Play the chime directly, with no detector, no detection and no streaming
                 * session as a precondition. This is the test that separates an audio fault from
                 * a wake-trigger fault: the wake path silently skips most triggers when the mic
                 * is not streaming, which is indistinguishable from a broken speaker. */
                int rc = wake_detector_play_chime();
                snprintf(response, sizeof(response), "%s CHIME (bypasses the detector entirely)",
                         rc == 0 ? "OK" : "ERR");
            } else if (!strncmp(line.data, "I2S_FLOAT ", 10)) {
                /* "I2S_FLOAT 1" tri-states the I2S data-out line (GPIO44) so it floats;
                 * "I2S_FLOAT 0" drives it again. This is a CONTROL experiment: if floating the
                 * line reproduces the intermittent clicking exactly, then that clicking is what
                 * an undriven data line sounds like -- and the real fault is that our line is
                 * intermittently not being driven, not the codec or its clock. */
                int on = 0;
                if (sscanf(line.data, "I2S_FLOAT %d", &on) != 1 || (on != 0 && on != 1)) {
                    snprintf(response, sizeof(response), "ERR I2S_FLOAT usage: I2S_FLOAT 0|1");
                } else {
                    audio_playback_i2s_float(on);
                    snprintf(response, sizeof(response), "OK I2S_FLOAT %d (%s)",
                             on, on ? "GPIO44 now floating" : "GPIO44 driven again");
                }
            } else if (!strncmp(line.data, "AMP_LOCK ", 9)) {
                /* Freeze/unfreeze the amp so a diagnostic can hold the speaker open while the
                 * ring is empty -- otherwise the pump releases it within milliseconds. */
                int on = 0;
                if (sscanf(line.data, "AMP_LOCK %d", &on) != 1 || (on != 0 && on != 1)) {
                    snprintf(response, sizeof(response), "ERR AMP_LOCK usage: AMP_LOCK 0|1");
                } else {
                    audio_playback_amp_lock(on);
                    snprintf(response, sizeof(response), "OK AMP_LOCK %d", on);
                }
            } else if (!strncmp(line.data, "AMP ", 4)) {
                /* Force the amplifier on or off for diagnostics: "AMP 1" / "AMP 0". Normally the
                 * pump arms it only while the playback ring has frames, so a routing change alone
                 * is silent -- you cannot hear what a mux setting delivers unless the amp is held
                 * open. This is that override. */
                int on = 0;
                if (sscanf(line.data, "AMP %d", &on) != 1 || (on != 0 && on != 1)) {
                    snprintf(response, sizeof(response), "ERR AMP usage: AMP 0|1");
                } else {
                    audio_playback_amp(on);
                    snprintf(response, sizeof(response), "OK AMP %d", on);
                }
            } else if (!strncmp(line.data, "XVF_OP ", 7)) {
                /* Set the XVF3800's output mux: "XVF_OP <category> <source> [R]" chooses which
                 * signal the DAC plays. Category 4 source 0 is the host's own playback over I2S;
                 * the board defaults to the beamformed microphone output instead. */
                int cat = 0, src = 0, right = 0;
                int got = sscanf(line.data, "XVF_OP %d %d", &cat, &src);
                if (strstr(line.data, " R")) right = 1;
                if (got != 2 || cat < 0 || cat > 12 || src < 0 || src > 5) {
                    snprintf(response, sizeof(response), "ERR XVF_OP usage: XVF_OP <category 0-12> <source 0-5> [R]");
                } else {
                    int rc = audio_xvf_route_output(right, cat, src);
                    snprintf(response, sizeof(response), "%s XVF_OP %d %d%s",
                             rc == 0 ? "OK" : "ERR", cat, src, right ? " R" : "");
                }
            } else if (!strcmp(line.data, "WAKE_STATUS") || !strcmp(line.data, "WAKE_FIRE")) {
                /* The wake detector's provisioning-console surface (increment 4). WAKE_STATUS
                 * reports the detector state (real vs placeholder model, deaf, force-armed) so a
                 * live test can confirm what the detector will do before speaking. WAKE_FIRE is
                 * the live-test gate: a one-shot force-fire that drives the whole chime -> wake ->
                 * run-satellite path with no trained model on the device (the #110 placeholder
                 * scorer never crosses the threshold on real audio). */
                if (!strcmp(line.data, "WAKE_FIRE")) {
                    wake_detector_force_fire();
                    snprintf(response, sizeof(response), "OK WAKE FORCE ARMED");
                } else {
                    char status[128];
                    wake_detector_status(status, sizeof(status));
                    snprintf(response, sizeof(response), "OK WAKE %s", status);
                }
            } else if (!strncmp(line.data, "SET_NAME ", 9) ||
                       !strncmp(line.data, "SET_AREA ", 9) ||
                       !strncmp(line.data, "SET_FIRMWARE_VERSION ", 22)) {
                /* The device identity the bridge and the house know it by. The name and area are
                 * what describe/info and the registration report; firmware_version is the build
                 * label. Staged to NVS like the Wi-Fi profile, so a re-flash keeps them. */
                const char *key = !strncmp(line.data, "SET_NAME ", 9) ? "device_name"
                                 : !strncmp(line.data, "SET_AREA ", 9) ? "device_area"
                                 : "firmware_version";
                const char *value = strchr(line.data, ' ');
                value = value ? value + 1 : "";
                if (save_identity_field(&storage_ready, key, value) == 0)
                    snprintf(response, sizeof(response), "OK SAVED STAGED");
                else
                    snprintf(response, sizeof(response), "ERR STORAGE");
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