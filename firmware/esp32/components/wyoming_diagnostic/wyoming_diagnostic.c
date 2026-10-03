#include "wyoming_diagnostic.h"
#include "wyoming_wire.h"
#include "wyoming_json.h"
#include <string.h>

typedef struct { unsigned char bytes[512]; size_t used; size_t position; } loopback;

static int emit(void *ctx, const void *data, size_t length) {
    loopback *wire = ctx;
    size_t n = length > 3 ? 3 : length;
    if (n > sizeof(wire->bytes) - wire->used) return -1;
    memcpy(wire->bytes + wire->used, data, n);
    wire->used += n;
    return (int)n;
}

static int receive(void *ctx, void *data, size_t length) {
    loopback *wire = ctx;
    size_t n = length > 2 ? 2 : length;
    if (n > wire->used - wire->position) n = wire->used - wire->position;
    memcpy(data, wire->bytes + wire->position, n);
    wire->position += n;
    return (int)n;
}

int wyoming_diagnostic(void) {
    loopback wire = {0};
    const unsigned char expected[] = {0, 255, '\n', 1};
    const char metadata[] = "{\"rate\":16000,\"width\":2,\"channels\":1}";
    int result = wy_send(emit, &wire, "audio-chunk", metadata,
                         expected, sizeof(expected));
    if (result != WY_OK) return result;
    if ((result = wy_send(emit, &wire, "audio-stop", NULL, NULL, 0)) != WY_OK)
        return result;
    char header[WY_HEADER_MAX];
    wy_frame frame;
    if ((result = wy_read_header(receive, &wire, header, sizeof(header))) != WY_OK)
        return result;
    if ((result = wy_parse_header(header, strlen(header), &frame)) != WY_OK)
        return result;
    if (strcmp(frame.type, "audio-chunk") || frame.data_length != strlen(metadata) ||
        frame.payload_length != sizeof(expected)) return WY_INVALID;
    char data[sizeof(metadata)] = {0};
    unsigned char payload[sizeof(expected)];
    if ((result = wy_read_exact(receive, &wire, data, frame.data_length)) != WY_OK)
        return result;
    if ((result = wy_read_exact(receive, &wire, payload, frame.payload_length)) != WY_OK)
        return result;
    if (strcmp(data, metadata) || memcmp(payload, expected, sizeof(expected)))
        return WY_INVALID;
    if ((result = wy_read_header(receive, &wire, header, sizeof(header))) != WY_OK)
        return result;
    if ((result = wy_parse_header(header, strlen(header), &frame)) != WY_OK)
        return result;
    if (strcmp(frame.type, "audio-stop") || frame.data_length || frame.payload_length ||
        wire.position != wire.used) return WY_INVALID;
    return WY_OK;
}
