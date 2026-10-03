#include "tls_credentials_store.h"
#include "esp_partition.h"
#include <stdio.h>
#include <string.h>

/* The region is read into a static buffer, and the parsed credentials point into it: mbedTLS
 * parses the certificates out of this memory during the handshake, so it has to outlive the load
 * rather than being a local of the function that read it. */
static unsigned char region[TLS_BLOB_REGION_BYTES];
static tls_credentials loaded;
static int have_loaded = 0;

static const esp_partition_t *storage_partition(void) {
    /* Matched by name rather than by subtype: the name is what the layout promises, and the
     * subtype label is a hint about what the partition could hold, not a promise about this one. */
    return esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY, "storage");
}

static int read_and_parse(char *reason, size_t reason_capacity) {
    const esp_partition_t *part = storage_partition();
    if (!part) {
        snprintf(reason, reason_capacity, "no storage partition in the layout");
        return -1;
    }
    if (part->size < TLS_BLOB_REGION_BYTES) {
        snprintf(reason, reason_capacity, "storage partition is smaller than the credential region");
        return -1;
    }
    if (esp_partition_read(part, 0, region, TLS_BLOB_REGION_BYTES) != ESP_OK) {
        snprintf(reason, reason_capacity, "credential region unreadable");
        return -1;
    }
    /* An erased region reads as 0xFF. That is a device that was never provisioned, which deserves
     * to be told apart from one whose blob is damaged. */
    int blank = 1;
    for (size_t index = 0; index < TLS_BLOB_REGION_BYTES; index++) {
        if (region[index] != 0xFF) {
            blank = 0;
            break;
        }
    }
    if (blank) {
        snprintf(reason, reason_capacity, "none provisioned");
        return -1;
    }
    /* The whole region is handed over, and the parser refuses anything that claims to extend past
     * it while ignoring the zero padding after the blob. */
    if (tls_credentials_parse(region, TLS_BLOB_REGION_BYTES, &loaded) != 0) {
        snprintf(reason, reason_capacity, "blob failed validation");
        return -1;
    }
    have_loaded = 1;
    return 0;
}

int tls_credentials_load(tls_credentials *out, char *reason, size_t reason_capacity) {
    if (!out) return -1;
    if (reason && reason_capacity) reason[0] = 0;
    if (!have_loaded && read_and_parse(reason, reason_capacity) != 0) return -1;
    *out = loaded;
    return 0;
}

int tls_credentials_probe(tls_credentials *out, char *reason, size_t reason_capacity) {
    if (reason && reason_capacity) reason[0] = 0;
    if (read_and_parse(reason, reason_capacity) != 0) return -1;
    if (out) *out = loaded;
    return 0;
}
