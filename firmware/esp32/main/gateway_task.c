#include "gateway_task.h"
#include "led_status.h"
#include "gateway_supervisor.h"
#include "gateway_tls.h"
#include "gateway_socket.h"
#include "gateway_link.h"
#include "wifi_station.h"
#include "provisioning.h"
#include "device_identity.h"
#include "audio_capture.h"
#include "audio_playback.h"
#include "wyoming_audio.h"   /* WY_VOLUME_DEFAULT_PERCENT: the device's conservative default level */
#include "wyoming_session.h"
#include "wyoming_config.h"  /* the config-plane agent the session transports */
#include "cJSON.h"           /* the adapter parses the agent's serialized settings */
#include "nvs.h"             /* the hermes_cfg durable storage backend + inc4 wake staging */
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_heap_caps.h"
#include <string.h>
#include "esp_log.h"
#include "esp_app_desc.h"

/* The device half of "register on boot": a persistent gateway task that runs the supervisor
 * (item 1) for the life of the session, re-dialing -- and re-associating Wi-Fi -- whenever the
 * link goes away. It is deliberately single-network: the boot gate chooses which of the saved
 * networks to join, and this task holds that one network and retries it. Re-picking across several
 * networks on a later reconnect is a refinement, not the boot path. */

#define GATEWAY_TASK_STACK 10240
#define GATEWAY_TASK_PRIORITY 1
#define GATEWAY_DIAL_TIMEOUT_MS 8000
#define GATEWAY_REGISTER_BUDGET_MS 25000  /* the register exchange, the same budget the one-shot run uses */
#define GATEWAY_BACKOFF_BASE_US 2000000LL   /* 2 s: a bridge that just restarted is often back in seconds */
#define GATEWAY_BACKOFF_MAX_US 60000000LL   /* 60 s: capped so a satellite never gives up, but never hammers */
/* The playback ring, sized for the bridge's 2x send pace. Each slot is one int32 (one channel
 * sample); a stereo-32 frame is two slots. At 2x pace the device receives each second of an
 * answer in 0.5 s and plays it in 1 s, so the ring's lead grows to HALF the answer length at the
 * end of the send, then decays. A 30 s answer therefore needs ~15 s of hold. 16 s of stereo-32
 * at 16 kHz = 16 * 16000 * 2 = 512000 slots = 2 MiB of PSRAM (ample on the 8 MiB XIAO S3).
 * Overflow drops the OLDEST frame (a late sample is unplayable), never the link. */
#define GATEWAY_PLAYBACK_RING_FRAMES 512000

static struct {
    char host[PROVISION_HOST_MAX];
    unsigned port;
    int transport;
    char ssid[PROVISION_SSID_MAX + 1];
    char psk[PROVISION_PSK_MAX + 1];
    char device_id[65];
    char boot_id[129];
    char report[1024];
    gateway_tls_link tls_link;
    gateway_socket tcp_socket;
} state;

static gateway_supervisor supervisor;
static volatile bool active;
static portMUX_TYPE state_lock = portMUX_INITIALIZER_UNLOCKED;
/* The gateway's playback ring (PSRAM). A gateway restart frees the old session (the
 * supervisor does it on re-init) and thus the only pointer to the old ring, so the ring is
 * owned HERE, by the gateway, and freed here on the terminal stop and on a restart that
 * re-allocates. It is cleared only once the new pump is confirmed running (or the start has
 * failed) so the pump never points at a ring that has just been freed. */
static int32_t *playback_ring;

/* Establish one link: bring the radio up to the held network (fresh each attempt, which is also
 * how a dropped Wi-Fi link recovers), then dial the endpoint. For TLS the handshake -- and the
 * pin check -- happen inside gateway_tls_establish. The link comes up under a FINITE deadline,
 * not "no deadline": the register exchange is not yet the idle regime, and a bridge whose
 * per-connection handler is wedged while its kernel keeps answering must not be able to hold the
 * device in the handshake for ever -- the same rule the one-shot path lives under. The deadline
 * is lifted to "no deadline" by gateway_task_on_registered, once the session is actually
 * registered, when an idle read becomes the healthy state. On any failure the radio is left as
 * the sub-steps left it and -1 is returned, which makes the supervisor back off and re-try. */
