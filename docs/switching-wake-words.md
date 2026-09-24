# Changing the wake word

The wake word is a TFLite model file. Changing it is a file and a service argument, not a code change.

## The model

Custom models live in the directory named by `--custom-model-dir` on the wake service (the
`wyoming-openwakeword` drop-in). The file name **is** the wake word: a model called `hey_hermes.tflite`
answers to "hey hermes". Built-in models ship with `wyoming-openwakeword` itself
(`alexa`, `hey_jarvis`, `hey_mycroft`, `hey_rhasspy`, `okay_nabu` at the time of writing) and need no file.

**TFLite only.** The service loads `.tflite`; an `.onnx` file in the same directory is ignored.

## Choosing one

Community models are listed at the OpenWakeWord community index. Two things are worth checking before
installing one, because the library is unmoderated:

- **`false_activations_per_hour`**, with a target below **0.5 per hour**. Above that the device wakes on
  conversation and television.
- **`has_tflite`**, since an ONNX-only model cannot be loaded.

Star ratings are not a useful signal here. Neither is a model's name: it tells you nothing about accuracy.

**Avoid a wake word that collides with a stop phrase.** If the model answers to a word the device also acts
on, it can stop itself. "Stop" is the obvious trap.

## Applying it

1. Put `<wake word>.tflite` in the custom model directory.
2. Restart the wake service, then the shim, then the satellite, **in that order**. Restarting the shim
   before the wake service leaves it reconnecting.
3. Test by speaking. A synthesised voice is useless for this: Piper saying a wake word scores around 0.001
   against any model, so a TTS file will never trigger one.

## Rolling back

Keep the previous model file in place, renamed so the service does not load it, e.g.
`previous-name.tflite.off`. Rolling back is then deleting the new file and restarting the three services
in the same order.

## Sensitivity

Most wake services take a threshold, and lowering it makes the device wake more readily at the cost of false
activations. Change it in the service arguments, not in the model. Note that a wake word with a high
false-activation rate cannot be fixed by raising the threshold alone: the model is the problem.
