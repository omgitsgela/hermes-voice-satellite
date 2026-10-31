#ifndef GATEWAY_TLS_H
#define GATEWAY_TLS_H
#include <stddef.h>
#include "gateway_socket.h"

/* The registry link over TLS.
 *
 * Same handshake as the plaintext transport (see gateway_link.h), with a different carrier: mbedTLS
 * with the client certificate the Satellite Gateway identifies this device by, the CA that signed the Satellite Gateway's
 * certificate, and the pinned fingerprint of that certificate. The clock is brought up before the
 * handshake, because mbedTLS rejects every certificate in its validity window when the device still
 * believes it is 1970, and a validity error reads nothing like a clock problem.
 *
 * Report lines name the transport, the clock, the negotiated cipher suite and the pin result, so a
 * successful run says which of those actually happened rather than only that it worked.
 */

/* A live, already-handshaked TLS link: the dialled socket plus the mbedtls contexts that hold the
 * session. The handshake -- and with it the pin check and the clock check -- happens in
 * gateway_tls_establish, which is the one place it is implemented. The one-shot console path and
 * the supervisor's persistent path both establish through it, so they cannot drift apart.
 *
 * What makes a link one-shot or persistent is not the wrapper but the socket's deadline: while it
 * is finite, a stalled peer eventually errors the read (the console path, where a stalled peer must
 * not hold the port); when it is raised to gateway_socket's "no deadline", the read blocks until a
 * real end of stream (the persistent path, where an idle registered link is the healthy state). */
typedef struct gateway_tls_link {
    gateway_socket socket;
    void *contexts; /* the mbedtls contexts, in PSRAM; NULL when nothing is established */
} gateway_tls_link;

/* Dial, and (for TLS) complete the handshake including the pinned-fingerprint check. On success
 * returns 0 with `out` holding a live link whose socket deadline is the dial deadline (so the
 * handshake was bounded); on failure returns -1 and appends the same report lines the one-shot
 * path always emitted. The caller owns the link until it calls gateway_tls_release. */
int gateway_tls_establish(const char *host, unsigned port, gateway_tls_link *out, char *report,
                          size_t capacity);

/* Close a link established above: best-effort close_notify (bounded by the link's deadline),
 * then free the contexts and the socket. The one-shot console path; safe on a
 * partially-established or empty link. The persistent path tears down instead with
 * gateway_tls_close_socket, a stop of the capture task, and gateway_tls_free, in that order. */
void gateway_tls_release(gateway_tls_link *link);
/* Close the carrier only (no farewell alert): the first step of a persistent teardown, so an
 * in-flight write unwinds on EBADF before anything is freed. Idempotent. */
void gateway_tls_close_socket(gateway_tls_link *link);
/* Free the contexts (and close the socket if still open). The caller guarantees no write is in
 * flight: persistent teardown calls this only after the socket is closed and the capture task
 * stopped and joined. Idempotent. */
void gateway_tls_free(gateway_tls_link *link);

/* Read and write one record over an established link. WANT_READ/WANT_WRITE loop, because the call
 * that blocked is the socket read under the mbedTLS record. The context handed in is the link, so
 * the same two functions serve the one-shot register call and the supervisor's persistent link
 * alike (both hand back an opaque pointer, hence the void* first parameter). */
int gateway_tls_link_read(void *context, void *data, size_t size);
int gateway_tls_link_write(void *context, const void *data, size_t size);
/* Raise (or lower) the link's socket deadline. The persistent path sets this to "no deadline"
 * after the handshake so the session's reads block until a real end of stream. */
void gateway_tls_link_set_deadline(gateway_tls_link *link, int64_t deadline_us);

/* The one-shot registry run: establish, drive one session to registration over the link with the
 * bounded deadline the console needs, then release. Returns 0 when the session reached REGISTERED. */
int gateway_tls_run(const char *host, unsigned port, char *out, size_t capacity);

#endif