static int gateway_task_establish(void *user, gw_link *link) {
    (void)user;
    char line[192];
    wifi_station_release();  /* clean slate: idempotent, and it recovers a dropped association */
    if (wifi_station_associate(state.ssid, state.psk, line, sizeof(line)) != 0) {
        return -1;  /* no radio; the associate failure path already stopped it. Back off and retry. */
    }
    if (state.transport == PROVISION_TRANSPORT_TLS) {
        if (gateway_tls_establish(state.host, state.port, &state.tls_link, state.report,
                                  sizeof(state.report)) != 0) {
            return -1;  /* radio stays up; the next establish refreshes it and re-dials */
        }
        /* The register exchange gets its FULL budget, measured AFTER the establish -- exactly
         * what the one-shot path does (gateway_tcp_run and gateway_tls_run both raise the run
         * deadline to the session budget only once the dial and handshake are done). Capturing
         * it before establish would hand a slow first-boot TLS bring-up (clock wait, dial and
         * handshake, each individually bounded but together able to exceed the budget) an
         * already-expired deadline, so the link would come up and its first read would error
         * before any register byte was exchanged. */
        gateway_tls_link_set_deadline(&state.tls_link,
                                      gateway_now_us() + (int64_t)GATEWAY_REGISTER_BUDGET_MS * 1000);
        link->user = &state.tls_link;
        link->read_fn = gateway_tls_link_read;
        link->write_fn = gateway_tls_link_write;
    } else {
        state.tcp_socket.descriptor = -1;
        if (gateway_socket_dial(state.host, state.port,
                                gateway_now_us() + (int64_t)GATEWAY_DIAL_TIMEOUT_MS * 1000,
                                &state.tcp_socket) != 0) {
            return -1;
        }
        state.tcp_socket.deadline_us =
            gateway_now_us() + (int64_t)GATEWAY_REGISTER_BUDGET_MS * 1000;
        link->user = &state.tcp_socket;
        link->read_fn = gateway_socket_read_persistent;
        link->write_fn = gateway_socket_write_persistent;
    }
    return 0;
}

/* Whether this boot has ever reached the host. Decides green-once versus silent-reconnect; see
 * docs/leds.md. Declared here because both the registration and the link-down hooks read it. */
static int led_have_connected;

/* Called by the supervisor exactly when a session (re)reaches registered: the handshake regime
 * is over, the idle regime begins, and an idle read is the healthy state. Lifting the deadline
 * to "no deadline" is what makes the persistent read block until a real end of stream instead of
 * erroring at the register budget every time the link idles. The bridge can also SPEAK from this
 * moment, so the I2S TX and the pump must exist before the first answer: the DMA is primed, and
 * there is no channel bring-up latency on the first chunk of a TTS answer. Idempotent, so a
 * re-registration after a link bounce (which stopped the pump in on_link_down) starts it again. */
static int gateway_task_on_registered(void *user) {
    (void)user;
    /* The green ready flash on the first connection of a boot; silent on any reconnect. */
    led_status_connected(!led_have_connected);
    led_have_connected = 1;
    if (state.transport == PROVISION_TRANSPORT_TLS)
        gateway_tls_link_set_deadline(&state.tls_link, GATEWAY_SOCKET_NO_DEADLINE);
    else
        state.tcp_socket.deadline_us = GATEWAY_SOCKET_NO_DEADLINE;
    /* The pump reports `played` when an answer finishes sounding, so bind it to the
     * link alongside the capture path. Refreshed every registration: a reconnect
     * hands back the same tasks with a new transport. */
    {
        gw_link link = gateway_supervisor_link(&supervisor);
        audio_playback_bind(gateway_supervisor_session(&supervisor), link.user,
                            link.write_fn);
    }
    audio_playback_start();
    return 0;
}

/* ---- Configuration plane device glue (increment 3) --------------------------- */

/* The config agent's lifetime matches the gateway session's: it is (re)created in
 * gateway_task_start and freed on the terminal path, exactly like the playback ring. It is a
 * SEPARATE allocation from the session so that its durable state -- the generation guard and the
 * last-applied desired -- outlives every per-reconnect session re-init (the supervisor re-inits
 * the SAME session buffer on a reconnect, which preserves the session's config_handler/config_user
 * fields and therefore this agent pointer). After a full reboot the agent is fresh (generation 0)
 * and its set_storage restore brings the guard back from NVS. */
static wy_config *config_agent;

/* The session's config handler (the C-side trampoline wy_session_set_config takes). On the
 * device this is a real function -- no ctypes double-marshal -- so it is a thin forward to the
 * agent, whose first argument is the agent itself. The session stays the transport; the agent
 * owns all the config logic. */
static int cfg_handler_trampoline(void *config, const char *desired_json,
                                  char *report_out, size_t report_cap) {
    return wy_config_step((wy_config *)config, desired_json, report_out, report_cap);
}

