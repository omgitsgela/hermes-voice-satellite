#ifndef GATEWAY_TASK_H
#define GATEWAY_TASK_H
#include <stdbool.h>
#include <stddef.h>

/* The gateway session as a long-lived FreeRTOS task: the thing that "registers on boot" and then
 * keeps the session alive (item 2) by running the persistent supervisor (item 1) for as long as the
 * device is configured.
 *
 * gateway_task_start is the whole device half. It brings Wi-Fi up to the given network and then
 * launches a task that dials the endpoint, drives a session to registered, and pumps it for the
 * life of the link. When the transport ends -- or the Wi-Fi itself drops -- the supervisor backs
 * off and re-dials, and the re-dial re-associates, so a network that was off at boot and comes up
 * later is picked up by the very same backoff that reconnects a dropped bridge. A bridge that
 * *refuses* the registration (not authorized) is terminal: the task releases the radio and exits.
 *
 * It returns 0 when the task is running, -1 when there was nothing to do (no task launched). The
 * radio is held from the first successful associate until the task exits; console Wi-Fi commands
 * are expected to defer to an active gateway (see gateway_task_active), because a single radio
 * cannot be scanned and held associated at once. */
int gateway_task_start(const char *host, unsigned port, int transport,
                       const char *ssid, const char *psk, char *report, size_t capacity);
/* True while the persistent gateway task is running and holding the radio. */
bool gateway_task_active(void);
/* The last line the task appended to its report, for a console STATUS to show. "" when none yet. */
void gateway_task_report(char *out, size_t capacity);
#endif
