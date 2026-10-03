#include "wyoming_session.h"
#include "wyoming_json.h"
#include "cJSON.h"
#include <stdio.h>
#include <string.h>

size_t wy_session_size(void) { return sizeof(wy_session); }
int wy_session_state(const wy_session *session) { return session ? session->state : WY_SESSION_FAILED; }

const char *wy_session_state_name(int state) {
    switch (state) {
        case WY_SESSION_DESCRIBE: return "describe";
        case WY_SESSION_ACK: return "ack";
        case WY_SESSION_REGISTERED: return "registered";
        case WY_SESSION_RUN_REQUESTED: return "run_requested";
        case WY_SESSION_REFUSED: return "refused";
        case WY_SESSION_FAILED: return "failed";
        default: return "unknown";
    }
}

const char *wy_result_name(int result) {
    switch (result) {
        case WY_OK: return "ok";
        case WY_INVALID: return "invalid";
        case WY_IO: return "io";
        case WY_LIMIT: return "limit";
        case WY_REFUSED: return "refused";
        default: return "unknown";
    }
}

static int token_valid(const char *value, size_t maximum) {
    if (!value || !value[0] || strlen(value) > maximum) return 0;
    for (const char *p = value; *p; ++p)
        if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') ||
              (*p >= '0' && *p <= '9') || *p == '-' || *p == '_' || *p == '.')) return 0;
    return 1;
}

int wy_session_init(wy_session *session, const char *device_id, const char *boot_id) {
    if (!session) return WY_INVALID;
    memset(session, 0, sizeof(*session));
    session->state = WY_SESSION_FAILED;
    if (!token_valid(device_id, 64) || !token_valid(boot_id, 128)) return WY_INVALID;
    strcpy(session->device_id, device_id);
    strcpy(session->boot_id, boot_id);
    session->state = WY_SESSION_DESCRIBE;
    return WY_OK;
}

int wy_session_step(wy_session *session, wy_read_fn read_fn, wy_write_fn write_fn, void *ctx) {
    if (!session) return WY_INVALID;
    if (!read_fn || !write_fn) { session->state = WY_SESSION_FAILED; return WY_INVALID; }
    if (session->state == WY_SESSION_FAILED) return WY_INVALID;
    if (session->state == WY_SESSION_REFUSED) return WY_REFUSED;
    wy_frame frame;
    cJSON *data = NULL;
    int result = wy_read_header(read_fn, ctx, session->header, sizeof(session->header));
    if (result) goto done;
    result = wy_parse_header(session->header, strlen(session->header), &frame);
    if (result) goto done;
    if (frame.payload_length) { result = WY_INVALID; goto done; }
    result = wy_read_exact(read_fn, ctx, session->body, frame.data_length);
    if (result) goto done;
    result = wy_decode_control(session->header, strlen(session->header),
                               session->body, frame.data_length, &data);
    if (result) goto done;
    if (!strcmp(frame.type, "describe")) {
        result = wy_send(write_fn, ctx, "info",
            "{\"satellite\":{\"name\":\"ESP32 control probe\",\"area\":\"\","
            "\"installed\":true,\"attribution\":{\"name\":\"Hermes Voice Satellite\","
            "\"url\":\"https://github.com/omgitsgela/hermes-voice-satellite\"},"
            "\"active_wake_words\":[],\"max_active_wake_words\":0,\"supports_trigger\":false}}", NULL, 0);
        if (result) goto done;
        if (session->state == WY_SESSION_DESCRIBE) {
            char registration[512];
            int length = snprintf(registration, sizeof(registration),
                "{\"device_id\":\"%s\",\"boot_id\":\"%s\",\"protocol_version\":1,"
                "\"name\":\"ESP32 control probe\",\"area\":\"\",\"firmware_version\":\"control-probe-1\","
                "\"capabilities\":[]}", session->device_id, session->boot_id);
            if (length <= 0 || (size_t)length >= sizeof(registration)) { result = WY_LIMIT; goto done; }
            result = wy_send(write_fn, ctx, "hermes.register", registration, NULL, 0);
            if (!result) session->state = WY_SESSION_ACK;
        }
    } else if (!strcmp(frame.type, "hermes.register.ok")) {
        const cJSON *id = cJSON_GetObjectItemCaseSensitive(data, "device_id");
        const cJSON *version = cJSON_GetObjectItemCaseSensitive(data, "protocol_version");
        if (session->state != WY_SESSION_ACK || !cJSON_IsString(id) ||
            strcmp(id->valuestring, session->device_id) || !cJSON_IsNumber(version) ||
            version->valuedouble != 1) result = WY_INVALID;
        else session->state = WY_SESSION_REGISTERED;
    } else if (!strcmp(frame.type, "hermes.register.refused")) {
        session->state = WY_SESSION_REFUSED;
        result = WY_REFUSED;
    } else if (!strcmp(frame.type, "run-satellite") || !strcmp(frame.type, "pause-satellite")) {
        if (session->state != WY_SESSION_REGISTERED && session->state != WY_SESSION_RUN_REQUESTED)
            result = WY_INVALID;
        else session->state = !strcmp(frame.type, "run-satellite") ?
                              WY_SESSION_RUN_REQUESTED : WY_SESSION_REGISTERED;
    } else if (!strcmp(frame.type, "ping")) {
        /* The protocol expects an answer to a ping; a silent peer looks like a dead device. */
        result = wy_send(write_fn, ctx, "pong", NULL, NULL, 0);
    } else {
        /* A real bridge sends events this probe does not implement, starting with desired state
         * immediately after registration. Ignoring a well-formed control event keeps the link
         * usable: the frame already passed the strict decode above, so nothing malformed reaches
         * this branch, and anything carrying a payload was refused before it. */
    }
done:
    cJSON_Delete(data);
    if (result && session->state != WY_SESSION_REFUSED) session->state = WY_SESSION_FAILED;
    return result;
}