/* The agent's restore reads at most WY_CFG_LOAD_MAX (1024) bytes; the blob is well under that in
 * practice. The load buffer is sized to that cap (+NUL headroom). The restore runs on the MAIN
 * task, not the gateway task: gateway_task_start is called from provisioning_console_run (the
 * console loop under app_main, before xTaskCreate), and the whole restore is ~2.6 KB at its
 * deepest point (the blob buffer + the validate-into-locals), comfortably within the main
 * task's stack. */
#define CFG_BLOB_MAX 1024u

/* The agent's durable storage backend: the hermes_cfg NVS blob. save returns 0 only once the
 * state is committed (durable), which is what makes the agent's "persistence before acceptance"
 * real. load returns 0 with *outlen set when a blob exists, nonzero when there is none (first
 * boot) or it will not fit the caller's buffer -- both of which the agent treats as "no
 * persisted state" and degrades safely to first boot (the plan's torn-write recovery). */
static int cfg_nvs_save(void *user, const char *json, size_t len) {
    (void)user;
    nvs_handle_t handle;
    if (nvs_open("hermes_cfg", NVS_READWRITE, &handle) != ESP_OK) return -1;
    esp_err_t result = nvs_set_str(handle, "blob", json);
    if (result == ESP_OK) result = nvs_commit(handle);
    nvs_close(handle);
    return result == ESP_OK ? 0 : -1;
}

static int cfg_nvs_load(void *user, char *out, size_t cap, size_t *outlen) {
    (void)user;
    *outlen = 0;
    nvs_handle_t handle;
    esp_err_t opened = nvs_open("hermes_cfg", NVS_READONLY, &handle);
    if (opened == ESP_ERR_NVS_NOT_FOUND) return -1;   /* first boot: no state */
    if (opened != ESP_OK) return -1;
    /* Query the real stored size first (a NULL buffer returns the required size without
     * copying). For a stored string that size INCLUDES the NUL terminator, so the JSON content
     * is stored-1 bytes. Refuse a blob whose content will not fit the caller's cap: truncating
     * it would hand the agent a JSON fragment that fails its schema check and discard a GOOD
     * persisted state to first boot -- a false corruption. */
    size_t stored = 0;
    esp_err_t query = nvs_get_str(handle, "blob", NULL, &stored);
    if (query != ESP_OK) { nvs_close(handle); return -1; }
    if (stored == 0) { nvs_close(handle); return -1; }   /* empty string: treat as no state */
    size_t content = stored - 1;                          /* exclude the NUL */
    if (content > cap) { nvs_close(handle); return -1; }  /* will not fit the caller's cap */
    char tmp[CFG_BLOB_MAX + 1];
    if (stored > sizeof(tmp)) { nvs_close(handle); return -1; }
    size_t tmp_len = sizeof(tmp);
    esp_err_t result = nvs_get_str(handle, "blob", tmp, &tmp_len);
    nvs_close(handle);
    if (result != ESP_OK) return -1;
    memcpy(out, tmp, content);
    out[content] = '\0';
    *outlen = content;
    return 0;
}

/* Best-effort staging of one wake setting to NVS for the wake detector (the next increment).
 * inc4 reads these at startup. The keys live in hermes_cfg next to the state blob; a staging
 * failure is not fatal to the load-bearing apply (the live volume), so the error is swallowed
 * here and only the volume's success drives the report. */
static void cfg_stage_wake(const char *key, const char *value) {
    if (!value) return;
    nvs_handle_t handle;
    if (nvs_open("hermes_cfg", NVS_READWRITE, &handle) != ESP_OK) return;
    esp_err_t result = nvs_set_str(handle, key, value);
    if (result == ESP_OK) result = nvs_commit(handle);
    nvs_close(handle);
}

/* The apply adapter: the device's real "what can I actually apply" half. It sets the LIVE
 * playback volume (the one setting with a live path -- the next audio slice picks it up), stages
 * wake settings to NVS for the next increment, applies led_brightness to the ring, and is HONEST
 * about the rest: mic_gain / locale / update_channel have no live adapter, so they are never put
 * in `actual`. The health reported is degraded whenever any desired key is not in `actual` (which
 * is always, on this device, because the bridge sends the full 7-key profile). Only what is
 * really applied goes in `actual` -- nothing is claimed that was not. */
