/* wy_led -- see wy_led.h for why this is a pure, host-testable core. */
#include "wy_led.h"

#include <string.h>

/* The pending timed action. A satellite's light show is almost entirely "show this for a while,
 * then move on", so one small action slot covers every behaviour in the vocabulary without a
 * scheduler or a task. */
enum {
    ACT_NONE = 0,
    ACT_FLASH,          /* the first-connect green triple-flash */
    ACT_BRIEF,          /* show a colour briefly, then go dark */
    ACT_SWEEP,          /* the circular rainbow during the activation tone */
    ACT_VOICE,          /* the yellow "I hear you" beat, then direction mode */
    ACT_DELAYED_RED     /* retained; the outage now lives in its own slot (see outage_ms) */
};

struct wy_led {
    wy_led_emit_fn emit;
    void *user;

    unsigned state;
    uint8_t brightness;      /* the device scale, 0-255 */
    int gammify;

    int act;
    uint32_t act_ms;         /* ms until the current action step completes */
    uint32_t act_step;       /* which step of the action we are on */
    uint32_t act_r, act_g, act_b;
    uint32_t next_state;     /* the state to enter when an ACT_SWEEP completes */

    /* The outage countdown, kept DELIBERATELY apart from the presentation action slot above.
     * These used to share one slot, and any later presentation -- an idle, a flash, anything --
     * silently cancelled a pending red, so a real sustained outage never turned the ring red at
     * all. An outage is a fact about the link, not a thing being shown; it must survive every
     * presentation that happens to occur while it runs. */
    uint32_t outage_ms;      /* >0 while a disconnect is waiting out its grace period */
    int outage;              /* 1 while the link is down, whatever the ring is showing */
    int outage_red;          /* the grace expired: red is showing FOR THE OUTAGE */
    int error_latch;         /* a definite fault is showing: red until the host reconnects */
};

size_t wy_led_size(void) { return sizeof(struct wy_led); }

uint8_t wy_led_scale_brightness(unsigned level_1_31)
{
    if (level_1_31 < WY_LED_BRIGHTNESS_MIN) level_1_31 = WY_LED_BRIGHTNESS_MIN;
    if (level_1_31 > WY_LED_BRIGHTNESS_MAX) level_1_31 = WY_LED_BRIGHTNESS_MAX;
    /* 1-31 -> 0-255. Round to nearest so 31 lands on full and 1 stays visibly lit. */
    return (uint8_t)(((unsigned)level_1_31 * 255u + 15u) / 31u);
}

/* --- the three writes that make up every effect --- */

static int emit(wy_led *led, uint8_t cmd, const uint8_t *params, size_t n)
{
    if (!led || !led->emit) return -1;
    return led->emit(led->user, WY_LED_RESID, cmd, params, n);
}

static int set_effect(wy_led *led, uint8_t effect)
{
    uint8_t p[1] = { effect };
    return emit(led, WY_LED_CMD_EFFECT, p, sizeof(p));
}

static int set_color(wy_led *led, uint32_t r, uint32_t g, uint32_t b)
{
    /* The wire format is R, G, B, reserved -- four bytes, not three. A device that expects four
     * will misread a three-byte write as a truncated command. */
    uint8_t p[4] = { (uint8_t)r, (uint8_t)g, (uint8_t)b, 0x00 };
    return emit(led, WY_LED_CMD_COLOR, p, sizeof(p));
}

static int set_doa_colors(wy_led *led, uint32_t base_r, uint32_t base_g, uint32_t base_b,
                          uint32_t doa_r, uint32_t doa_g, uint32_t doa_b)
{
    /* Two uint32 colours: the quiet ring, then the bright marker at the speaker. Six bytes as
     * R,G,B,R,G,B with no reserved byte between them. */
    uint8_t p[6] = {
        (uint8_t)base_r, (uint8_t)base_g, (uint8_t)base_b,
        (uint8_t)doa_r, (uint8_t)doa_g, (uint8_t)doa_b
    };
    return emit(led, WY_LED_CMD_DOA_COLOR, p, sizeof(p));
}

static int set_brightness(wy_led *led)
{
    uint8_t p[1] = { led->brightness };
    return emit(led, WY_LED_CMD_BRIGHTNESS, p, sizeof(p));
}

static int set_speed(wy_led *led, uint8_t speed)
{
    uint8_t p[1] = { speed };
    return emit(led, WY_LED_CMD_SPEED, p, sizeof(p));
}

/* --- whole looks --- */

static void enter(wy_led *led, unsigned state) { led->state = state; }

/* True while the ring is showing red for a reason nothing else may overwrite: a real outage whose
 * grace has expired, or a definite fault. Only a reconnect releases it. Without this, a stray
 * idle arriving after the red was earned would blank the one message the device is trying to
 * send, and the room would read "everything is fine" while the link has been dead for minutes. */
