# Choosing the Hermes agent or model used by the satellite

The bridge reaches Hermes through the authenticated Runs API. There are two different routing choices:

1. **Select a model route** while keeping the same Hermes profile, memory, skills and identity.
2. **Select a named Hermes Bot/agent profile**, which changes the complete agent environment: model,
   SOUL, memory, skills, credentials and configuration.

They solve different problems and can be used independently.

## Default behaviour

With the repository defaults:

```python
# bridge/config.py
GATEWAY_BASE_URL = "http://127.0.0.1:8642"
VOICE_ROUTE_ALIAS = ""
```

The bridge sends each utterance to the unprefixed `/v1/runs` endpoint and omits the request's `model`
field.

**Current setting (2026-09-25): pinned.** `VOICE_ROUTE_ALIAS = "voice-fast"`, resolved by the default
profile's `gateway.api_server.model_routes` to `provider: deepseek` / `model: deepseek-flash`. Chosen
because the local `qwen38` route was too slow to iterate against. This is a **cross-provider** route -
the profile default is `llamacpp-qwen38` and this is DeepSeek - which is the condition that once
required the api_server `extra_body` re-scope patch. That patch is not currently carried; verified
2026-09-25 that routed runs succeed against DeepSeek anyway (`session_model_usage` shows
`deepseek-flash` / `deepseek` at `https://api.deepseek.com/v1`). Set the alias back to `""` to return
the voice surface to the profile default. Hermes therefore uses the default profile and that profile's normal model and fallback chain.
Rotating the bridge's session id starts a new conversation but does not change either choice.

## Option 1: pin the voice surface to a model route alias

Hermes' API server supports named entries under `gateway.api_server.model_routes`. The alias is a
server-side routing name, not necessarily a provider's real model id.

Add a route to the Hermes profile's `config.yaml`:

```yaml
gateway:
  api_server:
    enabled: true
    host: 127.0.0.1
    port: 8642
    model_routes:
      voice-fast:
        provider: <provider-id>
        model: <provider-model-id>
```

Then set the bridge to send that alias:

```python
# bridge/config.py
VOICE_ROUTE_ALIAS = "voice-fast"
```

`bridge/gateway.py` adds this to every `POST /v1/runs` body:

```json
{"input": "...", "session_id": "...", "model": "voice-fast"}
```

The gateway resolves `voice-fast` from its own `model_routes` map and constructs the agent with the
configured provider and model. The bridge never needs the provider credential and does not send the
real model id unless the alias happens to use the same text.

### Where each setting is saved

| Setting | Saved in | Read by |
|---|---|---|
| Alias definition (`model_routes.voice-fast`) | `~/.hermes/config.yaml`, under `gateway.api_server` | Hermes gateway/API server at startup |
| Alias selected by the satellite (`VOICE_ROUTE_ALIAS`) | this repository's `bridge/config.py` | `VoiceLoop`, then `VoiceGateway` |
| API-server bearer credential | `API_SERVER_KEY` in the bridge process environment or the selected profile's `.env` | `bridge/gateway.py` |
| Current voice conversation id | untracked `state/voice-session.txt` | the bridge's session-rotation code |

Restart the Hermes gateway after changing `model_routes`; restart the bridge after changing
`VOICE_ROUTE_ALIAS`. Start a fresh voice conversation when validating a routing change so an older
session selection or conversation history cannot obscure the result.

### What the alias does and does not change

A route alias changes the provider/model used for the run. It does **not** select another Hermes
profile and therefore does not change the profile's SOUL, memory, skills, credentials or tool
configuration. The ordinary fallback chain remains available because the bridge does not request a
strict model lock.

Do not confuse API-server `model_routes` with top-level `model_aliases`. `model_aliases` makes model
selection convenient inside Hermes; the API server resolves the bridge's `model` field through
`gateway.api_server.model_routes`.

### Cross-provider verification

A route that moves from the profile's default provider to a different provider must be verified with
a real run and the gateway log. Provider-specific request fields from the default provider must not
leak onto the routed provider. This repository previously carried an Hermes API-server patch for that
exact failure mode; see `hermes/patches/README.md`. If the installed Hermes release still exhibits the
problem, either keep the route on the same provider, remove the incompatible provider-specific option,
or restore the documented patch. A successful HTTP response is not sufficient proof because a failed
route may be answered by the fallback chain.

## Option 2: use a named Hermes Bot/agent profile

In Hermes Desktop, a named Bot is a Hermes **profile**. Selecting a profile changes the complete agent,
not only its model. Each profile has its own configuration, SOUL, memory, skills, credentials and
session history.

Stock Hermes supports routing API requests to a served profile through a URL prefix:

```text
http://127.0.0.1:8642/p/<profile-slug>/v1/runs
```

To make every satellite conversation use that profile:

1. Create or choose the Bot/profile in Hermes. Use its profile slug, not only its friendly display
   title.
2. Enable `gateway.multiplex_profiles` on the default gateway and verify the profile is in the served
   set.
3. Give the named profile its own `API_SERVER_KEY` in
   `~/.hermes/profiles/<profile-slug>/.env`. Hermes rejects the default profile's key on a named
   profile prefix.
4. Point the bridge at the profile-prefixed base URL:

   ```python
   # bridge/config.py
   GATEWAY_BASE_URL = "http://127.0.0.1:8642/p/<profile-slug>"
   ```

5. Supply that profile's API-server key to the bridge process as `API_SERVER_KEY`, restart the bridge,
   and run a real test.

`VoiceGateway` appends `/v1/runs`, `/v1/runs/<id>/events`, `/stop` and `/steer` to the configured base
URL. The fixed `/p/<profile-slug>` prefix therefore keeps the initial run and every follow-up operation
inside the same profile. Session-id rotation starts a fresh conversation inside that profile; it never
falls back to the default profile.

This uses Hermes' supported profile routing and requires no Hermes source patch. It does require bridge
configuration because the repository currently keeps `GATEWAY_BASE_URL` in `bridge/config.py` rather
than exposing it as a command-line option.

### Combining profile and model routing

A named profile can define its own `gateway.api_server.model_routes` and the bridge can send one of
those aliases while using the profile-prefixed URL. Usually this is unnecessary: pin the Bot's normal
model in that profile and leave `VOICE_ROUTE_ALIAS` empty. Use both only when the Bot should normally
use one model but the satellite should use another.

## Verification

After either change:

1. Read back the relevant profile's `config.yaml` and confirm the route or model pin.
2. Confirm the bridge is using the intended base URL and API key source without printing the key.
3. Restart only the components whose startup configuration changed.
4. Start a fresh voice conversation and complete a real run.
5. Inspect the gateway log for the resolved provider/model and for fallback activation or provider
   errors.
6. Confirm `/stop` and `/steer` still target the run successfully when using a profile prefix.

Keep the API server on loopback unless there is a separately designed authentication and network
boundary. It exposes the selected profile's full enabled tool surface, including terminal access when
that profile permits it.
