#ifndef WYOMING_WIRE_H
#define WYOMING_WIRE_H
#include <stddef.h>
#include <stdint.h>

/* Callbacks return transferred bytes, zero for EOF, or negative for I/O error.
 * The transport adapter must impose a finite deadline. One reader per link.
 */
typedef int (*wy_write_fn)(void *, const void *, size_t);
typedef int (*wy_read_fn)(void *, void *, size_t);
enum { WY_OK = 0, WY_INVALID = -1, WY_IO = -2, WY_LIMIT = -3 };
#define WY_HEADER_MAX 1024u
#define WY_DATA_MAX 4096u
#define WY_PAYLOAD_MAX 16384u

/* data_json is a trusted, already serialized JSON object, or NULL.
 * Lengths are UTF-8 byte counts, not character counts. No heap allocations.
 * After any failure, close the connection rather than retrying the same frame.
 */
int wy_send(wy_write_fn write_fn, void *ctx, const char *type,
            const char *data_json, const void *payload, size_t payload_length);
int wy_read_header(wy_read_fn fn, void *ctx, char *buffer, size_t capacity);
int wy_read_exact(wy_read_fn fn, void *ctx, void *buffer, size_t length);
#endif
