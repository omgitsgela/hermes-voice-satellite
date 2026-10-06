#include "wyoming_session.h"
#include "wyoming_json.h"
#include "cJSON.h"
#include <stdio.h>
#include <string.h>
#ifdef ESP_PLATFORM
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#endif

/* The wake word name this device reports. The local detector (a later increment) and the
 * bridge's wake-now both report the same name, so the bridge's capture path sees one wake
 * word whether it was heard or requested. */
#define WY_WAKE_WORD_NAME "hey-hermes"

/* Playback conversion scratch, sized for the largest slice wy_session_step reads in one go
 * (the 4096-byte body, 2048 mono samples). 2048 input samples resample to at most ~1483 mono
 * samples and 2966 stereo-32 frames, so these hold any slice with margin. File-static (not
 * heap), shared by every wy_session_step driver (the gateway task and the USB console both
 * drive the session core), so it is used only WHILE HOLDING the ring lock below: a second
 * converter that began mid-slice would otherwise clobber the first's buffers and append
 * corrupted frames.
 *
 * The ring lock also exists because the ring is NOT a pure single-writer/single-reader queue:
 * the overflow policy is "drop the OLDEST", which means the WRITER (the gateway task, in
 * playback_ring_append) must advance the READ pointer (ring_head) when the ring is full. The
 * reader (the playback task, in wy_session_playback_read) advances the same pointer. Two
 * non-atomic read-modify-write sites on one size_t, on two cores (CONFIG_FREERTOS_NUMBER_OF_
 * CORES=2), is a race: a lost update moves the head backwards (replaying dropped samples,
 * the ring appearing empty when full -- a stall) or skips slots. The mutex makes each
 * append/read/bytes/reset atomic with respect to the other. On the host the session is
 * single-threaded, so the lock compiles out and the layout is unchanged. */
#define WY_PLAYBACK_MONO_MAX   2048u   /* int16: resampled 16 kHz mono, one slice */
#define WY_PLAYBACK_FRAME_MAX  4096u   /* int32: stereo-32 frames, one slice */
static int16_t playback_mono_scratch[WY_PLAYBACK_MONO_MAX];
static int32_t playback_frame_scratch[WY_PLAYBACK_FRAME_MAX];
#ifdef ESP_PLATFORM
static SemaphoreHandle_t playback_ring_lock;
static portMUX_TYPE playback_ring_lock_mux = portMUX_INITIALIZER_UNLOCKED;
#endif

static void playback_ring_lock_take(void) {
#ifdef ESP_PLATFORM
    /* The lock is created lazily at the first take, and two tasks can reach that first take
     * concurrently (the gateway task and the USB console both drive the session core). A bare
     * check-then-act would let both create a mutex: the loser's give would release a mutex it
     * never took (FreeRTOS mutex-ownership corruption), and mutual exclusion for the ring and
     * scratch sections would be silently lost. The portMUX is the race-free single-creation
     * guard: whoever wins the critical section creates the mutex; the loser sees the handle
     * already set. Holding the portMUX across xSemaphoreCreateMutex is safe: it is a small
     * allocation that does not block or yield, and no task can yet be waiting on a lock that
     * does not exist yet. */
    if (!playback_ring_lock) {
        taskENTER_CRITICAL(&playback_ring_lock_mux);
        if (!playback_ring_lock) playback_ring_lock = xSemaphoreCreateMutex();
        taskEXIT_CRITICAL(&playback_ring_lock_mux);
    }
    if (playback_ring_lock) xSemaphoreTake(playback_ring_lock, portMAX_DELAY);
#endif
}
static void playback_ring_lock_give(void) {
#ifdef ESP_PLATFORM
    if (playback_ring_lock) xSemaphoreGive(playback_ring_lock);
#endif
}

/* Append n stereo-32 frames to the playback ring, dropping the OLDEST on overflow: a late
 * sample is unplayable the moment it could no longer reach the speaker in time, and dropping
 * keeps the link (and the conversion) moving instead of wedging on a full ring. The caller
 * already holds the ring lock (it is taken in the payload path, which also protects the
 * conversion scratch this call reads from). */
