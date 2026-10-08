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

**The ESP32 firmware bundles a default wake model** at `firmware/esp32/models/`
(`hey_hermes.tflite`, trained through microwakeword.com, with attribution and terms in that
directory's `NOTICE.md`). It is personal/non-commercial-licensed, which fits a home satellite in a
**private** repository; before any public release the bundle needs a licence pass — either a
commercial licence for that model, a switch to an Apache-2.0 model, or moving the model out of the
image and fetching it at install time.

**The Pi side fetches its model at install time** rather than shipping one in the image, which keeps
that licence question with the person installing it.

The bundled default is a community-library model, so it carries the non-commercial default above. A
commercial deployment has three clean routes: buy a commercial licence from the model's operator,
switch to one of the pre-trained Apache-2.0 models, or train your own with the open-source toolkit.

**Firmware is the case to handle carefully.** A model compiled into a firmware image is being
redistributed, so embedding a community model in a published binary is a licence question rather than a
technical one. Where a model has to be inside an image, use one whose licence permits redistribution,
or have the installer fetch the model separately and keep it out of the distributed image.

## Release readiness: the bundled ESP32 model

The bundled model `firmware/esp32/models/hey_hermes.tflite` (microwakeword.com community
library) is licensed **personal/non-commercial only**. That is fine for this home satellite in
a private repository. It is not fine for a **public prebuilt-bundle release**: publishing the
model file (or any release artifact that carries it) redistributes it, and redistribution is
what the non-commercial default does not grant. Before such a release ships, one of three
routes must be taken:

1. **A commercial licence** for that model from microwakeword.com's operator
   (<https://microwakeword.com/license>), covering the redistribution.
2. **An Apache-2.0 model swap** — replace the bundled default with a model whose licence
   permits redistribution, such as the pre-trained models in
   [esphome/micro-wake-word-models](https://github.com/esphome/micro-wake-word-models)
   (see the table above and `firmware/esp32/models/NOTICE.md` for the swap mechanics).
3. **Fetch-at-install** — keep the model out of every distributed artifact and have the
   installer download it at provisioning time. The plumbing already exists:
   `install-esp32.sh --download-model <url>` stages a model that way, and the model is written
   to its own flash partition at provision time (never part of the app bundle), so no rebuild
   is involved.

**The current gate, plainly:** the repository is private and no prebuilt bundle or model is
published, so no redistribution is happening today. Until one of the three routes above is
done, a public release must not include `hey_hermes.tflite` in the tree, in a release asset,
or inside any image. This is a description of the licence terms and the release decision they
force, not legal advice.

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
