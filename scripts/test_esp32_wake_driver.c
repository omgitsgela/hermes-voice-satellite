/* The host test driver for the wake core. It is compiled into the same .so as
 * the production core (see scripts/test_esp32_wake.py) and provides:
 *
 *   a scripted scorer -- one score per feed call, read from a file-static
 *     array set by td_reset; past the end of the script it returns 0.0f
 *     (silence, never a trigger), so a test is deterministic and never
 *     needs to marshal callback state;
 *   counting chime/wake seams that also record the call ORDER (the
 *     chime-then-wake property, increment 4 task 3);
 *   a load seam that records the model name the core asked for and can be
 *     made to fail (the model-less core, increment 4 task 4).
 *
 * Everything is file-static; td_reset clears all of it. The td_addr_* accessors
 * hand the test the raw function addresses so the test can install these as
 * the core's seams.
 */
#include "wy_wake.h"
#include <string.h>

#define TD_MAX_SCORES 256
#define TD_NAME_MAX 64

static float td_scores[TD_MAX_SCORES];
static int td_n;
static int td_i;
static int td_score_calls;
static int td_chime_calls;
static int td_wake_calls;
static int td_last_event; /* 0 = none yet, 1 = chime, 2 = wake */
static int td_load_calls;
static int td_wake_result; /* the wake seam's return value (0 = accepted) */
static char td_loaded_name[TD_NAME_MAX];
static int td_load_fail;
static uint8_t td_model_bytes[8];

void td_reset(const float *scores, int n)
{
    int i;
    td_n = (n < 0 || n > TD_MAX_SCORES) ? 0 : n;
    for (i = 0; i < td_n; i++)
        td_scores[i] = scores[i];
    td_i = 0;
    td_score_calls = 0;
    td_chime_calls = 0;
    td_wake_calls = 0;
    td_last_event = 0;
    td_load_calls = 0;
    td_wake_result = 0;
    td_loaded_name[0] = '\0';
    td_load_fail = 0;
    memset(td_model_bytes, 0xAB, sizeof td_model_bytes);
}

float td_score(void *user, const int16_t *mono, size_t n)
{
    (void)user;
    (void)mono;
    (void)n;
    td_score_calls++;
    if (td_i >= td_n)
        return 0.0f; /* past the script: silence, not a trigger */
    return td_scores[td_i++];
}

int td_chime(void *user)
{
    (void)user;
    td_chime_calls++;
    td_last_event = 1;
    return 0;
}

int td_wake(void *user)
{
    (void)user;
    td_wake_calls++;
    td_last_event = 2;
    return td_wake_result;
}

int td_load(void *user, const char *model_name, void **out_model, size_t *out_len)
{
    (void)user;
    td_load_calls++;
    if (model_name != NULL) {
        strncpy(td_loaded_name, model_name, TD_NAME_MAX - 1u);
        td_loaded_name[TD_NAME_MAX - 1u] = '\0';
    }
    if (td_load_fail)
        return -1;
    if (out_model != NULL)
        *out_model = td_model_bytes;
    if (out_len != NULL)
        *out_len = sizeof td_model_bytes;
    return 0;
}

/* The test reads these back. */
int td_get_score_calls(void)
{
    return td_score_calls;
}

int td_get_chime_calls(void)
{
    return td_chime_calls;
}

int td_get_wake_calls(void)
{
    return td_wake_calls;
}

/* 1 iff a chime->wake pair was recorded with the chime first and the two
 * balanced; 0 for no events, a wake without a chime, or a wake-first order. */
int td_chime_then_wake(void)
{
    return (td_chime_calls == td_wake_calls && td_chime_calls > 0 && td_last_event == 2) ? 1 : 0;
}

int td_get_load_calls(void)
{
    return td_load_calls;
}

const char *td_loaded_name_str(void)
{
    return td_loaded_name;
}

void td_set_load_fail(int fail)
{
    td_load_fail = fail ? 1 : 0;
}

/* Set the wake seam's return value: 0 (the default) is an accepted wake; non-zero
 * simulates a rejection (the session paused or not armed). */
void td_set_wake_result(int result)
{
    td_wake_result = result;
}

void *td_addr_score(void)
{
    return (void *)td_score;
}

void *td_addr_load(void)
{
    return (void *)td_load;
}

void *td_addr_chime(void)
{
    return (void *)td_chime;
}

void *td_addr_wake(void)
{
    return (void *)td_wake;
}