static void playback_ring_append_locked(wy_session *session, const int32_t *frames, size_t n) {
    size_t cap = session->ring_capacity / 4;
    if (!session->ring || cap == 0 || n == 0) return;
    for (size_t i = 0; i < n; ++i) {
        if ((session->ring_tail + 1) % cap == session->ring_head)
            session->ring_head = (session->ring_head + 1) % cap;   /* full: drop the oldest */
        session->ring[session->ring_tail] = frames[i];
        session->ring_tail = (session->ring_tail + 1) % cap;
    }
}

size_t wy_session_size(void) { return sizeof(wy_session); }
int wy_session_state(const wy_session *session) { return session ? session->state : WY_SESSION_FAILED; }
int wy_session_streaming(const wy_session *session) { return session ? session->streaming : 0; }

int wy_session_playback_configure(wy_session *session, int32_t *ring, size_t ring_capacity,
                                  int volume) {
    if (!session || !ring || ring_capacity == 0 || ring_capacity % 4) return WY_INVALID;
    playback_ring_lock_take();
    session->ring = ring;
    session->ring_capacity = ring_capacity;
    session->ring_head = 0;
    session->ring_tail = 0;
    session->playback_volume = volume;   /* clamped in wy_apply_volume; the floor never silences */
    session->playback_active = 0;
    session->playback_ended = 0;
    wy_resampler_reset(&session->playback_rs);
    playback_ring_lock_give();
    return WY_OK;
}

int wy_session_playback_active(const wy_session *session) {
    return session ? session->playback_active : 0;
}
int wy_session_playback_ended(const wy_session *session) {
    return session ? session->playback_ended : 0;
}
size_t wy_session_playback_bytes(const wy_session *session) {
    if (!session || !session->ring || session->ring_capacity % 4) return 0;
    size_t cap = session->ring_capacity / 4;
    playback_ring_lock_take();
    size_t bytes = ((session->ring_tail - session->ring_head + cap) % cap) * 4;
    playback_ring_lock_give();
    return bytes;
}
size_t wy_session_playback_read(wy_session *session, int32_t *dst, size_t cap) {
    if (!session || !dst || cap == 0 || !session->ring) return 0;
    size_t cap_el = session->ring_capacity / 4;
    if (cap_el == 0) return 0;
    playback_ring_lock_take();
    size_t avail = (session->ring_tail - session->ring_head + cap_el) % cap_el;
    size_t take = cap / 4;
    if (take > avail) take = avail;
    for (size_t i = 0; i < take; ++i) {
        dst[i] = session->ring[session->ring_head];
        session->ring_head = (session->ring_head + 1) % cap_el;
    }
    playback_ring_lock_give();
    return take * 4;
}

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
    /* The frame AND the flag move together, under one lock hold: a mic chunk that passed its
     * streaming check before the stop must not be able to land AFTER streaming-stopped. The
     * flag is cleared inside the lock, not after locked_send releases it -- on a preemptive
     * scheduler the capture task (higher priority) wakes at the unlock and would otherwise
     * still see streaming set and send its chunk into the gap. The under-lock re-check covers
     * a wake that started the stream between the pre-check and the lock. If the send fails the
     * flag stays set: the stream is not actually over. */
    if (!session->streaming) return WY_OK;
    int result = writer_lock(session);
    if (result != WY_OK) return result;
    if (session->streaming) {
        result = wy_send(write_fn, ctx, "streaming-stopped", NULL, NULL, 0);
        if (!result) session->streaming = 0;
    }
    writer_unlock(session);
    return result;
}

