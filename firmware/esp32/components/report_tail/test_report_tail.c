/* Host driver for report_tail_last_line: an independent oracle per (input, capacity).
 *
 * The oracle is the contract, hand-derived, not a copy of the implementation:
 *   - the last line is the text after the final '\n' (or the whole buffer if there is
 *     none);
 *   - it is copied whole when it fits with room for the NUL, otherwise truncated so the
 *     NUL always fits (capacity 1 holds only the NUL);
 *   - capacity 0 is a no-op: the buffer is not touched at all.
 * Each case prints PASS <token> exactly when every byte of the buffer matches the
 * oracle; any mismatch prints FAIL <token> and the exit code goes nonzero. The Python
 * harness then checks that the right number of PASS lines appeared.
 */
#include "report_tail.h"
#include <stdio.h>
#include <string.h>

static const char *INPUTS[] = {
    "abc",
    "abc",
    "abc",
    "abc",
    "abc",
    "abc",
    "first\nsecond",
    "first\nsecond",
    "first\nsecond",
    "a\nb",
    "a\n",
    "\n",
    "",
    "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\ntail",
    /* report_tail_last_complete_line: the gateway task's report is line-terminated, so the
     * last line's TEXT is what a status query must return -- not the empty string after its
     * terminator. These are the oracle cases for that copier. */
    "first\nsecond\n",           /* complete, newline-terminated: the last line's text */
    "first\nsecond\n",           /* same, clipped to 5 + NUL */
    "second",                    /* no newline: no complete line */
    "first\npartial",            /* trailing partial line is ignored */
    "a\nb\nc",                   /* the newest complete line wins */
    "a\nb\nc",                   /* same, clipped to 1 + NUL */
    "",                          /* empty report */
    "\n",                        /* one empty complete line */
    "x\ny\nz\n",                 /* the newest complete line, not the text after the final \n */
    "x\ny\nz\n",                 /* same, clipped to 1 + NUL */
    "abc",                       /* capacity 0: the buffer must be left untouched */
};
static const size_t CAPS[] = { 8, 4, 3, 2, 1, 0, 16, 7, 6, 16, 16, 16, 16, 16,
                               16, 6, 16, 16, 16, 3, 16, 16, 16, 2, 0 };
static const char *EXPECTED[] = {
    "abc",
    "abc",
    "ab",
    "a",
    "",
    "",          /* untouched sentinel below proves capacity 0 wrote nothing */
    "second",
    "second",
    "secon",
    "b",
    "",
    "",
    "",
    "tail",
    "second",
    "secon",
    "",
    "first",
    "b",
    "b",
    "",
    "",
    "z",
    "z",
    "",          /* untouched sentinel below proves capacity 0 wrote nothing */
};

int main(void) {
    int failures = 0;
    for (int i = 0; i < 25; ++i) {
        char buffer[48];
        const char sentinel = 'U';  /* untouched: capacity 0 must leave this intact */
        memset(buffer, sentinel, sizeof(buffer));
        size_t expected_len = strlen(EXPECTED[i]);
        /* Cases 0-13 drive report_tail_last_line; 14-24 drive the complete-line copier. */
        if (i < 14)
            report_tail_last_line(INPUTS[i], buffer, CAPS[i]);
        else
            report_tail_last_complete_line(INPUTS[i], buffer, CAPS[i]);
        int ok = 1;
        if (CAPS[i] == 0) {
            ok = 1;  /* the capacity-0 case is verified by the sentinel below */
        } else {
            if (expected_len >= CAPS[i]) ok = 0;  /* the oracle should never demand that */
            else {
                if (strncmp(buffer, EXPECTED[i], expected_len) != 0) ok = 0;
                if (buffer[expected_len] != '\0') ok = 0;
            }
        }
        if (CAPS[i] == 0 && buffer[0] != sentinel) ok = 0;
        char token[32];
        snprintf(token, sizeof(token), "case%d cap%zu", i, CAPS[i]);
        if (ok) printf("PASS %s\n", token);
        else { printf("FAIL %s\n", token); ++failures; }
    }
    /* report_tail_append: stateful sequences. Each case fills a small buffer with a scripted
     * list of lines and checks the exact final bytes. The oracle is hand-derived: the buffer
     * holds complete lines, rolls (evicting the oldest complete line) when the newest does not
     * fit behind the rest, truncates a line that does not fit at all, and always ends on a
     * complete line (or stays empty when not even a bare newline fits). */
    struct append_case { size_t cap; const char *lines[8]; int n; const char *want; };
    const struct append_case APPEND_CASES[] = {
        { 8, { "a", "b" }, 2, "a\nb\n" },
        { 8, { "a", "b", "c" }, 3, "a\nb\nc\n" },
        { 8, { "abcd", "efgh" }, 2, "efgh\n" },
        { 8, { "a", "b", "c", "d" }, 4, "b\nc\nd\n" },
        { 6, { "abcdef" }, 1, "abcd\n" },
        { 6, { "abcdef", "gh" }, 2, "gh\n" },
        { 4, { "ab" }, 1, "ab\n" },
        { 4, { "a", "b" }, 2, "b\n" },
        { 8, { "" }, 1, "\n" },
        { 2, { "a" }, 1, "" },
    };
    for (int i = 0; i < (int)(sizeof(APPEND_CASES) / sizeof(APPEND_CASES[0])); ++i) {
        const struct append_case *c = &APPEND_CASES[i];
        char buffer[64];
        memset(buffer, 0, sizeof(buffer));
        for (int j = 0; j < c->n; ++j)
            report_tail_append(buffer, c->cap, c->lines[j]);
        int ok = (strcmp(buffer, c->want) == 0);
        char token[32];
        snprintf(token, sizeof(token), "app%d cap%zu", i, c->cap);
        if (ok) printf("PASS %s\n", token);
        else {
            printf("FAIL %s want=\"%s\" got=\"%s\"\n", token, c->want, buffer);
            ++failures;
        }
    }
    printf(failures == 0 ? "ALL OK\n" : "FAILURES %d\n", failures);
    return failures ? 1 : 0;
}
