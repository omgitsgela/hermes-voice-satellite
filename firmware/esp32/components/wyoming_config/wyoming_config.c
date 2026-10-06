#include "wyoming_config.h"
#include "wyoming_wire.h"
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/*
 * The device half of the declarative configuration plane, for the XVF3800 satellite.
 *
 * Contract (docs/configuration-plane.md; the canonical device half is pi/config_agent.py):
 *   in : one hermes.config.desired `data` JSON string per call
 *   out: one hermes.config.reported `data` JSON string, always -- {generation, actual,
 *        health, error}. The report describes the device's ACTUAL state (what the adapter
 *        really applied + health), never the rejected desired. A report is only produced
 *        once the new state is durable: persist runs before the report returns.
 *
 * The agent owns the schema allowlist and the generation guard (the "generation-safe" part
 * of the contract). It does NOT own `actual`: the injected apply adapter performs the
 * side-effects and reports back the actual state it reached and the resulting health, and the
 * agent trusts only that (validating it against the allowlist first). A device with no
 * adapter installed therefore degrades to an empty actual -- nothing is claimed applied that
 * was not (the reference's stage-only fallback).
 *
 * This device's apply model lives in the ADAPTER (the device glue, task 6), not here:
 * playback_volume -> set the session's live volume (immediate); wake_word/threshold -> stage
 * to NVS for the wake detector (the next increment); mic_gain/led_brightness -> no adapter
 * yet (degraded); locale/update_channel -> not device-applied. The agent stays
 * hardware-agnostic and host-testable for that reason.
 */

#define WY_CFG_SCHEMA_VERSION 1
#define WY_CFG_ERROR_MAX      256u   /* well under the bridge's 500-char report bound */
#define WY_CFG_PENDING_MAX    7u
#define WY_CFG_LOAD_MAX       1024u  /* max persisted blob the load backend fills */

typedef struct wy_config {
    char device_id[65];
    int has_device_id;
    long generation;      /* 0 until the first accepted apply */
    cJSON *desired;       /* retained validated settings of the last accepted apply */
    cJSON *actual;        /* retained actual from the last apply (adapter-reported, validated) */
    char pending[WY_CFG_PENDING_MAX][32];
    int pending_count;
    int health;           /* WY_CFG_HEALTH_* */
    char error[WY_CFG_ERROR_MAX];
    wy_cfg_save_fn save;
    wy_cfg_load_fn load;
    void *storage_user;
    wy_cfg_apply_fn apply;
    void *apply_user;
} wy_config;

static const char *const HEALTH_NAMES[4] = {
    "unknown", "healthy", "degraded", "error"
};

/* Schema v1 allowlist, identical to bridge.config_plane DEVICE_SETTING_SCHEMA. */
typedef enum { CFG_INT, CFG_NUM, CFG_STR } cfg_kind;
typedef struct { const char *name; cfg_kind kind; double lo, hi;
                 const char *const *enum_values; int enum_count; } cfg_spec;

static const char *const LOCALES[] = { "en-US" };
static const char *const CHANNELS[] = { "stable", "beta" };
static const cfg_spec SPECS[] = {
    { "wake_word",        CFG_STR, 0, 0, NULL, 0 },
    { "wake_threshold",   CFG_NUM, 0.05, 0.99, NULL, 0 },
    { "mic_gain",         CFG_INT, 0, 100, NULL, 0 },
    { "playback_volume",  CFG_INT, 0, 100, NULL, 0 },
    { "led_brightness",   CFG_INT, 1, 31, NULL, 0 },
    { "locale",           CFG_STR, 0, 0, LOCALES, 1 },
    { "update_channel",   CFG_STR, 0, 0, CHANNELS, 2 },
};
#define CFG_SPEC_COUNT (int)(sizeof(SPECS) / sizeof(SPECS[0]))

static const cfg_spec *spec_of(const char *name) {
    for (int i = 0; i < CFG_SPEC_COUNT; ++i)
        if (!strcmp(SPECS[i].name, name)) return &SPECS[i];
    return NULL;
}

