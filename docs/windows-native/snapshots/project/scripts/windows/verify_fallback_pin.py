"""Forced-failure proof: an unreachable pinned provider must fall back to llamacpp/qwen38.

Simulates 'GPT unavailable' by pinning to a provider with no resolvable credentials.
Nothing here touches the real codex credentials or the durable voice-model.json.
"""
import asyncio, json, uuid
import aiohttp
from pathlib import Path
from satellite_gateway.config import GATEWAY_BASE_URL, GATEWAY_ENV_FILE
from satellite_gateway.gateway import read_api_key

async def main():
    key = read_api_key(GATEWAY_ENV_FILE)
    body = {
        "input": "Reply with exactly: FALLBACK_OK",
        "session_id": f"voice-fallback-probe-{uuid.uuid4().hex[:8]}",
        "model": "nonexistent-pin-model",
                "provider": "openai-codex",
        "require_model_lock": False,
    }
    async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {key}"}) as s:
        async with s.post(f"{GATEWAY_BASE_URL}/v1/runs", json=body) as r:
            payload = await r.json(content_type=None)
            print("create:", r.status, str(payload)[:200])
            run_id = payload.get("run_id")
            if not run_id:
                return 1
        text, runtime, terminal = [], None, None
        async with s.get(f"{GATEWAY_BASE_URL}/v1/runs/{run_id}/events") as resp:
            async for raw in resp.content:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except Exception:
                    continue
                if ev.get("event") == "message.delta":
                    text.append(ev.get("delta") or "")
                elif ev.get("event") == "run.completed":
                    runtime = ev.get("runtime"); terminal = "completed"
                    print("completed event keys:", sorted(ev.keys()))
                    print("completed raw:", json.dumps({k: v for k, v in ev.items() if k in ("runtime", "usage", "model", "provider")})[:400])
                    break
                elif ev.get("event") in ("run.failed", "run.cancelled"):
                    terminal = ev.get("event")
                    print("terminal payload:", str(ev)[:300])
                    break
        async with s.get(f"{GATEWAY_BASE_URL}/v1/runs/{run_id}") as r2:
            st = await r2.json(content_type=None)
        print("run status runtime:", json.dumps(st.get("runtime")))
        runtime = st.get("runtime") or runtime
        print("text:", "".join(text)[:120])
        print("runtime served by:", json.dumps(runtime))
        print("terminal:", terminal)
        # This gateway build leaves runtime=null on /v1/runs status; the
        # authoritative proof is the gateway agent log line (checked separately).
        text_served = "FALLBACK_OK" in "".join(text) and terminal == "completed"
        print("FALLBACK_PROOF (text-level):", "PASS" if text_served else "FAIL")
        print("Now grep agent.log for: Fallback activated -> qwen38 (llamacpp)")
        return 0 if text_served else 1

raise SystemExit(asyncio.run(main()))
