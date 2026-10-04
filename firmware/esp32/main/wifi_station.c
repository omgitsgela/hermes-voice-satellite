#include "wifi_station.h"
#include "wifi_report.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define WIFI_STATION_MAX_APS 32
#define WIFI_STATION_ACTIVE_MIN_MS 120
#define WIFI_STATION_ACTIVE_MAX_MS 200
#define WIFI_STATION_START_TIMEOUT_MS 3000
/* Long enough for a WPA2 four-way handshake and DHCP on a slow access point; an ESP32
 * that has not got an address in this long is not going to. */
#define WIFI_STATION_CONNECT_TIMEOUT_MS 20000

#define WIFI_STATION_STARTED_BIT BIT0
#define WIFI_STATION_GOT_IP_BIT BIT1
#define WIFI_STATION_FAILED_BIT BIT2

static EventGroupHandle_t station_events;
static volatile int station_reason;

/* One handler for every radio event. It only records facts; the wait in
 * wifi_station_connect decides what they mean. */
static void station_event(void *argument, esp_event_base_t base, int32_t id, void *data) {
    (void)argument;
    if (!station_events) return;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        xEventGroupSetBits(station_events, WIFI_STATION_STARTED_BIT);
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        station_reason = (int)((wifi_event_sta_disconnected_t *)data)->reason;
        xEventGroupSetBits(station_events, WIFI_STATION_FAILED_BIT);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(station_events, WIFI_STATION_GOT_IP_BIT);
    }
}

static int ensure_initialised(void) {
    static int ready = 0;
    if (ready) return 0;
    if (esp_netif_init() != ESP_OK) return -1;
    esp_err_t loop = esp_event_loop_create_default();
    if (loop != ESP_OK && loop != ESP_ERR_INVALID_STATE) return -1;
    if (esp_netif_create_default_wifi_sta() == NULL) return -1;
    wifi_init_config_t config = WIFI_INIT_CONFIG_DEFAULT();
    if (esp_wifi_init(&config) != ESP_OK) return -1;
    /* Credentials are ours, in the hermes_wifi namespace: keep the driver from storing
     * its own copy of the passphrase in flash. */
    if (esp_wifi_set_storage(WIFI_STORAGE_RAM) != ESP_OK) return -1;
    if (esp_wifi_set_mode(WIFI_MODE_STA) != ESP_OK) return -1;
    station_events = xEventGroupCreate();
    if (!station_events) return -1;
    if (esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, station_event, NULL,
                                            NULL) != ESP_OK) return -1;
    if (esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, station_event, NULL,
                                            NULL) != ESP_OK) return -1;
    ready = 1;
    return 0;
}

static unsigned char report_auth(wifi_auth_mode_t mode) {
    switch (mode) {
        case WIFI_AUTH_OPEN: return WIFI_REPORT_AUTH_OPEN;
        case WIFI_AUTH_WEP: return WIFI_REPORT_AUTH_WEP;
        case WIFI_AUTH_WPA_PSK: return WIFI_REPORT_AUTH_WPA;
        case WIFI_AUTH_WPA2_PSK: return WIFI_REPORT_AUTH_WPA2;
        case WIFI_AUTH_WPA_WPA2_PSK: return WIFI_REPORT_AUTH_WPA2;
        case WIFI_AUTH_WPA2_ENTERPRISE: return WIFI_REPORT_AUTH_ENTERPRISE;
        case WIFI_AUTH_WPA3_PSK: return WIFI_REPORT_AUTH_WPA3;
        case WIFI_AUTH_WPA2_WPA3_PSK: return WIFI_REPORT_AUTH_WPA3;
        default: return 0xffu;
    }
}

static int fail(char *out, size_t capacity, const char *reason) {
    snprintf(out, capacity, "ERROR %s\n", reason);
    return -1;
}

/* One scan, shared by the report and by the chooser, because two scan implementations would
 * eventually disagree about dwell time or about what "found" means. On success the radio is
 * still started and *records belongs to the caller, who frees it and stops the radio; on
 * failure everything done here is undone here. */
