---
name: hermes-local-patches
description: "Audit, preserve, migrate, and re-apply Hermes customizations across updates."
version: 1.0.0
author: Angela
created_by: agent
---

# Hermes Local Source Patches

Treat every customization as one of four classes before an update: supported configuration, out-of-tree user plugin, upstreamable core fix, or unavoidable local source patch. Prefer them in that order. Do not assume the updater merges or preserves local commits; inspect the live updater path, repository ancestry, dirty tree, and backup behavior first.

## When This Applies

- **Migrated to plugins on 2026-09-17 — do not re-add to core:** MiMo TTS + ASR, Trafilatura extract, and the geolocation tool implementation. See **Migrated customizations** below.
- Custom model catalog entries in `hermes_cli/models.py`, `codex_models.py`, `model_switch.py`
- `/model` provider double-count de-dup in `hermes_cli/model_switch.py` (`_configured_provider_matches`) — see **Model-Switch provider de-dup patch** below
- Geolocation **exposure wiring** (unavoidable, 4 lines): `geolocate` in `_HERMES_CORE_TOOLS` plus the `geolocation` entry in `TOOLSETS` (`toolsets.py`) and the `CONFIGURABLE_TOOLSETS` row (`hermes_cli/tools_config.py`). The implementation is a plugin; the wiring cannot be, per the deferral rule below.
- Geolocation reference data stays outside the repo: `~/.hermes/data/geolocation/` and `~/cdot_crossroads/`. Its tests now live at `~/.hermes/plugins/geolocation/test_geolocate_tool.py`.
- Any other source-level additions not yet upstreamed
- Background self-improvement filing policy in `agent/background_review.py` when the built-in memory-only prompt does not apply Angela's memory/skill/vault/session classifier

## OpenRouter Presets In The Picker (config-first, no source patch)

`openrouter/free` (and any other OpenRouter preset/id missing from the curated manifest) is NOT selectable via the normal picker, because the online picker intersects the remote Hermes manifest against the live OpenRouter catalog, and the upstream manifest only lists individual models. Do NOT patch `OPENROUTER_MODELS` in `hermes_cli/models.py` for this.

**Config-first mechanism** (discovered 2026-09-11): `hermes_cli/model_catalog.py` supports per-provider manifest URL overrides via `model_catalog.providers.<name>.url` in `~/.hermes/config.yaml`. The override is re-fetched on every picker call (no disk cache) and takes precedence over the global manifest.

