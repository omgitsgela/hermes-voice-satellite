# Interruption: talking to a task that is already running

**Where this sits:** stage across 4-7 of the voice pipeline. The end-to-end map, and every other stage, is in `docs/architecture.md`.

Built 2026-09-23. a real incident, which this exists to fix:

> I had it turn on christmas lights, but my husband was pranking me and turned them off remotely.
> I didnt know this, I asked it to investigate, found out it was a joke, and I tried asking the
> agent to stop because I found out it was irrelevant, but it continued working in the background.

The mechanism behind that: a stop phrase was only ever noticed *between* turns. Nothing in the
gateway ever called the gateway's stop endpoint, so a stop spoken while a task was in flight had
nowhere to land and the task ran to completion.

## What happens now

While a task is running, the satellite is still listening for its wake word, because a wake-word
satellite never streams audio without one. So a wake detection arriving mid-task means somebody
deliberately woke the device. The Gateway watches for exactly that, and what the person says next
decides the outcome:

| Said | Outcome | What the Gateway does |
|---|---|---|
| New information | `steered` | Hands the text to the run that is already going, via `POST /v1/runs/{id}/steer` |
| A stop phrase | `stopped` | Calls `POST /v1/runs/{id}/stop`, abandons the rest of the answer, says "Stopped." |
| A local control | `control` | Answers it locally, no model call, no steer |
| Nothing useful | `ignored` | Discards it. This is the safety case. |

**Why the safety case matters.** There is no echo cancellation (see
`docs/feature-roadmap.md`), so the device can hear its own speech. A wake word that self-triggers
produces a transcript like "you" or "beep". Steering that into a live run would inject nonsense
into a task that is already acting on the world, so short non-utterances are dropped instead.

Stop phrases are matched as whole utterances, using the same tested matcher as normal spoken
controls. That is deliberate: "stop the sprinklers tomorrow" is information for the agent, not a
command to it, and it is passed through as such.

## Configuration

In `bridge/config.py`:

- `INTERRUPT_ENABLED` (default `True`)
- `INTERRUPT_MIN_CHARS` (default `5`) applies to the steer path only, never to a stop

## How to test it by voice

1. Say something that takes a while: "hey hermes, count slowly from one to twenty out loud."
2. While it is counting, say: "hey hermes" then **"stop"**.
   Expected: it stops, says "Stopped.", and the counting does not resume.
3. Start a long task again, then say: "hey hermes" then **"actually, check the kitchen instead"**.
   Expected: no fresh question. The running task receives that information and adapts.
4. Wake it and say nothing at all.
   Expected: silence. The wake is ignored, and no phantom turn starts afterwards.

## Status, honestly

- **Verified:** 44 offline checks in `scripts/test_interrupt.py` covering all four outcomes, the
  stop forms, the sentences that must *not* count as stops, the noise cases, and the disabled flag.
  They run without a device, a model or a side effect on real state. The Gateway restarts clean.
- **Verified from source, not exercised live:** the steer and stop endpoints. The routes exist in
  the API server's route table, the handler contracts were read directly, and the Gateway
  authenticates against the same gateway for every turn it already serves. A scripted probe was
  attempted twice and blocked by credential extraction on the command line, so it was dropped
  rather than fiddled with.
- **Not yet verified:** the real end-to-end with a voice, which is step 2 above. That needs a person
  in front of the device.
- **Known limitation:** while the device is speaking, an interruption is heard through its own
  audio, so the transcript quality depends on the room. Hardware with on-chip echo cancellation
  would improve this, which is one of the arguments in `docs/supported-devices.md`.