static int scan_collect(wifi_ap_record_t **records, uint16_t *found, uint16_t *wanted,
                        char *out, size_t capacity) {
    *records = NULL;
    *found = 0;
    *wanted = 0;
    if (esp_wifi_start() != ESP_OK) return fail(out, capacity, "radio start");
    wifi_scan_config_t scan = {0};
    scan.show_hidden = true;
    scan.scan_type = WIFI_SCAN_TYPE_ACTIVE;
    scan.scan_time.active.min = WIFI_STATION_ACTIVE_MIN_MS;
    scan.scan_time.active.max = WIFI_STATION_ACTIVE_MAX_MS;

    /* Blocking, so the whole operation is bounded by the dwell time per channel. */
    if (esp_wifi_scan_start(&scan, true) != ESP_OK) {
        esp_wifi_stop();
        return fail(out, capacity, "scan start");
    }

    uint16_t total = 0;
    if (esp_wifi_scan_get_ap_num(&total) != ESP_OK) total = 0;
    uint16_t take = total > WIFI_STATION_MAX_APS ? WIFI_STATION_MAX_APS : total;
    if (take > 0) {
        *records = calloc(take, sizeof(**records));
        if (!*records) {
            esp_wifi_clear_ap_list();
            esp_wifi_stop();
            return fail(out, capacity, "no memory");
        }
        if (esp_wifi_scan_get_ap_records(&take, *records) != ESP_OK) {
            free(*records);
            *records = NULL;
            take = 0;
        }
    }
    *found = total;
    *wanted = take;
    return 0;
}

int wifi_station_scan(char *out, size_t capacity) {
    if (!out || capacity == 0) return -1;
    out[0] = 0;
    if (ensure_initialised() != 0) return fail(out, capacity, "wifi init");

    wifi_ap_record_t *records = NULL;
    uint16_t found = 0;
    uint16_t wanted = 0;
    if (scan_collect(&records, &found, &wanted, out, capacity) != 0) return -1;

    wifi_report_ap *view = NULL;
    if (wanted > 0) {
        view = calloc(wanted, sizeof(*view));
        if (!view) {
            free(records);
            esp_wifi_clear_ap_list();
            esp_wifi_stop();
            return fail(out, capacity, "no memory");
        }
        for (uint16_t i = 0; i < wanted; ++i) {
            size_t length = strnlen((const char *)records[i].ssid, WIFI_REPORT_SSID_MAX);
            memcpy(view[i].ssid, records[i].ssid, length);
            view[i].ssid_length = (unsigned char)length;
            view[i].channel = records[i].primary;
            view[i].rssi = (signed char)records[i].rssi;
            view[i].auth = report_auth(records[i].authmode);
        }
    }

    /* Count is reported from what the driver found, so a truncated report is honest. */
    size_t rendered = wifi_report_render(view, wanted, out, capacity);
    if (found > wanted) {
        char note[48];
        snprintf(note, sizeof(note), "SCAN FOUND %u TOTAL\n", (unsigned)found);
        if (rendered + strlen(note) + 1 <= capacity) strcat(out, note);
    }

    free(view);
    free(records);
    esp_wifi_clear_ap_list();
    esp_wifi_stop();  /* back to quiet: no radio unless a command asks for one */
    return 0;
}

int wifi_station_scan_match(const char *const *ssids, size_t count, int *rank,
                            char *out, size_t capacity) {
    if (!out || capacity == 0) return -1;
    out[0] = 0;
    if (!ssids || !rank || count == 0) return fail(out, capacity, "no networks to match");
    if (ensure_initialised() != 0) return fail(out, capacity, "wifi init");
    for (size_t index = 0; index < count; ++index) rank[index] = WIFI_STATION_NO_SIGNAL;

    wifi_ap_record_t *records = NULL;
    uint16_t found = 0;
    uint16_t wanted = 0;
    if (scan_collect(&records, &found, &wanted, out, capacity) != 0) return -1;

    for (uint16_t index = 0; index < wanted; ++index) {
        /* The driver's SSID field is not promised to be terminated, so it is bounded here and
         * the staged name is compared over exactly that many bytes. */
        size_t length = strnlen((const char *)records[index].ssid, WIFI_REPORT_SSID_MAX);
        if (length == 0) continue;  /* a hidden network cannot be matched by name */
        for (size_t candidate = 0; candidate < count; ++candidate) {
            if (strncmp((const char *)records[index].ssid, ssids[candidate], length) != 0) continue;
            if (ssids[candidate][length] != 0) continue;
            int rssi = (int)records[index].rssi;
            if (rank[candidate] == WIFI_STATION_NO_SIGNAL || rssi > rank[candidate])
                rank[candidate] = rssi;
        }
    }

    free(records);
    esp_wifi_clear_ap_list();
    esp_wifi_stop();
    /* A completed scan says nothing by itself: the caller reports which network it chose. */
    out[0] = 0;
    return 0;
}

void wifi_station_release(void) {
    esp_wifi_disconnect();
    esp_wifi_stop();  /* back to quiet: no radio unless a command asks for one */
}

