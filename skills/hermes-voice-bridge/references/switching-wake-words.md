# Switching the wake word

The wake word is one flag on the satellite and, for a custom model, one flag plus one file on the wake
service. Everything below was verified on the device on 2026-09-22.

## The three kinds of switch, cheapest first

| Kind | Effort | How |
|---|---|---|
| A built-in model | minutes | `--wake-word-name <model>` on the satellite |
| A downloaded community model | minutes | drop the `.tflite` in the model dir, then as above |
| A model trained for a new phrase | about 1 hour, free | official Colab notebook or the openwakeword.com trainer, then as above |

Built-in models in this install (ask the install, never a web page):

```
/home/wyoming1/wyoming-openwakeword/.venv/bin/python3 -c \
  "from pyopen_wakeword import Model; print(sorted(m.value for m in Model))"
# ['alexa', 'hey_jarvis', 'hey_mycroft', 'hey_rhasspy', 'okay_nabu']
```

## The one rule that causes silent failure

**A custom wake word is named after its model file's stem**, with a trailing `_vN.N` stripped. The
satellite sends `--wake-word-name` with no validation, the service silently skips names it cannot
resolve, and `Describe` advertises built-in models only. So a name with no matching file produces **no
error at all**: the device just keeps listening for whatever it was listening for before.

Therefore: the file must be named `<name>.tflite`, and the spoken name is that stem. Rename downloads
accordingly (`Hey_Hermes_20260707_013715.tflite` would never work).

## Procedure

On the Pi, with the script (dry run first, it prints what it would do):

```
scp scripts/switch-wake-word.py wyoming1:/tmp/
ssh wyoming1 sudo python3 /tmp/switch-wake-word.py --name hey_jarvis            # dry run
ssh wyoming1 sudo python3 /tmp/switch-wake-word.py --name hey_jarvis --apply
# a fresh download, installed and switched in one go:
ssh wyoming1 sudo python3 /tmp/switch-wake-word.py --name hey_hermes \\
    --from-file /home/wyoming1/Hey_Hermes_20260707_013715.tflite --apply
```

The script backs up both systemd files, refuses to edit if the current text does not match what it
expects, verifies a TFLite header on any file it installs, pre-flights the new command line on a
scratch port, restarts in the order that works, and then checks the relay legs, the shim log and the
satellite state.

Doing it by hand, in this order:

1. Model file in place: `/home/wyoming1/wake-models/<name>.tflite`.
2. `/etc/systemd/system/wyoming-openwakeword.service.d/10-threshold.conf`: the single `ExecStart` line
   carries uri, threshold and `--custom-model-dir`. They share one file on purpose, because systemd
   takes the last `ExecStart` and a second drop-in would silently cancel the threshold.
3. `/etc/systemd/system/wyoming-satellite.service`: `--wake-word-name '<name>'`.
4. `systemctl daemon-reload`, then restart `wyoming-openwakeword`.
5. **Then `systemctl restart wake-shim`** (which bounces the satellite, it `Requires=` it), **then
   `systemctl start wyoming-satellite`**, because it does not come back on its own.

## Verify, in this order

1. **The model itself**, before deploying anything. Run `scripts/probe-wake-model.py` on the Pi with the
   wake service's venv: silence must be ~0, the built-in must score high on its own recording, and the
   new model must score low on that same recording. Reference numbers from the Hey Hermes install:
   0.0005 silence, 0.9655 built-in on `okay_nabu.wav`, 0.0007 for the other model on it.
2. **Registration**, before restarting the live service: run the new command line by hand on a scratch
   port with `--debug` and look for `Found custom model <name> at <path>` then `Ready`.
3. **The relay chain**: two established legs (`satellite->10400`, `shim->10401`), no `reconnecting`
   lines in the shim log, and `Waiting for wake word` in the satellite journal.
4. **The real thing**: a person says it. Watch for `wake word: <name>` in the bridge log on autumn.
   No TTS or synthesised audio can substitute; those score about 0.001 against any wake model.

## Threshold

`--threshold` lives in the same drop-in, default 0.5 upstream, currently 0.35 here (raised sensitivity
for a lower voice). Every model has its own baseline, and a model's published false-activation rate is
measured at its own benchmark point, usually 0.5, so lowering the threshold costs accuracy that the
published number does not describe. Change one thing at a time: switch the word first, and only touch
the threshold if it feels wrong, then measure rather than guess.

## Traps

- **Restarting the wake service alone breaks the wake path.** The shim churns on `relay ended: EOF`
  and microphone audio never reaches the wake model, so nothing wakes the device. Fix with the shim
  restart order above. It looks exactly like "the new wake word does not work".
- **`WARNING:root:Failed to get info from wake service`** in the satellite journal is benign and
  pre-existing: a timeout on the startup `Describe` through the shim. Counted 16 times in a day,
  including before any wake-word change, alongside 101 successful detections.
- **Do not pick a wake word that collides with the stop phrases.** A community model literally named
  "stop" exists, and `STOP_PHRASES` matches whole utterances, so such a word would make the device end
  its own conversation.
- **Custom verifier models, Silero VAD and Speex noise suppression are not usable here**: the service
  exposes only `--custom-model-dir`, `--threshold`, `--trigger-level` and `--refractory-seconds`.

## Rollback

Every run of the script leaves `*.bak-wakeword-<stamp>` copies of both files. Restore them (or set
`--wake-word-name 'ok_nabu'`), `daemon-reload`, restart the wake service, then `restart wake-shim` and
`start wyoming-satellite`. `ok_nabu` is a built-in, so it needs no model file. The Hey Hermes install
from 2026-09-22 also has `*.bak-hermes-20260922-212534` copies.

## Finding new wake words

The community library at openwakeword.com is larger than it looks, and its model metadata is public
even though the site shows a sign-in wall (the page itself calls
`api/models/community?page=1&per_page=50&sort=recent`, which answers 200 unauthenticated; walk pages
until `has_more` is false). Filter on `has_tflite` and on the quality fields, not on names or star
ratings:

- `false_activations_per_hour`: upstream's own target is under 0.5. Several popular uploads are far
  worse (the most-downloaded "Hey Hermes" model is 0.87, and its only rating is 1.0).
- `clean_recall`: often absent, which means recall is unmeasured. Silence there is not a good sign.
- The library is unmoderated user uploads: duplicates, junk and slurs are present.

As of 2026-09-22 it held 1,787 words / 3,169 models, of which 1,174 English words had a TFLite file.
Downloading needs a free sign-in; reading the library does not.
