# Wake word model licensing

The software in this repository and the wake word **models** it uses are licensed separately, and the
models are the part with restrictions. Everything below was read from the sources linked at the end on
2026-09-24; licence text changes, so re-check before relying on it.

## Three sources of models, three sets of terms

| Where the model came from | Licence | Commercial use |
|---|---|---|
| The openWakeWord project and the pre-trained models it ships (`alexa`, `hey_jarvis`, `hey_mycroft`, `hey_rhasspy`, `okay_nabu` at the time of writing) | Apache-2.0 | Permitted |
| Trained through the hosted trainer at openwakeword.com, including anything downloaded from its community library | Personal and non-commercial only by default | Needs a commercial licence from the site's operator |
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

**No wake model is redistributed in this repository.** The setup instructions point at the source and
the model is fetched on to the device at install time, which keeps the licence question with the person
installing it rather than with this repository.

**The recommended default is a community-library model**, so it carries the non-commercial default
above. A commercial deployment has three clean routes: buy a commercial licence from the site's
operator, switch to one of the pre-trained Apache-2.0 models, or train your own with the open-source
toolkit.

**Firmware is the case to handle carefully.** A model compiled into a firmware image is being
redistributed, so embedding a community model in a published binary is a licence question rather than a
technical one. Where a model has to be inside an image, use one whose licence permits redistribution,
or have the installer fetch the model separately and keep it out of the distributed image.

## Changing the model

`switching-wake-words.md` covers installing, switching, verifying and rolling back a model. This
document is only about which terms apply to which model.

## Sources

- openWakeWord terms and conditions: <https://openwakeword.com/terms> (read 2026-09-24, page states
  "last updated February 2025")
- openWakeWord software licence: <https://github.com/dscripka/openWakeWord/blob/main/LICENSE>
  (Apache-2.0)
- The commercial licence page linked from those terms, for a quote or tiers.
