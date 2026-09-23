# Feature roadmap: what to build ourselves, inspired by other projects

Recorded 2026-09-23. The decision behind this file, and the reasoning, are at the bottom.

We compared our stack against **Linux Voice Assistant** (`OHF-Voice/linux-voice-assistant`), the
actively maintained successor to the archived Wyoming satellite software we run. Its feature list is
the best available shopping list of things a mature voice satellite does. **We are not switching to
it**, and this document exists so its ideas get planned rather than forgotten.

Each entry: what it is, where we stand, what adopting it here would actually take, and whether it is
worth doing.

## The gap list

### 1. Acoustic echo cancellation (AEC) — real gap, highest value

**Theirs:** a dedicated `docs/enabling_aec.md`, plus PipeWire support for it.
**Ours:** none. Our own answer can trigger the wake word, and we handle that with a 1.2 s deaf
lead-in after speech plus a self-trigger counter that logs and ignores detections during playback.
That is a workaround, not a fix.
**What it would take here, two routes:**
- *Hardware:* the XMOS XVF3800 arrays (the Seeed devices being evaluated) do AEC, beamforming and
  noise suppression on-chip, which means we would get it with no software change at all. This is the
  strongest argument for buying one.
- *Software:* an audio server with an echo-cancel module (PipeWire or PulseAudio) on the Pi. That is
  a real change to a device with 171 MB of headroom, and it would sit underneath everything else.
**Value:** high. It removes a whole class of "the device answered itself" weirdness.

### 2. Media playback — real gap, medium value

**Theirs:** media player support as a first-class capability.
**Ours:** we can speak (TTS) and announce (pre-rendered tones and text). We cannot play music or
stream audio to the device.
**What it would take here:** a player that shares the existing single-playback lock, a source
(local files or a stream), and a decision about how it yields to a wake word. The lock and the
queue already exist in the bridge, so this is more plumbing than invention.
**Value:** medium. Nothing currently asks for it. It is the capability we would miss first if the
device became a general house speaker.

### 3. MicroWakeWord as a lighter wake engine — real gap, high value on this hardware

**Theirs:** supports both OpenWakeWord and MicroWakeWord.
**Ours:** OpenWakeWord only, which is the heaviest thing running on the Pi (74 MB, the largest
process on the device).
**What it would take here:** MicroWakeWord is designed for microcontrollers, supports TFLite, and
would cut both memory and CPU on a 416 MB device. Our wake shim sits in front of the wake service,
so the shim would need to speak whichever protocol the new engine serves.
**Value:** high, purely because of the hardware constraint. Fewer MB here is headroom for
everything else.

### 4. Multiple wake words at once — partial gap, medium value

**Theirs:** multiple wake words and languages simultaneously.
**Ours:** one wake word at a time, switchable with a documented runbook and script.
**What it would take here:** our shim already fronts the wake service, so a shim that multiplexes
two wake model names is plausible. The honest constraint is behavioural, not technical: two wake
words means two ways to accidentally trigger the device.
**Value:** medium. Worth doing only for a specific reason, such as a second person preferring a
different word.

### 5. Stop wake word — mostly covered

**Theirs:** a dedicated stop word handled by the stack.
**Ours:** stop and cancel phrases, including "ok nabu stop" to end a follow-up conversation while
leaving it retriggerable. Built and verified.
**Value:** low. Theirs is a protocol feature, ours is behaviour we already have.

### 6. Peripheral API — equivalent, different mechanism

**Theirs:** a documented peripheral API for LEDs, buttons and displays.
**Ours:** the satellite forwards unrecognised events to our LED service over loopback, and the
bridge publishes `led-busy`, `led-idle` and `led-followup` states. Bespoke, but loopback only and
already proven.
**Value:** low. Worth revisiting only if we add a second kind of device.

### 7. Timers — already planned, and validated

**Theirs:** timer support built in, server-side.
**Ours:** speced in `docs/timers-and-alarms.md`, with the bridge owning countdowns and cron owning
alarms. Interesting detail found while vendoring: the satellite source ships a
`sounds/timer_finished.wav`, which we can use as the timer tone for free.
**Value:** already on the plan, and their doing it confirms it is a normal thing for this class of
device to do.

### 8. Announcements and start/continue conversation — equal

**Theirs:** announcements via Home Assistant actions, start and continue conversation.
**Ours:** announcements with queueing behind a live turn, plus follow-up mode. Both built and
verified.
**Value:** none, we are level.

### 9. Configuration page and packaging — planned or not relevant

**Theirs:** install and configuration docs, prebuilt images, artifact attestation.
**Ours:** the config-server feasibility work is already written up in
`docs/satellite-http-config.md`; our equivalent of prebuilt images is the gold SD card image.
**Value:** low to medium, tracked elsewhere.

## Our advantage, for the record

Theirs is a satellite for Home Assistant. Ours reaches a full Hermes assistant: the voice surface
currently carries 21 toolsets and 19 direct tools, including household control, calendar, tasks and
place lookups. Nothing about switching would preserve that without rebuilding the bridge, which is
the strongest practical argument for staying where we are.

## Why we are not switching

Angela, 2026-09-23: "Wyoming protocol does work just fine, and since we effectively control both
sides of the interaction of the speaker devices and the endpoint they connect to, we also control
the bug fixes. It would be a good idea to watch that repo for potential ideas on features we
implement, but I don't see a compelling reason to switch right now."

The verification behind that: the satellite runs at a pinned commit, we already patch its core
(`docs/owning-the-satellite-stack.md`), and our bridge's dependencies are fully pinned. When the
hourly reconnect bug appeared on 2026-09-23 it was diagnosed and fixed inside an hour without
needing anyone upstream.

The real costs of unmaintained software here are narrow: no security fixes in a network-listening
service, and dependency rot when the Pi's OS eventually moves on. Both are manageable for as long as
the code is small, readable and ours.

**Triggers that would justify revisiting the switch**, so this is a decision with conditions rather
than a mood:

- we want media playback or multi-room audio as a real feature, not a curiosity
- we want a voice device that is not a Linux SBC, which is where the ESPHome protocol genuinely wins
- we stop being able to fix something ourselves, for example the driver stops building on a new kernel

## Watch list

Watch `OHF-Voice/linux-voice-assistant` for ideas, specifically: its `docs/enabling_aec.md`, its
`docs/peripheral_api.md`, its `wakewords/` and `sounds/` directories (prebuilt tones we might reuse),
and its `CHANGELOG.md`.

A low-frequency cron job could check that repo monthly and report only when something relevant
appears, so this stays a background signal instead of a thing anyone has to remember. Not created
yet; it needs Angela's approval.
