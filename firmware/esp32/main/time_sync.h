#ifndef TIME_SYNC_H
#define TIME_SYNC_H
#include <stddef.h>
#include <stdint.h>

/* The clock, brought up before anything validates a certificate.
 *
 * An ESP32 has no battery-backed clock, so it starts at the epoch. mbedTLS then rejects every
 * certificate in its validity window as "not yet valid", which is a correct answer to the question
 * it was asked and a completely misleading one to read: it looks like a trust or a date problem on
 * the certificate when the certificate is fine and the device simply does not know what year it is.
 * The clock is therefore checked before the handshake and the check is reported with the result.
 */
int time_sync_wait(int timeout_ms, char *out, size_t capacity);
int64_t time_sync_now(void);

#endif
