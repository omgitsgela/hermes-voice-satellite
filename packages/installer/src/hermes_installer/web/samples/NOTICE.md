# Voice samples

Two previews ship with the installer so a person can hear the default voices with no network at all:

- `en_US-hfc_female-medium.mp3`
- `en_US-hfc_male-medium.mp3`

Both are the sample clips published alongside their voices in the `rhasspy/piper-voices` repository
(MIT licensed), fetched from:

    https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/hfc_female/medium/samples/speaker_0.mp3
    https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/hfc_male/medium/samples/speaker_0.mp3

They are unmodified. The voice models themselves carry their own terms in that repository's model
card; this project ships the sample clips only, and only for the two voices it defaults to. Any other
voice is previewed by fetching its published sample on demand.

If a voice's terms ever require it, deleting these two files makes the installer fall back to the
remote URLs in `catalogue.py` and nothing else changes.