int wy_session_init(wy_session *session, const char *device_id, const char *boot_id) {
    if (!session) return WY_INVALID;
    /* The writer-lock callbacks are installed once by the device glue (audio_capture) and must
     * survive every per-link re-init: a re-init that wiped them would let the two writers
     * (gateway task and capture task) send frames unserialized for the rest of the link. */
    int (*lock_writer)(void *) = session->lock_writer;
    int (*unlock_writer)(void *) = session->unlock_writer;
    void *writer_lock_user = session->writer_lock_user;
    /* The playback ring (a PSRAM buffer the glue allocates once) and the volume are the same
     * kind of installed-once config: the supervisor re-inits this SAME buffer on every
     * reconnect, so a re-init that wiped them would leave the session's single writer appending
     * into a NULL ring (or at a zeroed volume) after the first reconnect. Preserve the config;
     * the live stream state (active/ended/head/tail/resampler) is correctly reset by the memset. */
    int32_t *playback_ring = session->ring;
    size_t playback_ring_capacity = session->ring_capacity;
    int playback_volume = session->playback_volume;
    memset(session, 0, sizeof(*session));
    session->lock_writer = lock_writer;
    session->unlock_writer = unlock_writer;
    session->writer_lock_user = writer_lock_user;
    session->ring = playback_ring;
    session->ring_capacity = playback_ring_capacity;
    session->playback_volume = playback_volume;
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
    /* Armed only: the microphone starts on run-satellite (the armed hook), so a wake in any
     * other state -- not registered yet, registered but not armed, or the stream already
     * running -- would open a stream no capture task can feed. That is a drop, not a fault. */
    if (session->state != WY_SESSION_RUN_REQUESTED) return WY_INVALID;
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
     * the session would only grow on a dead link. The streaming check is repeated UNDER the
     * writer lock, not just before taking it: a stop that lands between the two would otherwise
     * let this chunk out after streaming-stopped, and the next utterance would carry the tail
     * of the last one. */
    if (!session || !pcm || !write_fn) return WY_INVALID;
    if (!session->streaming) return WY_INVALID;
    if (length == 0 || length > WY_AUDIO_CHUNK_SAMPLES * 2 || length % 2) return WY_INVALID;
    int lock_result = writer_lock(session);
    if (lock_result != WY_OK) return lock_result;
    if (!session->streaming) { writer_unlock(session); return WY_INVALID; }
    /* The chunk's format comes from the audio component, the one place that owns it: the
     * builder validates the format and the bytes that leave on the wire are exactly what the
     * host tests check against the canonical reader. */
    char data[64];
    if (wy_audio_chunk_data(data, sizeof(data), 16000, 2, 1) < 0) { writer_unlock(session); return WY_INVALID; }
    int result = wy_send(write_fn, ctx, "audio-chunk", data, pcm, length);
    writer_unlock(session);
    return result;
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
        if (session->playback_active && !strcmp(frame.type, "audio-chunk")) {
            /* Inbound playback for the speaker: convert the PCM as it is drained. The payload
             * can be as large as WY_PAYLOAD_MAX (16 KiB), beyond the 4 KiB body, so it is read
             * in bounded body-sized slices -- each slice resampled to the board's 16 kHz (the
             * resampler's absolute cursor makes the slices continuous), volume-applied (the
             * floor can never silence), expanded to the I2S TX's stereo-32 full-scale, and
             * appended to the ring. The wire is left at the next header either way. */
            size_t remaining = frame.payload_length;
            while (remaining) {
                size_t slice = remaining < sizeof(session->body) ? remaining : sizeof(session->body);
                size_t even = slice & 1 ? slice - 1 : slice;   /* whole 16-bit samples only */
                if (even) {
                    result = wy_read_exact(read_fn, ctx, session->body, even);
                    if (result) goto done;
                    /* The conversion (file-static scratch) and the ring append are one atomic
                     * unit under the ring lock: the scratch is shared by every wy_session_step
                     * driver (the gateway task and the USB console), and the append's drop-
                     * oldest touches the same head the playback task advances. Holding the
                     * lock across both is what keeps a second converter or a concurrent pump
                     * read from clobbering either. The wire read is OUTSIDE the lock, so a
                     * slow reader never holds the transport up. */
                    playback_ring_lock_take();
                    size_t samples = even / 2;
                    size_t mono_n = wy_resample_22050_to_16000(&session->playback_rs,
                                        (const int16_t *)session->body, samples,
                                        playback_mono_scratch, WY_PLAYBACK_MONO_MAX);
                    wy_apply_volume(playback_mono_scratch, mono_n, session->playback_volume);
                    size_t frame_n = wy_expand_mono16_to_stereo32(playback_mono_scratch, mono_n,
                                        playback_frame_scratch, WY_PLAYBACK_FRAME_MAX);
                    playback_ring_append_locked(session, playback_frame_scratch, frame_n);
                    playback_ring_lock_give();
                    remaining -= even;
                }
                if (remaining & 1) {
                    /* A lone trailing byte cannot be a sample: drain it off the wire and drop it. */
                    result = wy_read_exact(read_fn, ctx, session->body, 1);
                    if (result) goto done;
                    remaining--;
                }
            }
        } else {
            /* Any other payload (a chunk with no active stream, or a stream this device did not
             * arm) is drained off the wire but NOT kept: it can be as large as WY_PAYLOAD_MAX
             * (16 KiB), far beyond the 4 KiB parse body, so it is read in bounded body-sized
             * slices and discarded. Reading it whole into session->body wrote up to 12 KiB past
             * the buffer (a heap overflow proven to corrupt the next frame under ASan). */
            size_t remaining = frame.payload_length;
            while (remaining) {
                size_t slice = remaining < sizeof(session->body) ? remaining : sizeof(session->body);
                result = wy_read_exact(read_fn, ctx, session->body, slice);
                if (result) goto done;
                remaining -= slice;
            }
        }
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
    } else if (!strcmp(frame.type, "audio-start")) {
        /* The bridge's answer begins. The device plays the stream on the board's clock: it must
         * be the bridge's SND_FORMAT (22050 Hz / 16-bit / mono) -- the one rate this device
         * resamples to its 16 kHz I2S. A start in any other format is not armed: its chunks are
         * drained and its stop ignored, so an unplayable stream cannot wedge the link or leave
         * a half-armed ring. Arming is not a fault either way (well-formed, expected). */
        const cJSON *rate = cJSON_GetObjectItemCaseSensitive(data, "rate");
        const cJSON *width = cJSON_GetObjectItemCaseSensitive(data, "width");
        const cJSON *channels = cJSON_GetObjectItemCaseSensitive(data, "channels");
        if (cJSON_IsNumber(rate) && rate->valuedouble == 22050 &&
            cJSON_IsNumber(width) && width->valuedouble == 2 &&
            cJSON_IsNumber(channels) && channels->valuedouble == 1) {
            wy_resampler_reset(&session->playback_rs);
            /* Reset the ring under the lock: the playback task may be mid-read (draining the
             * last answer) while the bridge starts the next one, so an unlocked reset would
             * race the reader's head advance. */
            playback_ring_lock_take();
            session->ring_head = 0;
            session->ring_tail = 0;
            playback_ring_lock_give();
            session->playback_active = 1;
            session->playback_ended = 0;
        }
    } else if (!strcmp(frame.type, "audio-chunk")) {
        /* Inbound playback audio: converted into the ring by the payload block above when the
         * stream is armed, drained and discarded when it is not. Either way the link stays
         * usable -- refusing a payload (the old behavior) killed the session the first time the
         * bridge spoke. */
    } else if (!strcmp(frame.type, "audio-stop")) {
        /* The answer is over. The stream ends, but the ring KEEPS whatever has not been played
         * yet: the playback task drains it at realtime, and only when it is empty and the stream
         * is ended does the glue arm the amplifier off. A stop with no armed stream is ignored,
         * not a fault. */
        if (session->playback_active) {
            session->playback_active = 0;
            session->playback_ended = 1;
        }
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
