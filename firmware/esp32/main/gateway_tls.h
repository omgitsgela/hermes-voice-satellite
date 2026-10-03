#ifndef GATEWAY_TLS_H
#define GATEWAY_TLS_H
#include <stddef.h>

/* The registry link over TLS.
 *
 * Same handshake as the plaintext transport (see gateway_link.h), with a different carrier: mbedTLS
 * with the client certificate the bridge identifies this device by, the CA that signed the bridge's
 * certificate, and the pinned fingerprint of that certificate. The clock is brought up before the
 * handshake, because mbedTLS rejects every certificate in its validity window when the device still
 * believes it is 1970, and a validity error reads nothing like a clock problem.
 *
 * Report lines name the transport, the clock, the negotiated cipher suite and the pin result, so a
 * successful run says which of those actually happened rather than only that it worked.
 *
 * Returns 0 when the session reached REGISTERED. */
int gateway_tls_run(const char *host, unsigned port, char *out, size_t capacity);

#endif
