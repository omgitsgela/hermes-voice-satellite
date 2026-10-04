#ifndef GATEWAY_SUPERVISOR_H
#define GATEWAY_SUPERVISOR_H
#include <stddef.h>
#include <stdint.h>

/* The part of the gateway link that outlives registration.
 *
 * The session core (wyoming_session) knows how to register and to answer one control event, but
 * it is one-shot: nothing keeps a registered link open, and nothing re-dials when the transport
 * goes away. This component is the lifecycle that wraps it once: establish a transport, drive a
 * session to registered, then pump control events for as long as the link lives. When the
 * transport ends (peer closed, I/O error, or a frame the session core rejects), it tears the link
 * down, backs off, and starts again from a fresh establish and a fresh session.
 *
 * It is deliberately pure. No FreeRTOS, no sockets, no mbedTLS: the clock, the establish and
 * teardown of a transport, and the byte read and write are all callbacks, so the whole lifecycle
 * can be driven by a scripted, in-memory transport on the host. The on-device code supplies the
 * real transport (Wi-Fi join, dial, TLS handshake) and a FreeRTOS sleep between WAIT steps.
 *
 * Two properties are non-negotiable and are what the tests pin down:
 *   1. A registered link is long-lived. The bridge keeps an idle link alive with TCP keepalive and
 *      sends no application-level ping, so an idle read is NOT a failure. Only transport EOF/error
 *      or a rejected frame ends a link. The supervisor never imposes an idle timeout of its own.
 *      Registration is different: the drive to registered is bounded by the link's own deadline
 *      (finite until on_registered lifts it), so a bridge that accepts a connection and then goes
 *      silent cannot hold the device inside a handshake.
 *   2. Reconnect is bounded and observable. Backoff is exponential with a ceiling, a fresh
 *      successful registration resets the backoff, and every establish attempt, reconnect and
 *      terminal reason is counted so a device that cannot reach its bridge is legible rather than
 *      a silent retry loop.
 *
 * Step-driven: `gateway_supervisor_step` advances the state machine by at most one transition and
 * never sleeps. When it is time to back off it reports GW_WAIT and the next step's delay; the
 * caller sleeps that long and calls again. On the host the caller just advances a fake clock, so
 * the whole reconnect timeline is deterministic.
 */

/* Result codes for gateway_supervisor_step. */
enum {
    GW_OK = 0,        /* made progress; call step again to continue. */
    GW_WAIT = -1,     /* a backoff delay has not elapsed; sleep it and call again. Non-terminal. */
    GW_FAILED = -2,   /* terminal: the attempt budget is spent and no transport came up. */
    GW_REFUSED = -4   /* terminal: the bridge refused the registration; not retrying is correct. */
};

/* A live transport, as the supervisor sees it. The establish callback fills `read` and `write`
 * (and may store anything it wants in `user`); teardown closes it. The supervisor treats the
 * transport as opaque once establish succeeds. */
typedef struct gw_link {
    void *user;             /* transport-private state (socket, TLS context, ...). */
    int (*read_fn)(void *user, void *data, size_t size);   /* >0 bytes, 0 EOF, <0 I/O error. */
    int (*write_fn)(void *user, const void *data, size_t size); /* >0 bytes, <0 I/O error. */
} gw_link;

