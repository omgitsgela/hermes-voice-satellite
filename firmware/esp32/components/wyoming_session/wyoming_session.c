#include "wyoming_session.h"
#include "wyoming_json.h"
#include "cJSON.h"
#include <stdio.h>
#include <string.h>
#ifdef ESP_PLATFORM
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#else
/* Host builds (the offline test harnesses) have no ESP-IDF. This file is deliberately portable
 * so the session can be exercised away from the device; the logging was the one thing that
 * silently broke every host suite that compiled it. */
#ifndef ESP_LOGE
#define ESP_LOGE(tag, ...) ((void)0)
#define ESP_LOGW(tag, ...) do { fprintf(stderr, "%s: ", tag); \
    fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); } while (0)
#define ESP_LOGI(tag, ...) ((void)0)
#define ESP_LOGD(tag, ...) ((void)0)
#endif
#endif

/* The wake word name is WYAKE_WORD_NAME (wyoming_session.h): the local detector and the
 * bridge's wake-now both report it, so the bridge's capture path sees one wake word whether
 * it was heard or requested. It is the DETECTION name, distinct from the config-plane
 * wake_word (the model selector) -- see the plan's D5. */

/* The device identity (wy_session_set_identity) with its historical defaults: a device whose
 * glue never set a field reports exactly what it always has, and a configured one reports its
 * own name / area / firmware version / detection name. The detection name defaults to
 * WYAKE_WORD_NAME; the glue overrides it with the model's name so the two satellite variants
 * report the same wake word on the wire. */
static const char *session_name(const wy_session *s) {
    return s->name[0] ? s->name : "ESP32 control probe";
}
static const char *session_area(const wy_session *s) {
    return s->area[0] ? s->area : "";
}
static const char *session_firmware(const wy_session *s) {
    return s->firmware_version[0] ? s->firmware_version : "esp32-audio-1";
}
static const char *session_wake_name(const wy_session *s) {
    return s->wake_name[0] ? s->wake_name : WYAKE_WORD_NAME;
}

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

/* The largest 22050 Hz INPUT slice whose up-sampled result still fits WY_PLAYBACK_MONO_MAX.
 * 22050 -> 48000 expands by 2.177 and the up-sampler needs two samples of lookahead, so at most
 * (2048 - 2) * 22050 / 48000 = 940 input samples may be fed per call.
 *
 * This bound matters, not just tidies: wy_resample_22050_to_48000 stops when its OUTPUT buffer
 * fills, and marks the whole input chunk consumed regardless. Feed it more than 940 samples in
 * one call and the tail of the answer is silently discarded -- audible as a reply cut short. The
 * caller therefore slices its input to this, exactly as wy_session_local_play does for a tone. */
#define WY_PLAYBACK_IN_MAX     940u   /* 22050 Hz samples per call: never overflows the scratch */
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
/* Frames thrown away because the ring was FULL. The append drops the oldest frame to keep the
 * link moving, which is the right policy, but until this counter existed nothing anywhere recorded
 * that audio had been SKIPPED -- so "the ring was never empty" and "no writes were abandoned"
 * could both be true while the speaker was losing whole frames. */
static uint32_t plb_dropped;

/* The declared rate of the CURRENT inbound stream.
 *
 * 48000 means the bridge already produced audio at the link rate and the device must NOT touch it:
 * up-sampling on the microcontroller costs cycles, adds a cubic interpolation stage that can only
 * lose information, and is redundant work. 22050 is kept for hosts that still send Piper's native
 * rate; only then is the ESP32's resampler used. */
static uint32_t plb_in_rate;

