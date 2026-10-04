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

void report_tail_append(char *buffer, size_t capacity, const char *line) {
    if (!buffer || capacity == 0) return;
    if (!line) line = "";
    /* The buffer holds complete, newline-terminated lines, is NUL-terminated, and has one
     * writer (the gateway task's report), so this scan stops either at the NUL or at the end
     * of the buffer. `used` is the length of the content. */
    size_t used = 0;
    while (used + 1 < capacity && buffer[used]) ++used;
    if (used + 1 >= capacity && buffer[capacity - 1u] != 0) {
        buffer[capacity - 1u] = 0;  /* not NUL-terminated: not ours to extend */
        return;
    }
    size_t length = strlen(line);
    /* Room behind the current content: a line that fits is appended in place. */
    if (used + length + 2u <= capacity) {
        memcpy(buffer + used, line, length);
        buffer[used + length] = '\n';
        buffer[used + length + 1] = 0;
        return;
    }
    /* It does not fit behind what is there: evict the oldest complete lines (their text and
     * their '\n') until it does, so the report rolls once it fills instead of freezing -- a
     * status query that reads the last line must keep seeing the newest thing the link did,
     * for the life of the session. cut only tracks where to look for the next line's newline;
     * the eviction is always from the head, so next_used is absolute. */
    size_t cut = 0;
    while (cut < used) {
        const char *nl = NULL;
        for (size_t i = cut; i < used; ++i)
            if (buffer[i] == '\n') { nl = buffer + i; break; }
        if (!nl) break;  /* no more complete lines to give up */
        size_t next_used = used - (size_t)(nl - buffer) - 1u;
        if (next_used + length + 2u <= capacity) {
            memmove(buffer, nl + 1, next_used);
            used = next_used;
            break;
        }
        cut = (size_t)(nl - buffer) + 1u;
    }
    /* Bound the line by whatever room the evictions left (a line longer than the buffer is
     * truncated); a bare '\n' needs only two bytes. */
    size_t room = capacity > used + 2u ? capacity - used - 2u : 0u;
    if (length > room) length = room;
    if (length == 0 && room == 0) return;  /* not even a bare newline fits */
    memcpy(buffer + used, line, length);
    buffer[used + length] = '\n';
    buffer[used + length + 1] = 0;
}
