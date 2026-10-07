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
 *   4       2     version (1)
 *   6       2     reserved (0)
 *   8       64    model name, NUL-terminated (the config wake_word; the model
 *                 SELECTOR, distinct from the session's detection name)
 *   72      4     data length, little-endian
 *   76      32    SHA-256 of the model bytes that follow
 *   108     ...   the model bytes
 *
 * Header total: 108 bytes. The SHA-256 is computed by the WRITER (the
 * provisioner, in Python) and RE-checked by the device glue (mbedTLS) over the
 * bytes in flash; this module owns only the pure, hash-free layout logic, so
 * it compiles and is tested on the host with -Werror exactly like the core.
 * A blob that fails any check is rejected whole and the detector stays in its
 * no-model state: a corrupted or truncated model is a fallback, never a
 * garbage scorer.
 */

#define WY_MODEL_MAGIC      0x314D4B57u /* "WKM1" in little-endian byte order */
#define WY_MODEL_VERSION    1u
#define WY_MODEL_NAME_MAX   64u
#define WY_MODEL_SHA256_LEN 32u
#define WY_MODEL_HEADER_SIZE (4u + 2u + 2u + WY_MODEL_NAME_MAX + 4u + WY_MODEL_SHA256_LEN) /* 108 */

typedef struct wy_model_header {
    uint32_t magic;
    uint16_t version;
    uint16_t reserved;
    char name[WY_MODEL_NAME_MAX];
    uint32_t data_len;
    uint8_t sha256[WY_MODEL_SHA256_LEN];
} wy_model_header;

/* Pack one header into out (at least WY_MODEL_HEADER_SIZE bytes, the model
 * bytes themselves are written by the caller immediately after). Returns 0 on
 * success with *out_len = WY_MODEL_HEADER_SIZE, -1 on a bad argument (a NULL
 * name or sha, a name that does not fit, or a zero data length -- a model with
 * no bytes is not a model). */
int wy_model_header_pack(const char *name, uint32_t data_len,
                         const uint8_t sha256[WY_MODEL_SHA256_LEN],
                         uint8_t *out, size_t cap, size_t *out_len);

/* Parse and validate the header of a blob read from the partition. Returns 0
 * on success with *out filled, -1 (with *out untouched) when the blob is
 * truncated, the magic or version is wrong, the name is not terminated, or
 * data_len claims more bytes than the blob holds (a length that exactly fills
 * the blob is valid). The model bytes themselves are NOT touched: the caller
 * owns validating their hash. */
int wy_model_header_parse(const uint8_t *blob, size_t blob_len, wy_model_header *out);

#endif
