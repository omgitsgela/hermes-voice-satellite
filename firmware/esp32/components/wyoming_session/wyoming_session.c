#include "wyoming_session.h"
#include "wyoming_json.h"
#include "cJSON.h"
#include <stdio.h>
#include <string.h>

/* The wake word name this device reports. The local detector (a later increment) and the
 * bridge's wake-now both report the same name, so the bridge's capture path sees one wake
 * word whether it was heard or requested. */
#define WY_WAKE_WORD_NAME "hey-hermes"

size_t wy_session_size(void) { return sizeof(wy_session); }
int wy_session_state(const wy_session *session) { return session ? session->state : WY_SESSION_FAILED; }
int wy_session_streaming(const wy_session *session) { return session ? session->streaming : 0; }

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

/* Every write on the link holds the writer lock for exactly one frame (or one short sequence,
 * see wy_session_wake), because more than one task writes: the gateway task answers control
 * events and the capture task sends mic chunks. On the host the lock callbacks are NULL and
 * this is a no-op, which is exactly the single-threaded truth of a host test. */
static int writer_lock(wy_session *session) {
    return session->lock_writer ? session->lock_writer(session->writer_lock_user) : WY_OK;
}
static void writer_unlock(wy_session *session) {
    if (session->unlock_writer) session->unlock_writer(session->writer_lock_user);
}

/* One frame, whole, under the lock. */
static int locked_send(wy_session *session, wy_write_fn write_fn, void *ctx,
                       const char *type, const char *data, const void *payload, size_t length) {
    int lock_result = writer_lock(session);
    if (lock_result != WY_OK) return lock_result;
    int result = wy_send(write_fn, ctx, type, data, payload, length);
    writer_unlock(session);
    if (result != WY_OK) return result;
    return WY_OK;
}

/* Stop the mic stream (idempotent) and tell the bridge: this is the reference satellite's
 * "stop streaming and go back to wake-word listening", shared by transcript, pause and a
 * second run-satellite. */
