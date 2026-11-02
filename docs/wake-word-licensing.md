# Wake word model licensing

The software in this repository and the wake word **models** it uses are licensed separately, and the
models are the part with restrictions. Everything below was read from the sources linked at the end on
2026-09-24; licence text changes, so re-check before relying on it.

## Three sources of models, three sets of terms

| Where the model came from | Licence | Commercial use |
|---|---|---|
| The openWakeWord project and the pre-trained models it ships (`alexa`, `hey_jarvis`, `hey_mycroft`, `hey_rhasspy`, `okay_nabu` at the time of writing) | Apache-2.0 | Permitted |
| Trained through the hosted trainer at openwakeword.com, including anything downloaded from its community library | Personal and non-commercial only by default | Needs a commercial licence from the site's operator |
| Trained through the hosted trainer at microwakeword.com (ESP32 microWakeWord models, community library) | Personal and non-commercial only by default (Terms, section 5, updated March 2026) | Needs a written licence from the service operator |
| Trained by you with the open-source toolkit | Yours to license, subject to the terms of whatever you train on | Your decision |

## The hosted trainer's terms

Wake word models, from the site's terms (last updated February 2025):

> Wake word models generated through the Service are licensed for personal and non-commercial use only
> by default. You may not use them in commercial products, services, or applications without a
> commercial license.

Community sharing is addressed separately:

> Models shared to the community library are made available to all users. By sharing a model, you grant
> a perpetual, non-exclusive license for others to download and use it.

That grants download and use, but it does not say it lifts the non-commercial default. **Treat a
community model as non-commercial unless a commercial licence says otherwise.**

Voices have a stricter rule with no paid path at all:

> Piper voice models (custom text-to-speech voices trained or fine-tuned through the Voice Center,
> together with any audio they generate) are licensed for personal and non-commercial use only, and no
> commercial license is offered for them.

So a fine-tuned voice from that source is never usable commercially, at any price. That applies to the
voice and to any audio it generates.

## What this project does with models

**The ESP32 firmware bundles a default wake model** at `firmware/esp32/models/`:
`hey_hermes.tflite`, trained through the microwakeword.com hosted trainer's community library, with
attribution and its manifest in that directory's `NOTICE.md`. Its terms are personal and
non-commercial only.

That is a deliberate choice (recorded 2026-11-01): the repository's own code is under the PolyForm
Noncommercial License 1.0.0, and the default model matches what this project is. The model that a
plain install gets is the one the project is built around, not a placeholder.

**A commercial deployment must switch models.** Neither the community model's terms nor this
repository's licence grant commercial use. The switch does not need a firmware rebuild: each model
carries its own inference policy (cutoff, window, step, arena) inside the model blob, written from
its `.json` manifest at provision time, so changing the model is a provisioning step. The clean
routes are the Apache-2.0 `okay_nabu.tflite` in this tree, a model you train with the open-source
toolkit, or a model you license from its operator.

**The Pi side fetches its model at install time** rather than shipping one in the image, which keeps
that licence question with the person installing it.

**Firmware is the case to handle carefully.** A model compiled into a firmware image is being
redistributed, so embedding a community model in a published binary is a licence question rather than
a technical one. This project answers it by publishing under a non-commercial licence and naming the
default model's terms plainly, here and in `NOTICE.md`; a redistributor who needs different terms
switches the model and, if they redistribute the firmware, reviews that model's licence themselves.

## Release readiness: the bundled ESP32 model

The shipped default is `firmware/esp32/models/hey_hermes.tflite` (microwakeword.com community
library, personal and non-commercial only). Its delivered bytes and the four numbers in its manifest
are pinned by `NOTICE.md` and `scripts/test_esp32_wake_scorer.py`, so a silent substitution is
caught. It is the firmware's compiled default word, the installer's default choice, and the model
`install-esp32.sh` provisions when no model is named.

The Apache-2.0 alternative, `okay_nabu.tflite`
([esphome/micro-wake-word-models](https://github.com/esphome/micro-wake-word-models)), also ships in
the tree, with its own manifest. Switching to it is one provisioning run
(`install-esp32.sh --provision --model firmware/esp32/models/okay_nabu.tflite --model-name
okay_nabu ...`); the device scores it with the 0.97 cutoff from its own manifest, not with the
default's 0.5. `firmware/esp32/models/NOTICE.md` has the exact command and the manifest rules.

The model partition is written at provision time and is never part of the application flash
bundle, so a model swap and a firmware update are separate operations. This is a description of the
licence terms and the release decision they force, not legal advice.

## Changing the model

`switching-wake-words.md` covers installing, switching, verifying and rolling back a model. This
document is only about which terms apply to which model.

## Sources

- openWakeWord terms and conditions: <https://openwakeword.com/terms> (read 2026-09-24, page states
  "last updated February 2025")
- openWakeWord software licence: <https://github.com/dscripka/openWakeWord/blob/main/LICENSE>
  (Apache-2.0)
- The commercial licence page linked from those terms, for a quote or tiers.
- microwakeword.com terms and conditions: <https://microwakeword.com/terms> (read 2026-10-07, page
  states "last updated: March 2026"; section 5, non-commercial default) and its commercial licensing
  page: <https://microwakeword.com/license>
