# Native Windows build and deployment

A community-contributed, operator-verified runbook for running the Satellite Gateway and
building the ESP32 firmware on **native Windows** (no WSL, no VM), written on 2026-11-02
against a real Windows deployment. It was authored directly on the NAS mirror while the
repo mirror job was paused, then reviewed, sanitized, and committed here on 2026-11-02.

Sanitization applied before committing: user-directory paths (`C:\Users\...`), the LAN
address of that installation's gateway host, the NAS drive mapping, and the operator's
personal name were replaced with `<placeholders>`. The package was scanned for private-key
headers, provider/GitHub/AWS token patterns, quoted credential assignments, callsigns, and
emails: clean, in both the original and this copy. `SHA256-MANIFEST.TXT` was regenerated
for this sanitized copy; hashes intentionally differ from the NAS original's manifest
because placeholder substitution changed bytes. Prose meaning is unchanged.

## Contents

- `WINDOWS-DEPLOYMENT-BUILD-GUIDE.TXT` — the main runbook: ESP-IDF on Windows (isolated
  toolchain, verified SDK commit + per-archive sha256 checks), firmware build, TLS/registry
  setup, device provisioning, the scheduled-task startup design, and an explicit
  "what was actually verified vs. historical context" section.
- `CONTEXT-AND-FALLBACK-ADDENDUM.TXT`, `MODEL-PIN-ADDENDUM.TXT`,
  `REASONING-AND-VOICE-BEHAVIOR-ADDENDUM.TXT` — how the Windows installation pins a voice
  model + reasoning effort per request via `voice-model.json`, fallback behavior, and the
  "One moment." silence filler. (The pin feature itself is not in `main`; see below.)
- `RESTART-AND-RECOVERY-VERIFICATION.TXT` — restart/recovery evidence from that machine.
- `DOCUMENTATION-REVISION-NOTES.TXT` — the authors' own change log and scan record.
- `voice-model.example.json` — example voice-model pin file.
- `snapshots/` — reference copies, **not applied patches**:
  - `snapshots/project/scripts/windows/` — the supervisor, scheduled-task definition, and
    verification scripts used on that machine. These are genuinely useful and are the seed
    of what should eventually land in `scripts/windows/`.
  - `snapshots/project/satellite_gateway/`, `snapshots/project/packages/...` — that
    machine's working copies of four core files, carrying unmerged Windows-compatibility
    changes (platform-native Hermes home, `os.fchmod` guard, the voice-model pin /
    `load_runtime_config` feature, GBK-safe subprocess decoding). Diffs versus the source
    commit they name (`56cd727`) are small and reviewable; merging them into `main` is a
    separate decision, deliberately not made by this import.
  - `snapshots/esp/` — `run_idf.py` (isolated ESP-IDF invocation for Windows) and
    `verify_downloads.py` (sha256 verification of SDK tool archives).

## How to read it

Treat the guide as a runbook to review before running, not a script: it says so itself.
Every `<PLACEHOLDER>` must be replaced first. The deployment it describes used a Limited
interactive scheduled task rather than a service, approved by that operator for that
machine — the same choice is yours to make.