static int red_owns(const wy_led *led) { return led && (led->outage_red || led->error_latch); }

static int show_off(wy_led *led)
{
    enter(led, WY_LED_STATE_OFF);
    led->act = ACT_NONE;
    return set_effect(led, WY_LED_FX_OFF);
}

static int show_single(wy_led *led, unsigned state,
                       uint32_t r, uint32_t g, uint32_t b, uint8_t speed)
{
    int rc = 0;
    enter(led, state);
    led->act = ACT_NONE;
    rc |= set_color(led, r, g, b);
    rc |= set_speed(led, speed);
    rc |= set_effect(led, WY_LED_FX_SINGLE);
    return rc;
}

static int show_breath(wy_led *led, unsigned state,
                       uint32_t r, uint32_t g, uint32_t b, uint8_t speed)
{
    int rc = 0;
    enter(led, state);
    led->act = ACT_NONE;
    rc |= set_color(led, r, g, b);
    rc |= set_speed(led, speed);
    rc |= set_effect(led, WY_LED_FX_BREATH);
    return rc;
}

static int show_sweep(wy_led *led, unsigned state, uint8_t speed)
{
    int rc = 0;
    enter(led, state);
    led->act = ACT_NONE;
    rc |= set_speed(led, speed);
    rc |= set_effect(led, WY_LED_FX_RAINBOW);
    return rc;
}

static int show_doa(wy_led *led, unsigned state, uint8_t speed)
{
    int rc = 0;
    enter(led, state);
    led->act = ACT_NONE;
    /* A dim ring with a bright marker at the direction of the speaker: this is the whole point of
     * the direction-finding ask, and the XMOS computes the angle for us. */
    rc |= set_doa_colors(led,
                         WY_LED_BLACK_R, WY_LED_BLACK_G, WY_LED_BLACK_B,
                         WY_LED_WHITE_R, WY_LED_WHITE_G, WY_LED_WHITE_B);
    rc |= set_speed(led, speed);
    rc |= set_effect(led, WY_LED_FX_DOA);
    return rc;
}

/* --- lifecycle --- */

int wy_led_init(wy_led *led, wy_led_emit_fn fn, void *user)
{
    if (!led || !fn) return -1;
    memset(led, 0, sizeof(*led));
    led->emit = fn;
    led->user = user;
    led->state = WY_LED_STATE_OFF;
    led->brightness = wy_led_scale_brightness(WY_LED_BRIGHTNESS_MAX);
    led->gammify = 1;
    return 0;
}

int wy_led_boot(wy_led *led)
{
    int rc = 0;
    if (!led) return -1;
    if (red_owns(led)) return 0;   /* red is already saying something more important */
    /* Once per boot, and it ENDS when the host connects -- that is the moment the device is
     * genuinely ready, and a light show that outlives its welcome is just noise. The caller
     * reaches this once per boot by construction: the state lives in RAM. */
    rc |= set_brightness(led);
    rc |= show_breath(led, WY_LED_STATE_BOOT, WY_LED_WHITE_R, WY_LED_WHITE_G, WY_LED_WHITE_B, 1);
    return rc;
}

int wy_led_connected(wy_led *led, int first_this_boot)
{
    int rc = 0;
    if (!led) return -1;
    /* A reconnect is the ONLY thing that releases a red. The outage is over and any latched
     * refusal is answered by the fact that we are now connected. */
    led->outage = 0;
    led->outage_ms = 0;
    led->outage_red = 0;
    led->error_latch = 0;
    led->act = ACT_NONE;
    if (!first_this_boot) {
        /* Silent. Going dark here also stops a restart inside the startup window from leaving the
         * ring breathing forever. */
        return show_off(led);
    }
    /* The green triple-flash is the "ready" handshake. It also ends the startup pulse. */
    enter(led, WY_LED_STATE_IDLE);
    led->act = ACT_FLASH;
    led->act_step = 0;
    led->act_ms = WY_LED_FLASH_ON_MS;
    rc |= set_color(led, WY_LED_GREEN_R, WY_LED_GREEN_G, WY_LED_GREEN_B);
    rc |= set_effect(led, WY_LED_FX_SINGLE);
    return rc;
}

