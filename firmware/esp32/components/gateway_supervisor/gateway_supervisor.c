#include "gateway_supervisor.h"
#include "wyoming_session.h"
#include "report_tail.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* One wy_session_step per registration attempt may make several reads (describe, then the
 * register.ok). The drive is capped so a bridge that accepts the connection but never completes
 * the handshake cannot spin the device; the cap counts iterations, so it is a second, independent
 * guard on top of the link's own deadline, which is finite until on_registered lifts it -- the
 * first guard against a peer that blocks the read itself. */
#define GW_REGISTER_DRIVE_CAP 64

static void append(gateway_supervisor *sup, const char *line) {
    if (!sup->report || sup->report_capacity == 0) return;
    /* The report rolls: once it fills, the oldest complete lines are evicted so the newest
     * lines are what the console's GATEWAY_STATUS keeps reading (report_tail_append's contract).
     * A frozen, full buffer would hand the soak the same stale last line for the rest of the
     * session, and judge a healthy, flapping link by data it stopped updating. */
    report_tail_append(sup->report, sup->report_capacity, line);
}

/* Close the current transport (if any) and drop the session's live state. The session buffer is
 * reused on the next establish; it is not freed here, because the supervisor owns it for its
 * whole life (the device's supervisor is app-lifetime). */
static void link_down(gateway_supervisor *sup) {
    if (sup->session) {
        /* Mark the session dead so a stray step cannot advance it. */
        ((wy_session *)sup->session)->state = WY_SESSION_FAILED;
        sup->registered = 0;
    }
    if (sup->link.read_fn) {
        sup->teardown(sup->user, &sup->link);
    }
    sup->link.user = NULL;
    sup->link.read_fn = NULL;
    sup->link.write_fn = NULL;
    /* After the transport is closed, so a hook that touches the link sees it dead. */
    if (sup->on_link_down) sup->on_link_down(sup->user);
}

/* Compute and record the next backoff. Returns 1 if the attempt budget is exhausted (terminal),
 * 0 otherwise. The caller sets the state accordingly. */
static int schedule_backoff(gateway_supervisor *sup) {
    if (sup->max_attempts > 0 && sup->attempts >= sup->max_attempts) return 1;
    int64_t delay = sup->backoff_base_us;
    for (int i = 1; i < sup->consecutive_failures && delay < sup->backoff_max_us; ++i) {
        delay *= 2;
        if (delay > sup->backoff_max_us) delay = sup->backoff_max_us;
    }
    if (sup->backoff_max_us > 0 && delay > sup->backoff_max_us) delay = sup->backoff_max_us;
    if (delay < 0) delay = sup->backoff_max_us; /* overflow guard */
    sup->last_backoff_us = delay;
    sup->resume_at_us = sup->now_us() + delay;
    sup->state = GW_ST_BACKING_OFF;
    return 0;
}

size_t gateway_supervisor_size(void) { return sizeof(gateway_supervisor); }

int gateway_supervisor_init(gateway_supervisor *sup, int64_t (*now_us)(void),
                            int (*establish)(void *, gw_link *),
                            void (*teardown)(void *, gw_link *), void *user,
                            const char *device_id, const char *boot_id,
                            int64_t backoff_base_us, int64_t backoff_max_us, int max_attempts,
                            char *report, size_t report_capacity, int (*on_registered)(void *),
                            void (*on_link_down)(void *)) {
    if (!sup || !now_us || !establish || !teardown) return -1;
    if (!device_id || !device_id[0] || !boot_id || !boot_id[0]) return -1;
    if (strlen(device_id) >= sizeof(sup->device_id) || strlen(boot_id) >= sizeof(sup->boot_id))
        return -1;
    if (report && report_capacity == 0) return -1;
    /* The device re-initialises its file-static supervisor on every gateway start (after a bridge
     * refusal ends the previous task), so an already-initialised supervisor must release the
     * session it owns before the fields are overwritten; otherwise every start leaks a whole
     * wy_session on a device that runs for the life of the machine. A fresh or previously-failed
     * supervisor has session == NULL, and freeing that is a no-op. */
    free(sup->session);
    memset(sup, 0, sizeof(*sup));
    sup->now_us = now_us;
    sup->establish = establish;
    sup->teardown = teardown;
    sup->user = user;
    strcpy(sup->device_id, device_id);
    strcpy(sup->boot_id, boot_id);
    sup->backoff_base_us = backoff_base_us < 0 ? 0 : backoff_base_us;
    sup->backoff_max_us = backoff_max_us < 0 ? 0 : backoff_max_us;
    sup->max_attempts = max_attempts;
    sup->report = report;
    sup->report_capacity = report_capacity;
    sup->on_registered = on_registered;
    sup->on_link_down = on_link_down;
    sup->state = GW_ST_IDLE;
    /* The session buffer is owned by the supervisor for its whole life; allocate it once. */
    sup->session_size = wy_session_size();
    sup->session = malloc(sup->session_size ? sup->session_size : 1u);
    if (!sup->session) { sup->session_size = 0; return -1; }
    return 0;
}