**Angela's live setup:**
- `~/.hermes/config.yaml`: `model_catalog.providers.openrouter.url: file:///home/angela/.hermes/data/model-catalog/openrouter.json` (top-level `model_catalog.url` stays the upstream URL).
- `~/.hermes/data/model-catalog/openrouter.json`: a local manifest with `version: 1`, `providers: {openrouter: {models: [...]}}` — the upstream openrouter block verbatim (58 entries, preserving order) plus `{"id": "openrouter/free", "description": "free router"}` appended.
- Rebuild after `hermes update` or whenever upstream curation changes: re-fetch upstream manifest, re-append local additions, write back. Verify with a fresh process: `fetch_openrouter_models(force_refresh=True)` must include `openrouter/free`, and the model must exist in the live OpenRouter `/v1/models` catalog advertising tool support or the picker hides it.
- Gotcha: `providers` must be `{"openrouter": {"models": [...]}}` — spreading the provider block at the top level of `providers` passes `_validate_manifest` shape checks poorly and `_fetch_manifest` returns None (urlopen of the file:// URL works fine, so the failure is the JSON shape, not the URL).
- No gateway restart needed: the override is read from config on every picker fetch; `hermes config check` must stay green after the edit.

## Config-First Rule

**Always check if config.yaml can solve it before patching source files.** Config survives updates; source patches don't.

| Need | Config approach | Source patch needed? |
|------|----------------|---------------------|
| Quick model shortcuts | `model.aliases.<name>: provider/model-id` | No |
| Custom API endpoint | `providers.<name>` in config.yaml | No |
| New TTS/STT provider | User plugin via `register_tts_provider` / `register_transcription_provider` | No, when the live release exposes those registries |
| New web provider | User backend plugin via the web-provider registry | No, when the live release exposes that registry |
| New tool/toolset | User plugin via `register_tool` | No, when the live release exposes plugin toolsets |
| Model catalog entries | Per-provider catalog URL override when the consumer honors it | Maybe; verify the actual picker consumer |

## Plugin-hardening workflow

Before carrying a customization as a source patch, follow `references/plugin-hardening-and-update-recovery.md`. It covers live updater inspection, ancestry-sensitive reset risk, extension-surface classification, out-of-tree user-plugin layout, built-in/plugin name collisions, migration sequencing, and post-update verification.

## Migrated customizations (2026-09-17)

Three customizations left the source tree. They survive `hermes update` untouched because they live under `~/.hermes/plugins/`:

| Plugin dir | `plugins.enabled` key | Interface | Selected by |
|---|---|---|---|
| `mimo_voice/` | `mimo-voice` | `ctx.register_tts_provider` + `ctx.register_transcription_provider` | `tts.provider: mimo`, `stt.provider: mimo` |
| `web/trafilatura/` | `web/trafilatura` | `ctx.register_web_search_provider` | `web.extract_backend: trafilatura` |
| `geolocation/` | `geolocation` | `ctx.register_tool` (implementation only) | `geolocation` in `platform_toolsets` |

Everything they replaced was deleted from core: the MiMo handler code and the `mimo` entries in `BUILTIN_TTS_PROVIDERS` / `BUILTIN_STT_PROVIDERS` / both `_BUILTIN_NAMES` sets; `tools/geolocation_tool.py` and its in-tree test; the in-tree `plugins/web/trafilatura/`; and the `tools/lazy_deps.py` + `tools_config_post_setup.py` trafilatura wiring. The trafilatura plugin self-installs its pinned package (`trafilatura==2.1.0`, honoring `security.allow_lazy_installs`) rather than depending on the core lazy-deps table, which updates reset.

### The deferral rule — why geolocation keeps 4 core lines

`tools/tool_search.py:is_deferrable_tool_name` returns False **only** for names in `toolsets._HERMES_CORE_TOOLS`, plus a hardcoded `_DIRECT_SURFACE_TOOLSETS = {"desktop_ui", "project"}`. Every other tool, including any plugin tool, defers behind the `tool_search`/`tool_describe`/`tool_call` bridge as soon as one deferrable tool exists. No config key pins a plugin tool as always-visible: `defer` only *adds* deferral, and `tools.tool_search.enabled: off` would un-defer the whole plugin surface.

So a tool that must be **directly present** keeps its name in `_HERMES_CORE_TOOLS` even when the implementation is a plugin. Verify direct exposure against the **real** platform toolset list:

```python
import model_tools
from hermes_cli.config import load_config
cli_ts = (load_config() or {}).get("platform_toolsets", {}).get("cli") or []
names = [d["function"]["name"] for d in model_tools.get_tool_definitions(enabled_toolsets=cli_ts, quiet_mode=True)]
assert "geolocate" in names
```

Probing `enabled_toolsets=["geolocation"]` alone is a trap: with one plugin tool in the set, tool_search defers everything, the probe returns only the bridge, and a correctly-registered tool looks broken.

Post-update residue for these three is only the geolocation exposure wiring: the `geolocate` name in `_HERMES_CORE_TOOLS`, the `geolocation` `TOOLSETS` entry, and the `CONFIGURABLE_TOOLSETS` row.

**Angela's decision (2026-09-17): do NOT auto-re-apply any residue after an update.** Complete the update first, then reevaluate each residue item on its merits and apply only what is still wanted. This covers the geolocation exposure wiring and every core patch in this skill. State the behavioral consequence before applying: if the geolocation wiring is left off, `geolocate` still registers from the plugin and remains callable, but the name is no longer in `_HERMES_CORE_TOOLS`, so it is deferred behind the tool_search bridge instead of being directly present in the schema.

## Browser integration: update-adjacent but not a Hermes source patch

Angela's stealth-browser setup does not modify files inside the Hermes source repository. Its durable pieces are `/home/angela/.hermes/.env`, `/home/angela/.agent-browser/`, the user-local agent-browser installation, and browser skills. Load `angela-browser` and follow `references/stealth-browser-rebuild.md` after a Hermes or agent-browser update.

Do not rely on a manual edit to `/home/angela/.config/systemd/user/hermes-gateway.service`: Hermes can regenerate that unit during a planned restart. Verify executable resolution and the actual built-in-browser fingerprint after restart instead. As of 2026-08-03, the preferred user-local binary is `/home/angela/.local/bin/agent-browser` 0.33.2 while `/usr/bin/agent-browser` remains 0.27.0.

On Hermes 0.20+, an absent `browser.backend` defaults to the new Browser Use CLI when available. That hides the built-in `browser_*` agent-browser tools and bypasses Angela's custom fingerprint configuration. Set `browser.backend: off` to force the established built-in agent-browser route, then gracefully restart and live-test the fingerprint. Before diagnosing that route as broken, also check `browser.use_real_profile`: when enabled, profile snapshotting is a separate prerequisite and an incomplete native profile directory can shadow a populated Snap/Flatpak profile. Do not disable real-profile mode or move authenticated profile data without Angela's approval. Do not accept the plausible but incorrect diagnosis that Windows-identifying fields require a Windows VM: Angela's Linux Chrome setup intentionally and successfully normalizes UA, platform, client hints, window/screen dimensions, and webdriver through `config.json`, the consistency script, and launch-mutator plugin. See `references/browser-backend-selection-after-updates.md` for the source check, config-first repair, real-profile decision path, three-layer verification, and post-restart acceptance test.

### Picker Pins / Curated-First Models Are Not Aliases

Angela uses **per-provider curated-first ordering** as picker pinning: selected models remain at the top of a busy provider's `/model` list, while the provider's full live catalog remains available below them. Do not replace this with model aliases; aliases solve a different problem and are not wanted for this workflow.

As of Hermes v0.19.0 there is no documented `config.yaml` pin/favorites field. The prior NVIDIA and OpenRouter behavior was implemented through source-level curated list ordering and can be overwritten by `hermes update`.

- **NVIDIA:** the curated per-provider lists no longer live in `hermes_cli/models.py`; as of v0.21.0 they are in `hermes_cli/models_catalog_static.py` (the `"nvidia"` key) and are still merged curated-first with the live catalog, so their order acts as pins. Verify the live list against the canonical top five in Obsidian `20 Areas/AI Systems/Hermes.md`. This one is easy to lose silently: the file is **upstream content**, so after an update it shows clean in `git status` even when Angela's ordering has been replaced. Check the list contents, not the file's dirty state.
- **OpenRouter:** `fetch_openrouter_models()` prefers the remote model-catalog manifest over local `OPENROUTER_MODELS` and filters to live models advertising tool support. Merely editing local `OPENROUTER_MODELS` may not affect the normal online picker. Any pin restoration must account for that remote-manifest path rather than assuming the fallback list controls online ordering.
- Canonical desired lists and dated validity notes are in Obsidian `20 Areas/AI Systems/Hermes.md` under **Curated/pinned models in busy provider pickers**.

Prefer a supported pinning config if upstream adds one. Until then, treat picker pinning as a documented source customization and verify actual picker output after every update.

## Background Review Filing-Policy Patch

Angela's automatic self-improvement review must classify durable information before writing. The local source patch is in `agent/background_review.py` and applies to both `_MEMORY_REVIEW_PROMPT` and the memory portion of `_COMBINED_REVIEW_PROMPT`.

Required behavior:

1. Procedures, commands, troubleshooting, tool quirks, and workflow corrections go to an existing class-level skill first.
2. Stable identity and broad preferences go compactly to the user profile.
3. Detailed person/project/organization/decision facts are not dumped into memory when vault tools are unavailable.
4. Live state, temporary progress, completed-work logs, stale IDs, and one-session results are skipped.
5. Persistent memory keeps only cross-cutting rules and compact canonical-source pointers.
6. The reviewer verifies a skill destination before shortening memory to a pointer and prefers `Nothing to save.` over marginal capture.

After every `hermes update`, inspect both prompt constants and reapply the policy if upstream has not incorporated equivalent behavior. Verify with:

```bash
cd ~/.hermes/hermes-agent
./venv/bin/python -m py_compile agent/background_review.py
./venv/bin/python -m pytest -q -o 'addopts=' \
  tests/run_agent/test_background_review.py \
  tests/agent/test_skip_memory_store_65429.py
```

Also verify the auxiliary route independently of the chat default:

```bash
hermes config get auxiliary.background_review
```

Expected Angela-specific route: `openai-codex` / `gpt-5.6-sol`. Treat that as live configuration and verify it rather than relying on this skill as current-state evidence.

## Cerebras Qwen Pricing Patch

Preserve the narrow Cerebras pricing addition in `agent/usage_pricing.py` after updates:

- Add the `cerebras` / `qwen-3.8-27b` bundled snapshot at $0.99 input and $1.49 output per million tokens, version `cerebras-pricing-2026-09-06`. The user supplied and authorized these rates; they were subsequently independently verified by fetching `https://inference-docs.cerebras.ai/models/qwen-3.8-27b.md`, so `official_docs_snapshot` attribution is supported. Re-fetch before refreshing rates; do not invent cache discounts or cache-write prices.
- Resolve named provider `cerebras` and runtime provider `custom` with `base_url_hostname(url) == "api.cerebras.ai"` to the Cerebras snapshot route. Use exact hostname equality, not suffix/subdomain/path matches. Leave unrelated providers and unknown-model metadata fallback unchanged.
- Keep the existing accounting: `normalize_usage` includes reasoning in `output_tokens` via `completion_tokens`; `reasoning_tokens` is informational, not added again in `estimate_usage_cost`.
- Reapply to current upstream structure, not by overwriting with an old file. Preserve the two behavioral tests in `tests/agent/test_usage_pricing.py`: `test_cerebras_bundled_cost_counts_reasoning_once` and `test_cerebras_custom_route_matches_only_exact_hostname`.
- Verify compilation and run `scripts/run_tests.sh tests/agent/test_usage_pricing.py tests/agent/test_billing_usage.py tests/agent/test_account_usage.py tests/agent/test_codex_usage_attribution.py tests/gateway/test_usage_command.py tests/hermes_state/test_aux_usage_accounting.py --file-retries 0`. Also check a fresh-process resolver returns Decimal rates `0.99`/`1.49` for both routes; a synthetic 1,000-input/600-completion usage with 400 reasoning tokens costs $0.001884, not an extra reasoning charge.
- Source changes require a fresh consuming process to take effect; do not restart services or modify config unless that work is separately authorized.

## Model-Switch provider de-dup patch

Fixes the `/model <name>` error `'<name>' is declared by multiple configured providers (cerebras, custom:Cerebras). Re-run with --provider <slug>` that fires for a keyed `providers.<key>` entry that also carries a `name` field. It is NOT provider-specific — any named provider trips it, and it is masked when the current provider is already the declaring provider (the `current in matches` branch wins), so it only surfaces on cross-provider bare-name switches (e.g. typing `qwen-3.8-27b` while on local llama.cpp).

**Root cause** (`hermes_cli/model_switch.py`, `_configured_provider_matches`): it gathers candidates from BOTH the raw `providers:` dict (slug = the key, e.g. `cerebras`) AND the normalized legacy list that `get_compatible_custom_providers` builds from the *same* dict (slug = `custom:<name>`, e.g. `custom:Cerebras`). Both declare the model → two matches → ambiguity.

**Fix (shipped):** right after the raw-key candidate loop, collect the raw key slugs into a lowercased set, then in the `custom:<name>` loop skip any custom entry whose `provider_key` is already in that set (the canonical raw key keeps the match). Legacy `custom_providers:` list entries carry no `provider_key` and keep their alias. De-dup is keyed on the provider **key**, never the model id or `name`, so deliberately distinct model ids across providers are unaffected.

**Config-first note:** NOT solvable in config. Removing a provider's `name` field does **NOT** fix it — the normalizer falls back `name = provider_key`, still yielding a distinct `custom:<key>` that collides with the raw key. One-off bypass: `/model <model> --provider <key>`. This is a genuine source patch — re-apply to the current upstream version of `_configured_provider_matches` after every `hermes update` (do not overwrite the file with a saved copy; upstream may restructure it). Do **not** restart the gateway just for this unless separately authorized; a fresh consuming process picks it up. Angela intentionally names otherwise-similar models differently across providers (e.g. `qwen38` on `llamacpp-qwen38` vs `qwen-3.8-27b` on `cerebras`) to keep them easy to tell apart — preserve that.

Verify:
```bash
cd ~/.hermes/hermes-agent
./venv/bin/python -c "import py_compile; py_compile.compile('hermes_cli/model_switch.py', doraise=True)"
./scripts/run_tests.sh \
  tests/hermes_cli/test_model_switch_configured_provider_routing.py \
  tests/hermes_cli/test_model_switch_custom_providers.py \
  tests/hermes_cli/test_user_providers_model_switch.py \
  tests/hermes_cli/test_custom_provider_identity.py \
  tests/gateway/test_model_command_custom_providers.py --file-retries 0
```
Expect ~86 tests pass, 0 failed. Also confirm a fresh process routes a cross-provider bare-name switch to the declaring provider's key (not `custom:<name>`) instead of reporting ambiguity.

## Cron snapshot provider-name patch (cron/jobs.py)

**Why:** `_compute_provider_model_snapshots` stored `snap.get("provider")` which is the generic billing class (`custom`) for any user-defined provider in `providers:`, not the specific name (`llamacpp-qwen38`). At fire time the scheduler passed `requested='custom'` to `resolve_runtime_provider()`, which has no endpoint behind it and raised `AuthError`, causing the fallback chain to activate and the turn to run on `openrouter/free` instead of the local model. Two cron jobs were silently burning free-tier quota because of this.

**Fix (one line, `cron/jobs.py` around line 1695):** change
```python
provider_snapshot = str(snap.get("provider") or "").strip().lower() or None
```
to
```python
provider_snapshot = str(snap.get("requested_provider") or snap.get("provider") or "").strip().lower() or None
```

`requested_provider` is the specific provider name the caller asked for (e.g. `llamacpp-qwen38`); `provider` is the billing class (e.g. `custom`). Storing the former makes the fire-time resolver route to the correct named provider and its endpoint.

**Verify:**
```bash
cd ~/.hermes/hermes-agent
./venv/bin/python -c "import py_compile; py_compile.compile('cron/jobs.py', doraise=True)"
./venv/bin/python - <<'PY'
from cron.jobs import _compute_provider_model_snapshots
ps, ms = _compute_provider_model_snapshots(provider=None, model=None, base_url=None, no_agent=False)
assert ps == 'llamacpp-qwen38', f'expected llamacpp-qwen38, got {ps}'
print(f'snapshot OK: provider_snapshot={ps!r} model_snapshot={ms!r}')
PY
./scripts/run_tests.sh tests/cron/ --file-retries 0
```

Also verify a live route: `hermes -z "Reply with exactly: LOCAL OK" -m qwen38 --provider llamacpp-qwen38` must return `LOCAL OK`.

**Re-apply after every `hermes update`** — this is a one-line change in `cron/jobs.py` that upstream may restructure. Check `_compute_provider_model_snapshots` for the `requested_provider` fallback; if absent, re-apply. Do not overwrite the file with a saved copy.

**Config-first note:** setting `model.base_url` in config.yaml does NOT fix bare `custom` resolution (tested: `resolve_runtime_provider(requested='custom')` still raises `AuthError` even with `model.base_url` set). The config key is used by a different code path (`_config_base_url_trustworthy_for_bare_custom`) for the picker's bare-custom row, not the explicit `requested='custom'` path. The source patch is the only fix for the snapshot.

## config.set unknown-session error patch (tui_gateway/methods_config_set.py)

Authorized by Angela 2026-09-16: a session-scoped `config.set` that names a session which does not resolve must ERROR, not answer a successful `{key, value}`. Otherwise (the reported bug) a client whose runtime id had been reaped kept displaying the new value while the conversation ran the old one, with no error and no way to detect it. She wants the failure surfaced so there is something to fix.

**Where:** the `config.set` dispatcher (bottom of `tui_gateway/methods_config_set.py`), plus a new `_session_scope_required(key, params)` predicate above it:

```python
named_sid = str(params.get("session_id") or "").strip()
session = _sessions.get(named_sid)
if named_sid and session is None and _session_scope_required(key, params):
    return _err(rid, 4001, f"session not found: {named_sid}")
```

`_session_scope_required` is deliberately narrow — only keys whose ENTIRE effect is the live session: `model` (else: applied to a throwaway `{"agent": None}` record), `fast` (else: flips global `agent.service_tier`), and `reasoning`/`yolo` when `scope != "global"` (else: global config / process-global `HERMES_YOLO_MODE`). Global-by-design keys (`theme`, `density`, `cwd`, `verbose`, `focus`, display toggles) are EXEMPT so a stale id cannot break a legitimate global write. A request naming no session (a draft) keeps its old behaviour.

4001 is the established stale-runtime contract (`_sess_nowait`): the client re-resumes the STORED id and retries. Talaria's `setModel` does exactly that.

**Verify:**
```bash
cd ~/.hermes/hermes-agent
./venv/bin/python -c "import py_compile; py_compile.compile('tui_gateway/methods_config_set.py', doraise=True)"
./scripts/run_tests.sh tests/tui_gateway --file-retries 0
```
Expect 107 files / 985 tests, 0 failed, including the added `tests/tui_gateway/test_config_set_unknown_session.py`.

Pyright flags `_sessions`/`_err` as undefined in this module — expected: `bind_module` rebinds the module's functions onto server.py globals at install time, and the pre-existing code references those names the same way. Ignore that specific diagnostic.

**Needs a gateway restart to take effect** (source change). Do NOT restart unilaterally — a restart drops live sessions; agree a moment with Angela first.

## API-server route extra_body re-scope patch (gateway/platforms/api_server.py)

**Why:** with a user-defined local provider as the global default (`model.provider: llamacpp-qwen38`),
`gateway.api_server.model_routes` aliases pointing at a DIFFERENT provider silently ran on the wrong
backend. `_resolve_runtime_agent_kwargs()` returns the default provider's `request_overrides`
(including its `custom_providers` `extra_body`), and `AIAgent.__init__` merges the matched provider's
extra_body UNDER what is already present — `_merge_custom_provider_extra_body` lets existing keys win.
So after `_select_agent_runtime` switched the provider, the stale entry still reached the wire.

Concrete failure (2026-09-22): `chat_template_kwargs: {preserve_thinking: true}` from the local qwen38
provider was sent to `api.cerebras.ai`, which rejects that property outright:
`HTTP 400 chat_template_kwargs: property 'chat_template_kwargs' is unsupported (wrong_api_format)`.
Hermes then fell back to `openrouter/free` and **still returned HTTP 200**, so `GET /v1/models` looked
correct and the turn looked successful. Cost side effect: the fallback logged
`PAID lane engaged for auxiliary task`, so every failed turn spent real money on a paid auxiliary lane.

**Not Cerebras-specific.** Any API-server request routed to a provider other than the local default
inherits the local `extra_body`; only Cerebras rejects it loudly. The `/model` switch and fallback
paths are separate and were already correct.

**Fix:** in `_create_agent`, capture `runtime_kwargs["provider"]` / `["base_url"]` BEFORE
`_select_agent_runtime`, then after it drop the stale `extra_body` key from
`runtime_kwargs["request_overrides"]` when either value changed. `agent_init` re-derives extra_body for
the provider that actually runs. Only `extra_body` is dropped; `service_tier` / `speed` and any
caller-supplied keys are preserved. Marked with `--- LOCAL PATCH (Angela, 2026-09-22)` comment blocks in
both places. This mirrors the two paths that already handle it:
`agent/agent_runtime_helpers.py` (the /model switch: `overrides.pop("extra_body", None)`) and
`agent/chat_completion_helpers.py::_rescope_fallback_extra_body` (the fallback chain).

**Config-first note:** NOT solvable in config. Adding an explicit `base_url` to the route does **not**
help (tested — the stale extra_body is attached before the route applies). Removing
`chat_template_kwargs` from the default provider's `extra_body` does work, but costs the local model
its client-side `preserve_thinking`. `model_routes` allowed keys are only `model`, `provider`,
`api_key`, `base_url` — there is no per-route `extra_body` escape hatch.

**Diagnosis rule — never read this from the HTTP status.** An upstream 400 on a route still returns
HTTP 200 to the caller. Read `agent.conversation_loop` / `Model fallback` / `BadRequestError` lines in
the gateway journal and confirm which provider actually served the turn. To separate a payload problem
from an auth problem, probe the upstream directly field by field (Cerebras accepts `reasoning_effort`,
`clear_thinking` and `max_tokens`; it rejects `chat_template_kwargs`).

**Verify:**
```bash
cd ~/.hermes/hermes-agent
./venv/bin/python -c "import py_compile; py_compile.compile('gateway/platforms/api_server.py', doraise=True)"
```
Then prove it on a running gateway (a source change needs a fresh consuming process):
`POST /v1/chat/completions {"model": "voice"}` must report `qwen-3.8-27b` in its answer, and the
journal must show **no** `BadRequestError` and **no** `Model fallback` line for that session. Also run a
default-route turn (no `model` field) as a regression check: it must still run on the local provider.
Live proof 2026-09-22: voice route reported `Qwen 3.8 27B (qwen-3.8-27b)`; default route reported
`qwen38`; journal clean. No unit test was added — verification is by live end-to-end proof, so
re-verify this way after any upstream change to the route path.

**Re-apply after every `hermes update`.** Search `_create_agent` for `_pre_provider`; if absent,
re-apply both blocks. Do not overwrite the file with a saved copy — upstream may restructure it. As of
this patch the working tree carries two uncommitted edits: `gateway/platforms/api_server.py` (this one)
and `cron/jobs.py` (the cron snapshot patch above), on HEAD `251e5f7c0a`.

## Hubitat direct-exposure line (toolsets.py)

**Why:** the `hubitat` tool is a plugin tool, and plugin tools DEFER behind tool_search. On the
voice surface tool_search's catalogue is otherwise empty, so the tool was simply invisible:
`get_tool_definitions(enabled_toolsets=["safe","skills","hubitat"])` returned the bridge but not
`hubitat`, and the agent could not read a house sensor at all. Confirmed in-process:
`is_deferrable_tool_name("hubitat")` was True before the line and False after, and the name then
appeared in the assembled schema.

**Fix:** one line in `_HERMES_CORE_TOOLS` in `toolsets.py`, beside the `geolocate` precedent and with
a comment explaining it. The plugin itself lives at `~/.hermes/plugins/hubitat/` and is untouched by
updates.

**Blast radius — check this after any change here.** Adding a name to `_HERMES_CORE_TOOLS` makes it
directly present on every surface whose toolsets resolve through core. Measured after this change:
`cli` and `telegram` do NOT gain it (their explicit toolset lists do not include the hubitat
toolset), but **`discord` DOES** (its list is a single plugin toolset that inherits core). If that is
unwanted, the follow-up is a per-platform tool-level filter, which does not exist today.

**Verify:**
```bash
cd ~/.hermes/hermes-agent
./venv/bin/python - <<'PY'
import sys; sys.path.insert(0, '/home/angela/.hermes/hermes-agent')
from hermes_cli.plugins import discover_plugins
discover_plugins(force=True)
import model_tools
from tools.tool_search import is_deferrable_tool_name
assert is_deferrable_tool_name("hubitat") is False
names = [d["function"]["name"] for d in
         model_tools.get_tool_definitions(enabled_toolsets=["safe","skills","hubitat"], quiet_mode=True)]
assert "hubitat" in names, names
print("ok")
PY
```
Then one live turn on the voice route asking it to read the Porch Thermometer.

**Re-apply after every `hermes update`.** Search `_HERMES_CORE_TOOLS` for `"hubitat"`; if absent,
re-add it beside the home-assistant entries.

## Voice-surface tool filtering (2026-09-22, three related patches)

**Later additions, same day.** `"google"` joined `_HERMES_CORE_TOOLS` and `_DEFAULT_OFF_TOOLSETS`
for exactly the reasons above: a plugin tool that is not core gets deferred behind tool_search, which
this surface does not have, so it would be invisible; and a plugin toolset that is not default-off
turns on for every platform. One more trap, learned the hard way: **a plugin is not loaded just by
existing in `~/.hermes/plugins/`.** It must also be named in `plugins.enabled` in `config.yaml`,
which `hermes plugins` manages, or discovery never sees it and its toolset resolves to nothing.

Applied together for the Hermes Voice Satellite project. All three are needed to lock down a voice
surface properly, and all three must be re-applied after `hermes update`.

**1. `model_tools.py` — an empty deferrable catalogue must not add the tool_search bridge.** In
`get_tool_definitions` the assembly now only replaces the definitions when
`assembly.activated and assembly.deferred_count > 0`. Without it, a surface where every tool is core
still got `tool_search`/`tool_describe`/`tool_call` injected with nothing behind them, and the model
spent ~1.8 s poking at them inside a single research turn (three pointless round trips) on a channel
where silence is the most expensive thing there is.

**2. `toolsets.py` — new `skills_read` toolset** (`["skills_list", "skill_view"]`), the read-only
counterpart to `skills`. Toolset selection is at TOOLSET granularity (`_apply_toolset_selection`
resolves names via `resolve_toolset()`, so an individual tool name is rejected as an unknown toolset —
there is no per-tool disable). Without this the voice surface could only consult skills by also gaining
`skill_manage`, a write path into Angela's curated skills guarded by nothing but a prompt. Both tools
are already in `_HERMES_CORE_TOOLS`, so this adds nothing deferrable.

**3. `hermes_cli/tools_config.py` — two edits.** The `CONFIGURABLE_TOOLSETS` row for `skills_read`, and
`"hubitat"` added to `_DEFAULT_OFF_TOOLSETS`. The second matters: new plugin toolsets default ON
everywhere, so enabling the Hubitat plugin for the voice surface silently handed device control to
Discord. Default-off means only a platform whose saved list names it gets it.

**Verification that actually counts:** resolve through `hermes_cli.tools_config._get_platform_tools(cfg,
platform)` and then `resolve_toolset()`, **not** the raw saved `platform_toolsets` list. Passing the raw
list made an earlier check wrongly report that Discord still had `hubitat`: a bundle like
`hermes-discord` is `_HERMES_CORE_TOOLS + extras`, so it automatically contains anything newly added to
core. The raw-list check cannot distinguish "this platform gets it" from "the bundle happens to name it".

Also: the voice surface's list is now `['web', 'vision', 'skills_read', 'hubitat']` instead of starting
with `safe`. `safe` is web + vision + image_gen, and `image_gen` was the ONLY deferrable tool on that
surface — the sole reason the bridge existed there at all.

A patch-tool caution learned here: `patch` fuzzy-matches `old_string`, so a near-miss (I mistyped a row
label) still matched and then wrote MY version, silently renaming a `CONFIGURABLE_TOOLSETS` label. Read
the returned diff every time and confirm it changed only what was intended.

## Voice-surface tool exposure: three names in `_HERMES_CORE_TOOLS` (toolsets.py)

Plugin tools defer behind `tool_search` unless their name is in `_HERMES_CORE_TOOLS`. **One deferrable
tool is enough to drag the whole bridge in** (tool_search + tool_describe + tool_call, ~760 tokens, and
a round-trip detour that has cost seconds of silence on a voice channel), so the voice surface needs
every tool it can actually use listed as core:

| Name | Why |
|---|---|
| `hubitat` | house control; without it the tool was invisible on that surface |
| `google` | tasks, calendar, places |
| `voice_satellite` | follow-up mode control, added 2026-09-22 |
| `geolocate` | added 2026-09-22; it was the last deferrable name, so the bridge is now absent from the voice surface entirely |

Measured effect on the api_server surface: schemas 12,182 -> 9,946 chars (~3,045 -> ~2,486 tokens),
and `tool_search`/`tool_describe`/`tool_call` no longer appear in the assembled definitions at all.
`vision` was also removed from `tools.platforms.api_server`: a voice surface never receives images, and
`vision_analyze` was deferrable, which is what kept the bridge alive there.

**Blast radius, measured:** adding a name to `_HERMES_CORE_TOOLS` makes it directly present wherever its
toolset is enabled. `cli` and `telegram` do not gain it (their explicit lists exclude the toolset), but
`discord` does, because its list is a bundle that inherits core. `geolocate` is read-only, so this is
benign; hubitat was the case that mattered.

Verify with the deferral check rather than by eye:

```python
from tools.tool_search import is_deferrable_tool_name
assert not any(is_deferrable_tool_name(n) for n in
               ["google", "hubitat", "voice_satellite", "geolocate", "web_search",
                "web_extract", "skill_view", "skills_list"])
```

**Re-apply after every `hermes update`:** check `_HERMES_CORE_TOOLS` for all four names, and re-check
that `vision` is absent from `tools.platforms.api_server` in config.yaml.

## voice_satellite toolset (2026-09-22, same wiring rules)

`voice_satellite` (follow-up mode control for the voice satellite) follows the google pattern exactly:
a user plugin at `~/.hermes/plugins/voice_satellite/`, enabled with `hermes plugins enable`, its
toolset named in `tools.platforms.api_server`, and its name added to `_DEFAULT_OFF_TOOLSETS` in
`hermes_cli/tools_config.py` so it does not silently switch on for Discord.

**Do not try to edit `~/.hermes/config.yaml` with the `patch` tool** — it refuses ("Agent cannot
modify security-sensitive configuration"), correctly. Use `hermes plugins enable <name>` and
`hermes tools enable <name> --platform <platform>`, then verify with `hermes tools list --platform
<platform>`. A plugin that merely exists in `~/.hermes/plugins/` is NOT loaded: it must be enabled.

Verify the surface resolves it (a raw list cannot tell a named toolset from a bundle that happens to
contain it): `hermes tools list --platform api_server` must show it, and a live turn on the voice
route must actually change the device. See the voice-bridge skill for the live checks.

## Release Target vs Upstream Main

`hermes version` / `hermes update` can report the installed release as current while GitHub `main` already contains unreleased fixes. Do not equate “newer `main` exists” with “the supported updater will install it.” Before promising that an update contains a needed fix:

1. Resolve the exact release/updater target SHA.
2. Confirm every required fix and follow-up commit is an ancestor of that target.
3. If the fix exists only on unreleased `main`, either wait for the official release or rehearse a complete, narrowly scoped hotfix sequence in an isolated checkout.
4. Never move a locally patched production install wholesale to unreleased `main` merely to acquire one fix.

For the worked messaging-session case, reset-child listability semantics, read-only database verification, dirty-checkout preservation, and split dashboard/gateway restart sequence, see `references/session-reset-sidebar-update-case.md`.

## Pre-Update Checklist

Run this before starting `hermes update`, and report findings to Angela rather than silently clearing anything.

1. **Stale update markers.** Check `~/.hermes/.hermes-update-in-progress` and `~/.hermes/fleet_restart_pending`. Each records a PID and a timestamp; confirm whether that PID is still alive with `ps -p <pid>`. A marker whose PID is dead is a leftover from an interrupted fleet update and can confuse the next run. Do not delete them without Angela's approval.
2. **Gateway-vs-code drift.** `hermes doctor` warns when a previous update pulled new code without restarting running gateways (mixed `sys.modules`). Compare gateway start time (`systemctl --user show hermes-gateway.service -p ActiveEnterTimestamp`) against the last update time and state the gap.
3. **Inspect the live updater before predicting preservation.** Read the installed update command's fast-forward, divergence, reset, autostash, restore, and backup paths. Determine the current branch, target branch, shallow-clone status, and `git merge-base HEAD origin/<target>` result. Same-branch divergence can take a hard-reset path even when another branch would be merged to preserve commits.
4. **Inventory the dirty tree.** Record `git status --porcelain`, `git log --oneline origin/main..HEAD`, `git stash list`, and any updater-created rescue refs/tags so every local change can be proven back afterwards. Do not count a rescue ref as durable until its retention policy is known.
5. **Separate committed from uncommitted.** Carried local *commits* and uncommitted working-tree edits recover differently: commits may be discarded by reset while working-tree edits ride the updater's autostash. Enumerate both independently before updating.
6. **Backups.** Read `updates.pre_update_backup`; when it is `false`, say so explicitly rather than implying a backup exists. Also distinguish configured pre-update backups from temporary orphan/rescue refs created only on particular updater branches.
7. **Silent-loss checks.** Some customizations live in files that are upstream content and therefore look clean after an update even when overwritten (see the NVIDIA picker pin). Verify those by value, not by `git status`.

## Post-Update Checklist

After every `hermes update`:

1. **Check for conflicts:** The update output shows conflicted files. If any patched file appears, the patches were lost.
2. **Verify patches:**
   - **Plugins must still REGISTER.** They live outside the checkout so the update cannot revert them, but prove it in a fresh process: run `discover_plugins(force=True)`, assert `agent.tts_registry.get_provider("mimo")`, `agent.transcription_registry.get_provider("mimo")`, and `agent.web_search_registry.get_provider("trafilatura")` are all non-None, then exercise one real operation of each. Load `hermes_cli.env_loader.load_hermes_dotenv()` first or `XIAOMI_API_KEY` is missing and TTS reports a configuration error.
   - **Residue re-application is on hold by Angela's 2026-09-17 decision.** After the update, reevaluate each residue item (the geolocation exposure wiring and the core patches) and apply only what is still wanted. If the geolocation wiring stays off, `geolocate` is still registered and callable but becomes deferred behind tool search rather than directly present, so diagnose a missing-from-schema report that way before assuming a broken plugin.
   - **MiMo behavior:** confirm the TTS payload still carries the mandatory `audio` block (without it the API returns HTTP 500), and remember `.ogg`/Opus audio is rejected by the ASR API — convert to mp3/wav first.
3. **Check git stash:** `git stash list` in `~/.hermes/hermes-agent/` — the updater auto-stashes local changes. If the stash contains your patches, they need re-applying.
4. **Re-apply if needed:** Patch the current upstream files (not the stashed versions — upstream may have changed structurally).
5. **Drop the stash** after re-applying: `git stash drop`
6. **Restart the gateway:** For a healthy systemd-managed gateway, an agent running inside the gateway can request a graceful planned restart with `systemctl --user reload hermes-gateway.service` when the unit's `ExecReload` sends SIGUSR1. Follow and verify the full procedure in the `hermes-agent` skill. For an unhealthy/wedged gateway, use a separate terminal and the recovery path instead.

## How to Verify Model Catalog

Check if your models are still in the live API vs hardcoded:

```python
import json, urllib.request
req = urllib.request.Request('https://openrouter.ai/api/v1/models', headers={'Accept': 'application/json'})
with urllib.request.urlopen(req, timeout=15) as resp:
    data = json.loads(resp.read().decode())
models = {m['id'] for m in data.get('data', [])}
print('openrouter/auto' in models)  # True = live, no patch needed
```

Models in the live API don't need hardcoded entries — the picker fetches them automatically.

## Pitfalls

1. **Separate availability from ordering for OpenRouter.** Live `/v1/models` discovery determines whether a model exists and advertises tool support; it does not preserve Angela's preferred picker order. Local `OPENROUTER_MODELS` is only the offline fallback when the remote Hermes catalog is available. For picker pins, inspect `fetch_openrouter_models()` and its remote-manifest ordering path, then verify the actual online picker.
2. **Prefer voice-provider plugins when the live release exposes the TTS/STT registries.** Do not register a plugin under a name still present in the built-in-name set: built-ins win and the plugin registration is rejected. Remove the in-tree implementation and built-in-name reservation in the same migration step, then verify discovery and dispatch in a fresh process.
3. **Do not use `hermes gateway restart` from inside the gateway.** On the current systemd unit, the safe healthy-gateway self-restart path is `systemctl --user reload hermes-gateway.service`, which sends SIGUSR1 and lets Hermes exit with status 75 after the active turn drains; systemd then restarts it. Verify `ExecReload`, `RestartForceExitStatus=75`, PID/start-time change, and journal evidence as documented in `hermes-agent`. Use a separate terminal for a wedged gateway.
4. **Stash naming:** The updater creates stashes like `hermes-update-autostash-YYYYMMDD-HHMMSS`. Use `git stash show stash@{0} --stat` to see what's in them before dropping.
5. **Compilation check:** Always verify patched files compile: `python -c "import py_compile; py_compile.compile('file.py', doraise=True)"`
6. **git stash show syntax:** Use `stash@{0}` not the full hash. The `--` path separator doesn't work with `git stash show -p` for filtering — use `git diff stash@{0}^..stash@{0} -- path` instead.
