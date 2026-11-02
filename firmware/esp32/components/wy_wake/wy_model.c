/* The wake model blob's pure layout logic (see wy_model.h). Little-endian on
 * the wire (the ESP32-S3 is little-endian and the provisioner writes
 * little-endian too, so the fields are stored raw). No hash here: the
 * provisioner computes the SHA-256, the glue re-checks it with mbedTLS -- this
 * module owns only the layout, so it compiles and is host-tested with
 * -Werror.
 *
 * Two header versions are read: version 1 (no inference policy, 108 bytes) and
 * version 2 (the model's policy follows the hash, 120 bytes). Version 1 stays
 * accepted because a device provisioned before the policy moved into the blob
 * must keep waking after a firmware update; it uses the firmware's compiled
 * defaults, which is exactly what it was scored with before. */
#include "wy_model.h"
#include <string.h>

/* The policy bounds are the scorer's own (wy_mww.h / wy_mww_scorer.cpp). They
 * are repeated here so this module stays dependency-free and host-compilable on
 * its own; a value outside them is rejected at parse time rather than handed to
 * an interpreter that would refuse it later with a less useful message. */
#define MODEL_CUTOFF_MIN 0.01f
#define MODEL_WINDOW_MIN 1u
#define MODEL_WINDOW_MAX 32u
#define MODEL_STEP_MIN   1u
#define MODEL_STEP_MAX   100u
#define MODEL_ARENA_MIN  1024u
#define MODEL_ARENA_MAX  1048576u

size_t wy_model_header_size_for(uint16_t version)
{
    if (version == WY_MODEL_VERSION)
        return WY_MODEL_HEADER_SIZE;
    if (version == WY_MODEL_VERSION_LEGACY)
        return WY_MODEL_HEADER_SIZE_V1;
    return 0;
}

static int policy_valid(const wy_model_policy *policy)
{
    if (policy == NULL)
        return 0;
    if (!(policy->probability_cutoff >= MODEL_CUTOFF_MIN &&
          policy->probability_cutoff <= 1.0f))
        return 0;
    if (policy->sliding_window_size < MODEL_WINDOW_MIN ||
        policy->sliding_window_size > MODEL_WINDOW_MAX)
        return 0;
    if (policy->feature_step_ms < MODEL_STEP_MIN ||
        policy->feature_step_ms > MODEL_STEP_MAX)
        return 0;
    if (policy->tensor_arena_size < MODEL_ARENA_MIN ||
        policy->tensor_arena_size > MODEL_ARENA_MAX)
        return 0;
    return 1;
}

int wy_model_header_pack(const char *name, uint32_t data_len,
                         const uint8_t sha256[WY_MODEL_SHA256_LEN],
                         const wy_model_policy *policy,
                         uint8_t *out, size_t cap, size_t *out_len)
{
    size_t name_len;
    size_t header_size;
    if (name == NULL || sha256 == NULL || out == NULL || out_len == NULL)
        return -1;
    if (policy != NULL && !policy_valid(policy))
        return -1;
    header_size = policy != NULL ? WY_MODEL_HEADER_SIZE : WY_MODEL_HEADER_SIZE_V1;
    if (cap < header_size)
        return -1;
    name_len = strlen(name);
    if (name_len >= WY_MODEL_NAME_MAX)
        return -1;   /* no room for the NUL */
    if (data_len == 0)
        return -1;   /* a model with no bytes is not a model */
    {
        uint8_t *p = out;
        uint32_t magic = WY_MODEL_MAGIC;
        uint16_t version = policy != NULL ? WY_MODEL_VERSION : WY_MODEL_VERSION_LEGACY;
        uint16_t reserved = 0;
        memcpy(p + 0, &magic, 4);
        memcpy(p + 4, &version, 2);
        memcpy(p + 6, &reserved, 2);
        memcpy(p + 8, name, name_len + 1u);   /* name plus its NUL */
        memset(p + 8 + name_len + 1u, 0, WY_MODEL_NAME_MAX - (name_len + 1u));
        memcpy(p + 72, &data_len, 4);
        memcpy(p + 76, sha256, WY_MODEL_SHA256_LEN);
        if (policy != NULL) {
            memcpy(p + 108, &policy->probability_cutoff, 4);
            memcpy(p + 112, &policy->sliding_window_size, 2);
            memcpy(p + 114, &policy->feature_step_ms, 2);
            memcpy(p + 116, &policy->tensor_arena_size, 4);
        }
    }
    *out_len = header_size;
    return 0;
}

int wy_model_header_peek(const uint8_t *header, size_t header_len, wy_model_header *out)
{
    uint32_t magic;
    uint16_t version, reserved;
    uint32_t data_len;
    size_t name_len;
    size_t header_size;
    wy_model_header tmp;
    if (header == NULL || out == NULL || header_len < WY_MODEL_HEADER_SIZE_V1)
        return -1;
    memcpy(&magic, header + 0, 4);
    if (magic != WY_MODEL_MAGIC)
        return -1;
    memcpy(&version, header + 4, 2);
    header_size = wy_model_header_size_for(version);
    if (header_size == 0)
        return -1;   /* unknown version: never guess at a layout */
    if (header_len < header_size)
        return -1;   /* truncated below the header this version declares */
    memcpy(&reserved, header + 6, 2);
    if (reserved != 0)
        return -1;   /* unknown fields must stay zero */
    /* The name is NUL-terminated WITHIN its 64 bytes; a full field of
     * non-NULs would make a strlen run past the blob. */
    for (name_len = 0; name_len < WY_MODEL_NAME_MAX; name_len++)
        if (header[8 + name_len] == 0)
            break;
    if (name_len == WY_MODEL_NAME_MAX)
        return -1;
    memcpy(&data_len, header + 72, 4);
    memset(&tmp, 0, sizeof(tmp));
    tmp.magic = magic;
    tmp.version = version;
    tmp.reserved = reserved;
    memcpy(tmp.name, header + 8, name_len + 1u);
    tmp.data_len = data_len;
    memcpy(tmp.sha256, header + 76, WY_MODEL_SHA256_LEN);
    if (version == WY_MODEL_VERSION) {
        memcpy(&tmp.policy.probability_cutoff, header + 108, 4);
        memcpy(&tmp.policy.sliding_window_size, header + 112, 2);
        memcpy(&tmp.policy.feature_step_ms, header + 114, 2);
        memcpy(&tmp.policy.tensor_arena_size, header + 116, 4);
        if (!policy_valid(&tmp.policy))
            return -1;   /* a policy nobody validated is not a policy */
    }
    *out = tmp;
    return 0;
}

int wy_model_header_parse(const uint8_t *blob, size_t blob_len, wy_model_header *out)
{
    wy_model_header tmp;
    size_t header_size;
    if (blob == NULL || out == NULL || blob_len < WY_MODEL_HEADER_SIZE_V1)
        return -1;
    /* Fixed fields first (the same checks peek does) ... */
    if (wy_model_header_peek(blob, blob_len, &tmp) != 0)
        return -1;
    header_size = wy_model_header_size_for(tmp.version);
    /* ... then the whole-blob length check that only a full read can make:
     * the blob must actually hold the data the header claims. The header size
     * depends on the version, so a v1 blob is not asked for the policy bytes
     * it never had. */
    if (blob_len - header_size < tmp.data_len)
        return -1;   /* the blob does not hold what the header claims */
    /* Commit to *out only once the WHOLE parse succeeded: the documented contract is -1 with
     * *out untouched, so a caller still holding its last good header never loses it to a
     * half-validated one. */
    *out = tmp;
    return 0;
}
