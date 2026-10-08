# Vendored third-party components (ESP32 firmware)

These three components are **byte-for-byte upstream sources**, vendored here so `idf.py build`
works from a fresh checkout with **no component-registry or network access** (issue #110's
inference engine). Each keeps its own licence (all Apache-2.0); see `THIRD-PARTY.md` at the
repository root for the licence inventory and the models' NOTICE for the wake-word model's
separate personal/non-commercial terms.

| Component | Upstream | Version | Commit taken |
|---|---|---|---|
| `esp-tflite-micro/` | https://github.com/espressif/esp-tflite-micro | `1.4.1` (manifest) | `dd4085950a119d9e33a6de1d7be1fc7fe29e0cfc` (2026-09-17) |
| `esp-nn/` | https://github.com/espressif/esp-nn | `1.4.1` (manifest) | `5578173fa880a2bcf13f5d894e30ad998b03bcd3` (2026-10-08) |
| `esp-micro-speech-features/` | https://github.com/esphome-libs/esp-micro-speech-features | `v1.2.3` (tag) | `351c4c69530f5a802da5433581c4863afadf0a00` (2026-04-20) |

What they are, in this build:

- **esp-tflite-micro** — Espressif's port of TensorFlow Lite Micro (the interpreter the wake
  model runs on). **esp-nn** is its SIMD-optimised kernel library for Espressif chips
  (`CONFIG_ESP_TFLITE_MICRO_USE_ESP_NN=y` by default; disabling it falls back to the portable
  reference kernels with no code change).
- **esp-micro-speech-features** — ESPHome's fork of TFLM's audio microfrontend: the log-mel
  feature pipeline every microWakeWord model is trained against (`frontend.h` /
  `frontend_util.h`). It is the same engine ESPHome's `micro_wake_word` component runs
  (`espressif/esp-tflite-micro` + `esphome/esp-micro-speech-features`), so the bundled model
  sees exactly the feature pipeline it was trained on.

## What was dropped vs upstream

Only non-build files: `examples/`, `scripts/`, `test_app/`, `tests/`, `tools/`, `.github/`,
`.gitlab-ci.yml`, `.claude/`, `.vscode/`. No source file under `tensorflow/`, `signal/`,
`third_party/`, `src/`, or `include/` was modified.

## Local modifications (the only edits to vendored files)

- `esp-tflite-micro/idf_component.yml`: the `espressif/esp-nn` dependency gained
  `override_path: "../esp-nn"` so the dependency resolves against the vendored copy instead of
  the component registry. Nothing else in the manifests or build files was touched.

## Upgrading

Re-clone at the new tag/commit into the same directory (keeping the two exclusions above),
re-apply the `override_path` line, update the table here and the `THIRD-PARTY.md` rows, then
rebuild fresh (`rm -rf firmware/esp32/build && idf.py build`) and re-run
`scripts/test_esp32_wake_scorer.py` — it pins the model's tensor layout against the pipeline,
so an engine change that alters what the model expects fails there or at the scorer's on-device
tensor validation.
