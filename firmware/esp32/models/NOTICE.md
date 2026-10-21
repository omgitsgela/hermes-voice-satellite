# Bundled wake model — attribution and license

This directory holds the **default wake-word model** bundled with the ESP32/XVF3800 satellite so a
plain install works without the user supplying a model. The installer
(`scripts/install-esp32.sh`) provisions it by default; `--model <path>` or `--download-model
<url>` replace it with a custom model after the fact.

## Model

| | |
|---|---|
| File | `okay_nabu.tflite` (microWakeWord v2, streaming INT8 TFLite) |
| Wake word | "Okay Nabu" |
| Trained by | [esphome/micro-wake-word-models](https://github.com/esphome/micro-wake-word-models) (Kevin Ahrendt) |
| Inference params | `okay_nabu.json` (`probability_cutoff` 0.97, `sliding_window_size` 5, `feature_step_size` 10, `tensor_arena_size` 26080) |
| Delivered SHA-256 (tflite) | `0689abe1912a95a3318a0d8cb2e67bad0cbcfe3e24dd6e050c75debddfb6f891` |

## License

The model comes from [esphome/micro-wake-word-models](https://github.com/esphome/micro-wake-word-models),
which is **Apache-2.0**. The delivered file is byte-identical to the repository's
`models/v2/okay_nabu.tflite` (SHA-256 verified 2026-10-20), and
`scripts/test_esp32_wake_scorer.py` pins that hash, so a silent substitution is caught.
Apache-2.0 permits redistribution, so — unlike its predecessor — this model is clean for a
public prebuilt bundle.

## Why this model and why now

The previous bundled default was `hey_hermes` (microwakeword.com hosted trainer, community
library), licensed **personal and non-commercial only** — sufficient for a home satellite in a
private repository, but not for a public prebuilt bundle. In the house, the ESP32 satellite
reliably missed "Hey Hermes" while the Pi satellite's own model worked, so the ESP32's bundled
default moved to `okay_nabu`: an Apache-2.0 microWakeWord v2 model from the same ESPHome model
library the firmware's engine runs, with the same tensor contract (`[1, 3, 40]` int8 in,
`[1, 1]` uint8 out) the scorer validates at init.

`hey_hermes.tflite` + `hey_hermes.json` remain in this tree as a **rollback pair**, but a
complete rollback is three steps, not one, because the model, the model *selector*, and the
inference *policy* live in three separate places:

1. **Provision the blob.** Re-run the installer's provision step with the old model
   (`install-esp32.sh --provision --port <usb-port> --model firmware/esp32/models/hey_hermes.tflite
   --model-name hey_hermes`, plus the board's usual `--device-id`/`--endpoint`/Wi-Fi arguments —
   the provision path requires them, so a bare `--model` line exits with usage).
2. **Point the selector at it.** The firmware loads a model only when the blob's header name
   equals the NVS `wake_word` selector (which the config plane's `wake_word` setting drives).
   Set the board's desired `wake_word` to `hey_hermes`, or it will reject the blob and fall back
   to the placeholder.
3. **Restore the inference policy.** The cutoff/window/step/arena are compile-time defaults in
   `wy_mww_params_default()`, currently `okay_nabu`'s 0.97/5/10/26080. `hey_hermes` wants
   0.5/5/10/32000 — until those are reverted and the firmware rebuilt and flashed, the old model
   runs under the new (stricter, wrong-sized) policy. Reverting the parameters and rebuilding is
   the part that makes this a "restore the previous model" rather than a partial swap.

The model itself keeps the microwakeword.com personal/non-commercial terms, so it must not
enter a public bundle.

One difference that is expected, not a bug: `okay_nabu` ships `probability_cutoff` 0.97 while
`hey_hermes` shipped 0.5. The cutoff is the model's primary selectivity gate — the scorer only
reports a score when a 5-slice sliding window crosses it, and that score is the window's mean —
so 0.97 demands a much stronger, sustained match before a wake, at the cost of the phrase having
to be said clearly. The engine's `wake_threshold` (default 0.35) is a secondary gate on the
crossing score, which at a 0.97 cutoff is always ≥ ~0.97; the cutoff does the work. If real
activation proves too strict, the levers are saying the phrase clearly and the model itself,
not the threshold.
