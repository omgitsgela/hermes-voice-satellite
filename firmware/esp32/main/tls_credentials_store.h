#ifndef TLS_CREDENTIALS_STORE_H
#define TLS_CREDENTIALS_STORE_H
#include "tls_credentials.h"
#include <stddef.h>

/* Provisioned TLS material, read from flash.
 *
 * It lives at the head of the `storage` data partition, written by scripts/provision-device-tls.py
 * over the USB download path rather than through the console: the material includes a private key,
 * and a key that was never typed into a line protocol has no chance of being echoed into a log,
 * a shell history or a terminal capture. The same reasoning as the Wi-Fi passphrase, one level up.
 *
 * The buffer is static because mbedTLS parses certificates out of it during the handshake, so the
 * credentials must outlive this call. The returned struct points into that buffer and is valid
 * until the next load.
 */
int tls_credentials_load(tls_credentials *out, char *reason, size_t reason_capacity);

/* Report what is in the region without dialling anything, for the console: what was provisioned,
 * for which bridge, and with which pinned fingerprint. Returns 0 when an intact blob is there and
 * fills `out` when a caller wants the details; `reason` explains anything that is not intact. */
int tls_credentials_probe(tls_credentials *out, char *reason, size_t reason_capacity);

#endif