typedef struct {
    /* The clock, in microseconds. Injected so tests run a fake timeline; the device uses the
     * real monotonic clock. Must be non-decreasing. */
    int64_t (*now_us)(void);
    /* Bring a transport up: dial and (for TLS) handshake. On success fill `link` (read/write/user)
     * and return 0; on failure return -1. Must not be called while a previous link is still open. */
    int (*establish)(void *user, gw_link *link);
    /* Close a transport the establish call opened. Called on every teardown, including before a
     * failed establish that may have partially opened a descriptor. Pass-through of `user`. */
    void (*teardown)(void *user, gw_link *link);
    void *user;            /* handed to establish/teardown unchanged. */

    /* Optional lifecycle hook, called exactly when a session (re)reaches registered, with the
     * same `user`, before pumping begins on that link. The device uses it to lift the link from
     * its finite registration deadline to "no deadline", so an idle registered link blocks until
     * a real end of stream. A NULL hook means the link keeps whatever deadline establish set. */
    int (*on_registered)(void *user);

    /* Device identity, supplied to every fresh session. Filled by the caller (device_identity on
     * the device); the supervisor does not read NVS or the MAC itself. */
    char device_id[65];
    char boot_id[129];

    /* Backoff schedule, in microseconds. The Nth consecutive failed establish waits
     * min(base << (N-1), max); N counts failures since the last successful registration. */
    int64_t backoff_base_us;
    int64_t backoff_max_us;
    /* Number of establish attempts after which the supervisor gives up. 0 means "no limit": it
     * keeps retrying forever, which is what a satellite wants (a dropped bridge must not mean a
     * dead device). Finite values exist so tests and diagnostics can observe a clean stop. */
    int max_attempts;

    /* Bounded, NUL-terminated report buffer; the supervisor appends one line per transition worth
     * a line (established, registered, reconnect, backoff, refused, failed). May be NULL. */
    char *report;
    size_t report_capacity;

    /* --- state (private; access through the getters below) --- */
    int state;              /* internal phase, see .c */
    int registered;         /* 1 once the current session is registered (0 when the link is down). */
    int ever_registered;    /* 1 once any registration has succeeded; drives the reconnect count. */
    int events;             /* control events pumped while registered. */
    int attempts;           /* total establish attempts. */
    int reconnects;         /* successful re-registrations after the first. */
    int refused;            /* 1 if the last registration was refused by the bridge. */
    int64_t resume_at_us;   /* absolute time the current backoff ends; 0 when none is pending. */
    int64_t last_backoff_us;/* the most recent backoff that was chosen. */
    int consecutive_failures; /* establishes in a row without a registration; resets on success. */
    size_t session_size;
    /* Private: the single wy_session buffer the supervisor owns for its life, reused across
     * reconnects. Opaque to the caller. */
    void *session;
    /* Private: the currently-open transport, or all-null when none is open. */
    gw_link link;
} gateway_supervisor;

/* Internal session phase, exposed only so tests can assert the machine is where they think. */
enum { GW_ST_IDLE = 0, GW_ST_REGISTERING = 1, GW_ST_PUMPING = 2, GW_ST_BACKING_OFF = 3 };

size_t gateway_supervisor_size(void);
/* Returns 0, or -1 when the arguments are invalid (no clock, no establish, empty identity, or a
 * report buffer that is neither NULL nor non-empty). on_registered may be NULL (no deadline
 * switch at registration). */
int gateway_supervisor_init(gateway_supervisor *sup, int64_t (*now_us)(void),
                            int (*establish)(void *, gw_link *),
                            void (*teardown)(void *, gw_link *), void *user,
                            const char *device_id, const char *boot_id,
                            int64_t backoff_base_us, int64_t backoff_max_us, int max_attempts,
                            char *report, size_t report_capacity, int (*on_registered)(void *));

/* Advance the state machine one transition. Returns a GW_ code (see above). On GW_WAIT,
 * gateway_supervisor_delay_us reports how long to sleep. */
int gateway_supervisor_step(gateway_supervisor *sup);

/* Microseconds to sleep before the next step is meaningful; 0 when none is pending. */
int64_t gateway_supervisor_delay_us(const gateway_supervisor *sup);
int gateway_supervisor_registered(const gateway_supervisor *sup);
int gateway_supervisor_events(const gateway_supervisor *sup);
int gateway_supervisor_attempts(const gateway_supervisor *sup);
int gateway_supervisor_reconnects(const gateway_supervisor *sup);
int gateway_supervisor_refused(const gateway_supervisor *sup);
int64_t gateway_supervisor_last_backoff_us(const gateway_supervisor *sup);
int gateway_supervisor_state(const gateway_supervisor *sup);
/* Stable, human-readable name for the terminal reason, for reports: "ok", "wait", "failed",
 * "refused", or "no_transport". */
const char *gateway_supervisor_reason_name(int result);

#endif
