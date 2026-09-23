# Core patches

These are diffs against the Hermes Agent source tree (`~/.hermes/hermes-agent`), captured from a working
install on 2026-09-23 at `HEAD 251e5f7c0a`.

They are uncommitted working-tree edits. `hermes update` does not preserve them: with
`updates.non_interactive_local_changes: stash` they are autostashed, and the voice surface degrades
until they are put back. `updates.pre_update_backup` is `false`, so this directory is the durable copy.

| Patch | File | Why | Voice-critical |
|---|---|---|---|
| `0001-toolsets-voice-surface-core-tools.patch` | `toolsets.py` | `hubitat`, `google`, `voice_satellite` and `geolocate` must be in `_HERMES_CORE_TOOLS`, otherwise they defer behind the `tool_search` bridge that this surface does not carry; also adds the read-only `skills_read` toolset | Yes |
| `0002-tools-config-skills-read-and-default-off.patch` | `hermes_cli/tools_config.py` | the `skills_read` row, plus `hubitat`, `google` and `voice_satellite` in `_DEFAULT_OFF_TOOLSETS` so starting a plugin does not hand house control, calendar access or device control to every bundled platform | Yes |
| `0003-api-server-route-extra-body-rescope.patch` | `gateway/platforms/api_server.py` | a route pointing at a different provider than the global default inherited the local provider's `extra_body`, which one provider rejects outright and others ignore; Hermes then fell back to a paid lane while still answering HTTP 200 | Yes |
| `0004-model-tools-empty-deferrable-catalogue.patch` | `model_tools.py` | stops the tool_search bridge being injected when nothing is deferrable. Costs tokens and a detour otherwise | No |
| `0005-cron-provider-snapshot.patch` | `cron/jobs.py` | cron stored the generic provider class instead of the requested provider, so jobs silently ran on the fallback model | No |

## Check what is applied

```bash
./check-patches.sh          # read-only, exit 1 if anything is missing
```

For each patch it tries `git apply --check --reverse`: success means it is already applied, a clean
forward check means it is missing, and neither means upstream restructured the file so the patch needs
review rather than a blind apply.

## Re-applying after an update

1. Run `check-patches.sh` and see what is missing.
2. Apply each missing patch **to the current upstream file**, never by restoring an old copy:
   `git apply -p1 <patch>` from the repository root, or patch by hand if it does not apply cleanly.
3. Compile what you touched:
   `./venv/bin/python -c "import py_compile; py_compile.compile('<file>', doraise=True)"`
4. Restart the gateway, then verify the surface (see the README's verification block) and speak to the
   device.

Do not restart the gateway casually: a restart drops live sessions. Agree a moment first.

The authoritative notes on why each patch exists, and the verification each one deserves, are in
`skills/hermes-local-patches/SKILL.md`.
