# Browser backend selection after Hermes updates

Use this reference when a Hermes update changes Angela's browser tool surface or the built-in browser loses the established fingerprint.

## Symptom pattern

- Fresh sessions expose `browser_exec` instead of the built-in `browser_navigate`, `browser_snapshot`, and `browser_console` tools.
- A browser check returns a generic Linux HeadlessChrome user agent, Linux platform/client hints, or an 800×600 screen even though raw `agent-browser` still passes Angela's fingerprint test.
- `hermes doctor` reports the built-in browser toolset unavailable while Browser Use CLI is present.

Do not conclude that the Windows-identifying fingerprint requires a Windows host. Angela's local Linux Chrome setup deliberately normalizes UA, platform, client hints, webdriver, screen, and viewport through agent-browser configuration, an init script, and a launch-mutator plugin.

## Confirm the active routing logic

Inspect the current source before changing config. In Hermes 0.20, `tools/browser_use_cli.py::is_browser_use_cli_mode()` treats an absent `browser.backend` as Browser Use CLI mode whenever that CLI is runnable. In that mode, `tools/browser_tool.py::check_browser_requirements()` intentionally hides the built-in `browser_*` tools.

Verify the effective config:

```bash
hermes config get browser.backend
```

An empty/unset result combined with `browser_exec` exposure confirms this route on versions with the 0.20 selection logic.

## Config-first repair

Use the supported config switch rather than patching source or editing the systemd unit:

```bash
hermes config set browser.backend off
hermes config get browser.backend
hermes config check
```

In this code path, `off` means "disable Browser Use CLI mode and use the built-in agent-browser stack." Re-inspect current source/docs on later releases in case upstream renames the selector.

## Check real-profile mode before blaming the stealth stack

`browser.use_real_profile` is a separate launch path from Angela's configured stealth browser. When it is enabled, Hermes first snapshots an installed Chromium profile into `~/.hermes/browser-profile/<browser>/`; a snapshot failure can stop `browser_navigate` even while raw `agent-browser`, its fingerprint files, and backend routing are all healthy.

Check the effective value and inspect the profile resolver in the installed release before changing anything:

```bash
hermes config get browser.use_real_profile
```

If navigation reports a missing `Default` directory, compare the exact source path named by the error with every real browser profile location on the host. On Linux, an existing but incomplete native directory such as `~/.config/chromium` can outrank a populated Snap or Flatpak profile because the resolver selects the first existing candidate, not the first candidate containing `Default`. Diagnose that path-shadowing condition explicitly; do not describe the stealth browser as broken.

Choose the repair according to intent:

- If authenticated real-profile browsing was not intentionally requested, propose disabling `browser.use_real_profile` so Hermes returns to the isolated stealth route.
- If real-profile browsing is intentional, preserve the authenticated profile and repair resolver/path selection only after explaining that Hermes will copy cookies and login databases into its browser-profile snapshot.

Do not move profile directories, disable the setting, or expose authenticated state without explicit approval. After any approved change, verify both the profile snapshot result and a live built-in browser launch.

## Verify before restart

Run a fresh-process schema check from the Hermes repository:

```bash
./venv/bin/python - <<'PY'
from dotenv import load_dotenv
load_dotenv('/home/angela/.hermes/.env', override=True)
import model_tools
names = {
    d['function']['name']
    for d in model_tools.get_tool_definitions(
        enabled_toolsets=['browser'], quiet_mode=True
    )
}
print('browser_navigate:', 'browser_navigate' in names)
print('browser_console:', 'browser_console' in names)
print('browser_exec:', 'browser_exec' in names)
from tools.browser_tool import check_browser_requirements
print('requirements:', check_browser_requirements())
PY
```

Required result: built-in navigate/console present, `browser_exec` absent, requirements true.

A temporary direct import test can prove the built-in stack honors Angela's agent-browser environment before a restart, but it is not evidence about the running gateway. The acceptance test must use actual Hermes built-in browser tools under the replacement gateway.

## Restart and live acceptance

For a healthy systemd gateway, follow the graceful SIGUSR1 reload procedure in the `hermes-agent` skill. Record the old PID, request `systemctl --user reload hermes-gateway.service`, and verify from a later turn or one-shot verifier that the PID/start timestamp changed and the service is active/running.

Then use `browser_navigate` and `browser_console` against `https://whatmyuseragent.com/`. Expected fingerprint fields are maintained in `angela-browser` and its `references/stealth-browser-rebuild.md`. Verify every field live rather than inferring success from config or raw CLI output.

## Proven distinction

Raw `agent-browser` passing while Hermes browser verification fails usually means the backend route changed, not that the fingerprint files were lost. Compare these independently:

1. Raw CLI executable/config/fingerprint.
2. Fresh-process Hermes tool schema.
3. Replacement-gateway built-in browser fingerprint.

This three-layer check prevents a generic Browser Use or hand-launched CDP browser from being mistaken for Angela's configured built-in route.
