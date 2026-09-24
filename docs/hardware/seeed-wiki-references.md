# Seeed documentation for this board

Local copies of what matters, so this page still works if Seeed reorganises their wiki or discontinues
the product. The board is a revision most of the web documents wrongly, so having the right documents
locally is worth more here than usual.

| Item | Where |
|---|---|
| **V2.0 schematic** (the revision we run, TLV320AIC3104) | [`seeed-2mic-hat-v2-schematic.pdf`](seeed-2mic-hat-v2-schematic.pdf), downloaded from Seeed's file host 2026-09-24 |
| Product page | https://www.seeedstudio.com/ReSpeaker-2-Mics-Pi-HAT.html |
| Seeed wiki | https://wiki.seeedstudio.com/ReSpeaker_2_Mics_Pi_HAT/ |
| Driver source, with the overlays and asound state | mirrored from `respeaker/seeed-voicecard`, see `pi/vendor/MANIFEST.md` |
| Enclosure (a third-party design, not Seeed's, and **not redistributable**) | https://makerworld.com/en/models/1232745-voice-assistant-wyoming-satellite |

## Licensing, before anyone commits a file from somewhere else

This repository is meant to be shareable, which makes third-party assets a question rather than a detail.

- **The enclosure design is under MakerWorld's Standard Digital File License and must not be committed
  here.** That licence forbids sharing, hosting, transferring or distributing the digital or printed files,
  including remixes, and forbids use in anything that charges money. A personal copy kept off this
  repository is fine; a copy in a repository is redistribution, and this repository is mirrored to a remote.
  **Do not add those files, in any format, to this tree.**
- **The V2.0 schematic** comes from Seeed's own wiki and is included for reference. Seeed publishes it
  publicly for this board; if their terms ever change, replace it with the link.
- **The sound assets** are CC BY 4.0, credited to Clayton Charles Tapp (cctaudio.com) via the Home Assistant
  Voice Preview Edition. Keep the credit if they are reused or modified.
- **Wake-word models** are community files with no stated licence. They are deliberately not committed here.
- Everything else in this repository is ours.

## Traps this board sets

- **The wiki mostly documents the older V1.x with a WM8960 codec.** Ours is the **V2.0 with a
  TLV320AIC3104**. Different driver, different overlay, different mixer controls. The V2.0 schematic is
  linked at the very bottom of the wiki page, which is easy to miss, and a local copy is above.
- **The HAT is a 40-pin board**, so it fits any Raspberry Pi with that header. Fitting is not support:
  see the host list in `../supported-devices.md`.
- **The 3.5 mm jack is unused** in our build. Playback goes through the speaker header.
- **Both `/dev/spidev0.0` and `/dev/spidev0.1` exist** on a Pi. Our LED service uses **bus 0, device 1**.
- **The enclosure is for the older revision too.** Check the insert before printing, per
  `seeed-2mic-hat-v2.md`.

## Mirroring

Every upstream we depend on is mirrored as a git bundle on the NAS under
`AI Projects/hermes-voice-satellite/upstream`, refreshed monthly. Rationale and the manifest are in
`pi/vendor/MANIFEST.md`.
