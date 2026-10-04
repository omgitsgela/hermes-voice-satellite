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

/* The gateway task's report is line-terminated: every line the supervisor appends ends with
 * '\n', and the buffer grows for the life of the session. A query that wants "the most recent
 * thing the link did" needs the last line's TEXT, not the empty string that follows its
 * terminator (which is what report_tail_last_line returns for a '\n'-terminated buffer).
 * report_tail_last_complete_line returns that text: the characters after the second-to-last '\n',
 * up to (not including) the last '\n'. It reads from the END of the report, so it is the right
 * answer even when the report is far larger than `capacity`. A trailing partial line (no '\n'
 * yet, a write in flight) is ignored; a report with no complete line yields "" (0). Bounded so
 * the NUL always fits; capacity 0 is a no-op that leaves `out` untouched. Returns the number of
 * characters written, NUL excluded.
 */
size_t report_tail_last_complete_line(const char *report, char *out, size_t capacity);

#endif
