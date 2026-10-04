#include "report_tail.h"
#include <string.h>

size_t report_tail_last_line(const char *report, char *out, size_t capacity) {
    if (!report || !out || capacity == 0) return 0;
    /* The last line starts after the final newline; a report with no newline is its own
     * last line. A trailing newline (an empty last line) is a real, answerable state: the
     * caller decides what "nothing has happened yet" reads as, not the copier. */
    const char *last = report;
    for (const char *p = report; *p; ++p)
        if (*p == '\n') last = p + 1;
    size_t length = 0;
    while (length + 1 < capacity && last[length]) ++length;
    memcpy(out, last, length);
    out[length] = 0;
    return length;
}

size_t report_tail_last_complete_line(const char *report, char *out, size_t capacity) {
    if (!report || !out || capacity == 0) return 0;
    /* The text of the last COMPLETE line: after the second-to-last '\n', up to (not
     * including) the last '\n'. The report's writers append line-terminated, so a
     * non-empty report normally ends with '\n', and report_tail_last_line would hand back
     * the empty string after it; this is the text that line actually holds. A report whose
     * tail is a partial line (its '\n' not down yet) ignores that line; a report with no
     * '\n' at all has no complete line. The walk reads from the END of the report, so a
     * report far larger than `capacity` still answers with its newest line. */
    const char *last = NULL;
    for (const char *p = report; *p; ++p)
        if (*p == '\n') last = p;
    if (!last) { out[0] = 0; return 0; }  /* no complete line: an empty, terminated answer */
    const char *start = report;
    for (const char *p = report; p < last; ++p)
        if (*p == '\n') start = p + 1;
    size_t length = 0;
    while (length + 1 < capacity && start + length < last) ++length;
    memcpy(out, start, length);
    out[length] = 0;
    return length;
}
