# Core patches

**As of 2026-09-23 there is ONE patch left**, down from five. It is a diff against the Hermes Agent
source tree (`~/.hermes/hermes-agent`).

| Patch | File | Why | Needed? |
|---|---|---|---|
| `0001-cron-provider-snapshot.patch` | `cron/jobs.py` | cron stored the generic billing class instead of the requested provider, so jobs without a provider pin silently ran against a different endpoint (here, OpenRouter) while still reporting success | Unrelated to voice. Upstream tracking: issue **#109765**, fix in flight as PR **#109773** (`custom:<name>` healing plus a regression test). Verified 2026-09-23 that the canonical form resolves for this config shape too, so drop this patch once a release carries that fix |

## Why the other four went away

The voice surface was widened to full parity with a normal Hermes conversation, which removed the
reasons the tool patches existed:

- **Widening removed two.** The `model_tools` empty-catalogue guard only mattered when every tool on the
  surface was core, and the `toolsets.py` core-tool lines only existed to keep four plugin tools out of
  the `tool_search` bridge. With a real deferrable catalogue, the bridge is legitimate and the detour is
  accepted, so both went.
- **Dropping the pinned voice model removed one.** The api_server `extra_body` re-scope was only
  triggered by a `model_routes` entry pointing at a different provider than the global default. With no
  route, the leak cannot happen.
- **The `skills_read` toolset was dropped** because the surface carries the full `skills` toolset now.
- **kanban and browser are not enabled on the voice platform**, which is plain configuration, not a patch.

Their diffs remain in this repository's git history. **If a `model_routes` entry pointing at another
provider is ever added again, the api_server patch's reason comes back** and it should be restored from
history.

## Check what is applied

```bash
./check-patches.sh          # read-only, exit 1 if anything is missing
```

It tries `git apply --check --reverse` (already applied), then `git apply --check` (missing), and
reports REVIEW when upstream restructured the file so the patch needs a human, not a blind apply.

## Re-applying after an update

1. Run `check-patches.sh` and see what is missing.
2. Apply to the **current** upstream file, never by restoring an old copy:
   `git apply -p1 <patch>` from the repository root.
3. Compile what you touched:
   `./venv/bin/python -c "import py_compile; py_compile.compile('<file>', doraise=True)"`
4. Restart the gateway, then verify the surface (README) and speak to the device.

Do not restart the gateway casually: a restart drops live sessions. Agree a moment first.
