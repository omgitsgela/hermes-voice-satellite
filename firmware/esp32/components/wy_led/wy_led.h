/* wy_led -- the satellite's LED vocabulary, as a host-testable state machine.
 *
 * WHY THIS EXISTS AS A PURE CORE
 * -----------------------------
 * The ring is 12 x WS2812 driven by the XMOS chip, not by this CPU. Control reaches it over the
 * SAME I2C bus that already configures the audio (SDA 5 / SCL 6, address 0x2C, resource 20), so
 * there is no new wiring and no new code path: the LEDs are one more XVF control write. That makes
 * the whole feature testable off-device, which is the point of keeping the policy here and the
 * I2C bytes in the glue (main/led_status.c).
 *
 * THE VOCABULARY IS THE REFERENCE SATELLITE'S
 * ---------------------------------------
 * Every colour and every meaning is copied from pi/2mic_service.py, so the two satellites read the
 * same from across a room. Someone who learns the lights on one device has learned them on both.
 * If you change a colour here, change it there and in docs/leds.md too.
 *
 * WHAT THE HARDWARE CAN AND CANNOT DO
 * ----------------------------------
 * The XMOS exposes EFFECT-level control, not per-pixel control: there is no "make LED 7 full and
 * LED 6 half". The ring runs one of five effects -- off, breath, rainbow, single colour, doa --
 * and we choose which. The direction-of-arrival effect centres its highlight on the auto-selected
 * beam for us, which is why "point at the person who is talking" needs no angle maths of our own.
 * A fully bespoke per-pixel animation would mean reflashing the XMOS, which we do not do.
 *
 * TIMING IS INJECTED
 * ------------------
 * wy_led_tick() advances every timer. The caller decides how often; 100 ms is plenty, since the
 * slowest event here is the 30 s disconnect grace. Nothing in this file sleeps or owns a clock,
 * so the tests can drive time exactly and assert the grace period rather than hope for it. */
#ifndef WY_LED_H
#define WY_LED_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* --- the XMOS control plane these commands ride on (see wyoming_audio.h for the I2C facts) --- */
#define WY_LED_XVF_ADDR        0x2C
#define WY_LED_RESID           20    /* the GPO Servicer resource */

/* Command IDs on that resource. The first five are taken from Seeed's own published Arduino
 * example for this board, so they are not guesses. */
#define WY_LED_CMD_EFFECT      12
#define WY_LED_CMD_BRIGHTNESS  13
#define WY_LED_CMD_GAMMIFY     14    /* PROVISIONAL: inferred from the host_control command order */
#define WY_LED_CMD_SPEED       15
#define WY_LED_CMD_COLOR       16
#define WY_LED_CMD_DOA_COLOR   17    /* PROVISIONAL: inferred from the host_control command order */

/* LED_EFFECT values. This is the device's entire expressive range. */
#define WY_LED_FX_OFF          0
#define WY_LED_FX_BREATH       1
#define WY_LED_FX_RAINBOW      2
#define WY_LED_FX_SINGLE       3
#define WY_LED_FX_DOA          4

/* --- the palette: identical to pi/2mic_service.py --- */
#define WY_LED_BLACK_R   0u    /* idle, and the end of a turn */
#define WY_LED_BLACK_G   0u
#define WY_LED_BLACK_B   0u
#define WY_LED_WHITE_R   255u  /* startup pulse */
#define WY_LED_WHITE_G   255u
#define WY_LED_WHITE_B   255u
#define WY_LED_RED_R     255u  /* a real outage, never a blink */
#define WY_LED_RED_G     0u
#define WY_LED_RED_B     0u
#define WY_LED_YELLOW_R  255u  /* the microphone is open */
#define WY_LED_YELLOW_G  255u
#define WY_LED_YELLOW_B  0u
#define WY_LED_BLUE_R    0u    /* wake detected */
#define WY_LED_BLUE_G    0u
#define WY_LED_BLUE_B    255u
#define WY_LED_GREEN_R   0u    /* a transcript arrived */
#define WY_LED_GREEN_G   255u
#define WY_LED_GREEN_B   0u
#define WY_LED_FOLLOWUP_R 0u   /* dim teal: listening for a follow-up, steady */
#define WY_LED_FOLLOWUP_G 64u
#define WY_LED_FOLLOWUP_B 64u
#define WY_LED_WORKING_R 150u  /* violet: thinking about the answer */
#define WY_LED_WORKING_G 70u
#define WY_LED_WORKING_B 255u

