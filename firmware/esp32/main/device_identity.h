#ifndef DEVICE_IDENTITY_H
#define DEVICE_IDENTITY_H
#include <stddef.h>

/* The same identity the USB path registers with, so the gateway sees one device whichever
 * way it arrives. The boot token is a non-secret discriminator, not a credential.
 * Returns 0 on success and -1 when the factory MAC cannot be read, which makes the identity
 * unprovable rather than merely different. */
int device_identity(char *device_id, size_t device_id_capacity,
                    char *boot_id, size_t boot_id_capacity);

#endif
