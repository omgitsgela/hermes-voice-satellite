# Plugin hardening and update recovery

Use this procedure when deciding whether a Hermes customization must remain a source patch or can survive updates as configuration or an out-of-tree plugin.

## Agent-run update: approvals and process lifetime

- If a terminal call returns an approval timeout, stop that blocked action. Plain-text user consent does not necessarily satisfy the runtime approval gate. Do not change approval settings, set bypass environment variables, or route through another tool to evade it. The user can enable the session's YOLO toggle themselves; only resume after they confirm. After user-enabled YOLO, `hermes update --help`, `hermes update --plan`, Git commits, and approved stale-marker cleanup have executed successfully. An exit 130 only proves interruption, not who caused it; ask rather than blaming the user or claiming changes happened.
- Do not infer the cause of invisible prompts from the configured timeout. Missing transport/UI delivery and an insufficient timeout are separate hypotheses. Increasing the timeout is not a verified repair for missing prompts.
- Read `/proc/self/cgroup` and the actual service units before choosing the update launcher. Desktop tool children can belong to `hermes-dashboard.service`, not `hermes-gateway.service`; updater restarts can kill either service's children. A plain background process or `nohup` does not escape a systemd cgroup. For self-updates, use a separately supervised process with durable output, and verify its cgroup and result rather than equating launch with completion.
- For preparation, inspect `git diff --cached --check`, staged paths, and current HEAD before committing. If Git reports missing author identity, use an agreed command-local identity (`git -c user.name=angela -c user.email=angela@local commit ...` for this local checkpoint) instead of changing global Git config. Tag the resulting commit, not just the pre-commit HEAD, and verify a clean tree.
- Before approved stash cleanup, inspect the exact stash and preserve its object with a named tag if recovery is useful. Before approved marker deletion, re-read each exact marker, confirm its recorded PID is dead, and remove only those confirmed stale paths. Never use an old session's PID as current evidence.
- Read `hermes update --help` on the installed version. `--yes` accepts updater prompts; it is not a bypass for the agent's command-approval gate. `--keep-stash` parks dirty changes rather than restoring them. Respect Angela's hold on source-patch reapplication. Check the configured backup mode explicitly, and never claim Git tags also back up external plugins or state.

### Verified isolated launcher (Linux user systemd)

The following launcher successfully started an updater under its own cgroup, distinct from the dashboard. Use a unique unit/log name on each run. This proves launch/isolation, not successful update completion; require the receipt and checks below.

```bash
bash -n /absolute/path/update-wrapper.sh
systemd-run --user --unit=hermes-update-<unique-id> \
  --property=Type=exec \
  --property=WorkingDirectory=/home/angela/.hermes \
  --property=StandardOutput=append:/home/angela/.hermes/logs/update-<unique-id>.log \
  --property=StandardError=append:/home/angela/.hermes/logs/update-<unique-id>.log \
  /bin/bash /absolute/path/update-wrapper.sh
systemctl --user show hermes-update-<unique-id>.service \
  -p ActiveState -p SubState -p MainPID -p ControlGroup
```

The wrapper must set `HERMES_HOME`, a verified executable `PATH`, and checkout cwd explicitly; run canonical `hermes update --yes --keep-stash`; capture its exit code immediately; then log fresh-process version, Git HEAD/status, config checks, plugin registration, and service PID/start times. Use `registry.get_entry('geolocate')`, not a nonexistent `registry.get`. Preserve the updater's nonzero exit even if later checks pass. Never add approval-bypass variables to this wrapper. The updater may restart the dashboard and disconnect the chat; the independent service and append-only log survive it. After reconnecting, read the log and `systemctl --user show <unit> -p Result -p ExecMainStatus`; perform real plugin operations separately before declaring the entire task complete. A full filesystem backup is separate from source Git tags: inspect backup config and free space first.

### Finishing after a self-update cleanup exception

