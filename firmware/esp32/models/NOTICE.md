# Bundled wake model: attribution and licence

This directory holds the wake-word models that ship with the repository, so a plain install works
without the user supplying one. The installer (`scripts/install-esp32.sh`) provisions the default
model; `--model <path>` or `--download-model <url>` replace it afterwards.

## The default model

| | |
|---|---|
| File | `hey_hermes.tflite` (microWakeWord v2, streaming INT8 TFLite) |
| Wake word | "Hey Hermes" |
| Trained by | [microwakeword.com](https://microwakeword.com) hosted trainer, community library |
| Inference params | `hey_hermes.json` (`probability_cutoff` 0.5, `sliding_window_size` 5, `feature_step_size` 10, `tensor_arena_size` 32000) |
| Delivered SHA-256 (tflite) | `377545239b78e0671a92f1ae7a4bbbf9a18c170263edb9aba3db5f23fcb0ed4e` |

`scripts/test_esp32_wake_scorer.py` pins that hash and the four manifest numbers, so a silent
substitution or a drifted default is caught by the test suite.

## Licence, and what it means for a deployer

The default model comes from a hosted community trainer, and its terms are **personal and
non-commercial use only**. Shipping it as the default is a deliberate decision for this project,
whose own code is under the PolyForm Noncommercial License 1.0.0: the repository as published is a
non-commercial project, and the default model matches that.

**A commercial deployment must switch models.** The terms of the community model do not grant
commercial use, and this repository cannot grant it either. The switch is one provisioning command,
not a firmware rebuild, because each model carries its own inference policy in the model blob
(see below). Use a model whose licence permits your use, such as the Apache-2.0
`okay_nabu.tflite` in this directory, a model trained with the open-source toolkit, or a
commercially licensed model. `docs/wake-word-licensing.md` sets out the sources and their terms.

## The alternative model in this tree

| | |
|---|---|
| File | `okay_nabu.tflite` (microWakeWord v2, streaming INT8 TFLite) |
| Wake word | "Okay Nabu" |
| Trained by | [esphome/micro-wake-word-models](https://github.com/esphome/micro-wake-word-models) (Kevin Ahrendt) |
| Inference params | `okay_nabu.json` (`probability_cutoff` 0.97, `sliding_window_size` 5, `feature_step_size` 10, `tensor_arena_size` 26080) |
| Delivered SHA-256 (tflite) | `0689abe1912a95a3318a0d8cb2e67bad0cbcfe3e24dd6e050c75debddfb6f891` |
| Licence | **Apache-2.0**, redistribution permitted |

It ships so that switching to a redistributable model needs no download and no network access.

## Switching models is one provisioning step

The model partition holds one blob: a `WKM1` header followed by the model bytes. From header
version 2 the model's own inference policy (cutoff, window, step, arena) travels in that header,
written from the model's `.json` manifest by `scripts/provision-device-model.py`. The device scores
the model with the numbers the model was trained for, so a swap does not need a firmware change:

```sh
# provision the model and point the selector at it, in one install run
scripts/install-esp32.sh --provision --port <usb-port> \
    --model firmware/esp32/models/okay_nabu.tflite --model-name okay_nabu \
    --tls-certs <dir> --device-id <id> --endpoint <host>:10702 \
    --wifi-ssid <ssid> --wifi-psk-file <file>
```

The manifest is picked up automatically from `<model>.json` beside the model file; `--params-json`
overrides it. A model with no manifest is provisioned as header version 1, and the firmware's
compiled defaults apply, which is honest but only correct for the model those defaults describe.

Two numbers worth knowing, because they are the model's own selectivity gate rather than a bug:
`hey_hermes` ships `probability_cutoff` 0.5 and `okay_nabu` 0.97. The scorer reports a score only
when the 5-slice sliding window mean crosses that cutoff, so 0.97 demands a much stronger, sustained
match before a wake. The engine's `wake_threshold` (default 0.35) is a second gate on the crossing
score; at either cutoff the model's own gate does the work. If activation proves too strict or too
loose, the levers are the model and its manifest, not the engine threshold.

## Do not hand-edit the models

`hey_hermes.tflite`, `okay_nabu.tflite` and their `.json` manifests are upstream artifacts, pinned
by SHA-256 in the test suite and by the licence above. Replace a model by adding a file and
updating this notice, not by editing one in place.
