#include "gateway_task.h"
#include "gateway_supervisor.h"
#include "gateway_tls.h"
#include "gateway_socket.h"
#include "gateway_link.h"
#include "wifi_station.h"
#include "provisioning.h"
#include "device_identity.h"
#include "audio_capture.h"
#include "wyoming_session.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_heap_caps.h"
#include <string.h>

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

/* Called by the supervisor exactly when a session (re)reaches registered: the handshake regime
 * is over, the idle regime begins, and an idle read is the healthy state. Lifting the deadline
 * to "no deadline" is what makes the persistent read block until a real end of stream instead of
 * erroring at the register budget every time the link idles. */
static int gateway_task_on_registered(void *user) {
    (void)user;
    if (state.transport == PROVISION_TRANSPORT_TLS)
        gateway_tls_link_set_deadline(&state.tls_link, GATEWAY_SOCKET_NO_DEADLINE);
    else
        state.tcp_socket.deadline_us = GATEWAY_SOCKET_NO_DEADLINE;
    return 0;
}

/* Stop the microphone when the link goes down. The capture task and this task share the
 * transport's writer; a link-down that left the capture task pointing at a dead transport
 * would hand it a writer that is about to be reused by the next dial. */
static void gateway_task_on_link_down(void *user) {
    (void)user;
    audio_capture_stop();
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
    if (wy_session_state((wy_session *)session) == WY_SESSION_RUN_REQUESTED &&
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
    if (state_now == WY_SESSION_REGISTERED || state_now == WY_SESSION_REFUSED ||
        state_now == WY_SESSION_FAILED)
        audio_capture_stop();
}

/* Close the transport a link was established over. The radio is left alone: the next establish
 * re-associates it, so reconnecting a dropped bridge does not need to re-associate Wi-Fi. */
static void gateway_task_teardown(void *user, gw_link *link) {
    (void)user;
    if (!link || !link->user) return;
    if (state.transport == PROVISION_TRANSPORT_TLS) {
        gateway_tls_release((gateway_tls_link *)link->user);
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
            if (state_after == WY_SESSION_RUN_REQUESTED && state_before != WY_SESSION_RUN_REQUESTED)
                gateway_task_maybe_start_capture();
            else if (state_before == WY_SESSION_RUN_REQUESTED && state_after != WY_SESSION_RUN_REQUESTED)
                gateway_task_maybe_stop_capture();
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
        wifi_station_release();
    active = false;
    vTaskDelete(NULL);
}

int gateway_task_start(const char *host, unsigned port, int transport,
                       const char *ssid, const char *psk, char *report, size_t capacity) {
    if (active) return -1;  /* one gateway session at a time */
    if (!host || !host[0] || !ssid || !ssid[0]) return -1;

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