- Treat `✓ Update complete!` as provisional until the process exits. The old updater can successfully swap code/dependencies and restart services, then raise when its cached modules import freshly changed code. Observed examples: `cannot import name 'file_signature' from 'utils'` and missing `_loaded_launchd_backend_jobs` on `hermes_cli.main_dashboard`. Do not immediately rerun a reset or reinstall: first inspect the durable log, actual checkout/version, service PIDs/start times, and fresh-process imports.
- The isolated systemd wrapper survives dashboard replacement and retains the true updater exit code even when the post-update checks pass. A failed wrapper does not alone mean the new services failed; equally, active services do not erase an updater exception. Report both.
- Run `hermes config check` in a fresh process. An old updater may say config is current using old defaults while the installed release requires a newer schema. Use canonical `hermes config migrate`, then read back `_config_version` and relevant custom values. Review and report behavioral migrations too: newer releases can shorten curator stale/archive windows or enable toolsets. Do not silently conflate those with a version-number-only update.
- Verify real TTS output using the returned `file_path`, not the requested extension: the delivery layer may convert a requested MP3 to an OGG voice bubble and remove the intermediate. Convert that returned audio to MP3 with ffmpeg before MiMo ASR. Check success/provider/transcript rather than asserting a requested path exists.
- Finish with fresh-process plugin registrations, a real TTS/ASR round trip, real extraction, and geolocation catalog reachability. Leave approved deferred core fixes parked, and report unrelated health warnings (disk space, dependency advisories, stale toolset names) rather than silently doing maintenance outside scope.

## 1. Establish the live update semantics

Do not infer updater behavior from `git status`, branch names, release notes, or a previous Hermes version. Read the installed updater implementation and trace these paths:

1. fetch/target resolution;
2. fast-forward attempt;
3. divergence reconciliation;
4. hard reset conditions;
5. dirty-tree autostash and restore;
6. syntax/import checks after stash restoration;
7. backup tags, rescue refs, and their retention;
8. service drain/restart behavior.

Collect repository facts before predicting loss:

```bash
cd ~/.hermes/hermes-agent
git status --porcelain
git branch --show-current
git rev-parse --is-shallow-repository
git merge-base HEAD origin/main
git merge-base --is-ancestor HEAD origin/main; printf 'exit=%s\n' "$?"
git log --oneline origin/main..HEAD
git stash list
git for-each-ref --format='%(refname) %(objectname)' refs/hermes-update-backups refs/tags/pre-update-
```

Interpret committed and uncommitted changes separately. A same-branch divergence path may hard-reset local commits while the dirty working tree is handled by autostash. A no-common-ancestor path may create a temporary rescue ref, but that is not a substitute for a configured backup: inspect pruning/retention before relying on it.

## 2. Classify each customization

Use this order:

1. **Supported configuration.** Prefer a documented config field or per-provider catalog override and verify that the actual consumer reads it.
2. **Out-of-tree user plugin.** Prefer `~/.hermes/plugins/<category>/<name>/` or `~/.hermes/plugins/<name>/` with `plugin.yaml` and `__init__.py`; user plugins are outside the source checkout.
3. **Upstreamable core fix.** Bugs in dispatch, state handling, accounting, or policy with no extension hook should become focused upstream changes rather than permanent plugins that monkeypatch core.
4. **Local source patch.** Keep only when no supported extension surface exists, and attach exact behavioral tests and a re-land procedure.

Inspect the live plugin APIs rather than relying on old notes. Relevant surfaces can include:

- `PluginContext.register_tts_provider`
- `PluginContext.register_transcription_provider`
- `PluginContext.register_web_search_provider`
- `PluginContext.register_tool`
- declared plugin hooks and provider registries

A hook's existence does not prove it can implement the requirement. Trace the real dispatcher or picker consumer end to end.

## 3. Build update-resistant plugins

For each migrated customization:

1. Put the implementation and manifest under the user plugin root, not the repository's bundled `plugins/` tree.
2. Declare the correct plugin kind and capability. User backend plugins may require an explicit `plugins.enabled` entry even when equivalent bundled backends auto-load.
3. Keep dependencies outside version-controlled source edits where supported; verify imports using Hermes's active Python environment.
4. Verify discovery, registry membership, config selection, and real dispatch in a fresh process. A successful import alone is insufficient.
5. Verify tool visibility separately from registration. Progressive tool search and configured toolsets can hide a registered tool.

## 4. Avoid built-in/provider name collisions

Built-in provider names take precedence over plugin providers in the TTS/STT registries. A plugin registered under a still-reserved built-in name is rejected, often with only a warning.

Migrate in this order:

1. Create the out-of-tree plugin and its tests.
2. In one controlled change, remove the in-tree implementation and its built-in-name reservation.
3. Enable the user plugin if its kind is gated.
4. Start a fresh process and verify plugin discovery, provider availability, config resolution, and one real operation.
5. Only then remove obsolete source-patch instructions.

Also test the reverse future collision: if upstream later introduces the same built-in name, discovery must surface the shadowing clearly rather than silently selecting the wrong implementation.

## 5. Update acceptance check

Before running the real update, produce a table for every customization with:

- current location;
- classification (config/plugin/core patch/local patch);
- expected updater treatment;
- backup/recovery handle;
- post-update verification command;
- restart requirement.

