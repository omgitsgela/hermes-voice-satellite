# Third-party components and their licences

This is a good-faith inventory, not legal advice, and it is dated: every licence below was checked on
**2026-09-24** and licences do change. Verify at the source before relying on an entry.

The project's own code is licensed under the PolyForm Noncommercial License 1.0.0, in [`LICENSE`](LICENSE).
Nothing here is re-licensed by that file.

## A. Redistributed in this repository

These are the components whose files actually ship in this repository. Each keeps its own licence, and a
redistributor must preserve those notices.

| Path | Component | Licence | Verified how |
|---|---|---|---|
| `pi/vendor/seeed-voicecard-0.3/` (53 files) | `respeaker/seeed-voicecard`, the audio driver for the Pi HATs | **GPL-3.0** | the vendored `LICENSE` file is the GNU GPL v3 text; the GitHub API reports `GPL-3.0` |
| `pi/vendor/native-satellite-13bb024/` (46 files) | `rhasspy/wyoming-satellite` at commit `13bb024`, our vendored copy with the local patch already applied | **MIT** | the GitHub API reports `MIT`; the upstream `LICENSE.md` is the MIT text, copyright 2023 Michael Hansen. The licence text now travels with the vendored copy at `pi/vendor/native-satellite-13bb024/LICENSE.md` |
| `reference/` | upstream copies of the satellite's own files, kept for protocol truth | **MIT** | same project and licence as above |
| `firmware/esp32/components/esp-tflite-micro/` | `espressif/esp-tflite-micro` v1.4.1 at commit `dd40859`, the TensorFlow Lite Micro interpreter the ESP32 wake model runs on | **Apache-2.0** | the vendored `LICENSE` is the Apache-2.0 text; see `firmware/esp32/components/VENDORED.md` for the exact commit and the one local edit |
| `firmware/esp32/components/esp-nn/` | `espressif/esp-nn` v1.4.1 at commit `5578173`, the SIMD kernel library esp-tflite-micro links against | **Apache-2.0** | the vendored `LICENSE` is the Apache-2.0 text |
| `firmware/esp32/components/esp-micro-speech-features/` | `esphome-libs/esp-micro-speech-features` v1.2.3 at commit `351c4c6`, ESPHome's fork of TFLM's microfrontend (the log-mel feature pipeline microWakeWord models train against) | **Apache-2.0** | the vendored `LICENSE` is the Apache-2.0 text |
| `scripts/data/enroll-words.txt` (2048 words) | a 2048-entry subset of the Electronic Frontier Foundation's **"Large Wordlist for Passphrases"** (2016, `eff_large_wordlist.txt`, 7776 entries): the shortest words first, sorted, lowercase — picked with `secrets.choice` to form the five-word satellite enrolment token | **CC BY 3.0** | downloaded from `https://www.eff.org/files/2016/07/18/eff_large_wordlist.txt`; the EFF page "A Deep Dive on Passphrase Generation" licenses the list CC BY 3.0. Attribution: the word list is by the Electronic Frontier Foundation, reused under CC BY 3.0 |

**Why this matters for the licence split.** The GPL-3.0 driver is somebody else's copyleft code: it stays
GPL-3.0, it cannot be made non-commercial, and this project's licence does not reach it. It is kept as
separate vendored files rather than merged into our own source. If you redistribute this repository, keep
the GPL text beside those files and keep the MIT notice beside the vendored satellite.

**If you are running the driver or the vendored satellite on a device**, do not strip those notices when
you copy files on to the device. They are small files; there is no reason to remove them.

## B. Installed, not redistributed

`requirements.txt` pins the Python environment. The installer fetches these from PyPI, so their licences
are the terms you accept by installing them, not terms this repository imposes.

| Component | Licence | Verified how |
|---|---|---|
| `wyoming`, the protocol library | MIT | GitHub API on `rhasspy/wyoming` and `OHF-Voice/wyoming` |
| `wyoming-openwakeword`, the wake service | Apache-2.0 | GitHub API on `rhasspy/wyoming-openwakeword` |
| `openwakeword`, the wake model library | Apache-2.0 | GitHub API on `dscripka/openWakeWord`, and its `LICENSE` file |
| `piper` and `piper-tts`, speech synthesis | MIT | GitHub API on `rhasspy/piper` |
| `faster-whisper`, speech recognition | MIT | GitHub API on `SYSTRAN/faster-whisper` |
| Whisper models | MIT | GitHub API on `openai/whisper` |
| `hermes-agent` | MIT | per the project's own package metadata |

The remaining packages in `requirements.txt` are transitive or auxiliary (numpy, aiohttp, onnxruntime,
ctranslate2, tokenizers, protobuf, webrtcvad, pysilero-vad and so on). Their licences are recorded in each
package's own metadata after install; every one of them is permissive in the versions pinned here, but
that was not individually re-verified for this file. To inspect what is actually installed:

```bash
python -c "import importlib.metadata as m; \
  print('\n'.join(f'{d.metadata[\"Name\"]}: {d.metadata.get(\"License\") or \"see LICENSE\"}' for d in m.distributions()))"
```

## C. Models and voices fetched at install

Neither the wake word model nor the voice is redistributed in this repository. Both carry terms of
their own, and the wake word default is non-commercial:

- **The recommended wake word model** comes from the community library of the hosted openWakeWord
  trainer, whose terms license such models for personal and non-commercial use only. Full detail, with the
  terms quoted: [`docs/wake-word-licensing.md`](docs/wake-word-licensing.md).
- **The voice the code defaults to**, `en_US-hfc_male-medium` (single speaker, 22,050 Hz, medium
  quality), is fetched at install time by `piper.download_voices` from the Piper voices catalog
  (<https://huggingface.co/rhasspy/piper-voices>) into the git-ignored `voices/` directory. Its
  dataset is **CC BY-NC-SA 4.0** (non-commercial). On 2026-10-26 the household auditioned two more
  permissive voices and kept them installed without making either the default:
  `en_US-libritts-high` (LibriTTS, **CC BY 4.0**, 904 speakers) and `clean100` (245 speakers,
  hosted by its author at <https://brycebeattie.com/files/tts> outside the catalog, dataset the
  "Clean 100" subset of LibriTTS-r, **CC BY 4.0** (<https://www.openslr.org/141/>), the author
  imposes no further restriction). A curated voice like `clean100` is fetched by
  `scripts/fetch-voice.py` with pinned sha256 (hashes recorded there and verified at fetch time).
  None is redistributed here. Piper voices differ from each other on terms, so check the terms of
  whichever voice you choose. Changing the voice is
  [`docs/switching-voices.md`](docs/switching-voices.md); this section must name the voice actually installed.

So the default configuration of this project as a whole is a non-commercial one, because of the
wake model and, since 2026-10-26 again, the voice too. A commercial deployment needs a wake model
whose own terms allow it and a voice whose terms do; the CC BY 4.0 voices auditioned on 2026-10-26
(`clean100`, `en_US-libritts-high`) would satisfy the voice half of that, and
[`docs/wake-word-licensing.md`](docs/wake-word-licensing.md) sets out the routes for the wake model.

## D. Keeping this file honest

When a vendored component is added, removed or updated, update the table above in the same change: the
path, the licence and what was checked. A stale inventory is worse than none, because it looks like a
review.