static int cfg_apply(void *user, const char *settings_json,
                     char *out_actual, size_t actual_cap,
                     int *out_health, char *out_error, size_t err_cap) {
    (void)err_cap;
    out_error[0] = '\0';
    if (!user || !settings_json) { *out_health = WY_CFG_HEALTH_ERROR; return WY_INVALID; }
    wy_session *session = (wy_session *)user;
    cJSON *settings = cJSON_Parse(settings_json);
    if (!settings || !cJSON_IsObject(settings)) {
        /* A non-object (a malformed or mis-shape-parsed JSON) is an error, not an apply; the
         * parsed tree (if any) is freed here -- it is unreachable through the agent (which
         * serializes a validated object), but a future caller must not leak it. */
        cJSON_Delete(settings);
        *out_health = WY_CFG_HEALTH_ERROR;
        return WY_INVALID;
    }
    cJSON *actual = cJSON_CreateObject();
    if (!actual) { cJSON_Delete(settings); *out_health = WY_CFG_HEALTH_ERROR; return WY_INVALID; }
    int degraded = 0;
    /* playback_volume: the live, immediately-applied setting. */
    const cJSON *volume = cJSON_GetObjectItemCaseSensitive(settings, "playback_volume");
    if (volume) {
        if (cJSON_IsNumber(volume) && !cJSON_IsBool(volume) &&
            volume->valuedouble >= 0 && volume->valuedouble <= 100 &&
            volume->valuedouble == (double)(long)volume->valuedouble &&
            wy_session_set_playback_volume(session, (int)volume->valuedouble) == WY_OK) {
            cJSON_AddNumberToObject(actual, "playback_volume", volume->valuedouble);
        } else {
            degraded = 1;   /* a value the live path refused: not applied */
        }
    }
    /* wake_threshold / wake_word: staged to NVS for the next increment's detector. They are NOT
     * live-applied (there is no detector yet), so they are never in `actual` -- they land in the
     * agent's pending set. Staging is a side effect; its failure does not change the report. */
    const cJSON *threshold = cJSON_GetObjectItemCaseSensitive(settings, "wake_threshold");
    if (threshold && cJSON_IsNumber(threshold) && !cJSON_IsBool(threshold)) {
        char buf[24];
        snprintf(buf, sizeof(buf), "%.6f", threshold->valuedouble);
        cfg_stage_wake("wake_threshold", buf);
    }
    const cJSON *word = cJSON_GetObjectItemCaseSensitive(settings, "wake_word");
    if (word && cJSON_IsString(word) && word->valuestring)
        cfg_stage_wake("wake_word", word->valuestring);
    /* led_brightness: NOW a live adapter. It was staged as "no adapter" before the ring had a
     * driver, which left every device pinned at full regardless of what the fleet asked for. The
     * fleet's scale is 1-31 (docs/configuration-plane.md); the ring's own scale is 0-255 and the
     * core does the scaling. Only a value the device actually took is reported in `actual`. */
    const cJSON *brightness = cJSON_GetObjectItemCaseSensitive(settings, "led_brightness");
    if (brightness) {
        if (cJSON_IsNumber(brightness) && !cJSON_IsBool(brightness) &&
            brightness->valuedouble >= 1 && brightness->valuedouble <= 31 &&
            brightness->valuedouble == (double)(long)brightness->valuedouble &&
            led_status_set_brightness((unsigned)brightness->valuedouble) == 0) {
            cJSON_AddNumberToObject(actual, "led_brightness", brightness->valuedouble);
        } else {
            degraded = 1;
        }
    }
    /* Any desired key that did not land in `actual` makes this a partial apply. */
    if (!cJSON_Compare(actual, settings, 1)) degraded = 1;
    *out_health = degraded ? WY_CFG_HEALTH_DEGRADED : WY_CFG_HEALTH_HEALTHY;
    /* Serialize `actual` into the out buffer (NUL-terminated, capped). */
    char *text = cJSON_PrintUnformatted(actual);
    if (text) {
        size_t len = strlen(text);
        if (len + 1 <= actual_cap) memcpy(out_actual, text, len + 1);
        else if (actual_cap >= 2) memcpy(out_actual, "{}", 2);
        free(text);
    } else if (actual_cap >= 2) {
        memcpy(out_actual, "{}", 2);
    }
    cJSON_Delete(actual);
    cJSON_Delete(settings);
    return WY_OK;
}

