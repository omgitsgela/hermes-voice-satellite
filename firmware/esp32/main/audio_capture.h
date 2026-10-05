#ifndef AUDIO_CAPTURE_H
#define AUDIO_CAPTURE_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* The microphone capture task: owns the I2S RX from the XVF3800 for the life of a registered
 * session and, while the session is streaming, converts the 32-bit stereo stream the codec
 * delivers into the 16-bit mono chunks the Wyoming mic contract speaks, sending each one
 * through the session's send-audio path (which holds the writer lock for the whole frame).

 * The task is started by the armed hook, the moment the bridge's run-satellite makes the
 * session armed, and stopped by the link-down hook. It keeps reading while the stream is
 * closed (the audio is dropped, which also keeps the DMA from wedging) and drops everything
 * still buffered the moment the stream opens, so a fresh utterance never carries the
 * pre-wake chime.

 * Device-only: nothing here is host-testable. It is proven by the firmware build and the
 * symbols in the ELF; that the microphone actually produces audio is the live-test gate.
 */

/* One I2S read fills this many 32-bit stereo frames (160 = 10 ms at 16 kHz). One read is one
 * audio-chunk -- 160 samples, 320 bytes, a 10 ms cadence for low latency, well under the
 * WY_AUDIO_CHUNK_SAMPLES (1024) bound the session enforces, so no per-chunk buffering is needed. */
#define AUDIO_CAPTURE_FRAMES 160u

/* Convert one I2S read into mono and send it if the stream is open. Exposed separately so the
 * read/convert/send sequence can be read top to bottom; not for host use. */
void audio_capture_send_chunk(const int32_t *frames, size_t frame_count);

/* Install the session's writer lock (the FreeRTOS mutex the capture task and the gateway task
 * share) into the session buffer the supervisor owns. Called before any session is driven. */
void audio_capture_install_writer_lock(void *session);

/* Start the I2S RX and the capture task. `session`, `user` and `write` describe the live link:
 * every chunk goes through wy_session_send_audio on that session over that writer. Returns 0
 * when the task is running, -1 when the I2S channel could not be created or the task could not
 * be spawned (the link itself is not touched: the session carries on without a microphone). */
int audio_capture_start(void *session, void *user,
                        int (*write)(void *, const void *, size_t));

/* Stop the task and release the I2S channel. Idempotent: safe to call when nothing is
 * running, and safe to call from the link-down path while the task is mid-read. */
void audio_capture_stop(void);

bool audio_capture_running(void);

/* Bounded confirmation that the writer lock is free (no write in flight); the TLS teardown
 * gates its contexts free on this. 1 when free within the 2 s bound, 0 otherwise (the caller
 * then defers the free to the next establish). */
bool audio_capture_wait_writer_idle(void);
#endif
