#ifndef WYOMING_SESSION_H
#define WYOMING_SESSION_H
#include "wyoming_wire.h"
/* State: the host's request to run (RUN_REQUESTED) is the armed state -- the device is
 * listening for its wake word. `streaming` (below) means the mic is being forwarded to the
 * bridge right now, between a wake and the bridge's transcript. */
enum { WY_SESSION_DESCRIBE = 0, WY_SESSION_ACK = 1, WY_SESSION_REGISTERED = 2,
       WY_SESSION_RUN_REQUESTED = 3, WY_SESSION_REFUSED = 4, WY_SESSION_FAILED = 5 };
enum { WY_REFUSED = -4 };
typedef struct {
    int state;
    int streaming;        /* 1 while the mic stream is being forwarded to the bridge */
    int paused;           /* 1 after pause-satellite; run-satellite clears it */
    char device_id[65];
    char boot_id[129];
    char header[WY_HEADER_MAX];
    char body[WY_DATA_MAX + 1];
    /* Every byte the device puts on the link goes through the writer lock, because more than
     * one task writes: the gateway task (control answers, streaming markers) and the capture
     * task (mic audio chunks). The callbacks are injected: NULL on the host, where the session
     * is single-threaded and the lock is a no-op. */
    int (*lock_writer)(void *user);
    int (*unlock_writer)(void *user);
    void *writer_lock_user;
} wy_session;
size_t wy_session_size(void);
int wy_session_init(wy_session *session, const char *device_id, const char *boot_id);
int wy_session_state(const wy_session *session);
/* Read exactly one inbound event. One owner (the gateway task); callbacks impose finite
 * deadlines. Close the transport on every non-OK result; init a fresh session on reconnect.
 * A well-formed control event this device does not implement (config, or an unknown type) is
 * safely ignored and the call returns OK: the frame already passed the strict decode above,
 * and a real bridge sends several of these -- starting with desired state immediately after
 * registration -- so tearing the link on the first one would make it unusable. Inbound
 * playback audio (audio-start / audio-chunk / audio-stop) is accepted and its payload drained
 * off the wire: the speaker path is a later increment, but refusing the frame the way the old
 * core refused every payload killed the link the first time the bridge spoke. Only a malformed
 * frame, an UNKNOWN event carrying a payload, or a refused registration fails. */
int wy_session_step(wy_session *session, wy_read_fn read_fn, wy_write_fn write_fn, void *ctx);
/* The wake detector's one call (the local wake word fired): send detection, run-pipeline and
 * streaming-started in that order and start the mic stream. Valid while the host is ready
 * (registered or running) and not paused; a wake that lands while the stream is already
 * running, or while paused, is dropped. Returns OK when the stream is running (it may already
 * have been), INVALID when the wake is not allowed, and the send result on a dead link. */
int wy_session_wake(wy_session *session, const char *wake_word,
                    wy_write_fn write_fn, void *ctx);
/* The capture task's one call: send one chunk of microphone audio (16 kHz / S16 / mono, at
 * most WY_AUDIO_CHUNK_SAMPLES*2 bytes) as an audio-chunk frame. The frame is written whole
 * under the writer lock, so the link never carries two frames at once. Returns INVALID when
 * the stream is not running (the mic is closed and the audio is dropped, exactly like the
 * reference satellite's event_from_mute path). */
int wy_session_send_audio(wy_session *session, const void *pcm, size_t length,
                          wy_write_fn write_fn, void *ctx);
int wy_session_streaming(const wy_session *session);

/* Stable, human-readable names for session state and result codes, used in reports. A code
 * this build does not know reports as "unknown" rather than being silently unnamed. */
const char *wy_session_state_name(int state);
const char *wy_result_name(int result);
#endif