/* Stop the microphone when the link goes down. The capture task and this task share the
 * transport's writer; a link-down that left the capture task pointing at a dead transport
 * would hand it a writer that is about to be reused by the next dial. The speaker is stopped
 * too (idempotent: the TLS teardown calls this hook twice, once directly and once from the
 * supervisor's link_down).
 *
 * The ORDER is load-bearing, not incidental: the playback TX is created first (at
 * registration) and is the full-duplex clock MASTER; the capture RX is created later (at
 * run-satellite) and is demoted to the slave that samples on the TX's looped-back BCLK/WS
 * (see audio_playback.c). The microphone's clock is therefore the speaker's clock, so the
 * slave (capture) must be torn down BEFORE the master (playback) -- stop the TX first and the
 * mic loses its clock for the rest of the link. Reordering these two calls is a live-mic
 * regression, not a refactor. */
static void gateway_task_on_link_down(void *user) {
    /* WHY the link went down is the single most useful line in a field log: a reader error, a
     * write error and a deliberate close all look identical from the bridge ("Connection reset
     * by peer"). This path is reached at the exact moment a voice reply should arrive, and until
     * now it has been silent. */
    ESP_LOGW("gwlink", "on_link_down: the gateway link is going away (user=%p)", user);
    /* NOT led_have_connected = 0. This latch is "has this MCU boot ever reached the host", and a
     * disconnect is not a reboot. Clearing it here made every reconnect flash green, which turns
     * the once-per-boot handshake into a routine blink people learn to ignore. */
    led_status_disconnected();   /* red only if it stays away: see docs/leds.md */
    (void)user;
    audio_capture_stop();
    audio_playback_stop();
}

/* Start the microphone the moment the bridge's run-satellite arms the session, and stop it
 * when the session leaves the armed state (pause, or the link going down). The capture task
 * itself reads the I2S stream continuously while running and drops its audio unless the
 * session says the stream is open, so starting it at "armed" rather than at "streaming" is
 * what makes the first utterance start clean: the DMA is already primed, and there is no
 * half-second of I2S bring-up between the wake and the first chunk. */
static void gateway_task_maybe_start_capture(void) {
    void *session = gateway_supervisor_session(&supervisor);
    if (!session) return;
    /* Listen from REGISTRATION, not only from run-satellite.
     *
     * The capture task feeds wake_detector_feed on every frame and lets the session decide
     * whether a frame is sent onward (wy_session_send_audio drops it while the stream is
     * closed), so running capture continuously is exactly what a voice satellite needs and
     * costs the bridge nothing while idle. Arming only in RUN_REQUESTED left the microphone
     * OFF whenever no conversation was in progress -- so "hey hermes" could never be heard
     * except mid-session, which is precisely why the wake word did nothing when spoken cold.
     * The DMA also stays primed, so the first utterance starts clean. */
    int state_now = wy_session_state((wy_session *)session);
    if ((state_now == WY_SESSION_RUN_REQUESTED || state_now == WY_SESSION_REGISTERED) &&
        !audio_capture_running()) {
        gw_link link = gateway_supervisor_link(&supervisor);
        if (link.user && link.write_fn)
            audio_capture_start(session, link.user, link.write_fn);
    }
}

static void gateway_task_maybe_stop_capture(void) {
    if (!audio_capture_running()) return;
    void *session = gateway_supervisor_session(&supervisor);
    if (!session) return;
    int state_now = wy_session_state((wy_session *)session);
    /* Registered is NOT a reason to stop: that is the idle listening state now. Only a
     * refused or failed session releases the microphone. */
    if (state_now == WY_SESSION_REFUSED || state_now == WY_SESSION_FAILED)
        audio_capture_stop();
}

/* Close the transport a link was established over. The radio is left alone: the next establish
 * re-associates it, so reconnecting a dropped bridge does not need to re-associate Wi-Fi.
 *
 * The TLS teardown is three phases, and the order is the safety argument. First the socket
 * closes: an in-flight capture write hits EBADF and unwinds (the persistent write retries only
 * EAGAIN/EINTR/EWOULDBLOCK, never EBADF), and with the session's streaming flag already
 * cleared by link_down no new chunk starts. Then the capture task is stopped and joined (the
 * on_link_down hook, run here rather than by the supervisor's later call so the join happens
 * before the free, not after). Only then are the mbedTLS contexts freed: the in-flight write
 * still holds a reference to them through the socket callbacks, and the writer-idle wait is
 * the gate between the stop and the free. Freeing before the stop is a use-after-free, and a
 * close_notify in between would wedge on a stuck peer, because the persistent deadline is
 * "no deadline" and the alert would route through the deadline-blind persistent write. The
 * one-shot console path keeps close_notify, where its finite deadline bounds it for real. */
