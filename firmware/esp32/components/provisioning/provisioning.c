#include "provisioning.h"
#include <stdio.h>
#include <string.h>

/* One implementation of "what is a valid endpoint", used for both staging and connecting.
 * Two implementations would eventually disagree, and a stored endpoint that passes staging
 * but cannot be parsed is the worst outcome: the console reports STAGED and the transport
 * has nothing to dial. */
int provision_endpoint_parse(const char *value, char *host, size_t host_capacity,
                             unsigned *port, int *transport) {
    if (!value) return -1;
    size_t length = strlen(value);
    if (length >= PROVISION_URI_MAX || length < 9) return -1;
    int tls;
    if (strncmp(value, "tcp://", 6) == 0) tls = 0;
    else if (strncmp(value, "tls://", 6) == 0) tls = 1;
    else return -1;

    const char *name = value + 6;
    const char *colon = strchr(name, ':');
    if (!colon || colon == name || colon - name > 127) return -1;

    size_t label = 0;
    for (const char *p = name; p < colon; ++p) {
        unsigned char c = (unsigned char)*p;
        if (c == '.') {
            if (!label || p[-1] == '-') return -1;
            label = 0;
        } else {
            if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                  (c >= '0' && c <= '9') || c == '-')) return -1;
            if (!label && c == '-') return -1;
            if (++label > 63) return -1;
        }
    }
    if (!label || colon[-1] == '-') return -1;

    const char *port_text = colon + 1;
    size_t digits = strlen(port_text);
    if (!digits || digits > 5 || port_text[0] == '0') return -1;
    unsigned number = 0;
    for (size_t i = 0; i < digits; ++i) {
        if (port_text[i] < '0' || port_text[i] > '9') return -1;
        number = number * 10u + (unsigned)(port_text[i] - '0');
    }
    if (number > 65535) return -1;

    size_t host_length = (size_t)(colon - name);
    if (host) {
        /* Refuse rather than truncate: a shortened host is a different destination. */
        if (host_capacity <= host_length) return -1;
        memcpy(host, name, host_length);
        host[host_length] = 0;
    }
    if (port) *port = number;
    if (transport) *transport = tls ? PROVISION_TRANSPORT_TLS : PROVISION_TRANSPORT_TCP;
    return 0;
}

static int endpoint_valid(const char *value) {
    return provision_endpoint_parse(value, NULL, 0, NULL, NULL) == 0;
}

int provision_handle(const char *command, provision_save_fn save, provision_load_fn load,
                     void *ctx, char *response, size_t capacity) {
    if (!command || !save || !load || !response || capacity < 256) return -1;
    char value[PROVISION_URI_MAX] = {0};
    const char *error = "ERR COMMAND";
    if (strcmp(command, "STATUS") == 0) {
        if (load(ctx, value, sizeof(value)) != 0 || !memchr(value, 0, sizeof(value)) ||
            (value[0] && !endpoint_valid(value))) error = "ERR STORAGE";
        else {
            if (value[0]) snprintf(response, capacity, "OK ENDPOINT %s STAGED", value);
            else snprintf(response, capacity, "OK UNCONFIGURED");
            return 0;
        }
    } else if (strncmp(command, "SET_ENDPOINT ", 13) == 0 || strcmp(command, "CLEAR_ENDPOINT") == 0) {
        const char *endpoint = command[0] == 'C' ? "" : command + 13;
        if (command[0] != 'C' && !endpoint_valid(endpoint)) error = "ERR ENDPOINT";
        else if (save(ctx, endpoint) != 0 || load(ctx, value, sizeof(value)) != 0 ||
                 !memchr(value, 0, sizeof(value)) || strcmp(value, endpoint) != 0) error = "ERR STORAGE";
        else { snprintf(response, capacity, "OK SAVED STAGED"); return 0; }
    }
    snprintf(response, capacity, "%s", error);
    return -1;
}

static int printable_ascii(const char *value) {
    for (const unsigned char *p = (const unsigned char *)value; *p; ++p)
        if (*p < 32 || *p > 126) return 0;
    return 1;
}

int provision_wifi_ssid_valid(const char *ssid) {
    if (!ssid) return 0;
    size_t length = strlen(ssid);
    /* 802.11 allows 32 octets; only printable ASCII is accepted here so a staged
     * value is always safe to print back and to type at a console. */
    return length >= 1 && length <= PROVISION_SSID_MAX && printable_ascii(ssid);
}

static int hex_digit(char c) {
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
}