/* --- timings, matching pi/2mic_service.py --- */
#define WY_LED_DISCONNECT_GRACE_MS   30000u  /* red only after a real outage, not a blip */
#define WY_LED_FLASH_ON_MS           300u    /* the first-connect green triple-flash */
#define WY_LED_FLASH_OFF_MS          300u
#define WY_LED_FLASH_COUNT           3u
#define WY_LED_BRIEF_MS              1000u   /* the green "show for 1 sec" */
/* The circular sweep is measured from the moment the tone is QUEUED, not from the moment it is
 * heard: the pump has to prime its DMA and settle the amplifier first, which typically costs
 * 50-150 ms. The rendered tone is 308 ms (4928 samples at 16 kHz), so 400 ms covers the audible
 * window rather than the render length. See docs/leds.md for why this is not simply 308. */
#define WY_LED_WAKE_SWEEP_MS          400u
#define WY_LED_VOICE_MS               500u   /* the "I hear you" beat between tone and query */
#define WY_LED_TICK_MS_DEFAULT       100u    /* how often the glue should call wy_led_tick */

/* --- brightness contract ---
 * The fleet-wide setting `led_brightness` is 1-31 (the APA102 global-brightness scale the Pi uses,
 * and the scale documented in docs/configuration-plane.md). This ring wants 0-255. The device
 * adapter scales; the fleet contract does not change per model. 31 is full. */
#define WY_LED_BRIGHTNESS_MIN  1u
#define WY_LED_BRIGHTNESS_MAX  31u
uint8_t wy_led_scale_brightness(unsigned level_1_31);

/* Injected emit: one XVF control write. Returns 0 on success. */
typedef int (*wy_led_emit_fn)(void *user, uint8_t resid, uint8_t cmd,
                              const uint8_t *params, size_t nparams);

typedef struct wy_led wy_led;

size_t wy_led_size(void);
int wy_led_init(wy_led *led, wy_led_emit_fn emit, void *user);

/* The lifecycle. Each returns 0 on success. The names are the meanings, not the colours, so a
 * caller never has to know what "violet" is supposed to signify. */
int wy_led_boot(wy_led *led);        /* startup: white breath, once per boot */
int wy_led_connected(wy_led *led, int first_this_boot);
int wy_led_disconnected(wy_led *led);/* starts the grace timer; red only if it stays away */
int wy_led_wake(wy_led *led);        /* circular sweep during the activation tone */
int wy_led_listening(wy_led *led);   /* direction of arrival, bright on the speaker */
int wy_led_voice_started(wy_led *led);
int wy_led_transcript(wy_led *led);  /* green, briefly */
int wy_led_thinking(wy_led *led);    /* violet breath */
int wy_led_speaking(wy_led *led);    /* the answer is audible */
int wy_led_followup(wy_led *led);    /* dim teal, steady */
int wy_led_idle(wy_led *led);        /* dark */
int wy_led_error(wy_led *led);       /* red, immediately: something is actually wrong */

/* Advance every timer. elapsed_ms is how much time has passed since the previous call. */
int wy_led_tick(wy_led *led, uint32_t elapsed_ms);

/* Brightness on the fleet's 1-31 scale. */
int wy_led_set_brightness(wy_led *led, unsigned level_1_31);

/* Introspection, for the tests and for the provisioning console. */
unsigned wy_led_state(const wy_led *led);
int wy_led_red_pending(const wy_led *led);

/* The states, for logging and tests. */
enum {
    WY_LED_STATE_OFF = 0,
    WY_LED_STATE_BOOT,
    WY_LED_STATE_IDLE,
    WY_LED_STATE_WAKE,        /* rainbow sweep during the activation tone */
    WY_LED_STATE_LISTENING,   /* doa, centred on the speaker */
    WY_LED_STATE_VOICE,
    WY_LED_STATE_TRANSCRIPT,
    WY_LED_STATE_THINKING,
    WY_LED_STATE_SPEAKING,
    WY_LED_STATE_FOLLOWUP,
    WY_LED_STATE_ERROR
};

#ifdef __cplusplus
}
#endif
#endif /* WY_LED_H */