static void playback_ring_append_locked(wy_session *session, const int32_t *frames, size_t n) {
    size_t cap = session->ring_capacity / 4;
    if (!session->ring || cap == 0 || n == 0) return;
    /* Stereo frames are TWO int32 words (L then R). Dropping ONE word on overflow desynchronises
     * the pairing: the retained stream then starts mid-frame and every subsequent sample is read
     * as the wrong channel -- an effect no amount of volume or codec tuning can fix. The usable
     * capacity is also intentionally one word short of the allocation (the ring keeps one slot to
     * tell full from empty), so it is ODD while the data is in pairs. Drop whole frames. */
    size_t pairs = n & ~(size_t)1;          /* never write a half frame */
    for (size_t i = 0; i + 1 < pairs; i += 2) {
        /* Make room for both words before writing either, so a frame is never split. */
        while ((session->ring_tail + 2) % cap == session->ring_head
               || (session->ring_tail + 1) % cap == session->ring_head) {
            session->ring_head = (session->ring_head + 2) % cap;   /* full: drop the oldest FRAME */
            plb_dropped++;
        }
        session->ring[session->ring_tail] = frames[i];
        session->ring_tail = (session->ring_tail + 1) % cap;
        session->ring[session->ring_tail] = frames[i + 1];
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
    wy_upsampler_reset(&session->playback_rs);
    playback_ring_lock_give();
    return WY_OK;
}

int wy_session_set_playback_volume(wy_session *session, int volume) {
    if (!session) return WY_INVALID;
    /* Change the LIVE level for the next audio slice (the config plane's apply path). The write
     * takes the ring lock: the audio-chunk conversion reads playback_volume under that same
     * lock while converting into the ring, so an unlocked write here would race the reader on a
     * two-core device (a torn 32-bit store is the failure mode). The value is stored raw;
     * wy_apply_volume clamps it to the no-silence floor at use, so the setter never silences
     * the speaker and the bridge's conservative values (25..100) pass through unchanged. */
    playback_ring_lock_take();
    session->playback_volume = volume;
    playback_ring_lock_give();
    return WY_OK;
}

/* ---- arrival telemetry ------------------------------------------------------------------
 *
 * "Is the producer keeping up, and is it bursty?" decides where a playback stall lives: in the
 * transport (arrival slower or spikier than the 48 kHz clock), or in the pump. Frames per second
 * is the answer -- 48000 frames/s of stereo-32 is exactly realtime, and the bridge paces at 2x, so
 * a healthy stream should read ~96000 frames/s here.
 *
 * The max gap matters as much as the average: a stream that averages 2x realtime but stalls for
 * 300 ms will empty ANY small buffer, which is why a pre-roll only helps if the long gaps are
 * bounded.
 */
/* Portable tick source. This component is compiled for the HOST by the test harnesses, where
 * FreeRTOS does not exist -- using TickType_t directly here broke eight of them at compile time.
 * On the device the counts are real; on the host they are zero, and the telemetry is simply inert
 * (the harnesses assert protocol behaviour, not timing). */
#ifdef ESP_PLATFORM
static uint32_t plb_ticks(void) { return (uint32_t)xTaskGetTickCount(); }
static uint32_t plb_ticks_per_s(void) { return (uint32_t)configTICK_RATE_HZ; }
#else
static uint32_t plb_ticks(void) { return 0u; }
static uint32_t plb_ticks_per_s(void) { return 1000u; }
#endif

static uint32_t plb_in_frames;          /* frames appended since the last report */
static uint32_t plb_in_appends;         /* append calls since the last report */
static uint32_t plb_in_last;            /* tick of the previous append */
static uint32_t plb_in_max_gap;         /* largest inter-append gap, ticks */
static uint32_t plb_in_window;          /* window start */

static void plb_in_report(void) {
    uint32_t now = plb_ticks();
    if (!plb_in_window) { plb_in_window = now; return; }
    uint32_t elapsed = now - plb_in_window;
    if (elapsed < plb_ticks_per_s()) return;
    if (plb_in_frames || plb_in_appends) {
        uint32_t ms = (uint32_t)(elapsed * 1000u / plb_ticks_per_s());
        uint32_t frames_s = ms ? (uint32_t)(((uint64_t)plb_in_frames * 1000u) / ms) : 0u;
        uint32_t gap_ms = (uint32_t)(plb_in_max_gap * 1000u / plb_ticks_per_s());
        /* ESP_LOGI compiles to nothing on the host harnesses, which would leave these "unused"
         * under -Werror. Consume them explicitly so the same source builds for device and host. */
        (void)frames_s;
        (void)gap_ms;
        ESP_LOGI("plbin", "arrival: %u frames/s (%u%% of realtime), %u appends, max gap %u ms, "
                          "%u frames DROPPED (ring full)",
                 (unsigned)frames_s, (unsigned)((frames_s * 100u) / 48000u),
                 (unsigned)plb_in_appends, (unsigned)gap_ms, (unsigned)plb_dropped);
    }
    plb_in_frames = 0; plb_in_appends = 0; plb_in_max_gap = 0;
    plb_in_window = now;
}

/* Set while device-generated audio (the wake chime, the done tone) is still in the ring.
 *
 * These share the playback ring with stream audio, and the pump's pre-roll gate exists only for
 * STREAM audio -- it buffers until the network has delivered a cushion. Applying it to a local
 * chime silenced and chopped the very tone that confirms the wake, because the chime is queued at
 * the instant a stream may also be starting. */
static volatile int s_local_pending;

int wy_session_local_pending(wy_session *session) { (void)session; return s_local_pending; }
void wy_session_local_done(void) { s_local_pending = 0; }

int wy_session_local_play(wy_session *session, const int16_t *mono, size_t n) {
    /* Every drop point is reported: a chime that vanishes here is exactly a "no ding" symptom,
     * and "no ring" is the one that looks identical to a broken speaker from outside. */
    if (!session || !mono) {
        ESP_LOGW("plb_local", "local_play dropped: session=%p mono=%p", (void *)session, (const void *)mono);
        return WY_INVALID;
    }
    if (!session->ring || session->ring_capacity % 4) {
        ESP_LOGW("plb_local", "local_play dropped: ring=%p capacity=%u (no ring is nowhere to go)",
                 (void *)session->ring, (unsigned)session->ring_capacity);
        return WY_INVALID;
    }
    ESP_LOGI("plb_local", "local_play %u samples, ring %u bytes, volume %u",
             (unsigned)n, (unsigned)session->ring_capacity, (unsigned)session->playback_volume);
    /* Marked BEFORE the appends so the pump's pre-roll gate cannot slip in between and start
     * buffering network audio in front of this tone. */
    s_local_pending = 1;
    /* The device's own local tone (the wake chime): the same speaker path as a bridge answer,
     * so the pump task's amplifier policy (armed while the ring is not empty) carries it. The
     * difference from the inbound audio-chunk path is only the source: the chime is rendered at
     * 16 kHz by the same generator the Pi satellite uses, so it is UP-SAMPLED to the link rate here
     * (one up-sampler per tone, reset at entry, so its history is the tone's own) -- then the
     * volume (the config plane's level, the floor never silences) and the stereo-32 expand
     * are the SAME wy_apply_volume / wy_expand_mono16_to_stereo32 the answer uses, and the ring
     * append is under the ring lock, exactly like the inbound path: the append's drop-oldest
     * touches the same head the pump advances, and the scratch is shared with every
     * wy_session_step driver, so the conversion+append is one atomic unit. Input longer than
     * one scratch slice is sliced: the scratch is file-static and shared, and a second driver
     * between slices must not find a half-converted buffer. */
    /* One up-sampler per tone, as an AUTOMATIC (stack) variable -- not static. A file-static
     * one was reset outside the ring lock and the lock was released between slices, so a second
     * tone (a diagnostic tone overlapping the playback task's done tone) could reset or advance
     * the first one's cursor and history mid-tone. The resampler's lifetime is per invocation;
     * only the scratch and the ring are shared, and those stay under the per-slice lock.
     * It is reset at entry rather than per slice: the cursor and the cubic history must span the
     * whole tone or every slice boundary would step the waveform. */
    wy_upsampler tone_up;
    wy_upsampler_reset_at(&tone_up, 16000.0, (double)WY_AUDIO_I2S_RATE_HZ);
    /* The slice is sized for the UP-SAMPLED result: a 3:1 expansion must fit the scratch, so at
     * most a third of it may be fed in. Slicing larger would overflow playback_mono_scratch. */
    const size_t max_in = WY_PLAYBACK_MONO_MAX / 3;
    size_t remaining = n;
    const int16_t *src = mono;
    while (remaining) {
        size_t slice = remaining < max_in ? remaining : max_in;
        playback_ring_lock_take();
        size_t mono_n = wy_resample_22050_to_48000(&tone_up, src, slice,
                                playback_mono_scratch, WY_PLAYBACK_MONO_MAX);
        wy_apply_volume(playback_mono_scratch, mono_n, session->playback_volume);
        size_t frame_n = wy_expand_mono16_to_stereo32(playback_mono_scratch, mono_n,
                            playback_frame_scratch, WY_PLAYBACK_FRAME_MAX);
        playback_ring_append_locked(session, playback_frame_scratch, frame_n);
        playback_ring_lock_give();
        src += slice;
        remaining -= slice;
        }
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
    session->stream_frames = 0;   /* the next wake starts a fresh, bounded stream */
    ESP_LOGI("gwstop", "stop-streaming: acquiring writer lock");
    int result = writer_lock(session);
    if (result != WY_OK) return result;
    if (session->streaming) {
        result = wy_send(write_fn, ctx, "streaming-stopped", NULL, NULL, 0);
        if (!result) session->streaming = 0;
    }
    writer_unlock(session);
    ESP_LOGI("gwstop", "stop-streaming: result=%d streaming=%d", result, session->streaming);
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
    /* The config-plane handler and its config object are installed once by the glue, the same
     * kind of installed-once config as the writer lock and the playback ring: the supervisor
     * re-inits this SAME buffer on every reconnect, so a re-init that wiped them would leave a
     * device that silently ignores every config dispatch after its first reconnect (and the
     * agent's durable state -- the generation guard -- would be orphaned along with it). */
    int (*config_handler)(void *, const char *, char *, size_t) = session->config_handler;
    void *config_user = session->config_user;
    /* The presentation seam is the same kind of installed-once config: the glue announces it once
     * and a reconnect must not leave the device unlit for the rest of the boot. */
    void (*phase_notify)(void *, int) = session->phase_notify;
    void *phase_user = session->phase_user;
    /* The device identity is the same kind of installed-once config: a reconnect must not
     * forget the satellite's name, area, firmware version or detection name. */
    char name[65], area[65], firmware_version[33], wake_name[65];
    memcpy(name, session->name, sizeof(name));
    memcpy(area, session->area, sizeof(area));
    memcpy(firmware_version, session->firmware_version, sizeof(firmware_version));
    memcpy(wake_name, session->wake_name, sizeof(wake_name));
    memset(session, 0, sizeof(*session));
    session->lock_writer = lock_writer;
    session->unlock_writer = unlock_writer;
    session->writer_lock_user = writer_lock_user;
    session->ring = playback_ring;
    session->ring_capacity = playback_ring_capacity;
    session->playback_volume = playback_volume;
    session->config_handler = config_handler;
    session->config_user = config_user;
    session->phase_notify = phase_notify;
    session->phase_user = phase_user;
    memcpy(session->name, name, sizeof(name));
    memcpy(session->area, area, sizeof(area));
    memcpy(session->firmware_version, firmware_version, sizeof(firmware_version));
    memcpy(session->wake_name, wake_name, sizeof(wake_name));
    session->state = WY_SESSION_FAILED;
    if (!token_valid(device_id, 64) || !token_valid(boot_id, 128)) return WY_INVALID;
    strcpy(session->device_id, device_id);
    strcpy(session->boot_id, boot_id);
    session->state = WY_SESSION_DESCRIBE;
    return WY_OK;
}

void wy_session_set_identity(wy_session *session, const wy_session_identity *identity) {
    if (!session || !identity) return;
    /* A NULL or empty field keeps the historical default (see the payload builders): the glue
     * passes what it read from NVS, and an unset setting stays the documented default rather
     * than becoming an empty string on the wire. */
    if (identity->name && identity->name[0])
        snprintf(session->name, sizeof(session->name), "%s", identity->name);
    if (identity->area && identity->area[0])
        snprintf(session->area, sizeof(session->area), "%s", identity->area);
    if (identity->firmware_version && identity->firmware_version[0])
        snprintf(session->firmware_version, sizeof(session->firmware_version), "%s",
                 identity->firmware_version);
    if (identity->wake_name && identity->wake_name[0])
        snprintf(session->wake_name, sizeof(session->wake_name), "%s", identity->wake_name);
}

int wy_session_set_config(wy_session *session,
                          int (*handler)(void *config, const char *desired_json,
                                         char *report_out, size_t report_cap),
                          void *config) {
    if (!session) return WY_INVALID;
    session->config_handler = handler;
    session->config_user = config;
    return WY_OK;
}

/* Announce a phase to the presentation layer. Called only from wy_session_step -- the LINK task,
 * never the realtime playback pump -- so a device that lights its ring does so off the audio
 * path. A missing callback is not an error: the core does not require a presentation. */
static void announce_phase(wy_session *session, int phase)
{
    if (session && session->phase_notify) session->phase_notify(session->phase_user, phase);
}

void wy_session_set_phase_notify(wy_session *session,
                                 void (*fn)(void *user, int phase), void *user)
{
    if (!session) return;
    session->phase_notify = fn;
    session->phase_user = user;
}

int __attribute__((used)) wy_session_wake(wy_session *session, const char *wake_word,
                    int wake_level, float wake_score, wy_write_fn write_fn, void *ctx) {
    if (!session || !wake_word || !write_fn) return WY_INVALID;
    /* Armed only: the microphone starts on run-satellite (the armed hook), so a wake in any
     * other state -- not registered yet, registered but not armed, or the stream already
     * running -- would open a stream no capture task can feed. That is a drop, not a fault. */
    if (session->state != WY_SESSION_RUN_REQUESTED) return WY_INVALID;
    if (session->paused || session->streaming) return WY_INVALID;
    if (!token_valid(wake_word, 64)) return WY_INVALID;
    /* The detection data: the historical {"name":...} shape, plus the arbitration context
     * when the detector measured a level (issue #32). Anything outside the core's 0..100
     * contract (or the scorer's (0,1] for the score) is treated as "no measurement" and sent
     * exactly like the old frame -- a buggy or older caller can never corrupt the detection
     * the bridge depends on. The score only rides with a level: a level-less wake is the
     * historical frame, full stop, so an old bridge sees byte-identical output. */
    char name[144];
    int length;
    int score_ok = (wake_score > 0.0f && wake_score <= 1.0f);
    if (wake_level >= 0 && wake_level <= 100 && score_ok)
        length = snprintf(name, sizeof(name),
                          "{\"name\":\"%s\",\"context\":{\"wake_level\":%d,\"wake_score\":%.3f}}",
                          wake_word, wake_level, wake_score);
    else if (wake_level >= 0 && wake_level <= 100)
        length = snprintf(name, sizeof(name),
                          "{\"name\":\"%s\",\"context\":{\"wake_level\":%d}}",
                          wake_word, wake_level);
    else
        length = snprintf(name, sizeof(name), "{\"name\":\"%s\"}", wake_word);
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
    if (!result) {
        session->streaming = 1;
        session->stream_frames = 0;
    }
    writer_unlock(session);
    if (result != WY_OK) { session->state = WY_SESSION_FAILED; return result; }
    /* The microphone is open and the query is being captured. The device's wake animation
     * already covers this moment and will usually get here first; the announcement is for the
     * case where a stream opens without one (a bridge-driven wake-now), and the presentation
     * layer is free to ignore it when it is already showing this phase. */
    announce_phase(session, WY_PHASE_VOICE);
    return WY_OK;
    }

    /* Tell the bridge its speaker has finished.
 *
 * `played` is the device saying "that answer is done sounding". The bridge waits for
 * it after every reply, and firmware that never sends it parks the conversation in
 * that wait -- measured at a full 20 s of dead air after every answer. It is one
 * control frame under the writer lock, exactly like every other link write. */
int wy_session_send_played(wy_session *session, wy_write_fn write_fn, void *ctx) {
    if (!session || !write_fn) return WY_INVALID;
    return locked_send(session, write_fn, ctx, "played", NULL, NULL, 0);
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
    /* An open microphone stream is a promise to someone in the room, and it must not be
     * open-ended. The bridge ends it with `transcript`; if that never arrives -- a wake the
     * bridge loses track of, a crash mid-turn -- the device would keep streaming a room
     * nobody is listening to. The reference satellite closes its stream at the end of the
     * utterance; this is the same guarantee, enforced from the device side. */
    if (++session->stream_frames >= WY_STREAM_MAX_FRAMES) {
        ESP_LOGW("gwstop", "streaming: closing after %u frames with no transcript "
                 "(the host stopped driving this utterance)",
                 (unsigned)session->stream_frames);
        (void)stop_streaming(session, write_fn, ctx);
        /* A stream that ends without a transcript still ends. Without this the ring is left
         * showing a query that is not happening -- a false wake with no speech would leave the
         * direction marker lit for the rest of the boot. */
        announce_phase(session, WY_PHASE_IDLE);
        return WY_INVALID;
        }
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
    wy_frame frame = {0};
    const char *phase = "header-read";
    cJSON *data = NULL;
    int result = wy_read_header(read_fn, ctx, session->header, sizeof(session->header));
    if (result) goto done;
    phase = "header-parse";
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
    if (strcmp(frame.type, "audio-chunk"))
        ESP_LOGI("gwframe", "received type=%s data=%u payload=%u", frame.type,
                 (unsigned)frame.data_length, (unsigned)frame.payload_length);
    phase = "body-read";
    if (frame.data_length) {
        result = wy_read_exact(read_fn, ctx, session->body, frame.data_length);
        if (result) goto done;
        if (!frame.payload_length) {
            phase = "body-decode";
            result = wy_decode_control(session->header, strlen(session->header),
                                       session->body, frame.data_length, &data);
            if (result) goto done;
        }
    }
    phase = "payload-read";
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
                    /* Sub-slice to WY_PLAYBACK_IN_MAX so the up-sampled result always fits the
                     * scratch (see its definition for what silently breaks otherwise). Each
                     * sub-slice is one lock/append unit; the resampler's absolute cursor keeps
                     * the sub-slices continuous. */
                    const int16_t *src = (const int16_t *)session->body;
                    size_t samples = even / 2;
                    while (samples) {
                        size_t take = samples < WY_PLAYBACK_IN_MAX ? samples : WY_PLAYBACK_IN_MAX;
                        playback_ring_lock_take();
                        size_t mono_n;
                        if (plb_in_rate == (uint32_t)WY_AUDIO_I2S_RATE_HZ) {
                            /* Native: the bridge already did the conversion, on a CPU with room to
                             * do it properly. Copy, scale, expand -- no interpolation stage at all. */
                            if (take > WY_PLAYBACK_MONO_MAX) take = WY_PLAYBACK_MONO_MAX;
                            memcpy(playback_mono_scratch, src, take * sizeof(int16_t));
                            mono_n = take;
                        } else {
                            mono_n = wy_resample_22050_to_48000(&session->playback_rs,
                                                src, take,
                                                playback_mono_scratch, WY_PLAYBACK_MONO_MAX);
                        }
                        wy_apply_volume(playback_mono_scratch, mono_n, session->playback_volume);
                        size_t frame_n = wy_expand_mono16_to_stereo32(playback_mono_scratch, mono_n,
                                            playback_frame_scratch, WY_PLAYBACK_FRAME_MAX);
                        playback_ring_append_locked(session, playback_frame_scratch, frame_n);
                        playback_ring_lock_give();
                        /* Where the audio actually lands, measured. See plb_in_report(). */
                        plb_in_frames += (uint32_t)frame_n;
                        plb_in_appends++;
                        {
                            uint32_t now_t = plb_ticks();
                            if (plb_in_last) {
                                uint32_t gap = now_t - plb_in_last;
                                if (gap > plb_in_max_gap) plb_in_max_gap = gap;
                            }
                            plb_in_last = now_t;
                        }
                        plb_in_report();
                        src += take;
                        samples -= take;
                    }
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
    phase = "dispatch";
    if (!strcmp(frame.type, "describe")) {
        char info[768];
        int info_len = snprintf(info, sizeof(info),
            "{\"satellite\":{\"name\":\"%s\",\"area\":\"%s\","
            "\"installed\":true,\"attribution\":{\"name\":\"Hermes Voice Satellite\","
            "\"url\":\"https://github.com/omgitsgela/hermes-voice-satellite\"},"
            "\"active_wake_words\":[],\"max_active_wake_words\":0,\"supports_trigger\":false}}",
            session_name(session), session_area(session));
        if (info_len <= 0 || (size_t)info_len >= sizeof(info)) { result = WY_LIMIT; goto done; }
        result = locked_send(session, write_fn, ctx, "info", info, NULL, 0);
        if (result) goto done;
        if (session->state == WY_SESSION_DESCRIBE) {
            char registration[512];
            int length = snprintf(registration, sizeof(registration),
                "{\"device_id\":\"%s\",\"boot_id\":\"%s\",\"protocol_version\":1,"
                "\"name\":\"%s\",\"area\":\"%s\","
                "\"firmware_version\":\"%s\","
                "\"capabilities\":[\"microphone\",\"speaker\"]}",
                session->device_id, session->boot_id,
                session_name(session), session_area(session), session_firmware(session));
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
        /* The host said no. This is not a wobbly link that might come back -- it is a definite
         * refusal -- so it is red at once rather than after the disconnect grace. */
        announce_phase(session, WY_PHASE_ERROR);
        session->state = WY_SESSION_REFUSED;
        result = WY_REFUSED;
    } else if (!strcmp(frame.type, "run-satellite")) {
        if (session->state != WY_SESSION_REGISTERED && session->state != WY_SESSION_RUN_REQUESTED)
            result = WY_INVALID;
        else {
            /* The reference stops a running stream on run-satellite too (it is "back to
             * wake-word listening"), and clears a pause. */
            int was_streaming = session->streaming;
            if (session->streaming) result = stop_streaming(session, write_fn, ctx);
            if (!result) {
                session->paused = 0;
                session->state = WY_SESSION_RUN_REQUESTED;
                /* The bridge closes a silent window this way: "back to wake-word listening". A
                 * wake that heard nothing must not leave the ring claiming a query. */
                if (was_streaming) announce_phase(session, WY_PHASE_IDLE);
            }
        }
    } else if (!strcmp(frame.type, "pause-satellite")) {
        if (session->state != WY_SESSION_REGISTERED && session->state != WY_SESSION_RUN_REQUESTED)
            result = WY_INVALID;
        else {
            int was_streaming = session->streaming;
            if (session->streaming) result = stop_streaming(session, write_fn, ctx);
            if (!result) {
                session->paused = 1;
                session->state = WY_SESSION_REGISTERED;
                if (was_streaming) announce_phase(session, WY_PHASE_IDLE);
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
        /* The utterance has been captured and handed over. From here the bridge is working out
         * the answer, which is exactly the "thinking" the device should be showing: the gap
         * between the person stopping talking and the first word coming back. */
        if (result == WY_OK) announce_phase(session, WY_PHASE_TRANSCRIPT);
    } else if (!strcmp(frame.type, "audio-start")) {
        /* The bridge's answer begins. The device plays the stream on the board's clock: it must
         * be the bridge's SND_FORMAT (22050 Hz / 16-bit / mono) -- the one rate this device
         * resamples to its 16 kHz I2S. A start in any other format is not armed: its chunks are
         * drained and its stop ignored, so an unplayable stream cannot wedge the link or leave
         * a half-armed ring. Arming is not a fault either way (well-formed, expected). */
        const cJSON *rate = cJSON_GetObjectItemCaseSensitive(data, "rate");
        const cJSON *width = cJSON_GetObjectItemCaseSensitive(data, "width");
        const cJSON *channels = cJSON_GetObjectItemCaseSensitive(data, "channels");
        /* Accept the link rate (nothing to convert) or Piper's 22050 (legacy hosts).
         * Anything else cannot be played, so it is drained rather than half-armed. */
        if (cJSON_IsNumber(rate) &&
            (rate->valuedouble == 22050.0 || rate->valuedouble == (double)WY_AUDIO_I2S_RATE_HZ) &&
            cJSON_IsNumber(width) && width->valuedouble == 2 &&
            cJSON_IsNumber(channels) && channels->valuedouble == 1) {
            plb_in_rate = (uint32_t)rate->valuedouble;
            if (plb_in_rate != (uint32_t)WY_AUDIO_I2S_RATE_HZ)
                wy_upsampler_reset(&session->playback_rs);   /* only when actually converting */
            /* Reset the ring under the lock: the playback task may be mid-read (draining the
             * last answer) while the bridge starts the next one, so an unlocked reset would
             * race the reader's head advance. */
            playback_ring_lock_take();
            /* Do NOT clear a pending device tone. The wake chime is queued by the capture task at
             * the instant of detection, and the bridge's first audio-start arrives milliseconds
             * later; resetting here deleted the chime before it was ever played, which is heard as
             * "the chime is extremely laggy" (in fact it was dropped, and the prompt tone that
             * followed started from silence). Only stale STREAM audio is discarded. */
            if (!wy_session_local_pending(session)) {
                session->ring_head = 0;
                session->ring_tail = 0;
            } else {
                ESP_LOGI("audstart", "audio-start: a device tone is still queued; keeping the ring");
            }
            playback_ring_lock_give();
            ESP_LOGW("audstart", "audio-start: ring reset done, arming playback");
            session->playback_active = 1;
            ESP_LOGW("audstart", "audio-start: playback armed, returning to the link loop");
            session->playback_ended = 0;
            /* The answer is arriving: stop glowing and start showing that it is being said. */
            announce_phase(session, WY_PHASE_SPEAKING);
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
            /* Everything the bridge will say has been sent. The ring still drains and the
             * speaker still sounds its tail, so the light goes dark a moment early -- chosen
             * deliberately over announcing from the playback pump, which is the realtime path
             * and must not be made to do I2C work. */
            announce_phase(session, WY_PHASE_IDLE);
        }
    } else if (!strcmp(frame.type, "wake-now")) {
        /* The bridge's "open the microphone" command, for a follow-up window that has no wake
         * word: the reference device's wake shim answers it by injecting a synthetic detection.
         * Here the session core IS the device, so it runs the wake sequence itself, with the
         * device's wake word name. A wake-now that lands while the stream is already running or
         * the device is paused is dropped (wy_session_wake checks both), and one that arrives
         * before the host is ready fails the same way an out-of-order wake would. */
        result = wy_session_wake(session, session_wake_name(session), -1, -1.0f, write_fn, ctx);
        if (result == WY_INVALID) result = WY_OK;   /* a dropped wake-now is not a fault */
        /* A follow-up window is open: the bridge is listening for more speech with no wake word.
         * This is the moment the reference satellite shows its dim teal, and it is the only
         * signal the device has that the conversation is not over. */
        announce_phase(session, WY_PHASE_FOLLOWUP);
    } else if (!strcmp(frame.type, "led-idle") || !strcmp(frame.type, "led-busy") ||
               !strcmp(frame.type, "led-followup")) {
        /* The bridge's explicit presentation commands. The reference satellite receives these
         * over its own control socket; here they arrive as ordinary events. They matter because
         * the bridge uses led-idle to clean up after aborted work and an expired follow-up --
         * exactly the cases the device cannot work out for itself. */
        if (!strcmp(frame.type, "led-idle")) announce_phase(session, WY_PHASE_IDLE);
        else if (!strcmp(frame.type, "led-busy")) announce_phase(session, WY_PHASE_TRANSCRIPT);
        else announce_phase(session, WY_PHASE_FOLLOWUP);
    } else if (!strcmp(frame.type, "hermes.config.desired")) {
        /* The bridge's declarative config dispatch, sent right after registration (and on
         * reconnect). The session is only the transport: it NUL-terminates the decoded data
         * (body was read raw, not terminated) and hands it to the injected config handler,
         * which owns all the config logic -- the agent's guard, the apply, the report. The
         * handler's returned reported JSON goes straight back as hermes.config.reported. A
         * send failure is a real link fault (the report is the dispatch's only answer); a
         * handler that fails to build a report is a protocol bug we surface the same way.
         * With no handler wired the frame is drained and the link stays alive (the pre-config
         * behavior), so a device that never wires the plane is not bricked by it.
         *
         * A config frame carrying a payload is malformed and is refused BEFORE the handler:
         * the desired state rides in `data` only, and the payload drain above REUSES this same
         * parse body -- by dispatch time the body holds the payload, not the desired. Handing
         * it to the handler would let a payload tail that happens to parse as a desired be
         * silently APPLIED; refusing (the same policy as an unknown-with-payload) is the only
         * honest answer, and the bridge reconnects and re-dispatches the well-formed desired. */
        if (frame.payload_length) {
            result = WY_INVALID;
        } else if (session->config_handler) {
            session->body[frame.data_length < sizeof(session->body) ? frame.data_length
                                                                    : sizeof(session->body) - 1] = '\0';
            /* Sized for the agent's worst-case report: generation (11 digits, the INT32_MAX
             * bound) + the full 7-setting actual (~200 bytes) + health + a SANITIZED error
             * (validate_settings caps the peer-controlled key at 31 printable chars, so the
             * error is bounded ~50 bytes and adds nothing under JSON escaping). The real
             * worst case is ~300 bytes; 1024 is a comfortable multiple, small for the gateway
             * task's 10 KB stack, and gives headroom if the settings grow. */
            char report[1024];
            int handled = session->config_handler(session->config_user, session->body,
                                                  report, sizeof(report));
            if (handled != WY_OK) { result = handled; goto done; }
            result = locked_send(session, write_fn, ctx, "hermes.config.reported", report, NULL, 0);
        }
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
    if (result)
        ESP_LOGW("gwframe", "failed phase=%s type=%s data=%u payload=%u result=%d",
                 phase, frame.type[0] ? frame.type : "unknown",
                 (unsigned)frame.data_length, (unsigned)frame.payload_length, result);
    cJSON_Delete(data);
    if (result && session->state != WY_SESSION_REFUSED) session->state = WY_SESSION_FAILED;
    return result;
}