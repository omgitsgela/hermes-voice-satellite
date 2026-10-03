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
