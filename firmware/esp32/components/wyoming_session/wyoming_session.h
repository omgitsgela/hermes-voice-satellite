#ifndef WYOMING_SESSION_H
#define WYOMING_SESSION_H
#include "wyoming_wire.h"
#include "wyoming_audio.h"
/* State: the host's request to run (RUN_REQUESTED) is the armed state -- the device is
 * listening for its wake word. `streaming` (below) means the mic is being forwarded to the
 * bridge right now, between a wake and the bridge's transcript. */
enum { WY_SESSION_DESCRIBE = 0, WY_SESSION_ACK = 1, WY_SESSION_REGISTERED = 2,
       WY_SESSION_RUN_REQUESTED = 3, WY_SESSION_REFUSED = 4, WY_SESSION_FAILED = 5 };
enum { WY_REFUSED = -4 };
/* The wake word name this device reports in the detection event. The local wake detector
 * (the device glue) and the bridge's wake-now both report this SAME name, so the bridge's
 * capture path sees one wake word whether it was heard locally or requested over the link.
 * It is the DEFAULT detection name: the glue may override it per device with the model's own
 * name (the config-plane wake_word) via wy_session_set_identity, which is what makes the two
 * satellite variants report the same name on the wire -- the Pi reports whatever model it
 * loaded, so the ESP32 reports its model name too. The model file is named by the config
 * wake_word (e.g. hey_hermes); the detection event reports that same model name. */
#define WYAKE_WORD_NAME "hey-hermes"

/* The device's identity as the bridge and the house know it: the same fields the Pi reads from
 * env (VOICE_DEVICE_NAME / VOICE_DEVICE_AREA / VOICE_FIRMWARE_VERSION) and the model name it
 * reports as the detection name. Set once by the device glue (from NVS + the loaded model);
 * the session core keeps them across re-init and uses them in its registration, describe/info
 * and wake sequences. A caller that never sets them keeps the historical defaults. */
typedef struct {
    const char *name;             /* the satellite's name (the Pi's VOICE_DEVICE_NAME) */
    const char *area;             /* the room/area (the Pi's VOICE_DEVICE_AREA) */
    const char *firmware_version; /* (the Pi's VOICE_FIRMWARE_VERSION) */
    const char *wake_name;        /* the detection name: the model name / config wake_word */
} wy_session_identity;

typedef struct {
    int state;
    int streaming;        /* 1 while the mic stream is being forwarded to the bridge */
    int paused;           /* 1 after pause-satellite; run-satellite clears it */
    char device_id[65];
    char boot_id[129];
    char name[65];             /* the identity (wy_session_set_identity); empty = default */
    char area[65];
    char firmware_version[33];
    char wake_name[65];        /* the detection name; empty = WYAKE_WORD_NAME */
    char header[WY_HEADER_MAX];
    char body[WY_DATA_MAX + 1];
    /* Every byte the device puts on the link goes through the writer lock, because more than
     * one task writes: the gateway task (control answers, streaming markers) and the capture
     * task (mic audio chunks). The callbacks are injected: NULL on the host, where the session
     * is single-threaded and the lock is a no-op. */
    int (*lock_writer)(void *user);
    int (*unlock_writer)(void *user);
    void *writer_lock_user;
    /* The playback (bridge -> speaker) side: a bounded byte ring of I2S-ready stereo-32 frames
     * the session core (the gateway task) writes as each inbound audio-chunk is converted, and
     * the playback task reads at realtime. The ring is caller-provided (PSRAM on the device);
     * the conversion buffers are internal. Active from audio-start to audio-stop; `ended` marks
     * the stream complete so the glue can drain, then arm the amp off. */
    int playback_active;  /* 1 between audio-start and audio-stop */
    int playback_ended;   /* 1 after audio-stop (the ring still drains) */
    int playback_volume;  /* percent, the no-silence floor applied in wy_apply_volume */
    int playback_rate;    /* the stream's rate, from audio-start (the bridge's 22050) */
    wy_resampler playback_rs;
    int32_t *ring;        /* caller-provided; NULL on the host until configured */
    size_t ring_capacity; /* bytes */
    size_t ring_head;     /* read pointer, bytes consumed */
    size_t ring_tail;     /* write pointer, bytes appended */
    int16_t *pcm;         /* inbound chunk PCM (S16), up to WY_PAYLOAD_MAX */
    int16_t *mono;        /* resampled 16 kHz mono */
    int32_t *frames;      /* expanded stereo-32, ready for the ring */
    /* The config-plane seam (installed once by the glue, preserved across re-init, exactly
     * like the writer-lock fields). The session is only the transport: it decodes a
     * hermes.config.desired frame, passes its data JSON to this handler, and sends the
     * returned reported JSON as hermes.config.reported. It owns no config state of its own --
     * the config agent (owned by the glue, whose address is the first argument) owns that.
     * NULL until wired, where a config frame is drained and the link stays alive. */
    int (*config_handler)(void *config, const char *desired_json, char *report_out, size_t report_cap);
    void *config_user;
} wy_session;
size_t wy_session_size(void);
int wy_session_init(wy_session *session, const char *device_id, const char *boot_id);
/* Set the device identity (name / area / firmware_version / detection name) that the
 * registration, describe/info and wake sequences report. The glue calls it once with the values
 * it read from NVS and the loaded model's name (the detection name is the model name, which is
 * what makes the two satellite variants report the same wake word on the wire). NULL or empty
 * fields fall back to the historical defaults, and the values survive a per-link re-init. */
