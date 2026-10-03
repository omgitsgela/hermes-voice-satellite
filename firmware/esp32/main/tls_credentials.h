#ifndef TLS_CREDENTIALS_H
#define TLS_CREDENTIALS_H
#include <stddef.h>
#include <stdint.h>

/* Provisioned TLS material for the registry link.
 *
 * The bridge terminates TLS itself and identifies this device by the SHA-256 fingerprint of the
 * client certificate, so on that side the certificate IS the credential. This device has to be able
 * to tell the real bridge from any other certificate its own CA might have signed, and it cannot do
 * that by name: it dials an address, and the name check a desktop client performs against an IP
 * subjectAltName is not something to reimplement here. It compares the fingerprint of the
 * certificate the handshake produced with the fingerprint provisioned below, which is the same kind
 * of check the bridge makes on this device. Neither direction trusts a name.
 *
 * The blob lives at the head of the `storage` data partition. scripts/provision-device-tls.py writes
 * it and reads it back before it is trusted; nothing here allocates, and every string is validated
 * to be NUL-terminated inside its own declared length, so a PEM block can go straight to mbedTLS.
 */
#define TLS_BLOB_MAGIC "HVTLS01" /* 7 characters plus the NUL that makes 8 bytes on flash */
#define TLS_BLOB_MAGIC_LEN 8u
#define TLS_BLOB_VERSION 1u
#define TLS_BLOB_HEADER_LEN 132u
/* The reserved credential region at the head of `storage`, header included. A future filesystem on
 * that partition must start after this many bytes. */
#define TLS_BLOB_REGION_BYTES 8192u
#define TLS_SERVER_PIN_LEN 32u
#define TLS_SERVER_HOST_MAX 64u

typedef struct {
    /* PEM text, each NUL-terminated as its last byte and free of embedded NULs. */
    const char *ca;
    uint32_t ca_len;
    const char *cert;
    uint32_t cert_len;
    const char *key;
    uint32_t key_len;
    unsigned char server_pin[TLS_SERVER_PIN_LEN];
    /* Informational: what this material is for, so a report can say it. */
    char server_host[TLS_SERVER_HOST_MAX];
    uint32_t server_port;
    uint32_t total_len;
} tls_credentials;

/* Parse and validate a credential blob. Returns 0 and fills `out` when the blob is intact, or -1
 * when any field is impossible: bad magic, wrong version, lengths that do not add up, a PEM block
 * without its terminator or with an embedded NUL, an impossible port, or a CRC mismatch. Bytes past
 * `total_len` (the zero padding of the region) are ignored. */
int tls_credentials_parse(const unsigned char *blob, size_t blob_len, tls_credentials *out);

uint32_t tls_crc32(const unsigned char *data, size_t length);

/* A clock that has never been set starts at the epoch, and mbedTLS then rejects every certificate
 * as "not yet valid" - an error that looks nothing like a clock problem. Refuse before dialling. */
#define TLS_CLOCK_FLOOR_UNIX 1700000000LL /* 2023-11-14 */
int tls_clock_is_sane(int64_t unix_seconds);

/* Compare the digest the handshake produced against the provisioned one. This is the check that
 * says "that is really the bridge", and every byte is compared whatever the first ones say. */
int tls_server_pin_matches(const unsigned char actual[TLS_SERVER_PIN_LEN],
                           const unsigned char expected[TLS_SERVER_PIN_LEN]);

#endif
