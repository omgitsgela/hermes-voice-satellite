#include "time_sync.h"
#include "tls_credentials.h"
#include "esp_netif_sntp.h"
#include "freertos/FreeRTOS.h"
#include <stdio.h>
#include <time.h>

/* A pool name rather than an address: the satellite is expected to work on networks it has never
 * seen, and every one of them hands out a resolver over DHCP. */
#define TIME_SYNC_SERVER "pool.ntp.org"

int64_t time_sync_now(void) {
    return (int64_t)time(NULL);
}

int time_sync_wait(int timeout_ms, char *out, size_t capacity) {
    if (out && capacity) out[0] = 0;
    if (tls_clock_is_sane(time_sync_now())) {
        snprintf(out, capacity, "clock=unix=%lld synced=yes", (long long)time_sync_now());
        return 0;
    }

    esp_sntp_config_t config = ESP_NETIF_SNTP_DEFAULT_CONFIG(TIME_SYNC_SERVER);
    config.start = true;
    if (esp_netif_sntp_init(&config) != ESP_OK) {
        snprintf(out, capacity, "clock=sync_failed reason=init");
        return -1;
    }
    esp_err_t result = esp_netif_sntp_sync_wait(pdMS_TO_TICKS(timeout_ms));
    esp_netif_sntp_deinit();
    if (result != ESP_OK) {
        snprintf(out, capacity, "clock=sync_failed reason=timeout");
        return -1;
    }
    if (!tls_clock_is_sane(time_sync_now())) {
        /* A reply arrived but produced an implausible time. Saying so beats letting the handshake
         * fail later with a validity error that points at the certificate. */
        snprintf(out, capacity, "clock=sync_failed reason=implausible unix=%lld",
                 (long long)time_sync_now());
        return -1;
    }
    snprintf(out, capacity, "clock=unix=%lld synced=yes", (long long)time_sync_now());
    return 0;
}