/* The device_id grammar, matching the bridge's _DEVICE_ID. */
static int device_id_valid(const char *id, size_t len) {
    if (len < 1 || len > 64) return 0;
    for (size_t i = 0; i < len; ++i) {
        char ch = id[i];
        if (!((ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9') ||
              ch == '.' || ch == '_' || ch == '-')) return 0;
    }
    return 1;
}

/* The wake_word grammar, matching the bridge's _WAKE_WORD (no dot). */
static int wake_word_valid(const char *w, size_t len) {
    if (len < 1 || len > 64) return 0;
    for (size_t i = 0; i < len; ++i) {
        char ch = w[i];
        if (!((ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9') ||
              ch == '_' || ch == '-')) return 0;
    }
    return 1;
}

/* Copy a peer-controlled name into a report, keeping it SHORT and PRINTABLE. The validation
 * error interpolates the desired key, and the desired key is peer input: a hostile key (control
 * characters) expands under JSON escaping (\u00XX, 6x), which used to push the report past the
 * session's 1024-byte buffer -- the agent then produced no report, the session treated that as
 * a link fault, and the bridge re-dispatched the SAME poisoned frame on every registration: a
 * reconnect storm the device could never clear. A non-printable byte is a '?' and anything past
 * 31 chars is dropped (the detail is not the point of the error; the event is still a clean,
 * parseable refusal), so the report's worst case stays far below the buffer at any peer input.
 * Returns the number of chars written (the buffer is always NUL-terminated by the caller's use). */
static size_t sanitize_name(const char *name, char *out, size_t cap) {
    size_t n = 0;
    for (size_t i = 0; name && name[i] && n + 1 < cap; ++i) {
        unsigned char ch = (unsigned char)name[i];
        out[n++] = (ch >= 0x21 && ch <= 0x7e) ? (char)ch : '?';   /* printable ASCII only */
    }
    out[n] = '\0';
    return n;
}

static int json_values_equal(const cJSON *a, const cJSON *b) {
    if (a == b) return 1;
    if (!a || !b) return 0;
    if (cJSON_IsNumber(a) && cJSON_IsNumber(b)) return a->valuedouble == b->valuedouble;
    if (cJSON_IsString(a) && cJSON_IsString(b))
        return a->valuestring && b->valuestring && !strcmp(a->valuestring, b->valuestring);
    if (cJSON_IsBool(a) && cJSON_IsBool(b)) return a->valueint == b->valueint;
    if (cJSON_IsNull(a) && cJSON_IsNull(b)) return 1;
    return 0;
}

/* Semantic object equality: same keys, same values. For the "same generation has different
 * settings" guard (the reference compares dicts, not serialized strings). */
static int json_objects_equal(const cJSON *a, const cJSON *b) {
    if (!a || !b || !cJSON_IsObject(a) || !cJSON_IsObject(b)) return a == b;
    if (cJSON_GetArraySize(a) != cJSON_GetArraySize(b)) return 0;
    const cJSON *it = NULL;
    cJSON_ArrayForEach(it, a) {
        const cJSON *other = it->string ? cJSON_GetObjectItemCaseSensitive(b, it->string) : NULL;
        if (!other || !json_values_equal(it, other)) return 0;
    }
    return 1;
}

/* Any desired key whose actual value differs (or is missing) is "unapplied". After a restore,
 * `actual` is deliberately NULL (it was not persisted), and every desired key is then
 * unapplied: that is the intended "the volatile value is gone, re-apply on redelivery" state.
 * A NULL `actual` therefore reports unapplied, not a crash: cJSON_GetObjectItemCaseSensitive is
 * NULL-safe (returns NULL), so each key misses and the check fires. */
static int any_unapplied(const cJSON *desired, const cJSON *actual) {
    if (!cJSON_IsObject(desired)) return 0;
    const cJSON *it = NULL;
    cJSON_ArrayForEach(it, desired) {
        const cJSON *a = it->string ? cJSON_GetObjectItemCaseSensitive(actual, it->string) : NULL;
        if (!a || !json_values_equal(it, a)) return 1;
    }
    return 0;
}

/* The device's "staged for a future increment" set: a concrete later consumer (the wake
 * detector, the next increment). Remembered as pending when unapplied, so a redelivery knows
 * what is still waiting. The reference's analog is its restart-class set. */
static int is_staged(const char *name) {
    return !strcmp(name, "wake_word") || !strcmp(name, "wake_threshold");
}

/* Validate an object against the allowlist. allow_empty permits an empty object (the adapter
 * may report an empty actual). On success returns 1; on failure 0 with *err filled. */
static int validate_settings(const cJSON *settings, int allow_empty, char *err, size_t err_cap) {
    if (!cJSON_IsObject(settings)) {
        snprintf(err, err_cap, "settings must be a JSON object");
        return 0;
    }
    if (cJSON_GetArraySize(settings) < 1 && !allow_empty) {
        snprintf(err, err_cap, "settings must be a non-empty object");
        return 0;
    }
    const cJSON *item = NULL;
    cJSON_ArrayForEach(item, settings) {
        const char *name = item->string ? item->string : "";
        char safe_name[33];
        sanitize_name(name, safe_name, sizeof(safe_name));
        /* A setting that appears more than once is malformed: the guard's object equality
         * (json_objects_equal) matches a key against its FIRST occurrence while
         * cJSON_GetArraySize counts ALL of them, so a duplicate could be applied once and then
         * refused forever as "same generation has different settings" on every byte-identical
         * redelivery -- and after a reboot the durable blob carries it, so the volatile value
         * would never re-apply. The reference collapses duplicates via dict semantics; the wire
         * is peer-controlled (and the adapter's actual is validated here too), so reject rather
         * than silently adopt last-wins. A first occurrence's first-look is itself, so the
         * "first != item" test fires only on a genuine second occurrence. */
        const cJSON *first = item->string ?
            cJSON_GetObjectItemCaseSensitive(settings, name) : NULL;
        if (first && first != item) {
            snprintf(err, err_cap, "duplicate setting: %s", safe_name); return 0;
        }
        const cfg_spec *spec = spec_of(name);
        if (!spec) {
            /* The name is peer input: sanitize it (see sanitize_name) so the report's worst
             * case stays far below the buffer at any input, even if the key repeats. */
            snprintf(err, err_cap, "unknown setting: %s", safe_name); return 0;
        }
        int ok = 0;
        if (spec->kind == CFG_INT) {
            if (cJSON_IsNumber(item) && !cJSON_IsBool(item) &&
                item->valuedouble == (double)(long)item->valuedouble &&
                (long)item->valuedouble >= (long)spec->lo &&
                (long)item->valuedouble <= (long)spec->hi) ok = 1;
        } else if (spec->kind == CFG_NUM) {
            if (cJSON_IsNumber(item) && !cJSON_IsBool(item) &&
                item->valuedouble >= spec->lo && item->valuedouble <= spec->hi) ok = 1;
        } else { /* CFG_STR */
            if (cJSON_IsString(item) && item->valuestring) {
                if (!strcmp(name, "wake_word")) {
                    ok = wake_word_valid(item->valuestring, strlen(item->valuestring));
                } else if (spec->enum_count) {
                    for (int i = 0; i < spec->enum_count; ++i)
                        if (!strcmp(item->valuestring, spec->enum_values[i])) { ok = 1; break; }
                }
            }
        }
        if (!ok) { snprintf(err, err_cap, "%s has an invalid value", name); return 0; }
    }
    return 1;
}

/* Build the report with cJSON so escaping is always correct: {generation, actual, health,
 * error}. override_health < 0 means use the stored health; override_error NULL means the
 * stored error (or null when empty). `actual` defaults to an empty object. Returns 1 on
 * success. */
static int report_json(const wy_config *c, char *out, size_t cap,
                       int override_health, const char *override_error) {
    int health = override_health < 0 ? c->health : override_health;
    if (health < 0 || health > WY_CFG_HEALTH_ERROR) health = WY_CFG_HEALTH_UNKNOWN;
    const char *error = override_error ? override_error
                        : (c->error[0] ? c->error : NULL);
    cJSON *o = cJSON_CreateObject();
    if (!o) return 0;
    /* Every add is checked. On a key-strdup allocation failure the vendored
     * add_item_to_object returns false leaving the item UNATTACHED and still caller-owned:
     * unchecked, the field would silently drop from the serialized report -- breaking the
     * {generation, actual, health, error} contract (the host logs and drops a report missing
     * a required field) -- and the unattached item would leak. A build that cannot attach a
     * field is a failed report: return 0 so the caller maps it to WY_INVALID rather than
     * emitting a malformed success. The convenience adders (AddNumber/AddString/AddNull)
     * return NULL on failure and free their own internal item, so only the object needs
     * deleting; the one AddItemToObject that fails leaves `actual` still ours, so free it. */
    if (!cJSON_AddNumberToObject(o, "generation", (double)c->generation)) {
        cJSON_Delete(o); return 0;
    }
    cJSON *actual = c->actual ? cJSON_Duplicate(c->actual, 1) : cJSON_CreateObject();
    if (!actual) { cJSON_Delete(o); return 0; }
    if (!cJSON_AddItemToObject(o, "actual", actual)) {
        cJSON_Delete(actual); cJSON_Delete(o); return 0;   /* not attached: still ours */
    }
    if (!cJSON_AddStringToObject(o, "health", HEALTH_NAMES[health])) {
        cJSON_Delete(o); return 0;
    }
    if (error) {
        if (!cJSON_AddStringToObject(o, "error", error)) { cJSON_Delete(o); return 0; }
    } else {
        if (!cJSON_AddNullToObject(o, "error")) { cJSON_Delete(o); return 0; }
    }
    char *s = cJSON_PrintUnformatted(o);
    cJSON_Delete(o);
    if (!s) return 0;
    int ok = (strlen(s) < cap);
    if (ok) { snprintf(out, cap, "%s", s); }
    free(s);
    return ok;
}

/* Write the durable state blob through the injected storage backend and report whether the
 * commit succeeded. The caller passes the exact state it wants durable (the pre-commit
 * snapshot), so a failed persist never half-updates the stored state. NOT persisted is
 * `actual`: it describes a live runtime condition (the session's current volume), not a
 * durable fact -- a firmware reboot resets the session, so a persisted "applied" would be a
 * lie after a power cycle. Leaving actual out is what makes the bridge's reconnect
 * re-dispatch re-apply the volatile value (on restore, actual is empty and the unapplied
 * check fires). Persisted: schema, generation (the guard), device_id, desired (the
 * same-generation check), pending, health, error. A torn NVS write is caught by restore's
 * schema check and degrades to "no persisted state", which is safe because the bridge
 * re-dispatches the desired on the next registration. */
static int persist(const wy_config *c, long new_generation, const cJSON *new_desired,
                   int health, const char *new_error, const char *new_device_id,
                   const char (*pending)[32], int pending_count) {
    if (!c->save) return 1;   /* no storage installed: nothing to persist, nothing to fail */
    cJSON *root = cJSON_CreateObject();
    if (!root) return 0;
    /* d and p are ours until attached to root: the convenience adders free their own item
     * on failure (only root needs deleting), but a failed AddItemToObject leaves the item
     * UNATTACHED and still ours -- checked here, or it silently drops the field from the
     * blob AND leaks the unattached item while persist still claims durability. After a
     * successful attach the local is nulled so the root-owned copy is freed exactly once.
     * The device_id and error are passed IN (not read from c->device_id / c->error): they
     * belong to the state ABOUT to be committed, and a persist failure must leave the
     * stored state -- identity and error included -- untouched. The caller commits them
     * into c-> only when this returns true. */
    cJSON *d = NULL;
    cJSON *p = NULL;
    if (!cJSON_AddNumberToObject(root, "schema_version", WY_CFG_SCHEMA_VERSION) ||
        !cJSON_AddStringToObject(root, "device_id", new_device_id) ||
        !cJSON_AddNumberToObject(root, "generation", (double)new_generation))
        goto fail;
    /* The desired is a REQUIRED field of the blob (restore rejects an empty/missing one). If
     * it cannot be duplicated, persist FAILS -- the caller reports "could not persist
     * configuration state" and leaves the generation unspent -- never substitutes an empty
     * object and claims success: a success-claim over an empty-desired blob is a lie across
     * the reboot (the guard the report says is durable is the one restore must throw away). */
    d = cJSON_Duplicate(new_desired, 1);
    if (!d) goto fail;
    p = cJSON_CreateArray();
    if (!p) goto fail;
    for (int i = 0; i < pending_count; ++i) {
        cJSON *s = cJSON_CreateString(pending[i]);
        if (!s) continue;   /* an optional pending entry cannot defeat the commit */
        cJSON_AddItemToArray(p, s);   /* allocation-free list append: cannot fail (s, p non-NULL) */
    }
    if (!cJSON_AddItemToObject(root, "desired", d)) goto fail;   /* d stays ours on failure */
    d = NULL;   /* root owns it now */
    if (!cJSON_AddItemToObject(root, "pending", p)) goto fail;   /* p stays ours on failure */
    p = NULL;   /* root owns it now */
    if (!cJSON_AddNumberToObject(root, "health", (double)health) ||
        !cJSON_AddStringToObject(root, "error", new_error))
        goto fail;
    {
        char *s = cJSON_PrintUnformatted(root);
        cJSON_Delete(root);   /* also frees d and p (root owns both); locals already nulled */
        if (!s) return 0;
        /* Final parse-back confirmation: the bytes about to be written must read back with
         * every field restore requires (schema_version, a non-empty device_id, an integer
         * generation, a non-empty desired). The checked adds above already guarantee each
         * field is present in the tree, so this re-verifies the SERIALIZED string -- the
         * exact durability claim restore will be made to honour. Cheap: one extra parse of a
         * ~700-byte string on a rare path (once per accepted config change). */
        cJSON *check = cJSON_Parse(s);
        if (!check) { free(s); return 0; }
        const cJSON *c_sv = cJSON_GetObjectItemCaseSensitive(check, "schema_version");
        const cJSON *c_id = cJSON_GetObjectItemCaseSensitive(check, "device_id");
        const cJSON *c_gen = cJSON_GetObjectItemCaseSensitive(check, "generation");
        const cJSON *c_des = cJSON_GetObjectItemCaseSensitive(check, "desired");
        int usable = cJSON_IsObject(check) &&
            cJSON_IsNumber(c_sv) && c_sv->valuedouble == WY_CFG_SCHEMA_VERSION &&
            cJSON_IsString(c_id) && c_id->valuestring && c_id->valuestring[0] &&
            cJSON_IsNumber(c_gen) && !cJSON_IsBool(c_gen) && c_gen->valuedouble >= 0 &&
            cJSON_IsObject(c_des) && cJSON_GetArraySize(c_des) >= 1;
        cJSON_Delete(check);
        if (!usable) { free(s); return 0; }
        int rc = c->save(c->storage_user, s, strlen(s));
        free(s);
        return rc == 0;
    }
fail:
    if (d) cJSON_Delete(d);   /* unattached and ours (a failed AddItemToObject) */
    if (p) cJSON_Delete(p);
    cJSON_Delete(root);   /* frees any children already attached */
    return 0;
}

/* Restore the state blob after a reboot. Returns 1 when a valid state was restored, 0 when
 * there is none (first boot) or it is corrupt (discarded -- the safe state).
 *
 * The restore is ALL-OR-NOTHING: the blob's fields are validated into locals, and the stored
 * state changes only once every required field is present and valid -- schema version, a
 * grammar-valid device_id, an integer generation in the device's 32-bit long range, and a
 * usable NON-EMPTY desired. A partially restored guard is exactly what the discard exists to
 * prevent: a torn blob's identity left behind would clobber the identity seeded by
 * wy_config_init and wedge the guard for the device's whole life (the blob re-reads on every
 * boot, so the wedge never clears), and a missing desired would leave c->desired NULL, which
 * makes the same-generation path no-op on EVERYTHING -- no re-apply of the volatile value and
 * a conflicting payload silently accepted. With no valid state the device converges on the
 * bridge's next dispatch.
 *
 * A VALID blob whose device_id is another device's is adopted (faithful to the reference's
 * state.update): the guard then refuses every desired addressed to the real device -- an
 * explicit "this state is not this device's; an operator must erase the NVS" wedge, never a
 * silent adoption of another device's settings. */
static int restore(wy_config *c) {
    if (!c->load) return 0;
    char blob[WY_CFG_LOAD_MAX];
    size_t len = 0;
    if (c->load(c->storage_user, blob, sizeof(blob) - 1, &len) != 0) return 0;
    if (len == 0 || len >= sizeof(blob)) return 0;
    blob[len] = '\0';
    cJSON *root = cJSON_Parse(blob);
    if (!cJSON_IsObject(root)) { cJSON_Delete(root); return 0; }
    const cJSON *sv = cJSON_GetObjectItemCaseSensitive(root, "schema_version");
    if (!cJSON_IsNumber(sv) || cJSON_IsBool(sv) || sv->valuedouble != WY_CFG_SCHEMA_VERSION) {
        cJSON_Delete(root); return 0;
    }
    /* device_id: required, and it must pass the same grammar the wire guard applies (a blob
     * with no valid identity is not a guard, it is trash). */
    const cJSON *id = cJSON_GetObjectItemCaseSensitive(root, "device_id");
    size_t id_len = cJSON_IsString(id) && id->valuestring ? strlen(id->valuestring) : 0;
    if (id_len < 1 || id_len >= sizeof(c->device_id) ||
        !device_id_valid(id->valuestring, id_len)) {
        cJSON_Delete(root); return 0;
    }
    char new_id[65];
    snprintf(new_id, sizeof(new_id), "%s", id->valuestring);
    /* generation: required, an integer in the device's 32-bit long range (no UB cast). */
    const cJSON *gen = cJSON_GetObjectItemCaseSensitive(root, "generation");
    if (!cJSON_IsNumber(gen) || cJSON_IsBool(gen) || gen->valuedouble < 0 ||
        gen->valuedouble > (double)INT32_MAX ||
        gen->valuedouble != (double)(long)gen->valuedouble) {
        cJSON_Delete(root); return 0;
    }
    long generation = (long)gen->valuedouble;
    /* desired: required, a usable NON-EMPTY object -- the guard's conflict check and the
     * unapplied check both run against it. A missing or non-object desired, or a duplicate
     * that fails, discards the blob. An empty-object desired is rejected too: the wire
     * validate_settings rejects empty settings AND persist() fails the commit (rather than
     * substituting an empty object) if the desired cannot be duplicated, so no legitimate
     * device state ever writes one -- an empty-object blob is corruption, the same hole as a
     * missing one (the same-generation path would no-op everything under that generation). */
    const cJSON *d = cJSON_GetObjectItemCaseSensitive(root, "desired");
    if (!cJSON_IsObject(d) || cJSON_GetArraySize(d) < 1) { cJSON_Delete(root); return 0; }
    cJSON *new_desired = cJSON_Duplicate(d, 1);
    if (!new_desired) { cJSON_Delete(root); return 0; }
    /* The optional fields default to safe values (unknown health, no pending, empty error);
     * a torn blob that lost them still restores a working guard that converges on the
     * bridge's next dispatch (the volatile value re-applies because actual is never
     * persisted). */
    int health = WY_CFG_HEALTH_UNKNOWN;
    const cJSON *health_item = cJSON_GetObjectItemCaseSensitive(root, "health");
    if (cJSON_IsNumber(health_item) && !cJSON_IsBool(health_item) &&
        health_item->valuedouble >= 0 && health_item->valuedouble <= WY_CFG_HEALTH_ERROR)
        health = (int)health_item->valuedouble;
    char new_error[WY_CFG_ERROR_MAX] = "";
    const cJSON *error = cJSON_GetObjectItemCaseSensitive(root, "error");
    if (cJSON_IsString(error) && error->valuestring && error->valuestring[0])
        snprintf(new_error, sizeof(new_error), "%s", error->valuestring);
    char new_pending[WY_CFG_PENDING_MAX][32];
    int new_pending_count = 0;
    const cJSON *pending = cJSON_GetObjectItemCaseSensitive(root, "pending");
    if (cJSON_IsArray(pending)) {
        const cJSON *item = NULL;
        cJSON_ArrayForEach(item, pending) {
            if (new_pending_count < (int)WY_CFG_PENDING_MAX && cJSON_IsString(item) &&
                item->valuestring && strlen(item->valuestring) < sizeof(new_pending[0]))
                snprintf(new_pending[new_pending_count++], sizeof(new_pending[0]), "%s",
                         item->valuestring);
        }
    }
    cJSON_Delete(root);
    /* Commit all-or-nothing: the stored state changes only after every required field
     * validated. `actual` is deliberately NOT restored -- it describes a live runtime
     * condition (the session's current volume) that a reboot resets; starting from an empty
     * actual is what makes the bridge's reconnect re-dispatch re-apply the volatile value. */
    cJSON_Delete(c->desired);
    cJSON_Delete(c->actual);
    c->desired = new_desired;
    c->actual = NULL;
    snprintf(c->device_id, sizeof(c->device_id), "%s", new_id);
    c->has_device_id = 1;
    c->generation = generation;
    c->health = health;
    snprintf(c->error, sizeof(c->error), "%s", new_error);
    c->pending_count = new_pending_count;
    for (int i = 0; i < new_pending_count; ++i)
        snprintf(c->pending[i], sizeof(c->pending[i]), "%s", new_pending[i]);
    return 1;
}

size_t wy_config_size(void) { return sizeof(wy_config); }

int wy_config_init(wy_config *c, const char *device_id) {
    if (!c) return WY_INVALID;
    memset(c, 0, sizeof(*c));
    c->health = WY_CFG_HEALTH_UNKNOWN;
    if (device_id && device_id_valid(device_id, strlen(device_id))) {
        snprintf(c->device_id, sizeof(c->device_id), "%s", device_id);
        c->has_device_id = 1;
    }
    return WY_OK;
}

void wy_config_destroy(wy_config *c) {
    if (!c) return;
    cJSON_Delete(c->desired);
    cJSON_Delete(c->actual);
    c->desired = NULL;
    c->actual = NULL;
}

void wy_config_set_storage(wy_config *c, wy_cfg_save_fn save, wy_cfg_load_fn load, void *user) {
    if (!c) return;
    c->save = save; c->load = load; c->storage_user = user;
    if (load) restore(c);   /* a reboot: bring the durable state back before any event */
}

void wy_config_set_apply(wy_config *c, wy_cfg_apply_fn apply, void *user) {
    if (!c) return;
    c->apply = apply; c->apply_user = user;
}

int wy_config_step(wy_config *c, const char *desired_json, char *report_out, size_t report_cap) {
    if (!c || !desired_json || !report_out || report_cap == 0) return WY_INVALID;
    cJSON *desired = cJSON_Parse(desired_json);
    if (!cJSON_IsObject(desired)) { cJSON_Delete(desired); return WY_INVALID; }
    char err[WY_CFG_ERROR_MAX];
    err[0] = '\0';

    const cJSON *sv = cJSON_GetObjectItemCaseSensitive(desired, "schema_version");
    const cJSON *id = cJSON_GetObjectItemCaseSensitive(desired, "device_id");
    const cJSON *gen = cJSON_GetObjectItemCaseSensitive(desired, "generation");
    const cJSON *settings = cJSON_GetObjectItemCaseSensitive(desired, "settings");

    /* -- guard: schema, device_id, generation. A failure reports the CURRENT state with
     * health=error and leaves the stored state untouched (the reference returns
     * current_report(health="error") without mutating self.state). */
    int guard_failed = 0;
    if (!cJSON_IsNumber(sv) || sv->valuedouble != WY_CFG_SCHEMA_VERSION) {
        snprintf(err, sizeof(err), "unsupported configuration schema"); guard_failed = 1;
    } else if (!cJSON_IsString(id) || !id->valuestring ||
               !device_id_valid(id->valuestring, strlen(id->valuestring))) {
        snprintf(err, sizeof(err), "invalid device_id"); guard_failed = 1;
    } else if (c->has_device_id && strcmp(id->valuestring, c->device_id)) {
        snprintf(err, sizeof(err), "desired state belongs to another device"); guard_failed = 1;
    } else if (cJSON_IsBool(gen) || !cJSON_IsNumber(gen) || gen->valuedouble < 1 ||
               gen->valuedouble > (double)INT32_MAX ||
               gen->valuedouble != (double)(long)gen->valuedouble) {
        /* A fraction (1.5) is not an integer, and a value above INT32_MAX is refused: the
         * generation must fit the device's 32-bit long on the wire, whatever the host's long
         * is (on the 64-bit test host a 2^32 value would pass a plain cast and only the
         * device-bound bound catches it). The integrality check is the same one
         * validate_settings uses for the integer settings. */
        snprintf(err, sizeof(err), "generation must be a positive integer"); guard_failed = 1;
    }
    /* Identity is NOT adopted in the guard. The reference (pi/config_agent.py) binds the
     * device_id in the COMMITTING state.update only -- a REJECTED frame (a bad setting, a
     * wrong generation, a different payload) must not bind the identity. Adopting here
     * (before validate_settings can reject) let one malformed frame pin the identity, and
     * every later well-formed frame addressed to the real device was then refused as
     * "belongs to another device": the guard re-reads the adopted id on every step, so the
     * wedge lasts the device's whole life. `new_id` carries the event's device_id into the
     * commit block, where it is committed only if the event is accepted and durable. */
    char new_id[65] = "";
    if (!guard_failed)
        snprintf(new_id, sizeof(new_id), "%s", id->valuestring);
    if (guard_failed) {
        int rc = report_json(c, report_out, report_cap, WY_CFG_HEALTH_ERROR, err);
        cJSON_Delete(desired);
        return rc ? WY_OK : WY_INVALID;
    }

    long generation = (long)gen->valuedouble;
    if (generation < c->generation) {
        /* Never apply an older generation: re-report the current state, unchanged. */
        int rc = report_json(c, report_out, report_cap, -1, NULL);
        cJSON_Delete(desired);
        return rc ? WY_OK : WY_INVALID;
    }
    if (generation == c->generation) {
        /* A spent generation must be redelivered with the SAME settings, or it is a
         * different event replayed under a generation already used: a protocol violation,
         * refused without applying. Checked before the idempotency decision so a
         * conflicting payload can never be silently swallowed by the no-op path. */
        if (c->desired &&
            (!cJSON_IsObject(settings) || !json_objects_equal(settings, c->desired))) {
            int rc = report_json(c, report_out, report_cap, WY_CFG_HEALTH_ERROR,
                                 "same generation has different settings");
            cJSON_Delete(desired);
            return rc ? WY_OK : WY_INVALID;
        }
        /* Same settings. Idempotent only when there is genuinely nothing left to apply: the
         * state is not in error and nothing is unapplied. The decision must depend on
         * `actual` (what was applied), never on the persisted health ALONE: after a restore,
         * health survives the blob but actual is deliberately empty (the volatile value is
         * gone), so a healthy-but-lost state must re-apply on the bridge's reconnect
         * re-dispatch -- a no-op keyed on health would leave the speaker at the fresh
         * default for the life of the link. Re-applying the same value is harmless. */
        int unapplied = c->desired ? any_unapplied(c->desired, c->actual) : 0;
        if (c->health != WY_CFG_HEALTH_ERROR && !unapplied) {
            int rc = report_json(c, report_out, report_cap, -1, NULL);
            cJSON_Delete(desired);
            return rc ? WY_OK : WY_INVALID;
        }
        /* else: a partial apply, an error, or a reboot left the state unapplied --
         * fall through and re-apply once. */
    }

    /* -- validate the settings we are about to apply. A rejection reports the current state
     * with health=error and leaves the stored state untouched. */
    if (!validate_settings(settings, 0, err, sizeof(err))) {
        int rc = report_json(c, report_out, report_cap, WY_CFG_HEALTH_ERROR, err);
        cJSON_Delete(desired);
        return rc ? WY_OK : WY_INVALID;
    }

    /* -- apply: the adapter does the side-effects and reports the actual state it reached.
     * No adapter (or a failing one) -> nothing is claimed applied: actual empty, health set. */
    cJSON *new_actual = cJSON_CreateObject();
    if (!new_actual) { cJSON_Delete(desired); return WY_INVALID; }
    int health = WY_CFG_HEALTH_DEGRADED;
    /* new_error is the error for the state ABOUT to be committed. It is built in a LOCAL
     * (not c->error) for the whole apply section: persist() serializes the error into the
     * blob, and a persist failure must leave the STORED state untouched -- including the
     * error. Writing c->error here (before the commit) used to leave the RAM a mix of the
     * NEW/cleared error and the OLD generation/health/desired once the persist failed: a
     * divergence from the durable blob that a same-session current-state read (an
     * older-generation event) would expose. new_error is committed into c->error only after
     * the persist succeeds (below), so a failed step never mutates the stored error. */
    char new_error[WY_CFG_ERROR_MAX] = "";
    if (c->apply) {
        char *s = cJSON_PrintUnformatted(settings);
        if (!s) {
            health = WY_CFG_HEALTH_ERROR;
            snprintf(new_error, sizeof(new_error), "could not serialize settings");
        } else {
            char actual_json[256] = "{}";
            char out_error[WY_CFG_ERROR_MAX] = {0};
            int out_health = -1;
            int rc = c->apply(c->apply_user, s, actual_json, sizeof(actual_json),
                              &out_health, out_error, sizeof(out_error));
            int good = (rc == WY_OK && out_health >= WY_CFG_HEALTH_HEALTHY &&
                        out_health <= WY_CFG_HEALTH_ERROR);
            cJSON *parsed = cJSON_Parse(actual_json);
            if (good && cJSON_IsObject(parsed) &&
                validate_settings(parsed, 1, err, sizeof(err))) {
                health = out_health;
                cJSON_Delete(new_actual);
                new_actual = parsed; parsed = NULL;
                if (out_error[0]) snprintf(new_error, sizeof(new_error), "%s", out_error);
            } else {
                health = WY_CFG_HEALTH_ERROR;
                if (err[0]) snprintf(new_error, sizeof(new_error), "%s", err);
                else snprintf(new_error, sizeof(new_error), "%s",
                              (good && out_error[0]) ? out_error : "apply failed");
            }
            free(s);
            cJSON_Delete(parsed);
        }
    } else {
        snprintf(new_error, sizeof(new_error),
                 "desired state is staged; no hardware adapter is installed");
    }

    /* -- commit: persist FIRST, and only if the commit is durable do we change the stored
     * state. A report claiming a new generation is only produced once that state is on disk
     * (durable persistence before acceptance). If the persist fails, the side effects
     * already ran in RAM (the volume is set), but the stored state is left untouched: the
     * generation does not advance, so the bridge's redelivery re-applies and retries the
     * persist once storage recovers. */
    cJSON *new_desired = cJSON_Duplicate(settings, 1);
    if (!new_desired) { cJSON_Delete(new_actual); cJSON_Delete(desired); return WY_INVALID; }
    int pending_count = 0;
    char pending[WY_CFG_PENDING_MAX][32];
    if (health == WY_CFG_HEALTH_ERROR || health == WY_CFG_HEALTH_DEGRADED) {
        const cJSON *item = NULL;
        cJSON_ArrayForEach(item, settings) {
            if (item->string && is_staged(item->string)) {
                const cJSON *a = cJSON_GetObjectItemCaseSensitive(new_actual, item->string);
                if (!a || !json_values_equal(item, a))
                    if (pending_count < (int)WY_CFG_PENDING_MAX)
                        snprintf(pending[pending_count++], sizeof(pending[0]),
                                 "%s", item->string);
            }
        }
    }
    /* The identity that would become durable on commit. A device seeded without an id
     * (an embedding that does not give wy_config_init a valid id, so the plane
     * self-identifies from the first accepted frame) adopts the event's device_id ONLY
     * when the commit is durable -- never before, so a rejected frame cannot pin it. A
     * seeded device (the shipped path) always carries its own id and this passes it
     * straight through. */
    const char *commit_id = c->has_device_id ? c->device_id : new_id;
    if (!persist(c, generation, new_desired, health, new_error, commit_id,
                 pending, pending_count)) {
        cJSON_Delete(new_desired);
        cJSON_Delete(new_actual);
        int rc = report_json(c, report_out, report_cap, WY_CFG_HEALTH_ERROR,
                             "could not persist configuration state");
        cJSON_Delete(desired);
        return rc ? WY_OK : WY_INVALID;
    }
    /* -- commit: the persist is durable, so the stored state now changes -- including the
     * fields that only become durable here: the error (built in new_error above, never
     * written to c->error before the commit) and the identity (adopted on commit, not on
     * the guard). Until this point a failed step left the stored state byte-for-byte
     * intact: generation, health, desired, error, and identity all still the durable ones. */
    cJSON_Delete(c->desired);
    cJSON_Delete(c->actual);
    c->desired = new_desired;
    c->actual = new_actual;
    c->generation = generation;
    c->health = health;
    snprintf(c->error, sizeof(c->error), "%s", new_error);
    if (!c->has_device_id) {
        snprintf(c->device_id, sizeof(c->device_id), "%s", new_id);
        c->has_device_id = 1;
    }
    c->pending_count = pending_count;
    for (int i = 0; i < pending_count; ++i)
        snprintf(c->pending[i], sizeof(c->pending[0]), "%s", pending[i]);
    int rc = report_json(c, report_out, report_cap, -1, NULL);
    cJSON_Delete(desired);
    return rc ? WY_OK : WY_INVALID;
}
