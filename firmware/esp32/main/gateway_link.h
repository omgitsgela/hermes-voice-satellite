#ifndef GATEWAY_LINK_H
#define GATEWAY_LINK_H
#include <stddef.h>
#include <stdint.h>

/* The registration half of the link, shared by every transport.
 *
 * The transports differ in what carries the bytes, not in what the bytes are, so the Wyoming
 * handshake lives here once. Each transport dials, reports how it connected, and then hands its
 * read and write callbacks to this function.
 */
typedef int (*gateway_read_fn)(void *context, void *data, size_t size);
typedef int (*gateway_write_fn)(void *context, const void *data, size_t size);

/* Append one report line to a bounded buffer. The buffer is always NUL-terminated, including when
 * the line did not fit or the buffer was already full, so a truncated report cannot read as a
 * different one. */
void gateway_report_append(char *out, size_t capacity, const char *line);

/* Drive one session to registration over an established stream, appending the session line last.
 * Returns 0 when the session reached REGISTERED, and -1 otherwise: the deadline is absolute and
 * belongs to this call, so the transports cannot leave a stalled peer in charge of the console. */
int gateway_link_register(gateway_read_fn read, gateway_write_fn write, void *context,
                          int64_t deadline_us, char *out, size_t capacity);

#endif
