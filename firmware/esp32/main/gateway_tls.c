#include "gateway_tls.h"
#include "gateway_link.h"
#include "gateway_socket.h"
#include "time_sync.h"
#include "tls_credentials_store.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/md.h"
#include "mbedtls/pk.h"
#include "mbedtls/ssl.h"
#include "mbedtls/x509_crt.h"
#include <stdio.h>
#include <string.h>

#define TLS_DIAL_TIMEOUT_MS 8000
#define TLS_HANDSHAKE_TIMEOUT_MS 20000
#define TLS_RUN_BUDGET_MS 25000
#define TLS_CLOCK_WAIT_MS 15000
#define TLS_CLOSE_NOTIFY_MS 2000

/* The mbedTLS contexts are large, and the console runs on the main task's stack: they live in PSRAM
 * instead, which is also where the handshake's record buffers are most likely to land. */
typedef struct {
    mbedtls_ssl_context ssl;
    mbedtls_ssl_config config;
    mbedtls_x509_crt ca;
    mbedtls_x509_crt client_cert;
    mbedtls_pk_context client_key;
} tls_contexts;

/* The pin check, wired into the handshake rather than run after it.
 *
 * mbedTLS frees the peer's certificate as soon as verification finishes unless
 * MBEDTLS_SSL_KEEP_PEER_CERTIFICATE is on, and this build leaves that off: the stack is asked to
 * look at the certificate while it still has it. That is the documented way to inspect a peer
 * certificate for one handshake, and it is also the stronger arrangement, because a fingerprint
 * that does not match sets a verification flag and the handshake fails there and then, rather than
 * a wrong bridge being discovered after the session was already established.
 *
 * Certificate depth is counted from the bottom: depth 0 is the peer's own certificate, which is
 * the one the pin is about, and deeper certificates are the chain up to the trusted root.
 */
typedef struct {
    const unsigned char *expected;
    unsigned char presented[TLS_SERVER_PIN_LEN];
    int leaf_seen;
    int matched;
    int digest_failed;
} tls_pin_check;

static int pin_the_server_certificate(void *parameter, mbedtls_x509_crt *certificate, int depth,
                                     uint32_t *flags) {
    tls_pin_check *check = parameter;
    if (depth != 0 || !certificate || !certificate->raw.p || certificate->raw.len == 0) return 0;
    check->leaf_seen = 1;
    const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    if (!info || mbedtls_md_get_size(info) != TLS_SERVER_PIN_LEN ||
        mbedtls_md(info, certificate->raw.p, certificate->raw.len, check->presented) != 0) {
        check->digest_failed = 1;
        *flags |= MBEDTLS_X509_BADCERT_OTHER;
        return 0;
    }
    check->matched = tls_server_pin_matches(check->presented, check->expected);
    if (!check->matched) {
        /* Not the certificate this device was provisioned to trust. Returning 0 with a flag set is
         * how a verify callback refuses a certificate: only a fatal error aborts differently. */
        *flags |= MBEDTLS_X509_BADCERT_OTHER;
    }
    return 0;
}

/* mbedTLS writes and reads through the socket the carrier set, and the socket decides whether the
 * call is bounded or open: during the handshake the deadline is finite (a stalled peer must not
 * outlive the budget), and once a persistent session raises it to "no deadline" the read blocks
 * until a real end of stream, which is the healthy idle state. One bio, both regimes. */
static int tls_send(void *context, const unsigned char *buffer, size_t length) {
    gateway_socket *socket = context;
    if (socket->deadline_us == GATEWAY_SOCKET_NO_DEADLINE)
        return gateway_socket_write_persistent(socket, buffer, length);
    return gateway_socket_write(socket, buffer, length);
}

static int tls_receive(void *context, unsigned char *buffer, size_t length) {
    gateway_socket *socket = context;
    if (socket->deadline_us == GATEWAY_SOCKET_NO_DEADLINE)
        return gateway_socket_read_persistent(socket, buffer, length);
    return gateway_socket_read(socket, buffer, length);
}

