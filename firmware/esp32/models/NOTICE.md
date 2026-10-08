# Bundled wake model — attribution and license

This directory holds the **default wake-word model** bundled with the ESP32/XVF3800 satellite so a
plain install works without the user supplying a model. The installer
(`scripts/install-esp32.sh`) provisions it by default; `--model <path>` or `--download-model
<url>` replace it with a custom model after the fact.

## Model

| | |
|---|---|
| File | `hey_hermes.tflite` (microWakeWord v2, streaming INT8 TFLite) |
| Wake word | "Hey Hermes" |
| Trained by | [microwakeword.com](https://microwakeword.com) hosted trainer (community library) |
| Inference params | `hey_hermes.json` (`probability_cutoff`, `sliding_window_size`, `feature_step_size`, `tensor_arena_size`) |
| Delivered as | ZIP with a README (microwakeword.com standard package: `.tflite` + ESPHome manifest + README) |
| Delivered SHA-256 (tflite) | `377545239b78e0671a92f1ae7a4bbbf9a18c170263edb9aba3db5f23fcb0ed4e` |

## License

Models generated through the microwakeword.com service are licensed for **personal and
non-commercial use only**, per the service's Terms (section 5, last updated March 2026). Commercial
use requires a written license from the service operator. This is a home/personal satellite, so the
default terms are sufficient; **re-check before any commercial or wide redistribution**. The
repository is private, which keeps the redistribution question hypothetical.

The ESPHome `micro_wake_word` component and the microWakeWord inference engine are Apache-2.0.
The on-device engine this firmware runs is vendored under `firmware/esp32/components/`
(`esp-tflite-micro`, `esp-nn`, `esp-micro-speech-features` -- the same pair ESPHome runs; see
`firmware/esp32/components/VENDORED.md` for versions and commits). Those Apache-2.0 terms cover
the engine only and do **not** extend to this model.

## Why this model and why now

The previous bundled default was `hey_jarvis` from
[esphome/micro-wake-word-models](https://github.com/esphome/micro-wake-word-models) (Kevin Ahrendt,
Apache-2.0), deliberately chosen as a stand-in whose "Hey \<name\>" phrasing matched the intended
"Hey Hermes" experience while the project's own "Hey Hermes" model was not yet in hand. It is now in
hand, trained through the same microWakeWord engine the firmware runs, so it is the bundled default:
the detection name the device reports is the model it loaded (`hey_hermes`), exactly as the Pi
reports whatever model it loaded.

The two models' inference parameters differ, and the difference is expected, not a bug:
`hey_hermes` ships `probability_cutoff` 0.5 with `tensor_arena_size` 32000 (the service's published
spec: ~35–45 kB of tensor arena at 16 kHz), while `hey_jarvis` shipped a stricter 0.97 cutoff with a
22860 arena. The firmware reads these from the provisioned manifest, so the stricter/larger settings
are not silently imposed on the new model. Expect the 0.5 cutoff to be more sensitive (more
accepts, including possible false ones); if the room is noisy, raise `probability_cutoff` in
`hey_hermes.json` toward the 0.90–0.99 range the service's README recommends before reaching for
the engine's threshold.

> Replacing the default: drop the new `.tflite` + its `.json` manifest here, update the model file
> and `--model-name` default in `scripts/install-esp32.sh`, and regenerate the flash bundle. The
> model is provisioned to the `model` flash partition by `scripts/provision-device-model.py`.