int64_t gateway_supervisor_delay_us(const gateway_supervisor *sup) {
    if (!sup || sup->state != GW_ST_BACKING_OFF) return 0;
    int64_t left = sup->resume_at_us - sup->now_us();
    return left > 0 ? left : 0;
}

gw_link gateway_supervisor_link(const gateway_supervisor *sup) {
    gw_link none = {0};
    return sup ? sup->link : none;
}

void *gateway_supervisor_session(const gateway_supervisor *sup) {
    return sup ? sup->session : NULL;
}

int gateway_supervisor_registered(const gateway_supervisor *sup) { return sup ? sup->registered : 0; }
int gateway_supervisor_events(const gateway_supervisor *sup) { return sup ? sup->events : 0; }
int gateway_supervisor_attempts(const gateway_supervisor *sup) { return sup ? sup->attempts : 0; }
int gateway_supervisor_reconnects(const gateway_supervisor *sup) { return sup ? sup->reconnects : 0; }
int gateway_supervisor_refused(const gateway_supervisor *sup) { return sup ? sup->refused : 0; }
int64_t gateway_supervisor_last_backoff_us(const gateway_supervisor *sup) {
    return sup ? sup->last_backoff_us : 0;
}
int gateway_supervisor_state(const gateway_supervisor *sup) { return sup ? sup->state : GW_ST_IDLE; }

const char *gateway_supervisor_reason_name(int result) {
    switch (result) {
        case GW_OK: return "ok";
        case GW_WAIT: return "wait";
        case GW_FAILED: return "failed";
        case GW_REFUSED: return "refused";
        default: return "no_transport";
    }
}

/* Drive a freshly-established transport to a registered session. Returns:
 *  0  -> registered (state set to PUMPING by the caller)
 *  1  -> the bridge refused; terminal (GW_REFUSED)
 *  2  -> the link ended (I/O or a rejected frame); reconnect path
 */
static int drive_to_registered(gateway_supervisor *sup) {
    wy_session *session = (wy_session *)sup->session;
    for (int i = 0; i < GW_REGISTER_DRIVE_CAP; ++i) {
        int result = wy_session_step(session, sup->link.read_fn, sup->link.write_fn,
                                     sup->link.user);
        if (result == WY_OK) {
            if (wy_session_state(session) == WY_SESSION_REGISTERED) return 0;
            continue;
        }
        if (result == WY_REFUSED) return 1;
        /* WY_IO / WY_INVALID / WY_LIMIT: the transport or the protocol ended this attempt. */
        return 2;
    }
    /* The bridge accepted the link but the handshake never completed within the cap/budget. */
    return 2;
}