/* Read and write one record over an established link. WANT_READ/WANT_WRITE mean the record needs
 * more of the stream, and the call before it blocked inside the socket callback, so these loops
 * cannot spin. The context handed in is the link, so the same two functions serve the one-shot
 * register call and the supervisor's persistent link alike. */
int gateway_tls_link_read(void *context, void *data, size_t size) {
    gateway_tls_link *link = context;
    mbedtls_ssl_context *ssl = &((tls_contexts *)link->contexts)->ssl;
    for (;;) {
        int count = mbedtls_ssl_read(ssl, data, size);
        if (count > 0) return count;
        if (count == MBEDTLS_ERR_SSL_WANT_READ || count == MBEDTLS_ERR_SSL_WANT_WRITE) continue;
        return -1;
    }
}

int gateway_tls_link_write(void *context, const void *data, size_t size) {
    gateway_tls_link *link = context;
    mbedtls_ssl_context *ssl = &((tls_contexts *)link->contexts)->ssl;
    for (;;) {
        int count = mbedtls_ssl_write(ssl, data, size);
        if (count > 0) return count;
        if (count == MBEDTLS_ERR_SSL_WANT_READ || count == MBEDTLS_ERR_SSL_WANT_WRITE) continue;
        return -1;
    }
}

void gateway_tls_link_set_deadline(gateway_tls_link *link, int64_t deadline_us) {
    if (link) link->socket.deadline_us = deadline_us;
}

static void contexts_free(tls_contexts *contexts) {
    if (!contexts) return;
    mbedtls_ssl_free(&contexts->ssl);
    mbedtls_ssl_config_free(&contexts->config);
    mbedtls_x509_crt_free(&contexts->ca);
    mbedtls_x509_crt_free(&contexts->client_cert);
    mbedtls_pk_free(&contexts->client_key);
    heap_caps_free(contexts);
}

/* Dial and complete the TLS handshake -- the one place the handshake and the pin check live, so
 * the one-shot console path and the supervisor's persistent path cannot drift apart. On success
 * the link's socket keeps the dial deadline (still finite), and the caller decides the session's
 * deadline: the one-shot leaves it, the persistent path raises it to "no deadline" so an idle link
 * blocks instead of erroring. Appends the same report lines the run always emitted. */
