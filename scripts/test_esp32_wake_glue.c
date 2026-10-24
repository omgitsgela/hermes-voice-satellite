/* Increment 4, task 5 host-test glue: a wake-seam trampoline that routes the
 * detector's fire into the REAL session core's wy_session_wake. Compiled into
 * the same .so as the wake core and the session core (see
 * scripts/test_esp32_wake_integration.py) so the integration test proves the
 * exact seam wiring the device glue uses: the core calls its wake seam, and
 * that seam is the thing that calls wy_session_wake.
 *
 * The core's wake seam is int (*)(void *user). `user` is a pointer to an
 * iw_wake_ctx carrying the session, the detection name, the writer, and the
 * writer's context. The trampoline dereferences it and calls wy_session_wake.
 */
#include "wyoming_session.h"
#include "wy_wake.h"
#include <stdint.h>
#include <stddef.h>

typedef int (*wy_write_fn)(void *, const void *, size_t);

typedef struct iw_wake_ctx {
    void *session;      /* wy_session* (the session core's buffer) */
    const char *name;   /* the detection name (the device's WYAKE_WORD_NAME) */
    wy_write_fn write;  /* the link writer (the session's IO callback) */
    void *ctx;          /* the writer's context */
    void *core;         /* wy_wake* (issue #32): the seam reads the frozen level
                         * from it, exactly like wake_detector.c's wake_fire */
} iw_wake_ctx;

/* The wake seam: the core calls this on a fire. It is exactly the device
 * glue's wy_session_wake call. Returns the session's result (0 on success /
 * stream running). */
int iw_session_wake_seam(void *user)
{
    iw_wake_ctx *c = (iw_wake_ctx *)user;
    if (c == NULL || c->session == NULL || c->name == NULL || c->write == NULL)
        return -1;
    int level = (c->core != NULL) ? wy_wake_last_level((wy_wake *)c->core) : -1;
    return wy_session_wake((wy_session *)c->session, c->name, level, c->write, c->ctx);
}

/* The test reads back the seam's address (restype c_void_p) to install it as
 * the core's wake seam. A dedicated accessor avoids marshaling the address by
 * any path that could truncate it. */
void *iw_addr_wake_seam(void)
{
    return (void *)iw_session_wake_seam;
}