static void gateway_task_teardown(void *user, gw_link *link) {
    (void)user;
    if (!link || !link->user) return;
    if (state.transport == PROVISION_TRANSPORT_TLS) {
        gateway_tls_link *tls = (gateway_tls_link *)link->user;
        gateway_tls_close_socket(tls);
        gateway_task_on_link_down(user);   /* stop and join the capture task (idempotent) */
        if (audio_capture_wait_writer_idle())
            gateway_tls_free(tls);
        /* If the wait failed, the contexts stay set on purpose: the next establish frees them
         * first (before its memset), at a moment when no capture task can be running. */
    } else {
        gateway_socket_close((gateway_socket *)link->user);
    }
}

/* The supervisor is step-driven and never sleeps on its own; between WAIT steps the task sleeps
 * the reported backoff. A satellite keeps retrying (max_attempts 0), so the only terminal reason
 * is a bridge that refuses the registration, which is the correct time to stop. */
static void gateway_task_fn(void *argument) {
    (void)argument;
    int result;
    for (;;) {
        void *session = gateway_supervisor_session(&supervisor);
        int state_before = session ? wy_session_state((wy_session *)session) : WY_SESSION_FAILED;
        result = gateway_supervisor_step(&supervisor);
        if (result == GW_OK) {
            int state_after = wy_session_state((wy_session *)session);
            if (state_before == WY_SESSION_RUN_REQUESTED && state_after != WY_SESSION_RUN_REQUESTED)
                gateway_task_maybe_stop_capture();
            /* Restart is checked on every OK step, not just the armed transition: a capture
             * task that self-exited on an I2S error leaves the session armed (run-satellite
             * does not change state), so a repeat run-satellite fires no transition, and the
             * armed hook would never run again -- the mic dead until a pause or a link bounce.
             * The guard inside maybe_start_capture is armed-and-not-running, so this is
             * idempotent, and it is what gives a failed first start (channel still in use) its
             * retry. */
            if (state_after == WY_SESSION_RUN_REQUESTED)
                gateway_task_maybe_start_capture();
            continue;
        }
        if (result == GW_WAIT) {
            int64_t delay = gateway_supervisor_delay_us(&supervisor);
            vTaskDelay(pdMS_TO_TICKS(delay > 0 ? delay / 1000 : 1));
            continue;
        }
        /* GW_FAILED (only reachable with a finite attempt budget) or GW_REFUSED (the bridge said no). */
        gateway_report_append(state.report, sizeof(state.report),
                              (result == GW_REFUSED) ? "gateway=stopped reason=refused"
                                                     : "gateway=stopped reason=failed");
        break;
        }
        audio_capture_stop();
        audio_playback_stop();   /* the pump is joined and the amp disarmed */
        if (playback_ring) {     /* the pump no longer reads it: release the ring */
            heap_caps_free(playback_ring);
            playback_ring = NULL;
        }
        /* The config agent outlives every per-link re-init but not the gateway session: free it
         * (it owns its desired/actual cJSON) before the task exits, so a restart re-creates it
         * fresh (generation 0) and its set_storage restore brings the guard back from NVS. The
         * session it pointed at (apply's user) is freed by the supervisor on the next start, so
         * this free precedes that one -- no dangling apply. */
        if (config_agent) {
            wy_config_destroy(config_agent);
            free(config_agent);
            config_agent = NULL;
        }
        wifi_station_release();
    active = false;
    vTaskDelete(NULL);
}

/* Read one string from the hermes_cfg namespace, or empty when it was never staged. The Pi
 * reads its identity from env (VOICE_DEVICE_NAME / VOICE_DEVICE_AREA / VOICE_FIRMWARE_VERSION);
 * the ESP32 reads the same three from NVS so the two variants report the same fields on the
 * wire. A staging failure or an absent key is an empty value, never a guess. */
static void nvs_read_str(const char *key, char *out, size_t cap) {
    if (!out || cap == 0) return;
    out[0] = 0;
    nvs_handle_t handle;
    if (nvs_open("hermes_cfg", NVS_READONLY, &handle) != ESP_OK) return;
    size_t len = cap;
    if (nvs_get_str(handle, key, out, &len) != ESP_OK) out[0] = 0;
    nvs_close(handle);
}

/* Install the device identity on the session: the name / area / firmware_version the bridge
 * and the house know it by, and the detection name (the LOADED MODEL's name, which is what
 * makes the two satellite variants report the same wake word -- the Pi reports whatever model
 * it loaded). Empty values fall back to the session's own defaults. */
