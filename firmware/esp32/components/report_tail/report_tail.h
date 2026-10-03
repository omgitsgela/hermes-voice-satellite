#ifndef REPORT_TAIL_H
#define REPORT_TAIL_H
#include <stddef.h>

/* The gateway task accumulates a bounded, newline-separated report for the life of the
 * session (item 1). A console query and the soak harness want the LAST line -- "the most
 * recent thing the link did" -- not the whole buffer. This is the pure copier: no RTOS,
 * no device, one bounded write.
 *
 * report_tail_last_line copies the last line of `report` (the text after its final '\n',
 * or the whole buffer when there is no newline) into `out`, truncated so the NUL always
 * fits, and always NUL-terminates. capacity 0 is a no-op that leaves `out` untouched.
 * Returns the number of characters written, NUL excluded; 0 when nothing was written.
 */
size_t report_tail_last_line(const char *report, char *out, size_t capacity);

#endif
