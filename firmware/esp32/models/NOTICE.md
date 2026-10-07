# Bundled wake model — attribution and license

This directory holds the **default wake-word model** bundled with the ESP32/XVF3800 satellite so a
plain install works without the user supplying a model. The installer
(`scripts/install-esp32.sh`) provisions it by default; `--model <path>` or `--download-model
<url>` replace it with a custom model after the fact.

## Model

| | |
|---|---|
| File | `hey_jarvis.tflite` (microWakeWord v2, streaming INT8) |
| Wake word | "Hey Jarvis" |
| Author | Kevin Ahrendt (author of microWakeWord) |
| Source | [esphome/micro-wake-word-models](https://github.com/esphome/micro-wake-word-models) |
| License | [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0) |
| Inference params | `hey_jarvis.json` (`probability_cutoff`, `sliding_window_size`, `feature_step_size`, `tensor_arena_size`) |

## License and provenance

The model file is distributed by `esphome/micro-wake-word-models` under the **Apache License 2.0**
and is redistributed here under that license with this attribution. It is a
[TensorFlow Lite](https://www.tensorflow.org/lite) flatbuffer trained with
[microWakeWord](https://github.com/OHF-Voice/micro-wake-word) (Apache-2.0).

**Provenance caveat:** microWakeWord models are trained on synthetic data drawn from sources with
mixed licenses; the upstream project notes such models should be regarded as suitable for
**non-commercial personal use**. The precise training-data provenance of the published `.tflite`
files is being clarified upstream
([esphome/micro-wake-word-models#31](https://github.com/esphome/micro-wake-word-models/issues/31)).
This is a home/personal satellite, not a redistributed commercial product, so that is acceptable
here — but re-check the upstream position before any commercial or wide redistribution.

## Why "Hey Jarvis" and not "Hey Hermes"

"Hey Jarvis" is a stand-in: it is a well-tested microWakeWord model whose "Hey \<name\>" phrasing
matches the intended "Hey Hermes" experience, and its `.tflite` form matches the ESP32's
microWakeWord inference engine. The project's own **"Hey Hermes"** model is the long-term default;
it will replace this file once a version exists that runs on both wake-word engines (the Pi's
openWakeWord and the ESP32's microWakeWord). Until then this is the bundled default, and the
detection name the device reports is the model it loaded (`hey_jarvis`), exactly as the Pi reports
whatever model it loaded.

> Replacing the default: drop the new `.tflite` + its `.json` manifest here, update the model file
> and `--model-name` default in `scripts/install-esp32.sh`, and regenerate the flash bundle. The
> model is provisioned to the `model` flash partition by `scripts/provision-device-model.py`.
