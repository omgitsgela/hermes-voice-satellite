#ifndef WYOMING_JSON_H
#define WYOMING_JSON_H
#include "wyoming_wire.h"

typedef struct {
    char type[97];
    size_t data_length;
    size_t payload_length;
} wy_frame;

/* Validate a length-delimited header without its newline. This JSON layer uses
 * temporary heap allocations, unlike the wire core. Output is valid only on OK.
 * Event-specific schemas and metadata-body decoding are separate concerns.
 */
int wy_parse_header(const char *header, size_t length, wy_frame *frame);
/* Control frames only: no binary payload, exact metadata length, strict UTF-8,
 * finite numbers and unique object keys. Body fields override inline fields,
 * matching Python Wyoming. On OK caller owns *data and must cJSON_Delete it.
 * On failure *data is NULL. Parser allocations are temporary and bounded by
 * frame sizes/nesting; this is not an event-specific schema validator.
 */
struct cJSON;
int wy_decode_control(const char *header, size_t header_length,
                      const char *body, size_t body_length, struct cJSON **data);
#endif
