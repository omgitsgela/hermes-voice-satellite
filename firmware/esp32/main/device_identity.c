#include "device_identity.h"
#include "esp_mac.h"
#include "esp_random.h"
#include "esp_err.h"
#include <stdio.h>

int device_identity(char *device_id, size_t device_id_capacity,
                    char *boot_id, size_t boot_id_capacity) {
    if (!device_id || !boot_id || device_id_capacity == 0 || boot_id_capacity == 0) return -1;
    uint8_t mac[6];
    if (esp_read_mac(mac, ESP_MAC_EFUSE_FACTORY) != ESP_OK) return -1;
    int written = snprintf(device_id, device_id_capacity, "esp32-%02x%02x%02x%02x%02x%02x",
                           mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    if (written <= 0 || (size_t)written >= device_id_capacity) return -1;
    /* Non-secret boot discriminator, not a cryptographic identity or credential.
     * RNG entropy is not guaranteed when radio entropy sources are inactive. */
    written = snprintf(boot_id, boot_id_capacity, "%08lx%08lx%08lx%08lx",
                       (unsigned long)esp_random(), (unsigned long)esp_random(),
                       (unsigned long)esp_random(), (unsigned long)esp_random());
    if (written <= 0 || (size_t)written >= boot_id_capacity) return -1;
    return 0;
}