int gateway_tls_establish(const char *host, unsigned port, gateway_tls_link *out, char *report,
                          size_t capacity) {
    if (!host || !host[0] || !out) return -1;
    memset(out, 0, sizeof(*out));
    out->socket.descriptor = -1;

    char line[200];
    tls_credentials credentials;
    char reason[96];
    if (report && capacity > 0 &&
        tls_credentials_load(&credentials, reason, sizeof(reason)) != 0) {
        snprintf(line, sizeof(line), "tls=failed reason=no_credentials detail=%s", reason);
        gateway_report_append(report, capacity, line);
        return -1;
    }
    tls_pin_check check = {.expected = credentials.server_pin};

    /* The clock first, and reported whether or not it worked: mbedTLS answers a certificate whose
     * validity window the device cannot judge with the same error it uses for a certificate that is
     * genuinely wrong, so the state of the clock has to be visible next to the connection result. */
    char clock[128];
    if (time_sync_wait(TLS_CLOCK_WAIT_MS, clock, sizeof(clock)) != 0) {
        gateway_report_append(report, capacity, clock);
        gateway_report_append(report, capacity, "tls=failed reason=clock");
        return -1;
    }
    gateway_report_append(report, capacity, clock);
    if (!tls_clock_is_sane(time_sync_now())) {
        gateway_report_append(report, capacity, "tls=failed reason=clock");
        return -1;
    }

    int64_t dial_deadline = gateway_now_us() + (int64_t)TLS_DIAL_TIMEOUT_MS * 1000;
    if (gateway_socket_dial(host, port, dial_deadline, &out->socket) != 0) {
        snprintf(line, sizeof(line), "socket=failed host=%s port=%u transport=tls", host, port);
        gateway_report_append(report, capacity, line);
        return -1;
    }
    snprintf(line, sizeof(line), "socket=connected host=%s port=%u transport=tls", host, port);
    gateway_report_append(report, capacity, line);

    tls_contexts *contexts = heap_caps_calloc(1, sizeof(tls_contexts), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!contexts) {
        gateway_report_append(report, capacity, "tls=failed reason=no_memory");
        gateway_socket_close(&out->socket);
        return -1;
    }

    int established = -1;
    do {
        mbedtls_ssl_init(&contexts->ssl);
        mbedtls_ssl_config_init(&contexts->config);
        mbedtls_x509_crt_init(&contexts->ca);
        mbedtls_x509_crt_init(&contexts->client_cert);
        mbedtls_pk_init(&contexts->client_key);

        /* The PEM lengths in the credential blob count their own terminators, so mbedTLS is handed
         * exactly the strings this device provisioned, with no second copy to keep in step. */
        if (mbedtls_x509_crt_parse(&contexts->ca, (const unsigned char *)credentials.ca,
                                   credentials.ca_len) != 0) {
            gateway_report_append(report, capacity, "tls=failed reason=ca_parse");
            break;
        }
        if (mbedtls_x509_crt_parse(&contexts->client_cert, (const unsigned char *)credentials.cert,
                                   credentials.cert_len) != 0) {
            gateway_report_append(report, capacity, "tls=failed reason=client_cert_parse");
            break;
        }
        if (mbedtls_pk_parse_key(&contexts->client_key, (const unsigned char *)credentials.key,
                                 credentials.key_len, NULL, 0) != 0) {
            gateway_report_append(report, capacity, "tls=failed reason=client_key_parse");
            break;
        }
        if (mbedtls_ssl_config_defaults(&contexts->config, MBEDTLS_SSL_IS_CLIENT,
                                        MBEDTLS_SSL_TRANSPORT_STREAM,
                                        MBEDTLS_SSL_PRESET_DEFAULT) != 0) {
            gateway_report_append(report, capacity, "tls=failed reason=config");
            break;
        }
        /* The bridge's side of this link has a TLS 1.2 floor, and so does this one: an old
         * negotiation is a downgrade on both ends or on neither. */
        mbedtls_ssl_conf_min_tls_version(&contexts->config, MBEDTLS_SSL_VERSION_TLS1_2);
        mbedtls_ssl_conf_authmode(&contexts->config, MBEDTLS_SSL_VERIFY_REQUIRED);
        mbedtls_ssl_conf_ca_chain(&contexts->config, &contexts->ca, NULL);
        /* Registered before the handshake because it runs during it: this is where the pinned
         * fingerprint is compared, against the certificate while mbedTLS still holds it. */
        mbedtls_ssl_conf_verify(&contexts->config, pin_the_server_certificate, &check);
        if (mbedtls_ssl_conf_own_cert(&contexts->config, &contexts->client_cert,
                                      &contexts->client_key) != 0) {
            gateway_report_append(report, capacity, "tls=failed reason=own_cert");
            break;
        }
        if (mbedtls_ssl_setup(&contexts->ssl, &contexts->config) != 0) {
            gateway_report_append(report, capacity, "tls=failed reason=setup");
            break;
        }
        /* No server name is set, on purpose, and mbedTLS requires that to be said out loud rather
         * than assumed. A name would be the weaker check here: this bridge is identified by the
         * fingerprint of its certificate, below, which admits exactly one certificate, where a name
         * would admit any certificate this CA ever signs for that name. */
        if (mbedtls_ssl_set_hostname(&contexts->ssl, NULL) != 0) {
            gateway_report_append(report, capacity, "tls=failed reason=hostname");
            break;
        }
        /* The timeout callback is left NULL on purpose: the deadline for this session lives inside
         * the socket callbacks. Handing mbedTLS a second, relative notion of time would give one
         * session two clocks that can disagree about when it is over. */
        mbedtls_ssl_set_bio(&contexts->ssl, &out->socket, tls_send, tls_receive, NULL);

        int64_t deadline = gateway_now_us() + (int64_t)TLS_HANDSHAKE_TIMEOUT_MS * 1000;
        int handshake;
        do {
            handshake = mbedtls_ssl_handshake(&contexts->ssl);
        } while ((handshake == MBEDTLS_ERR_SSL_WANT_READ || handshake == MBEDTLS_ERR_SSL_WANT_WRITE) &&
                 gateway_millis_left(deadline) > 0);
        if (handshake != 0) {
            /* A pin mismatch fails the handshake, so the interesting question after a failure is
             * whether the certificate was the reason: saying so beats reporting a handshake error
             * for a bridge this device was simply never provisioned to talk to. */
            if (check.digest_failed) {
                snprintf(line, sizeof(line), "tls=failed reason=pin_compute");
            } else if (check.leaf_seen && !check.matched) {
                snprintf(line, sizeof(line),
                         "tls=failed reason=server_pin presented=%02x%02x%02x%02x",
                         check.presented[0], check.presented[1], check.presented[2],
                         check.presented[3]);
            } else {
                snprintf(line, sizeof(line), "tls=failed reason=handshake error=-0x%04x",
                         (unsigned int)(-handshake));
            }
            gateway_report_append(report, capacity, line);
            break;
        }

        uint32_t verify_flags = mbedtls_ssl_get_verify_result(&contexts->ssl);
        if (verify_flags != 0) {
            /* VERIFY_REQUIRED aborts the handshake on a bad chain, so this is a second look for the
             * record rather than the only guard. */
            snprintf(line, sizeof(line), "tls=failed reason=certificate flags=0x%08lx",
                     (unsigned long)verify_flags);
            gateway_report_append(report, capacity, line);
            break;
        }
        snprintf(line, sizeof(line), "tls=handshake ok cipher=%s",
                 mbedtls_ssl_get_ciphersuite(&contexts->ssl));
        gateway_report_append(report, capacity, line);
        gateway_report_append(report, capacity, "tls=server_pin matched");
        out->contexts = contexts;
        established = 0;
    } while (0);

    if (established != 0) {
        contexts_free(contexts);
        gateway_socket_close(&out->socket);
        out->socket.descriptor = -1;
    }
    return established;
}