After the update, verify values and behavior, not just file cleanliness. Configuration can survive while its referenced provider or tool code disappears, producing a successful update followed by runtime failure. Read back each configured backend and exercise its dispatcher in a fresh process.

## 6. Worked migration: MiMo voice, Trafilatura, geolocation (2026-09-17)

The acceptance table that preceded this update:

| Customization | Post-migration location | Class | Updater treatment | Recovery handle | Verification | Restart? |
|---|---|---|---|---|---|---|
| MiMo TTS + ASR | `~/.hermes/plugins/mimo_voice/` | plugin | untouched (outside checkout) | none needed | `get_provider("mimo")` on both registries + one live TTS/ASR call | yes |
| Trafilatura extract | `~/.hermes/plugins/web/trafilatura/` | plugin | untouched | none needed | `get_provider("trafilatura").extract()` | yes |
| geolocation tool | implementation in `~/.hermes/plugins/geolocation/`, exposure wiring in core | plugin + 4 core lines | **wiring reset** | re-apply 4 lines | `geolocate` directly present vs real toolset list | yes |
| Cerebras pricing | `agent/usage_pricing.py` | core patch | reset | safety tag + commit | fresh-process resolver returns 0.99/1.49 | no |
| `/model` de-dup | `hermes_cli/model_switch.py` | core patch | reset | safety tag + commit | the 5-file model-switch suite | no |
| `config.set` unknown-session | `tui_gateway/methods_config_set.py` | core patch | reset | safety tag + commit | `tests/tui_gateway` suite | yes |
| bg-review filing policy | `agent/background_review.py` | core patch | reset | safety tag + commit | both prompt constants | no |
| Service PATH ordering | `hermes_cli/gateway.py` | core patch | reset | safety tag + commit | unit regen keeps `~/.local/bin` ahead of `/usr/bin` | yes |

The three core patches and the log-check patches remain candidates for upstream PRs; upstreaming them is what actually retires the reconciliation work.

## 7. Migration lessons that cost real debugging

- **A plugin tool always defers.** `is_deferrable_tool_name` exempts only `toolsets._HERMES_CORE_TOOLS` (plus `{"desktop_ui", "project"}`). There is no config key to pin a plugin tool as always-visible. If the tool must be ambient, keep its NAME core-listed and only its implementation in the plugin.
- **Probe direct exposure against the real platform toolset list.** `get_tool_definitions(enabled_toolsets=["<one-plugin-toolset>"])` returns only the bridge tools, because a lone plugin tool makes tool_search activate. That looks like a failed registration when registration is fine.
- **Load dotenv in standalone verification.** A bare interpreter has no `.env`, so a credential-gated provider reports a configuration error that never happens inside the gateway. Call `hermes_cli.env_loader.load_hermes_dotenv()` first.
- **Never let a plugin depend on a core table that updates reset.** A plugin that calls into a core registry keyed by name (for example a lazy-dependency table) breaks when the entry vanishes, and the failure can be non-graceful: a missing key raises into a broad handler and surfaces as `ImportError` even when the dependency is installed. Make the plugin self-sufficient.
- **Remove the in-tree implementation and its name reservation in the same step as enabling the plugin.** Built-ins win over plugins and the rejection is only a log warning, so a plugin migrated while the built-in reservation still exists silently does nothing.
- **Prove behavior parity by A/B against the original.** Extract the pre-migration module from git (`git show <sha>:<path>`) and run both implementations on identical input. When the tool's logic is unchanged, byte-identical output is the pass criterion, and it cleanly separates migration defects from pre-existing quirks.
- **Expect pre-existing quirks to surface during migration and do not "fix" them silently.** Example: the shared audio mime map sends `.ogg` as `audio/ogg`, but the MiMo ASR API accepts only wav/mpeg/mp3, so transcribing an Opus clip (including TTS voice-bubble output) fails with HTTP 400. Identical in the old in-tree handler. Report it; do not quietly change behavior during a migration.
- **Migrating a tool means migrating its documentation, not just its code.** After the move, grep every skill for the old module path and for the tool name. The skill that documents invoking the tool may hardcode the removed module as its fallback, and stale import lines or pytest paths fail with `ModuleNotFoundError` at exactly the moment someone needs them. Also make the invocation guidance exposure-agnostic: a plugin tool may be directly in the schema or deferred behind `tool_search` depending on session size, so the skill should describe both routes and must not treat a missing direct schema entry as a runtime bug. Verify the documented command actually runs before writing it into a skill.
- **A doc-reference sweep is part of migration acceptance.** Searching only the file you moved misses the test file, the reference notes, and the sibling skills. Sweep the whole skill tree for the removed path and confirm each remaining hit is either genuinely historical or already updated.

