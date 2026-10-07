/* The wake model blob's pure layout logic (see wy_model.h). Little-endian on
 * the wire (the ESP32-S3 is little-endian and the provisioner writes
 * little-endian too, so the fields are stored raw). No hash here: the
 * provisioner computes the SHA-256, the glue re-checks it with mbedTLS -- this
 * module owns only the layout, so it compiles and is host-tested with
 * -Werror. */
#include "wy_model.h"
#include <string.h>

int wy_model_header_pack(const char *name, uint32_t data_len,
                         const uint8_t sha256[WY_MODEL_SHA256_LEN],
                         uint8_t *out, size_t cap, size_t *out_len)
{
    size_t name_len;
    if (name == NULL || sha256 == NULL || out == NULL || out_len == NULL)
        return -1;
    if (cap < WY_MODEL_HEADER_SIZE)
        return -1;
    name_len = strlen(name);
    if (name_len >= WY_MODEL_NAME_MAX)
        return -1;   /* no room for the NUL */
    if (data_len == 0)
        return -1;   /* a model with no bytes is not a model */
    {
        uint8_t *p = out;
        uint32_t magic = WY_MODEL_MAGIC;
        uint16_t version = WY_MODEL_VERSION;
        uint16_t reserved = 0;
        memcpy(p + 0, &magic, 4);
        memcpy(p + 4, &version, 2);
        memcpy(p + 6, &reserved, 2);
        memcpy(p + 8, name, name_len + 1u);   /* name plus its NUL */
        memset(p + 8 + name_len + 1u, 0, WY_MODEL_NAME_MAX - (name_len + 1u));
        memcpy(p + 72, &data_len, 4);
        memcpy(p + 76, sha256, WY_MODEL_SHA256_LEN);
    }
    *out_len = WY_MODEL_HEADER_SIZE;
    return 0;
}

int wy_model_header_peek(const uint8_t *header, size_t header_len, wy_model_header *out)
{
    uint32_t magic;
    uint16_t version, reserved;
    uint32_t data_len;
    size_t name_len;
    if (header == NULL || out == NULL || header_len < WY_MODEL_HEADER_SIZE)
        return -1;
    memcpy(&magic, header + 0, 4);
    if (magic != WY_MODEL_MAGIC)
        return -1;
    memcpy(&version, header + 4, 2);
    if (version != WY_MODEL_VERSION)
        return -1;
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
    out->magic = magic;
    out->version = version;
    out->reserved = reserved;
    memcpy(out->name, header + 8, name_len + 1u);
    memset(out->name + name_len + 1u, 0, WY_MODEL_NAME_MAX - (name_len + 1u));
    out->data_len = data_len;
    memcpy(out->sha256, header + 76, WY_MODEL_SHA256_LEN);
    return 0;
}

int wy_model_header_parse(const uint8_t *blob, size_t blob_len, wy_model_header *out)
{
    if (blob == NULL || out == NULL || blob_len < WY_MODEL_HEADER_SIZE)
        return -1;
    /* Fixed fields first (the same checks peek does) ... */
    if (wy_model_header_peek(blob, blob_len, out) != 0)
        return -1;
    /* ... then the whole-blob length check that only a full read can make:
     * the blob must actually hold the data the header claims. */
    if (blob_len - WY_MODEL_HEADER_SIZE < out->data_len)
        return -1;   /* the blob does not hold what the header claims */
    return 0;
}