int provision_wifi_psk_valid(const char *psk) {
    if (!psk) return 0;
    size_t length = strlen(psk);
    if (length == PROVISION_PSK_MAX) {  /* the 64-character raw key form */
        for (size_t i = 0; i < length; ++i)
            if (!hex_digit(psk[i])) return 0;
        return 1;
    }
    return length >= 8 && length <= 63 && printable_ascii(psk);
}

static int reload(const char *context, provision_wifi_load_fn load, void *ctx,
                  char *ssid, size_t ssid_capacity, char *psk, size_t psk_capacity) {
    (void)context;
    if (load(ctx, ssid, ssid_capacity, psk, psk_capacity) != 0) return -1;
    if (!memchr(ssid, 0, ssid_capacity) || !memchr(psk, 0, psk_capacity)) return -1;
    if (ssid[0] && !provision_wifi_ssid_valid(ssid)) return -1;
    if (psk[0] && !provision_wifi_psk_valid(psk)) return -1;
    return 0;
}

int provision_wifi_handle(const char *command, provision_wifi_save_ssid_fn save_ssid,
                          provision_wifi_save_psk_fn save_psk, provision_wifi_clear_fn clear,
                          provision_wifi_load_fn load, void *ctx,
                          char *response, size_t capacity) {
    if (!command || !save_ssid || !save_psk || !clear || !load || !response || capacity < 256) return -1;
    char ssid[PROVISION_SSID_MAX + 1] = {0};
    char psk[PROVISION_PSK_MAX + 8] = {0};
    const char *error = "ERR COMMAND";
    if (strcmp(command, "WIFI_STATUS") == 0) {
        if (reload(command, load, ctx, ssid, sizeof(ssid), psk, sizeof(psk)) != 0) error = "ERR STORAGE";
        else if (!ssid[0] && !psk[0]) { snprintf(response, capacity, "OK WIFI UNCONFIGURED"); return 0; }
        else {
            /* The SSID is reported; the PSK is only ever reported as staged or missing. */
            snprintf(response, capacity, "OK WIFI ssid=%s psk=%s",
                     ssid[0] ? ssid : "missing", psk[0] ? "staged" : "missing");
            return 0;
        }
    } else if (strncmp(command, "SET_WIFI_SSID", 13) == 0) {
        if (command[13] != 0 && command[13] != ' ') error = "ERR COMMAND";
        else {
            const char *value = command[13] == ' ' ? command + 14 : "";
            if (!provision_wifi_ssid_valid(value)) error = "ERR WIFI SSID";
            else if (save_ssid(ctx, value) != 0 ||
                     reload(command, load, ctx, ssid, sizeof(ssid), psk, sizeof(psk)) != 0 ||
                     strcmp(ssid, value) != 0) error = "ERR STORAGE";
            else { snprintf(response, capacity, "OK WIFI SSID STAGED"); return 0; }
        }
    } else if (strncmp(command, "SET_WIFI_PSK", 12) == 0) {
        if (command[12] != 0 && command[12] != ' ') error = "ERR COMMAND";
        else {
            const char *value = command[12] == ' ' ? command + 13 : "";
            if (!provision_wifi_psk_valid(value)) error = "ERR WIFI PSK";
            else if (save_psk(ctx, value) != 0 ||
                     reload(command, load, ctx, ssid, sizeof(ssid), psk, sizeof(psk)) != 0 ||
                     strcmp(psk, value) != 0) error = "ERR STORAGE";
            else { snprintf(response, capacity, "OK WIFI PSK STAGED"); return 0; }
        }
    } else if (strcmp(command, "CLEAR_WIFI") == 0) {
        if (clear(ctx) != 0 ||
            reload(command, load, ctx, ssid, sizeof(ssid), psk, sizeof(psk)) != 0 ||
            ssid[0] || psk[0]) error = "ERR STORAGE";
        else { snprintf(response, capacity, "OK WIFI CLEARED"); return 0; }
    }
    snprintf(response, capacity, "%s", error);
    return -1;
}

int provision_line_feed(provision_line *line, unsigned char byte) {
    if (byte == '\n') {
        int result = line->rejected ? -1 : 1;
        line->data[line->used] = 0;
        line->used = 0;
        line->rejected = 0;
        return result;
    }
    if (line->rejected) return 0;
    if (byte < 32 || byte > 126 || line->used >= sizeof(line->data) - 1) {
        line->rejected = 1;
        return 0;
    }
    line->data[line->used++] = (char)byte;
    return 0;
}

void provision_line_expire(provision_line *line) {
    if (line->used) line->rejected = 1;
}