static int stop_streaming(wy_session *session, wy_write_fn write_fn, void *ctx) {
    if (!session->streaming) return WY_OK;
    session->streaming = 0;
    return locked_send(session, write_fn, ctx, "streaming-stopped", NULL, NULL, 0);
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

int __attribute__((used)) wy_session_wake(wy_session *session, const char *wake_word,
                    wy_write_fn write_fn, void *ctx) {
    if (!session || !wake_word || !write_fn) return WY_INVALID;
    if (session->state != WY_SESSION_REGISTERED && session->state != WY_SESSION_RUN_REQUESTED)
        return WY_INVALID;
    if (session->paused || session->streaming) return WY_INVALID;
    if (!token_valid(wake_word, 64)) return WY_INVALID;
    char name[128];
    int length = snprintf(name, sizeof(name), "{\"name\":\"%s\"}", wake_word);
    if (length < 0 || (size_t)length >= sizeof(name)) return WY_INVALID;

    /* The three events go out as one sequence under a single lock hold: the bridge's capture
     * reader must never see a mic chunk interleaved between detection and streaming-started,
     * and if the link dies mid-sequence nothing is half-sent as "streaming". */
    int lock_result = writer_lock(session);
    if (lock_result != WY_OK) return lock_result;
    int result = wy_send(write_fn, ctx, "detection", name, NULL, 0);
    if (!result)
        result = wy_send(write_fn, ctx, "run-pipeline",
                         "{\"start_stage\":\"asr\",\"end_stage\":\"tts\",\"restart_on_end\":false}",
                         NULL, 0);
    if (!result)
        result = wy_send(write_fn, ctx, "streaming-started", NULL, NULL, 0);
    if (!result) session->streaming = 1;
    writer_unlock(session);
    if (result != WY_OK) { session->state = WY_SESSION_FAILED; return result; }
    return WY_OK;
}

int wy_session_send_audio(wy_session *session, const void *pcm, size_t length,
                          wy_write_fn write_fn, void *ctx) {
    /* The mic is open only between a wake and the bridge's transcript; outside that window
     * the audio is dropped on the device, exactly like the reference's muted path. Nothing is
     * queued: a chunk that cannot go out now is late by the time it could, and a backlog in
     * the session would only grow on a dead link. */
    if (!session || !pcm || !write_fn) return WY_INVALID;
    if (!session->streaming) return WY_INVALID;
    if (length == 0 || length > WY_AUDIO_CHUNK_SAMPLES * 2 || length % 2) return WY_INVALID;
    /* The chunk's format comes from the audio component, the one place that owns it: the
     * builder validates the format and the bytes that leave on the wire are exactly what the
     * host tests check against the canonical reader. */
    char data[64];
    if (wy_audio_chunk_data(data, sizeof(data), 16000, 2, 1) < 0) return WY_INVALID;
    return locked_send(session, write_fn, ctx, "audio-chunk", data, pcm, length);
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
    /* Wire order is data then payload (the canonical writer emits them in that order), and
     * both must be drained off the wire before the frame is judged, for whatever it turns out
     * to be: leaving either behind desynchronizes the next header.
     *
     * A frame WITH a payload is audio, not control: its data (the audio format field) is
     * drained but not decoded, because wy_decode_control is, by design, for control frames
     * only and refuses any frame that carries a payload. Decoding it is what made the first
     * inbound audio-chunk fail the whole session. */
    if (frame.data_length) {
        result = wy_read_exact(read_fn, ctx, session->body, frame.data_length);
        if (result) goto done;
        if (!frame.payload_length) {
            result = wy_decode_control(session->header, strlen(session->header),
                                       session->body, frame.data_length, &data);
            if (result) goto done;
        }
    }
    if (frame.payload_length) {
        result = wy_read_exact(read_fn, ctx, session->body, frame.payload_length);
        if (result) goto done;
    }
    if (!strcmp(frame.type, "describe")) {
        result = locked_send(session, write_fn, ctx, "info",
            "{\"satellite\":{\"name\":\"ESP32 control probe\",\"area\":\"\","
            "\"installed\":true,\"attribution\":{\"name\":\"Hermes Voice Satellite\","
            "\"url\":\"https://github.com/omgitsgela/hermes-voice-satellite\"},"
            "\"active_wake_words\":[],\"max_active_wake_words\":0,\"supports_trigger\":false}}",
            NULL, 0);
        if (result) goto done;
        if (session->state == WY_SESSION_DESCRIBE) {
            char registration[512];
            int length = snprintf(registration, sizeof(registration),
                "{\"device_id\":\"%s\",\"boot_id\":\"%s\",\"protocol_version\":1,"
                "\"name\":\"ESP32 control probe\",\"area\":\"\","
                "\"firmware_version\":\"esp32-audio-1\","
                "\"capabilities\":[\"microphone\"]}", session->device_id, session->boot_id);
            if (length <= 0 || (size_t)length >= sizeof(registration)) { result = WY_LIMIT; goto done; }
            result = locked_send(session, write_fn, ctx, "hermes.register", registration, NULL, 0);
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
    } else if (!strcmp(frame.type, "run-satellite")) {
        if (session->state != WY_SESSION_REGISTERED && session->state != WY_SESSION_RUN_REQUESTED)
            result = WY_INVALID;
        else {
            /* The reference stops a running stream on run-satellite too (it is "back to
             * wake-word listening"), and clears a pause. */
            if (session->streaming) result = stop_streaming(session, write_fn, ctx);
            if (!result) {
                session->paused = 0;
                session->state = WY_SESSION_RUN_REQUESTED;
            }
        }
    } else if (!strcmp(frame.type, "pause-satellite")) {
        if (session->state != WY_SESSION_REGISTERED && session->state != WY_SESSION_RUN_REQUESTED)
            result = WY_INVALID;
        else {
            if (session->streaming) result = stop_streaming(session, write_fn, ctx);
            if (!result) {
                session->paused = 1;
                session->state = WY_SESSION_REGISTERED;
            }
        }
    } else if (!strcmp(frame.type, "transcript")) {
        /* The bridge's stop signal: the utterance is over. End the stream and go back to
         * wake-word listening. The state stays where it is (armed or not) -- a transcript does
         * not undo the host's run request, and the next wake must be able to start a new stream
         * immediately, which is what the follow-up window relies on. */
        if (session->state != WY_SESSION_REGISTERED && session->state != WY_SESSION_RUN_REQUESTED)
            result = WY_INVALID;
        else
            result = stop_streaming(session, write_fn, ctx);
    } else if (!strcmp(frame.type, "audio-start") ||
               !strcmp(frame.type, "audio-chunk") ||
               !strcmp(frame.type, "audio-stop")) {
        /* Inbound playback audio. The speaker path is a later increment, but these frames are
         * expected parts of the protocol: the payload is already drained, so the link stays
         * usable. Refusing them -- the old behavior, which refused every payload -- killed the
         * session the first time the bridge spoke. */
    } else if (!strcmp(frame.type, "wake-now")) {
        /* The bridge's "open the microphone" command, for a follow-up window that has no wake
         * word: the reference device's wake shim answers it by injecting a synthetic detection.
         * Here the session core IS the device, so it runs the wake sequence itself, with the
         * device's wake word name. A wake-now that lands while the stream is already running or
         * the device is paused is dropped (wy_session_wake checks both), and one that arrives
         * before the host is ready fails the same way an out-of-order wake would. */
        result = wy_session_wake(session, WY_WAKE_WORD_NAME, write_fn, ctx);
        if (result == WY_INVALID) result = WY_OK;   /* a dropped wake-now is not a fault */
    } else if (!strcmp(frame.type, "ping")) {
        /* The protocol expects an answer to a ping; a silent peer looks like a dead device. */
        result = locked_send(session, write_fn, ctx, "pong", NULL, NULL, 0);
    } else {
        /* A real bridge sends events this device does not implement, starting with desired
         * state immediately after registration. Ignoring a well-formed control event keeps
         * the link usable: the frame already passed the strict decode above. Anything UNKNOWN
         * that carries a payload is still refused -- a payload this device will not consume
         * cannot be silently dropped mid-stream without a decision, and failing closed is the
         * honest answer. (Known playback types above are the expected ones.) */
        if (frame.payload_length) result = WY_INVALID;
    }
done:
    cJSON_Delete(data);
    if (result && session->state != WY_SESSION_REFUSED) session->state = WY_SESSION_FAILED;
    return result;
}
