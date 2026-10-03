#ifndef WYOMING_SESSION_H
#define WYOMING_SESSION_H
#include "wyoming_wire.h"
/* RUN_REQUESTED is logical control state only. This probe has no audio path. */
enum { WY_SESSION_DESCRIBE = 0, WY_SESSION_ACK = 1, WY_SESSION_REGISTERED = 2,
       WY_SESSION_RUN_REQUESTED = 3, WY_SESSION_REFUSED = 4, WY_SESSION_FAILED = 5 };
enum { WY_REFUSED = -4 };
typedef struct {
    int state;
    char device_id[65];
    char boot_id[129];
    char header[WY_HEADER_MAX];
    char body[WY_DATA_MAX + 1];
} wy_session;
size_t wy_session_size(void);
int wy_session_init(wy_session *session, const char *device_id, const char *boot_id);
int wy_session_state(const wy_session *session);
/* Read exactly one control event. One owner; callbacks impose finite deadlines.
 * Close the transport on every non-OK result; init a fresh session on reconnect.
 * Config/audio/unknown events are currently unsupported and fail closed.
 */
int wy_session_step(wy_session *session, wy_read_fn read_fn, wy_write_fn write_fn, void *ctx);

/* Stable, human-readable names for session state and result codes, used in reports. A code
 * this build does not know reports as "unknown" rather than being silently unnamed. */
const char *wy_session_state_name(int state);
const char *wy_result_name(int result);
#endif
