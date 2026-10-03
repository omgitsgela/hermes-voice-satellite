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
};
static const size_t CAPS[] = { 8, 4, 3, 2, 1, 0, 16, 7, 6, 16, 16, 16, 16, 16 };
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
};

int main(void) {
    int failures = 0;
    for (int i = 0; i < 14; ++i) {
        char buffer[48];
        const char sentinel = 'U';  /* untouched: capacity 0 must leave this intact */
        memset(buffer, sentinel, sizeof(buffer));
        size_t expected_len = strlen(EXPECTED[i]);
        report_tail_last_line(INPUTS[i], buffer, CAPS[i]);
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
    printf(failures == 0 ? "ALL OK\n" : "FAILURES %d\n", failures);
    return failures ? 1 : 0;
}