int wy_led_disconnected(wy_led *led)
{
    if (!led) return -1;
    /* Red means a REAL outage, never a blink. A cable that wobbles for half a second must not
     * paint the room red, so the colour only appears if the outage survives the grace period.
     *
     * The ring goes dark NOW, though: whatever it was showing -- speech, a flash, the ready
     * handshake -- was about a session that no longer exists, and leaving it lit would claim
     * otherwise for the whole grace period. The countdown itself lives in the outage slot, so
     * this OFF cannot cancel it the way a presentation action used to. */
    if (red_owns(led)) return 0;   /* red is already saying something more important */
    int rc = show_off(led);
    /* IDEMPOTENT: only the transition INTO an outage starts the countdown. A supervisor that
     * notifies twice (the TLS teardown does), or a registration retry that fails repeatedly, used
     * to reset the 30 s every time and push the red into the future indefinitely -- so a device
     * that had been offline for minutes stayed dark. A second notification must not restart a
     * clock that has been running, nor blank a red that has already been earned. */
    if (!led->outage) {
        led->outage = 1;
        led->outage_ms = WY_LED_DISCONNECT_GRACE_MS;
    }
    return rc;
}

int wy_led_wake(wy_led *led)
{
    if (!led) return -1;
    if (red_owns(led)) return 0;   /* red is already saying something more important */
    /* One animation, three stages, driven entirely from here so the pieces cannot race each
     * other: rainbow for the length of the activation tone, a yellow "I hear you" beat while the
     * microphone opens, then the direction marker for the query. The session announcing voice
     * started mid-chain must not restart or truncate this -- see wy_led_voice_started(). */
    int rc = show_sweep(led, WY_LED_STATE_WAKE, 1);
    led->act = ACT_SWEEP;
    led->act_step = 0;
    led->act_ms = WY_LED_WAKE_SWEEP_MS;
    led->next_state = WY_LED_STATE_VOICE;
    return rc;
}

int wy_led_listening(wy_led *led)
{
    if (!led) return -1;
    if (red_owns(led)) return 0;   /* red is already saying something more important */
    return show_doa(led, WY_LED_STATE_LISTENING, 1);
}

int wy_led_voice_started(wy_led *led)
{
    if (!led) return -1;
    if (red_owns(led)) return 0;   /* red is already saying something more important */
    /* The wake animation already owns this moment and will arrive here on its own. A session
     * that announces "the microphone opened" while the sweep or the beat is running would restart
     * the chain and could truncate the rainbow, so it is politely ignored: the lights are one
     * animation, not a queue of competing callers. */
    if (led->act == ACT_SWEEP || led->act == ACT_VOICE) return 0;
    int rc = show_single(led, WY_LED_STATE_VOICE, WY_LED_YELLOW_R, WY_LED_YELLOW_G, WY_LED_YELLOW_B, 1);
    led->act = ACT_VOICE;
    led->act_step = 0;
    led->act_ms = WY_LED_VOICE_MS;
    led->next_state = WY_LED_STATE_LISTENING;
    return rc;
}

int wy_led_transcript(wy_led *led)
{
    if (!led) return -1;
    if (red_owns(led)) return 0;   /* red is already saying something more important */
    /* Green, briefly: a transcript arrived. The reference satellite shows it for one second. */
    enter(led, WY_LED_STATE_TRANSCRIPT);
    led->act = ACT_BRIEF;
    led->act_step = 0;
    led->act_ms = WY_LED_BRIEF_MS;
    led->act_r = WY_LED_GREEN_R; led->act_g = WY_LED_GREEN_G; led->act_b = WY_LED_GREEN_B;
    return set_color(led, WY_LED_GREEN_R, WY_LED_GREEN_G, WY_LED_GREEN_B)
         | set_effect(led, WY_LED_FX_SINGLE);
}

int wy_led_thinking(wy_led *led)
{
    if (!led) return -1;
    if (red_owns(led)) return 0;   /* red is already saying something more important */
    /* The glow while the answer is worked out. Bounded on the reference satellite by the
     * busy-pulse budget; here
     * the turn's own end takes over, so an unbounded look cannot outlive the work. */
    return show_breath(led, WY_LED_STATE_THINKING,
                       WY_LED_WORKING_R, WY_LED_WORKING_G, WY_LED_WORKING_B, 1);
}

int wy_led_speaking(wy_led *led)
{
    if (!led) return -1;
    if (red_owns(led)) return 0;   /* red is already saying something more important */
    return show_single(led, WY_LED_STATE_SPEAKING,
                       WY_LED_WORKING_R, WY_LED_WORKING_G, WY_LED_WORKING_B, 1);
}

int wy_led_followup(wy_led *led)
{
    if (!led) return -1;
    if (red_owns(led)) return 0;   /* red is already saying something more important */
    /* Steady, dim, teal: "waiting for a follow-up". Steady so it cannot be mistaken for the
     * thinking glow, dim so it reads as waiting rather than as an alarm. */
    return show_single(led, WY_LED_STATE_FOLLOWUP,
                       WY_LED_FOLLOWUP_R, WY_LED_FOLLOWUP_G, WY_LED_FOLLOWUP_B, 1);
}