void wy_session_set_identity(wy_session *session, const wy_session_identity *identity);
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
/* Install the config-plane handler (the device glue wires in the config agent). The session
 * becomes the transport for the declarative configuration plane: a hermes.config.desired is
 * decoded, its data JSON handed to the handler, and the returned reported JSON sent as
 * hermes.config.reported. The handler's first argument is the config object it owns (the
 * glue's agent), which outlives every re-init. Install once; the field survives re-init. A
 * session with no handler installed drains config frames and stays alive (the pre-config
 * behavior), so a device that never wires the plane is not bricked by it. */
int wy_session_set_config(wy_session *session,
                          int (*handler)(void *config, const char *desired_json,
                                         char *report_out, size_t report_cap),
                          void *config);
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

/* Playback (bridge -> speaker). configure wires a caller-provided ring (bytes) and the volume
 * into the session before the link is driven; the device calls it once at startup (PSRAM ring).
 * The core converts each inbound audio-chunk -- resample to 16 kHz, apply the volume (no
 * silence floor), expand to the I2S TX's stereo 32-bit full-scale -- and appends the frames to
 * the ring, dropping the OLDEST on overflow (a late sample is unplayable; the link stays
 * alive). The playback task reads the ring at realtime. read() drains up to `cap` bytes of
 * I2S-ready frames from the ring head and advances it. active/ended/bytes report the stream
 * state. All accessors are safe on a NULL session. */
int wy_session_playback_configure(wy_session *session, int32_t *ring, size_t ring_capacity,
                                  int volume);
/* Change the live playback level for the next audio slice (the config plane's apply path). The
 * write is taken under the playback ring lock (the audio-chunk conversion reads the volume under
 * that lock); the value is stored raw and wy_apply_volume clamps it to the no-silence floor at
 * use, so the setter cannot silence the speaker. INVALID on a NULL session. */
int wy_session_set_playback_volume(wy_session *session, int volume);
/* The device's own local tone (the wake chime, D4): write n 16 kHz mono S16 samples into the
 * playback ring, the same speaker path as a bridge answer. No resample (the chime is rendered
 * at the board's 16 kHz), but the SAME volume (the config plane's level) and stereo-32 expand,
 * appended under the ring lock like the inbound audio-chunk path. INVALID on a NULL session or
 * pcm, or when no ring is configured (local audio has nowhere to go). The pump task's
 * amplifier policy (armed while the ring is not empty) carries it to the speaker. */
int wy_session_local_play(wy_session *session, const int16_t *mono, size_t n);
int wy_session_playback_active(const wy_session *session);
int wy_session_playback_ended(const wy_session *session);
size_t wy_session_playback_bytes(const wy_session *session);
size_t wy_session_playback_read(wy_session *session, int32_t *dst, size_t cap);

/* Stable, human-readable names for session state and result codes, used in reports. A code
 * this build does not know reports as "unknown" rather than being silently unnamed. */
const char *wy_session_state_name(int state);
const char *wy_result_name(int result);
#endif
