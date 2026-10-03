#include "wyoming_wire.h"
#include <stdio.h>
#include <string.h>

int wy_read_exact(wy_read_fn fn, void *ctx, void *buffer, size_t length) {
    if (!fn || (length && !buffer)) return WY_INVALID;
    if (length > WY_PAYLOAD_MAX) return WY_LIMIT;
    uint8_t *p = buffer;
    while (length) {
        int n = fn(ctx, p, length);
        if (n <= 0 || (size_t)n > length) return WY_IO;
        p += n;
        length -= (size_t)n;
    }
    return WY_OK;
}

int wy_read_header(wy_read_fn fn, void *ctx, char *buffer, size_t capacity) {
    if (!fn || !buffer || capacity < 2) return WY_INVALID;
    size_t used = 0;
    buffer[0] = '\0';
    while (used < capacity - 1 && used < WY_HEADER_MAX - 1) {
        char byte;
        if (fn(ctx, &byte, 1) != 1) return WY_IO;
        if (byte == '\n') {
            buffer[used] = '\0';
            return used ? WY_OK : WY_INVALID;
        }
        if (byte == '\0' || byte == '\r') return WY_INVALID;
        buffer[used++] = byte;
        buffer[used] = '\0';
    }
    return WY_LIMIT;
}

static int write_all(wy_write_fn fn, void *ctx, const void *data, size_t length) {
    const uint8_t *p = data;
    while (length) {
        int n = fn(ctx, p, length);
        if (n <= 0 || (size_t)n > length) return WY_IO;
        p += n;
        length -= (size_t)n;
    }
    return WY_OK;
}

int wy_send(wy_write_fn fn, void *ctx, const char *type,
            const char *data, const void *payload, size_t payload_length) {
    if (!fn || !type || !type[0] || (payload_length && !payload)) return WY_INVALID;
    size_t type_length = 0;
    for (const unsigned char *p = (const unsigned char *)type; *p; ++p) {
        if (++type_length > 96) return WY_LIMIT;
        if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') ||
              (*p >= '0' && *p <= '9') || *p == '-' || *p == '.' || *p == '_'))
            return WY_INVALID;
    }
    size_t data_length = 0;
    if (data) {
        while (data[data_length]) {
            if (++data_length > WY_DATA_MAX) return WY_LIMIT;
        }
    }
    if (payload_length > WY_PAYLOAD_MAX) return WY_LIMIT;
    char header[256];
    int n = snprintf(header, sizeof(header),
        "{\"type\":\"%s\",\"version\":\"1.0.0\",\"data_length\":%zu,\"payload_length\":%zu}\n",
        type, data_length, payload_length);
    if (n < 0 || (size_t)n >= sizeof(header)) return WY_LIMIT;
    int result = write_all(fn, ctx, header, (size_t)n);
    if (result != WY_OK) return result;
    if (data_length) {
        result = write_all(fn, ctx, data, data_length);
        if (result != WY_OK) return result;
    }
    return write_all(fn, ctx, payload, payload_length);
}
