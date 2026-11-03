"""Live context-continuity proof through the real gateway. No speaker involved."""
import asyncio, json
from pathlib import Path
from satellite_gateway.gateway import VoiceGateway, load_runtime_config, read_api_key
from satellite_gateway.config import GATEWAY_BASE_URL, GATEWAY_ENV_FILE

async def main():
    sid = "voice-context-probe-20261102"
    gw = VoiceGateway(base_url=GATEWAY_BASE_URL, api_key=read_api_key(GATEWAY_ENV_FILE),
                      session_id=sid, model="", instructions=None,
                      runtime_config=load_runtime_config())
    async with gw:
        async def ask(q):
            return "".join([d async for d in gw.ask(q)]).strip()
        a1 = await ask("Remember this for later: my umbrella code word is kumquat-9. Just reply 'noted'.")
        print("turn1:", a1[:120])
        a2 = await ask("What was my umbrella code word? Answer with just the code word.")
        print("turn2:", a2[:120])
        hist = await gw._session_history()
        print("history replayed turns:", len(hist), "roles:", [m['role'] for m in hist][-6:])
        ok = "kumquat-9" in a2.lower()
        print("CONTEXT_CONTINUITY:", "PASS" if ok else "FAIL")
        return 0 if ok else 1

raise SystemExit(asyncio.run(main()))
