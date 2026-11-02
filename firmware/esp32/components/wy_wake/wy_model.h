#ifndef WY_MODEL_H
#define WY_MODEL_H
#include <stddef.h>
#include <stdint.h>

/*
 * The wake model blob: the file format of the `model` data partition (see
 * partitions.csv). A single raw read-only region, read with esp_partition_read
 * -- NOT a mounted filesystem -- holding one header followed by the model
 * bytes, so a read can be validated before it is trusted:
 *
 *   offset  size  field
 *   0       4     magic "WKM1" (little-endian 0x314D4B57)
 *   4       2     version (1 or 2)
 *   6       2     reserved (0)
 *   8       64    model name, NUL-terminated (the config wake_word; the model
 *                 SELECTOR, distinct from the session's detection name)
 *   72      4     data length, little-endian
 *   76      32    SHA-256 of the model bytes that follow
 *   108     ...   the model bytes (version 1 header ends here)
 *
 * Version 2 appends the model's own inference policy between the hash and the
 * model bytes, so the model and the numbers it was trained to be scored with
 * travel as one provisioned unit:
 *
 *   108     4     probability cutoff, float32 little-endian (0..1]
 *   112     2     sliding window size (probabilities per average)
 *   114     2     feature step, milliseconds
 *   116     4     tensor arena size, bytes
 *   120     ...   the model bytes
 *
 * Why the policy is in the blob: the model partition cannot carry the model's
 * JSON manifest, and a compile-time policy meant that swapping models left the
 * old model running under the new model's cutoff/arena. Provisioning a model
 * without its manifest (version 1) is still accepted and uses the firmware's
 * compiled defaults, but every model this repository provisions is version 2.
 * A version 2 header is rejected whole when a policy field is out of range,
 * exactly like a corrupt hash: the device falls back to its no-model state
 * rather than scoring with numbers nobody validated.
 *
 * Header total: 108 bytes (v1), 120 bytes (v2). The SHA-256 is computed by the
 * WRITER (the provisioner, in Python) and RE-checked by the device glue
 * (mbedTLS) over the bytes in flash; this module owns only the pure, hash-free
 * layout logic, so it compiles and is tested on the host with -Werror exactly
 * like the core. A blob that fails any check is rejected whole and the detector
 * stays in its no-model state: a corrupted or truncated model is a fallback,
 * never a garbage scorer.
 */

#define WY_MODEL_MAGIC      0x314D4B57u /* "WKM1" in little-endian byte order */
#define WY_MODEL_VERSION    2u
#define WY_MODEL_VERSION_LEGACY 1u
#define WY_MODEL_NAME_MAX   64u
#define WY_MODEL_SHA256_LEN 32u
#define WY_MODEL_HEADER_SIZE_V1 (4u + 2u + 2u + WY_MODEL_NAME_MAX + 4u + WY_MODEL_SHA256_LEN) /* 108 */
#define WY_MODEL_POLICY_SIZE 12u
#define WY_MODEL_HEADER_SIZE (WY_MODEL_HEADER_SIZE_V1 + WY_MODEL_POLICY_SIZE) /* 120 */

typedef struct wy_model_policy {
    float probability_cutoff;    /* model's own gate, 0..1 */
    uint16_t sliding_window_size;/* probabilities per average */
    uint16_t feature_step_ms;    /* frontend step in ms */
    uint32_t tensor_arena_size;  /* manifest tensor arena hint, bytes */
} wy_model_policy;

typedef struct wy_model_header {
    uint32_t magic;
    uint16_t version;
    uint16_t reserved;
    char name[WY_MODEL_NAME_MAX];
    uint32_t data_len;
    uint8_t sha256[WY_MODEL_SHA256_LEN];
    wy_model_policy policy;
} wy_model_header;

/* The header bytes a blob of `version` actually carries (120 for v2, 108 for v1). */
size_t wy_model_header_size_for(uint16_t version);

/* Pack one header into out (at least WY_MODEL_HEADER_SIZE bytes, the model
 * bytes themselves are written by the caller immediately after). `policy` may
 * be NULL to emit a version 1 header (no inference policy); a non-NULL policy
 * emits version 2 and is validated before it is written. Returns 0 on success
 * with *out_len set to the header size for the emitted version, -1 on a bad
 * argument (a NULL name or sha, a name that does not fit, a zero data length --
 * a model with no bytes is not a model -- or an out-of-range policy). */
int wy_model_header_pack(const char *name, uint32_t data_len,
                         const uint8_t sha256[WY_MODEL_SHA256_LEN],
                         const wy_model_policy *policy,
                         uint8_t *out, size_t cap, size_t *out_len);

/* Read just the fixed-size header fields from a header-only buffer (no model bytes needed).
 * Returns 0 on success with *out filled, -1 on a bad magic/version/reserved, an unterminated
 * name, or (version 2) an out-of-range policy. Unlike wy_model_header_parse this does NOT check
 * that the buffer holds data_len model bytes -- a peek is exactly the "I have the header, tell me
 * the length" read the device loader does first (to size its buffer) before the full read +
 * authoritative parse. A data_len of 0 is NOT rejected here (that is a caller policy); it is left
 * to the caller. `header_len` must cover the header for the version the blob declares: 108 bytes
 * for version 1, 120 for version 2. A version 1 header leaves out->policy zeroed and callers must
 * use their compiled defaults. */
int wy_model_header_peek(const uint8_t *header, size_t header_len, wy_model_header *out);

/* Parse and validate the header of a blob read from the partition. Returns 0
 * on success with *out filled, -1 (with *out untouched) when the blob is
 * truncated, the magic or version is wrong, the name is not terminated, or
 * data_len claims more bytes than the blob holds (a length that exactly fills
 * the blob is valid). The model bytes themselves are NOT touched: the caller
 * owns validating their hash. This is the whole-blob check; use
 * wy_model_header_peek for a header-only first read. */
int wy_model_header_parse(const uint8_t *blob, size_t blob_len, wy_model_header *out);

#endif