int wy_led_idle(wy_led *led)
{
    if (!led) return -1;
    if (red_owns(led)) return 0;   /* red is already saying something more important */
    return show_off(led);
}

int wy_led_error(wy_led *led)
{
    if (!led) return -1;
    /* Immediate red: something is actually wrong, as opposed to merely not connected. Latched,
     * because the ordinary disconnect handling that follows a refusal would otherwise overwrite
     * it with darkness a moment later -- the device would show "offline" when the truth is "the
     * host said no", which is the one distinction red exists to make. */
    led->error_latch = 1;
    return show_single(led, WY_LED_STATE_ERROR, WY_LED_RED_R, WY_LED_RED_G, WY_LED_RED_B, 1);
}

/* --- time --- */

int wy_led_tick(wy_led *led, uint32_t elapsed_ms)
{
    if (!led) return -1;

    /* The outage countdown advances first and independently of whatever is being shown, so no
     * presentation can swallow it. */
    int outage_fired = 0;
    if (led->outage && led->outage_ms > 0) {
        if (elapsed_ms >= led->outage_ms) {
            led->outage_ms = 0;
            outage_fired = 1;
        } else {
            led->outage_ms -= elapsed_ms;
        }
    }

    /* Consume elapsed time across as MANY action steps as it covers. One tick can span several
     * phases -- a 1800 ms tick must finish the whole ready flash, not just its first step --
     * so returning after one step would stretch the sequence and leave an action pending long
     * after it should have completed. */
    while (led->act != ACT_NONE && elapsed_ms >= led->act_ms) {
        elapsed_ms -= led->act_ms;
        led->act_ms = 0;

        switch (led->act) {
        case ACT_FLASH: {
            /* on, off, on, off, on, off -> three visible flashes. */
            int on = (int)(led->act_step % 2u == 0u);
            led->act_step++;
            if (led->act_step >= WY_LED_FLASH_COUNT * 2u) {
                /* The final phase was an off, so the ring is already dark. Commanding it dark
                 * again would be a phantom fourth off-phase to anything counting the flashes. */
                led->act = ACT_NONE;
                enter(led, WY_LED_STATE_OFF);
                break;
            }
            led->act_ms = on ? WY_LED_FLASH_OFF_MS : WY_LED_FLASH_ON_MS;
            if (on) (void)set_effect(led, WY_LED_FX_OFF);
            else    (void)set_effect(led, WY_LED_FX_SINGLE);
            break;
        }
        case ACT_BRIEF:
            (void)show_off(led);
            break;
        case ACT_SWEEP:
            /* The tone has stopped ringing. The microphone is open: say so in the language the
             * reference satellite uses (yellow), then point at the person who is talking. */
            if (led->next_state == WY_LED_STATE_VOICE) {
                (void)show_single(led, WY_LED_STATE_VOICE,
                                  WY_LED_YELLOW_R, WY_LED_YELLOW_G, WY_LED_YELLOW_B, 1);
                led->act = ACT_VOICE;
                led->act_ms = WY_LED_VOICE_MS;
                led->next_state = WY_LED_STATE_LISTENING;
            } else if (led->next_state == WY_LED_STATE_LISTENING) {
                (void)show_doa(led, WY_LED_STATE_LISTENING, 1);
            } else {
                (void)show_off(led);
            }
            break;
        case ACT_VOICE:
            /* The beat is over: now the ring shows where the person is. */
            (void)show_doa(led, WY_LED_STATE_LISTENING, 1);
            break;
        default:
            led->act = ACT_NONE;
            (void)show_off(led);
            break;
        }
    }
    if (led->act != ACT_NONE && elapsed_ms > 0) led->act_ms -= elapsed_ms;

    /* An outage that outlived its grace period outranks whatever is on screen: the ring is being
     * asked to say the link is gone, and nothing else matters as much as that. */
    if (outage_fired) {
        led->outage_red = 1;   /* from here red is the outage's to keep */
        return show_single(led, WY_LED_STATE_ERROR, WY_LED_RED_R, WY_LED_RED_G, WY_LED_RED_B, 1);
    }
    return 0;
}

int wy_led_set_brightness(wy_led *led, unsigned level_1_31)
{
    if (!led) return -1;
    led->brightness = wy_led_scale_brightness(level_1_31);
    return set_brightness(led);
}

unsigned wy_led_state(const wy_led *led) { return led ? led->state : 0u; }
int wy_led_red_pending(const wy_led *led)
{
    /* True only while the grace period is still running. An outage that has already shown red,
     * or one that has been cancelled by a reconnect, is not pending. */
    return led && led->outage && led->outage_ms > 0;
}