int wifi_station_connect(const char *ssid, const char *psk, char *out, size_t capacity) {
    int result = wifi_station_associate(ssid, psk, out, capacity);
    /* Release even on failure: release is safe when nothing is connected, and it keeps the
     * "radio off unless a command is using it" rule true without every caller repeating it. */
    wifi_station_release();
    return result;
}

int wifi_station_associate(const char *ssid, const char *psk, char *out, size_t capacity) {
    if (!out || capacity == 0) return -1;
    out[0] = 0;
    if (ensure_initialised() != 0) return fail(out, capacity, "wifi init");

    wifi_connect_outcome outcome = {0};
    outcome.channel = -1;
    if (!ssid || !ssid[0] || !psk || !psk[0]) {
        wifi_connect_report_render(&outcome, out, capacity);
        return -1;
    }

    wifi_config_t config = {0};
    /* The driver reads both values as NUL-terminated strings and the struct carries no length
     * field, so a value must fit inside its field WITH a terminator. Copy with memcpy, not
     * snprintf: snprintf would silently drop the final byte of a full-length value and hand the
     * driver a wrong credential, which then fails the four-way handshake with a misleading
     * "wrong password" that points at the access point instead of at the truncation. */
    size_t ssid_len = strlen(ssid);
    size_t psk_len = strlen(psk);
    if (ssid_len >= sizeof(config.sta.ssid))
        return fail(out, capacity, "ssid exceeds the radio field");
    memcpy(config.sta.ssid, ssid, ssid_len);
    config.sta.ssid[ssid_len] = 0;
    memcpy(config.sta.password, psk, psk_len);
    if (psk_len < sizeof(config.sta.password))
        config.sta.password[psk_len] = 0;
    /* A full-field PSK (the 64-hex raw-key form the provisioner accepts) has no byte left inside
     * the field for a terminator; it is terminated by the zero-initialised field that follows
     * password[] (scan_method), which is left at 0 because config is {0} and only ssid/password
     * are written here. That is how the raw key reaches the driver intact. */


    xEventGroupClearBits(station_events, WIFI_STATION_STARTED_BIT | WIFI_STATION_GOT_IP_BIT |
                                            WIFI_STATION_FAILED_BIT);
    station_reason = 0;

    esp_err_t configured = esp_wifi_set_config(WIFI_IF_STA, &config);
    memset(&config, 0, sizeof(config));  /* the driver keeps its own copy; ours goes now */
    if (configured != ESP_OK) return fail(out, capacity, "config rejected");

    if (esp_wifi_start() != ESP_OK) {
        esp_wifi_stop();
        return fail(out, capacity, "radio start");
    }

    /* Wait for the driver to be up before asking it to associate: connect() against a
     * station that has not started is refused outright. */
    EventBits_t started = xEventGroupWaitBits(station_events, WIFI_STATION_STARTED_BIT, pdFALSE,
                                             pdFALSE, pdMS_TO_TICKS(WIFI_STATION_START_TIMEOUT_MS));
    if (!(started & WIFI_STATION_STARTED_BIT)) {
        esp_wifi_stop();
        return fail(out, capacity, "radio did not start");
    }

    if (esp_wifi_connect() != ESP_OK) {
        esp_wifi_stop();
        return fail(out, capacity, "connect refused");
    }

    EventBits_t result = xEventGroupWaitBits(station_events,
                                             WIFI_STATION_GOT_IP_BIT | WIFI_STATION_FAILED_BIT,
                                             pdTRUE, pdFALSE,
                                             pdMS_TO_TICKS(WIFI_STATION_CONNECT_TIMEOUT_MS));
    if (result & WIFI_STATION_GOT_IP_BIT) {
        char ip_text[16] = "(none)";
        esp_netif_ip_info_t info = {0};
        esp_netif_t *netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
        if (netif && esp_netif_get_ip_info(netif, &info) == ESP_OK)
            snprintf(ip_text, sizeof(ip_text), IPSTR, IP2STR(&info.ip));

        wifi_ap_record_t ap = {0};
        if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
            outcome.channel = ap.primary;
            outcome.rssi = ap.rssi;
        }
        outcome.connected = 1;
        outcome.ssid = ssid;
        outcome.ip = ip_text;
        wifi_connect_report_render(&outcome, out, capacity);
        return 0;  /* the link stays up: the caller owns it until wifi_station_release */
    }

    /* A disconnect event carries the reason; a plain timeout carries none, and saying so
     * is more useful than inventing one. */
    outcome.reason = station_reason;
    wifi_connect_report_render(&outcome, out, capacity);
    esp_wifi_disconnect();
    esp_wifi_stop();
    return -1;
}
