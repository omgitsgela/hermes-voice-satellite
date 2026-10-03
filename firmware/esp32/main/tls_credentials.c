#include "tls_credentials.h"
#include <string.h>

/* Field offsets in the fixed header. Named rather than inlined so the layout is one readable list,
 * and the last one plus its width is the header length the parser insists on. */
#define OFF_MAGIC 0u
#define OFF_VERSION 8u
#define OFF_HEADER_LEN 10u
#define OFF_TOTAL_LEN 12u
#define OFF_CA_LEN 16u
#define OFF_CERT_LEN 20u
#define OFF_KEY_LEN 24u
#define OFF_PORT 28u
#define OFF_PIN 32u
#define OFF_HOST 64u
#define OFF_CRC 128u

static uint16_t read_u16(const unsigned char *field) {
    return (uint16_t)((uint16_t)field[0] | ((uint16_t)field[1] << 8));
}

static uint32_t read_u32(const unsigned char *field) {
    return (uint32_t)field[0] | ((uint32_t)field[1] << 8) | ((uint32_t)field[2] << 16) |
           ((uint32_t)field[3] << 24);
}

/* CRC-32 (IEEE 802.3), bitwise so there is no table to keep in flash. The Python producer uses
 * zlib.crc32 and the host test asserts the two agree, which is the only reason a hand-written CRC
 * is acceptable at all. */
uint32_t tls_crc32(const unsigned char *data, size_t length) {
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t index = 0; index < length; index++) {
        crc ^= (uint32_t)data[index];
        for (int bit = 0; bit < 8; bit++) {
            if (crc & 1u) crc = (crc >> 1) ^ 0xEDB88320u;
            else crc >>= 1;
        }
    }
    return crc ^ 0xFFFFFFFFu;
}

/* A PEM block is a C string: the length counts a terminating NUL, no other NUL appears before it,
 * and it opens with a BEGIN line. That is the whole of what this parser claims to know about PEM.
 * Whether the text is really a certificate is mbedTLS's answer to give, and asking a second
 * implementation here would be two answers to one question. */
static int text_is_pem(const unsigned char *text, uint32_t length) {
    static const char prefix[] = "-----BEGIN ";
    if (length <= sizeof(prefix) - 1u + 1u) return -1;
    if (text[length - 1u] != 0) return -1;
    for (uint32_t index = 0; index + 1u < length; index++) {
        if (text[index] == 0) return -1;
    }
    if (memcmp(text, prefix, sizeof(prefix) - 1u) != 0) return -1;
    return 0;
}

int tls_credentials_parse(const unsigned char *blob, size_t blob_len, tls_credentials *out) {
    if (!blob || !out) return -1;
    if (blob_len < TLS_BLOB_HEADER_LEN) return -1;
    if (memcmp(blob + OFF_MAGIC, TLS_BLOB_MAGIC, TLS_BLOB_MAGIC_LEN) != 0) return -1;
    if (read_u16(blob + OFF_VERSION) != TLS_BLOB_VERSION) return -1;
    if (read_u16(blob + OFF_HEADER_LEN) != TLS_BLOB_HEADER_LEN) return -1;

    uint32_t total_len = read_u32(blob + OFF_TOTAL_LEN);
    uint32_t ca_len = read_u32(blob + OFF_CA_LEN);
    uint32_t cert_len = read_u32(blob + OFF_CERT_LEN);
    uint32_t key_len = read_u32(blob + OFF_KEY_LEN);
    uint32_t port = read_u32(blob + OFF_PORT);

    /* Summed in 64 bits: an impossible length must not wrap into one that adds up. */
    uint64_t declared = (uint64_t)TLS_BLOB_HEADER_LEN + (uint64_t)ca_len + (uint64_t)cert_len +
                        (uint64_t)key_len;
    if (declared != (uint64_t)total_len) return -1;
    if (ca_len == 0u || cert_len == 0u || key_len == 0u) return -1;
    if (total_len > blob_len) return -1;
    if (total_len > TLS_BLOB_REGION_BYTES) return -1;
    if (port == 0u || port > 65535u) return -1;

    const unsigned char *payload = blob + TLS_BLOB_HEADER_LEN;
    uint32_t payload_len = total_len - TLS_BLOB_HEADER_LEN;
    if (tls_crc32(payload, payload_len) != read_u32(blob + OFF_CRC)) return -1;

    const unsigned char *ca = payload;
    const unsigned char *cert = payload + ca_len;
    const unsigned char *key = cert + cert_len;
    if (text_is_pem(ca, ca_len) != 0) return -1;
    if (text_is_pem(cert, cert_len) != 0) return -1;
    if (text_is_pem(key, key_len) != 0) return -1;

    /* The host is a label for the report line, so it is only required to be printable and to end
     * inside its field. It is deliberately not what the connection is verified against. */
    char host[TLS_SERVER_HOST_MAX];
    memcpy(host, blob + OFF_HOST, TLS_SERVER_HOST_MAX);
    size_t host_len = 0;
    while (host_len < TLS_SERVER_HOST_MAX && host[host_len] != 0) host_len++;
    if (host_len == 0u || host_len == TLS_SERVER_HOST_MAX) return -1;
    for (size_t index = 0; index < host_len; index++) {
        if (host[index] < 0x20 || host[index] > 0x7e) return -1;
    }

    memset(out, 0, sizeof(*out));
    out->ca = (const char *)ca;
    out->ca_len = ca_len;
    out->cert = (const char *)cert;
    out->cert_len = cert_len;
    out->key = (const char *)key;
    out->key_len = key_len;
    memcpy(out->server_pin, blob + OFF_PIN, TLS_SERVER_PIN_LEN);
    memcpy(out->server_host, host, TLS_SERVER_HOST_MAX);
    out->server_port = port;
    out->total_len = total_len;
    return 0;
}

int tls_clock_is_sane(int64_t unix_seconds) {
    return unix_seconds >= TLS_CLOCK_FLOOR_UNIX ? 1 : 0;
}

int tls_server_pin_matches(const unsigned char actual[TLS_SERVER_PIN_LEN],
                           const unsigned char expected[TLS_SERVER_PIN_LEN]) {
    /* Every byte is examined whatever the earlier ones said, so the answer does not depend on how
     * far a difference happens to be from the start. */
    unsigned char difference = 0;
    for (size_t index = 0; index < TLS_SERVER_PIN_LEN; index++) {
        difference |= (unsigned char)(actual[index] ^ expected[index]);
    }
    return difference == 0 ? 1 : 0;
}
