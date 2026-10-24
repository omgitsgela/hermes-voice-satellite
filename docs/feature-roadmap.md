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

Decided 2026-09-23: "Wyoming protocol does work just fine, and since we effectively control both
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

A low-frequency cron job checks that repo monthly and reports only when something relevant appears,
so this stays a background signal instead of a thing anyone has to remember. Created 2026-09-23:
cron job `watch-successor-voice-repos`, 09:00 on the 1st, in monitor mode (a deterministic digest,
so it is completely silent unless something actually changed upstream).

## These are tracked as GitHub issues

The gaps above are filed as issues on this repository, which is where feature work is planned. The maintainer's
expectation, 2026-09-23: a feature enhancement belongs on the Issues page, not only in a document.

| Issue | What it covers |
|---|---|
| [#1](https://github.com/omgitsgela/hermes-voice-satellite/issues/1) | Acoustic echo cancellation, the biggest gap |
| [#2](https://github.com/omgitsgela/hermes-voice-satellite/issues/2) | MicroWakeWord as a lighter wake engine |
| [#3](https://github.com/omgitsgela/hermes-voice-satellite/issues/3) | Media playback |
| [#4](https://github.com/omgitsgela/hermes-voice-satellite/issues/4) | Multiple simultaneous wake words |
| [#5](https://github.com/omgitsgela/hermes-voice-satellite/issues/5) | Timers and alarms, from the written spec |
| [#6](https://github.com/omgitsgela/hermes-voice-satellite/issues/6) | Configuration server (on the host, not the Pi) |
| [#7](https://github.com/omgitsgela/hermes-voice-satellite/issues/7) | Buy and test the shortlisted devices |
| [#8](https://github.com/omgitsgela/hermes-voice-satellite/issues/8) | Evaluate their sound assets and stop model |
| [#9](https://github.com/omgitsgela/hermes-voice-satellite/issues/9) | The DKMS build-versus-installed warning |
| [#10](https://github.com/omgitsgela/hermes-voice-satellite/issues/10) | Volume control by voice, with a hard floor that is never zero |
| [#11](https://github.com/omgitsgela/hermes-voice-satellite/issues/11) | Multi-satellite configuration and management: onboarding, updates, options at fleet scale |
| [#32](https://github.com/omgitsgela/hermes-voice-satellite/issues/32) | Single-responder arbitration when several satellites hear one wake. The arbitration engine and the ESP32 level emission are landed (PR #112): measured level decides, wake score tiebreaks, arrival order last. Remaining: Pi-side level emission, and level comparability across different microphone chains |
| [#111](https://github.com/omgitsgela/hermes-voice-satellite/issues/111) | Registered devices were never armed (the bridge leased one satellite at a time). Fixed in PR #112: every registered device gets a lease, an arm, and a supervisor reader |
| [#26](https://github.com/omgitsgela/hermes-voice-satellite/issues/26) | Optional slower-speaking accessibility mode for utterance endpointing |
| [#61](https://github.com/omgitsgela/hermes-voice-satellite/issues/61) | **Connection inversion: devices dial the bridge.** The umbrella issue for the registry, the transport decision, enrolment and identity, and the licensing constraint |

#10 is a maintainer request rather than a gap against the successor project, and it is filed
here all the same: this index is the list of what we intend to build, whatever the source of the idea.

Keep both places in step: this document carries the reasoning, the issues carry the work. When an
issue is opened, link it here; when an idea arrives that is not already an issue, file it.

## Bug work, filed from a code audit (2026-09-25)

A read of `main` against the running system produced ten defects, each reproduced offline and filed as
a bug rather than an enhancement. They are listed here so the roadmap stays the single index of open
work; details, affected files and safe reproductions live on each issue.

| Issue | What is broken |
|---|---|
| [#79](https://github.com/omgitsgela/hermes-voice-satellite/issues/79) | A failed registry write skips connection cleanup and leaves lease waiters blocked |
| [#80](https://github.com/omgitsgela/hermes-voice-satellite/issues/80) | The outbound runner misses a bridge disconnect while retrying an unavailable local satellite |
| [#81](https://github.com/omgitsgela/hermes-voice-satellite/issues/81) | A failed config-plane save publishes an unpersisted desired generation |
| [#82](https://github.com/omgitsgela/hermes-voice-satellite/issues/82) | The interruption watcher spins forever when it receives `Played` before the waiter |
| [#83](https://github.com/omgitsgela/hermes-voice-satellite/issues/83) | A full announcement queue marks rejected messages as duplicates |
| [#84](https://github.com/omgitsgela/hermes-voice-satellite/issues/84) | The timer scheduler dies permanently after an expiry persistence error |
| [#85](https://github.com/omgitsgela/hermes-voice-satellite/issues/85) | Card deployment does not install the outbound runner's required sibling modules |
| [#86](https://github.com/omgitsgela/hermes-voice-satellite/issues/86) | The device config agent never retries a failed hardware apply for the same generation |
| [#87](https://github.com/omgitsgela/hermes-voice-satellite/issues/87) | Wi-Fi card provisioning leaves a source-device MAC restriction in the `[wifi]` section |
| [#88](https://github.com/omgitsgela/hermes-voice-satellite/issues/88) | The Hermes voice and timer tools are unavailable with environment-token authentication |
| [#89](https://github.com/omgitsgela/hermes-voice-satellite/issues/89) | A failed config-plane *report* publishes health in memory before the save succeeds |
| [#90](https://github.com/omgitsgela/hermes-voice-satellite/issues/90) | A failed timer set or cancel publishes the change in memory before the save succeeds |
| [#91](https://github.com/omgitsgela/hermes-voice-satellite/issues/91) | A host reboot can strand the outbound satellite on a half-open link and trigger a competing direct dial |
| [#93](https://github.com/omgitsgela/hermes-voice-satellite/issues/93) | The voice prompt refuses requested knowledge-base writes despite the API surface having the required tools |
| [#96](https://github.com/omgitsgela/hermes-voice-satellite/issues/96) | A spoken alarm is created as a plain cron reminder, so it never speaks and listing alarms reads the whole cron inventory |
| [#97](https://github.com/omgitsgela/hermes-voice-satellite/issues/97) | A spoken volume request can change a different device: the assistant picks a device and a value, then reads a repeat as agreement |
| [#99](https://github.com/omgitsgela/hermes-voice-satellite/issues/99) | The card deploy script omits four device modules, so a closed fix could never be deployed |
| [#102](https://github.com/omgitsgela/hermes-voice-satellite/issues/102) | A roaming device disconnecting with No route to host logs an unretrieved task exception and a traceback |
| [#104](https://github.com/omgitsgela/hermes-voice-satellite/issues/104) | The bridge cannot detect a vanished satellite: nothing sets keepalive on accepted registry sockets |
| [#105](https://github.com/omgitsgela/hermes-voice-satellite/issues/105) | A recovered link does not say it is listening again, so the journal cannot distinguish recovery from a stall |

Each of these is a defect in behaviour the repository already documents, so none of them needs a design
decision first. The one exception is the fix shape for #86, which has to choose between retrying a
failed generation and accepting a new one.

**#79 to #90 are fixed and closed.** Each fix carries a regression test that was observed failing before
the change and after deliberately restoring the defect, so every check is known to bite. #89 and #90 are
the siblings the first pass left behind: one writer each in the configuration plane and the timer store
that still mutated live state before the save returned. Acceptance now means durable persistence in all of
them, and the pattern is written down with the contract rather than with the incident: see
[`configuration-plane.md`](configuration-plane.md) for the configuration plane and
[`timers-and-alarms.md`](timers-and-alarms.md) for the timer store.

## Done

- **48 kHz audio path** — the I2S link now runs at 48 kHz in both directions, matching the Seeed
  reference profile. Playback keeps the bridge's full TTS bandwidth instead of being low-passed at
  7400 Hz. See `docs/audio-fidelity.md`. The capture side decimates back to 16 kHz for the wake
  model and the uplink, which are 16 kHz contracts and did not change.

## Planned, not yet started

These are design intentions, not commitments. Each needs its own decision note before work starts.

- **LED lifecycle on the ESP32 satellite.** The reference satellite's LEDs are the primary
  diagnostic: gentle pulse while booting, blue on detection, yellow while streaming, violet while
  thinking, dark when the answer starts. That vocabulary is worth having on every satellite in the
  house, and it is the only feedback a device with no display can give. The ESP32 has no LED
  hardware wired yet, so this starts with a hardware decision (which pixels, which pin) before any
  firmware.
- **48 kHz audio path.** Capture and playback are both 16 kHz today, which caps the audible band at
  8 kHz and is the main remaining quality ceiling. Going higher needs the XVF3800's 48 kHz I2S
  master build, a coordinated sample-rate change across capture, playback, the bridge's TTS format
  and the wake scorer's input, plus a bandwidth/memory check on the link. It is one migration, not
  a switch: do it as a single coordinated change or not at all. See `prompt-tones.md` for the
  existing rate split, which the migration would collapse.

## Built from this list

- **#26, pause-tolerant endpointing** (built 2026-09-24). Named, validated endpointing profiles with
  `standard` and `pause_tolerant`, selected by voice, by the authenticated settings endpoint, or from a
  state file, and applied to every capture path. The reasoning and the exact bounds are in
  [`endpointing-profiles.md`](endpointing-profiles.md). Note what it is and is not: a longer, still
  finite wait before an utterance closes, not a second conversation mode and not an open microphone.