/* Close a link established above: best-effort close_notify (bounded, so a gone bridge cannot hold
 * the caller), then free the contexts and the socket. Safe on a partially-established link. */
void gateway_tls_release(gateway_tls_link *link) {
    if (!link) return;
    if (link->contexts) {
        tls_contexts *contexts = link->contexts;
        int64_t close_deadline = gateway_now_us() + (int64_t)TLS_CLOSE_NOTIFY_MS * 1000;
        while (gateway_millis_left(close_deadline) > 0) {
            int notify = mbedtls_ssl_close_notify(&contexts->ssl);
            if (notify != MBEDTLS_ERR_SSL_WANT_READ && notify != MBEDTLS_ERR_SSL_WANT_WRITE) break;
        }
        contexts_free(contexts);
        link->contexts = NULL;
    }
    gateway_socket_close(&link->socket);
}

int gateway_tls_run(const char *host, unsigned port, char *out, size_t capacity) {
    if (!out || capacity == 0) return -1;
    out[0] = 0;
    gateway_tls_link link = {.socket = {.descriptor = -1}};
    if (gateway_tls_establish(host, port, &link, out, capacity) != 0) return -1;

    /* Same handshake as the plaintext transport, over this carrier: one implementation of the
     * session, two ways of carrying it. The console path keeps the bounded deadline establish set,
     * so a stalled peer cannot hold the port. */
    int result = gateway_link_register(gateway_tls_link_read, gateway_tls_link_write, &link,
                                       gateway_now_us() + (int64_t)TLS_RUN_BUDGET_MS * 1000, out,
                                       capacity);

    char line[160];
    snprintf(line, sizeof(line), "resource=stack_min_free=%u",
             (unsigned int)uxTaskGetStackHighWaterMark(NULL));
    gateway_report_append(out, capacity, line);
    gateway_tls_release(&link);
    return result;
}
