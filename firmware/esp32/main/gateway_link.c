#include "gateway_link.h"
#include "device_identity.h"
#include "gateway_socket.h"
#include "wyoming_session.h"
#include "report_tail.h"
#include "esp_heap_caps.h"
#include <stdio.h>
#include <string.h>

void gateway_report_append(char *out, size_t capacity, const char *line) {
    if (!out || capacity == 0) return;
    report_tail_append(out, capacity, line);
}

int gateway_link_register(gateway_read_fn read, gateway_write_fn write, void *context,
                          int64_t deadline_us, char *out, size_t capacity) {
    char line[160];
    wy_session *session = heap_caps_malloc(wy_session_size(), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    char device_id[32] = {0}, boot_id[33] = {0};
    if (!session) {
        gateway_report_append(out, capacity, "session=failed reason=no_memory");
        return -1;
    }
    if (device_identity(device_id, sizeof(device_id), boot_id, sizeof(boot_id)) != 0) {
        gateway_report_append(out, capacity, "session=failed reason=no_identity");
        heap_caps_free(session);
        return -1;
    }
    int result = wy_session_init(session, device_id, boot_id);
    if (result != WY_OK) {
        snprintf(line, sizeof(line), "session=failed result=%s", wy_result_name(result));
        gateway_report_append(out, capacity, line);
        heap_caps_free(session);
        return -1;
    }

    while (wy_session_state(session) != WY_SESSION_REGISTERED) {
        result = wy_session_step(session, read, write, context);
        if (result) break;
        if (gateway_millis_left(deadline_us) == 0) {
            result = WY_IO;
            break;
        }
    }
    int registered = wy_session_state(session) == WY_SESSION_REGISTERED;
    snprintf(line, sizeof(line), "session=%s result=%s device_id=%s",
             wy_session_state_name(wy_session_state(session)), wy_result_name(result), device_id);
    gateway_report_append(out, capacity, line);

    heap_caps_free(session);
    return registered ? 0 : -1;
}