static void gateway_task_set_identity(wy_session *session) {
    char name[65], area[65], firmware_version[33], wake_word[65];
    nvs_read_str("device_name", name, sizeof(name));
    nvs_read_str("device_area", area, sizeof(area));
    /* Identify the executing image, not a saved user label or a copied bundle. The
     * descriptor hash is populated by the image builder from the linked ELF. */
    const esp_app_desc_t *app = esp_app_get_description();
    char elf_hash[65];
    for (size_t i = 0; i < sizeof(app->app_elf_sha256); ++i)
        snprintf(elf_hash + i * 2, 3, "%02x", app->app_elf_sha256[i]);
    ESP_LOGI("fwbuild", "running ELF sha256=%s", elf_hash);
    snprintf(firmware_version, sizeof(firmware_version), "esp32-%.24s", elf_hash);
    /* The detection name IS the model name: wake_word is the model selector (the model file's
     * stem, e.g. hey_hermes), and the detection event reports that same name. */
    nvs_read_str("wake_word", wake_word, sizeof(wake_word));
    wy_session_identity identity = { name, area, firmware_version, wake_word };
    wy_session_set_identity(session, &identity);
}

int gateway_task_start(const char *host, unsigned port, int transport,
                       const char *ssid, const char *psk, char *report, size_t capacity) {
    if (active) return -1;  /* one gateway session at a time */
    if (!host || !host[0] || !ssid || !ssid[0]) return -1;

    /* The ring says "starting up" before anything else can fail, and keeps saying it until the
     * host connects. A device with power and no network should be visibly alive, not dark. */
    if (led_status_start() == 0) (void)led_status_boot();
    /* The latch is NOT reset here either: restarting the gateway session is not a reboot, and the
     * startup look must not replay for it. led_status_boot() itself is once-per-boot. */

    if (device_identity(state.device_id, sizeof(state.device_id),
                        state.boot_id, sizeof(state.boot_id)) != 0) return -1;

    memset(&state.report, 0, sizeof(state.report));
    snprintf(state.host, sizeof(state.host), "%s", host);
    state.port = port;
    state.transport = transport;
    snprintf(state.ssid, sizeof(state.ssid), "%s", ssid);
    /* A NULL psk means "nothing to join with": the field is cleared, not left holding the
     * previous session's key, or a new SSID would be associated with the old credential. */
    if (psk)
        snprintf(state.psk, sizeof(state.psk), "%s", psk);
    else
        state.psk[0] = 0;

    if (gateway_supervisor_init(&supervisor, gateway_now_us, gateway_task_establish,
                                gateway_task_teardown, &state, state.device_id, state.boot_id,
                                GATEWAY_BACKOFF_BASE_US, GATEWAY_BACKOFF_MAX_US, 0,
                                state.report, sizeof(state.report),
                                gateway_task_on_registered, gateway_task_on_link_down) != 0) {
        return -1;
    }

    /* The identity (name / area / firmware_version / detection name) is installed-once config
     * the session reports in its registration, describe/info and wake sequences. It is set
     * here, once, right after the session exists; it survives the supervisor's per-link
     * re-init exactly like the writer-lock callbacks and the playback ring. */
    gateway_task_set_identity((wy_session *)gateway_supervisor_session(&supervisor));

    /* The playback ring is a device-lifetime PSRAM buffer: the bridge's answers convert into it
     * and the pump reads it at realtime. It is owned here (the file-static playback_ring), not
     * in the session -- a gateway restart frees the old session (the supervisor does it on
     * re-init), and the old session's ring pointer is the ONLY reference to the old ring, so
     * the gateway must free the old ring before it allocates the next one, or every restart
     * leaks 2 MiB (and after ~3 restarts the 8 MiB PSRAM is exhausted). The old pump is
     * already stopped by the previous run's terminal path, so the old ring has no reader. The
     * volume is the device's conservative default; the live test calibrates it, and
     * increment 3's configuration plane can later override it per the bridge's volume command.
     * A failed allocation is not fatal to the link: playback simply has no ring to fill (the
     * session treats a NULL ring as "drain but don't keep"), so the satellite still registers,
     * listens, and streams the mic -- it just does not yet speak. */
    void *session = gateway_supervisor_session(&supervisor);
    /* Free the previous run's ring BEFORE allocating the next one: the old pump is already
     * stopped (the previous run's terminal path joined it), so the old ring has no reader, and
     * freeing first keeps the peak at one ring (2 MiB) instead of two (4 MiB) -- a start whose
     * allocation would fail at 4 MiB can still succeed at 2 MiB. */
    if (playback_ring) {
        heap_caps_free(playback_ring);
        playback_ring = NULL;
    }
    int32_t *ring = heap_caps_calloc(GATEWAY_PLAYBACK_RING_FRAMES, sizeof(int32_t),
                                     MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (ring) {
        wy_session_playback_configure((wy_session *)session, ring,
                                      GATEWAY_PLAYBACK_RING_FRAMES * sizeof(int32_t),
                                      WY_VOLUME_DEFAULT_PERCENT);
        playback_ring = ring;
        ESP_LOGI("plb_ring", "playback ring ready: %u frames", (unsigned)GATEWAY_PLAYBACK_RING_FRAMES);
    } else {
        ESP_LOGW("plb_ring", "playback ring allocation FAILED (%u frames)", (unsigned)GATEWAY_PLAYBACK_RING_FRAMES);
        gateway_report_append(state.report, sizeof(state.report), "playback=unavailable reason=psram");
    }
    /* Point the pump at THIS session unconditionally, not only when the ring allocated: on a
     * restart the session the pump held before was just freed by the supervisor's re-init, and
     * a start whose ring allocation failed must not leave the pump reading that freed block
     * (a use-after-free). Every accessor is safe on a NULL ring. */
    audio_playback_set_session(session);

    /* ---- Configuration plane: wire the agent into the session (installed once, preserved
     * across every per-link re-init by wy_session_init). The agent is a separate, app-lifetime
     * allocation: its durable state (the generation guard) outlives the supervisor's per-reconnect
     * session re-init, and set_storage's restore is what brings that guard back from the hermes_cfg
     * NVS blob after a reboot. The apply adapter's user is the session (it sets the live volume);
     * the handler trampoline forwards the session's desired frames to wy_config_step. A failed
     * agent allocation is NOT fatal: the session keeps draining config frames and the link stays
     * alive (a device that cannot wire the plane is not bricked by it), and the report names it. */
    if (config_agent) {  /* a restart: the previous run's terminal path should have freed it,
                             but never leak a whole agent across a re-start. */
        wy_config_destroy(config_agent);
        free(config_agent);
        config_agent = NULL;
    }
    wy_config *agent = calloc(1, wy_config_size());
    if (agent && wy_config_init(agent, state.device_id) == WY_OK) {
        wy_config_set_storage(agent, cfg_nvs_save, cfg_nvs_load, NULL);  /* restores the guard from NVS */
        wy_config_set_apply(agent, cfg_apply, session);
        wy_session_set_config((wy_session *)session, cfg_handler_trampoline, agent);
        config_agent = agent;
    } else {
        gateway_report_append(state.report, sizeof(state.report), "config=unavailable reason=alloc");
        free(agent);
    }

    TaskHandle_t handle = NULL;
    if (xTaskCreate(gateway_task_fn, "gw_task", GATEWAY_TASK_STACK, NULL,
                    GATEWAY_TASK_PRIORITY, &handle) != pdPASS || handle == NULL)
        return -1;
    active = true;
    if (report && capacity > 0)
        gateway_report_append(report, capacity, "gateway=started");
    return 0;
}

bool gateway_task_active(void) { return active; }

void gateway_task_report(char *out, size_t capacity) {
    if (!out || capacity == 0) return;
    taskENTER_CRITICAL(&state_lock);
    /* The report is a single-writer (the gateway task) / single-reader (the console)
     * diagnostic buffer, and it grows for the life of the session -- so the query's buffer
     * is usually smaller than the report, and the answer must be anchored at the END, not
     * the start: a prefix copy would show the oldest lines, not the newest. Take the newest
     * capacity-1 bytes and advance to the first complete line within that window (a line is
     * only complete once its newline is down, so a concurrent append can only ever add
     * lines after the copy point, never tear one inside it), then drop anything after the
     * last newline in the copy (a partial tail line, its newline not down yet). The
     * critical section is a best-effort single-core guard, not what makes the read
     * well-formed. */
    size_t length = strnlen(state.report, sizeof(state.report));
    size_t start = 0;
    if (length + 1 > capacity) {
        start = length + 1 - capacity;  /* the oldest byte that could fit */
        while (start < length && state.report[start] != '\n') ++start;
    }
    size_t used = length - start;
    while (used > 0 && state.report[start + used - 1] != '\n') --used;
    memcpy(out, state.report + start, used);
    out[used] = 0;
    taskEXIT_CRITICAL(&state_lock);
}