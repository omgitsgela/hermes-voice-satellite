#include "esp_log.h"
#include "esp_psram.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "wyoming_diagnostic.h"
#include "memory_probe.h"
#include "provisioning_console.h"

static void report_heap(const char *phase, const char *name, uint32_t caps) {
    ESP_LOGI("diagnostic", "%s %s free=%zu minimum=%zu largest=%zu", phase, name,
             heap_caps_get_free_size(caps), heap_caps_get_minimum_free_size(caps),
             heap_caps_get_largest_free_block(caps));
}

void app_main(void) {
    int result = wyoming_diagnostic();
    if (result != 0) {
        ESP_LOGE("diagnostic", "diagnostic failed: %d; audio and network inactive", result);
        return;
    }
    ESP_LOGI("diagnostic", "Wyoming diagnostic passed; audio and network inactive");
    report_heap("before", "internal", MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    report_heap("before", "psram", MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!esp_psram_is_initialized()) {
        ESP_LOGE("diagnostic", "PSRAM unavailable; audio and network inactive");
        return;
    }
    ESP_LOGI("diagnostic", "PSRAM detected_bytes=%zu", esp_psram_get_size());
    const size_t length = 1024u * 1024u;
    uint8_t *buffer = heap_caps_malloc(length, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!buffer) {
        ESP_LOGE("diagnostic", "PSRAM allocation failed; audio and network inactive");
        return;
    }
    memory_probe_fill(buffer, length, 0x5a);
    result = memory_probe_check(buffer, length, 0x5a);
    if (result == 0) {
        memory_probe_fill(buffer, length, 0xa5);
        result = memory_probe_check(buffer, length, 0xa5);
    }
    heap_caps_free(buffer);
    if (result != 0) {
        ESP_LOGE("diagnostic", "PSRAM pattern test failed; audio and network inactive");
        return;
    }
    ESP_LOGI("diagnostic", "PSRAM pattern test passed: %zu bytes", length);
    report_heap("after", "internal", MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    report_heap("after", "psram", MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    ESP_LOGI("diagnostic", "stack_min_free_bytes=%u",
             (unsigned)uxTaskGetStackHighWaterMark(NULL));
    provisioning_console_run();
}
