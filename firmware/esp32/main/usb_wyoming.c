#include "usb_wyoming.h"
#include "device_identity.h"
#include "wyoming_session.h"
#include "driver/usb_serial_jtag.h"
#include "esp_err.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include <stdio.h>
#include <string.h>

typedef struct { int64_t deadline; } usb_io;
static int quiet_log(const char *format, va_list arguments) {
    (void)format; (void)arguments; return 0;
}

static TickType_t remaining_ticks(int64_t deadline) {
    int64_t left = deadline - esp_timer_get_time();
    if (left <= 0) return 0;
    uint32_t ms = (uint32_t)((left + 999) / 1000);
    if (ms > 250) ms = 250;
    TickType_t ticks = pdMS_TO_TICKS(ms);
    return ticks ? ticks : 1;
}

static int usb_read(void *ctx, void *data, size_t size) {
    usb_io *io = ctx;
    for (;;) {
        if (!usb_serial_jtag_is_connected()) return -1;
        TickType_t wait = io->deadline ? remaining_ticks(io->deadline) : pdMS_TO_TICKS(250);
        if (!wait) return -1;
        int count = usb_serial_jtag_read_bytes(data, (uint32_t)size, wait);
        if (count < 0 || (size_t)count > size) return -1;
        int64_t now = esp_timer_get_time();
        if (io->deadline && now >= io->deadline) return -1;
        if (count) {
            if (!io->deadline) io->deadline = now + 5000000;
            return count;
        }
        /* A connected but idle host is not EOF. Once the first byte arrives,
         * the entire frame and responses share one absolute five-second budget. */
    }
}

static int usb_write(void *ctx, const void *data, size_t size) {
    usb_io *io = ctx;
    TickType_t wait = remaining_ticks(io->deadline);
    if (!wait) return -1;
    int count = usb_serial_jtag_write_bytes(data, size, wait);
    if (count <= 0 || (size_t)count > size || esp_timer_get_time() >= io->deadline) return -1;
    return count;
}

int usb_wyoming_run(void) {
    /* Zeroed: wy_session_init preserves the writer-lock fields, and a dirty SPIRAM block
     * would otherwise hand the first registration a garbage function pointer. */
    wy_session *session = heap_caps_calloc(1, sizeof(*session), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!session) return WY_IO;
    int result = WY_IO;
    char device_id[32], boot_id[33];
    if (device_identity(device_id, sizeof(device_id), boot_id, sizeof(boot_id)) != 0) goto done;
    result = wy_session_init(session, device_id, boot_id);
    if (result) goto done;
    esp_log_set_vprintf(quiet_log);
    usb_io io = {.deadline = esp_timer_get_time() + 1000000};
    const char *ack = "PROVISION OK WYOMING CONTROL_ONLY RESET_REQUIRED\n";
    size_t used = 0, length = strlen(ack);
    while (used < length) {
        int count = usb_write(&io, ack + used, length - used);
        if (count < 0) { result = WY_IO; goto done; }
        used += (size_t)count;
    }
    TickType_t wait = remaining_ticks(io.deadline);
    if (!wait || usb_serial_jtag_wait_tx_done(wait) != ESP_OK ||
        esp_timer_get_time() >= io.deadline) { result = WY_IO; goto done; }
    do {
        io.deadline = 0;
        result = wy_session_step(session, usb_read, usb_write, &io);
    } while (!result);
done:
    heap_caps_free(session);
    return result;
}