int gateway_supervisor_step(gateway_supervisor *sup) {
    if (!sup || !sup->session) return GW_FAILED;
    wy_session *session = (wy_session *)sup->session;

    switch (sup->state) {
    case GW_ST_BACKING_OFF:
        if (sup->now_us() >= sup->resume_at_us) {
            sup->state = GW_ST_IDLE;
            append(sup, "backoff=done");
            return GW_OK;
        }
        return GW_WAIT;

    case GW_ST_IDLE: {
        if (sup->max_attempts > 0 && sup->attempts >= sup->max_attempts) {
            /* Re-entry past a terminal stop (the caller stepped again after GW_FAILED). The
             * line names the actual condition -- the attempt budget, not a missing transport --
             * and carries the count, the way every other line in this report does. */
            char line[96];
            snprintf(line, sizeof(line), "supervisor=failed reason=attempt_budget attempts=%d",
                     sup->attempts);
            append(sup, line);
            return GW_FAILED;
        }
        sup->attempts += 1;
        gw_link link = {0};
        int established = sup->establish(sup->user, &link);
        if (established != 0) {
            /* establish may have opened a descriptor before failing; let it close it. */
            if (link.read_fn) sup->teardown(sup->user, &link);
            sup->consecutive_failures += 1;
            char line[96];
            snprintf(line, sizeof(line), "establish=failed attempt=%d", sup->attempts);
            append(sup, line);
            if (schedule_backoff(sup)) return GW_FAILED;
            snprintf(line, sizeof(line), "backoff=%lld us", (long long)sup->last_backoff_us);
            append(sup, line);
            return GW_WAIT;
        }
        sup->link = link;
        if (wy_session_init(session, sup->device_id, sup->boot_id) != WY_OK) {
            link_down(sup);
            sup->consecutive_failures += 1;
            append(sup, "session=failed reason=init");
            if (schedule_backoff(sup)) return GW_FAILED;
            return GW_WAIT;
        }
        int outcome = drive_to_registered(sup);
        if (outcome == 1) {
            sup->refused = 1;
            link_down(sup);
            append(sup, "registration=refused");
            return GW_REFUSED;
        }
        if (outcome == 2) {
            link_down(sup);
            sup->consecutive_failures += 1;
            append(sup, "registration=link_ended");
            if (schedule_backoff(sup)) return GW_FAILED;
            char line[96];
            snprintf(line, sizeof(line), "backoff=%lld us", (long long)sup->last_backoff_us);
            append(sup, line);
            return GW_WAIT;
        }
        /* Registered. A reconnect is any registration after the first, measured against the
         * lifetime flag rather than the current-link one, because link_down has just cleared it. */
        if (sup->ever_registered) {
            sup->reconnects += 1;
            char line[96];
            snprintf(line, sizeof(line), "reconnect=registered reconnects=%d", sup->reconnects);
            append(sup, line);
        }
        /* The handshake is over and the link is idle-healthy: hand the transport to the regime
         * where an idle read is the normal state. A failing hook leaves the link on its finite
         * deadline, which still bounds an idle link to a re-dial -- a slower, but safe, posture. */
        if (sup->on_registered && sup->on_registered(sup->user) != 0)
            append(sup, "deadline=register_hook_failed");
        sup->registered = 1;
        sup->ever_registered = 1;
        sup->consecutive_failures = 0;
        sup->state = GW_ST_PUMPING;
        append(sup, "session=registered");
        return GW_OK;
    }

    case GW_ST_PUMPING: {
        int result = wy_session_step(session, sup->link.read_fn, sup->link.write_fn,
                                     sup->link.user);
        if (result == WY_OK) {
            /* An event was pumped: a run/pause the bridge requested, a ping it answered, or a
             * well-formed event it safely ignored. The link is still alive. */
            sup->events += 1;
            return GW_OK;
        }
        if (result == WY_REFUSED) {
            sup->refused = 1;
            link_down(sup);
            append(sup, "session=refused");
            return GW_REFUSED;
        }
        /* WY_IO / WY_INVALID / WY_LIMIT: the bridge closed the link or the transport erred.
         * This is the only thing that ends an idle persistent session, on purpose. */
        link_down(sup);
        sup->consecutive_failures += 1;
        append(sup, "link=lost reconnecting");
        if (schedule_backoff(sup)) return GW_FAILED;
        char line[96];
        snprintf(line, sizeof(line), "backoff=%lld us", (long long)sup->last_backoff_us);
        append(sup, line);
        return GW_WAIT;
    }

    default:
        return GW_FAILED;
    }
}
