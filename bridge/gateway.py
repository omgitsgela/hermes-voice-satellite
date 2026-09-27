"""Client for the Hermes gateway's API server, over the constrained `voice` route.

Why the Runs API and not `/v1/chat/completions`: a run returns a `run_id`, which gives us
`/stop` for interrupting an answer and somewhere to answer an approval, plus an SSE stream
carrying token deltas for chunked TTS playback. That was the decision recorded in the
project plan, and it is implemented here.

Shapes verified against the gateway source, not guessed:

* ``POST /v1/runs``            body accepts ``input``, ``session_id``, ``instructions``, and the
                              standard overrides ``model`` / ``provider`` / ``model_options``
                              -> ``{"run_id": ..., "status": "started"}``
* ``GET  /v1/runs/{id}/events`` SSE; each ``data:`` frame is
                              ``{"event": <name>, "run_id": ..., "timestamp": ..., **fields}``
                              (``api_server_runs.py:80``). Deltas arrive as
                              ``event="message.delta"`` with the text in ``delta``.
* ``POST /v1/runs/{id}/stop``   interrupt; settles as ``cancelled``
* terminal events: ``run.completed`` / ``run.failed`` / ``run.cancelled``

The API key is read from ``~/.hermes/.env`` (or the environment) and is never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import AsyncIterator, Optional

import aiohttp

_LOGGER = logging.getLogger("bridge.gateway")

RunsEvent = dict


class VoiceGatewayError(RuntimeError):
    """Any failure the voice loop should report out loud rather than hang on."""


def read_api_key(env_file: Path, var: str = "API_SERVER_KEY") -> str:
    """Resolve the API server key from the environment or the Hermes .env file."""
    value = os.environ.get(var, "").strip()
    if value:
        return value
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{var}="):
                return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    raise VoiceGatewayError(
        f"no {var} found in the environment or {env_file}; the API server cannot be called"
    )


class VoiceGateway:
    """One long-lived client for the voice route."""

    def __init__(self, *, base_url: str, api_key: str, session_id: str,
                 model: str = "voice", instructions: Optional[str] = None,
                 connect_timeout: float = 10.0, run_timeout: float = 180.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.session_id = session_id
        self.model = model
        self.instructions = instructions
        self.connect_timeout = connect_timeout
        self.run_timeout = run_timeout
        self._session: Optional[aiohttp.ClientSession] = None
        self.last_run_id: Optional[str] = None

    # -- lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> "VoiceGateway":
        await self.open()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def open(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self._api_key}",
                         "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=None,
                                              connect=self.connect_timeout,
                                              sock_read=self.run_timeout),
            )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _require(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise VoiceGatewayError("client session is not open; call open() first")
        return self._session

    # -- calls --------------------------------------------------------------

    async def health(self) -> dict:
        """Cheap preflight so the loop can report a dead gateway immediately."""
        session = self._require()
        try:
            async with session.get(f"{self.base_url}/v1/health") as resp:
                if resp.status != 200:
                    raise VoiceGatewayError(
                        f"gateway health returned HTTP {resp.status}")
                return await resp.json()
        except aiohttp.ClientError as exc:
            raise VoiceGatewayError(f"gateway unreachable: {type(exc).__name__}: {exc}") from exc

    async def ask(self, text: str, *, on_tool_started=None) -> AsyncIterator[str]:
        """Send an utterance and yield the answer as text deltas.

        `on_tool_started`, when given, is awaited with the raw event as soon as the run starts
        using a tool. The voice loop uses it to fill the silence with a short "one moment",
        because a tool call means seconds of dead air on a voice channel.

        Raises VoiceGatewayError on any failure, so the loop can say something useful instead
        of silently going quiet.
        """
        session = self._require()
        body: dict = {"input": text, "session_id": self.session_id}
        if self.model:
            # Only sent when a route alias is configured; otherwise the gateway uses its default.
            body["model"] = self.model
        if self.instructions:
            body["instructions"] = self.instructions

        # Idempotency-Key makes a retry of the *same* question safe: an identical retry
        # returns the original run_id instead of asking the model twice (and paying twice).
        headers = {"Idempotency-Key": f"voice-{uuid.uuid4().hex}"}
        try:
            async with session.post(f"{self.base_url}/v1/runs", json=body,
                                    headers=headers) as resp:
                payload = await resp.json(content_type=None)
                if resp.status not in (200, 201, 202):
                    raise VoiceGatewayError(
                        f"run creation failed: HTTP {resp.status} {str(payload)[:200]}")
        except aiohttp.ClientError as exc:
            raise VoiceGatewayError(f"gateway unreachable: {type(exc).__name__}: {exc}") from exc

        run_id = str(payload.get("run_id") or "").strip()
        if not run_id:
            raise VoiceGatewayError(f"no run_id in the response: {str(payload)[:200]}")
        self.last_run_id = run_id
        _LOGGER.info("run %s started (session %s)", run_id, self.session_id)

        got_any_text = False
        async for event in self._events(run_id):
            name = str(event.get("event") or "")
            if name == "message.delta":
                delta = event.get("delta")
                if isinstance(delta, str) and delta:
                    got_any_text = True
                    yield delta
            elif name == "tool.started":
                _LOGGER.info("run %s: tool %s started", run_id, event.get("tool"))
                if on_tool_started is not None:
                    try:
                        await on_tool_started(event)
                    except Exception as exc:  # noqa: BLE001 - never break the delta stream
                        _LOGGER.warning("on_tool_started callback failed: %s", exc)
            elif name == "tool.completed":
                _LOGGER.info("run %s: tool %s completed (error=%s)",
                             run_id, event.get("tool"), event.get("error"))
            elif name == "approval.request":
                # A voice channel cannot answer an approval. The `safe` toolset should
                # never produce one; if it does, deny so the run cannot hang forever.
                _LOGGER.warning("run %s: approval requested (%s); denying, a voice channel "
                                "cannot answer it", run_id, event.get("tool"))
                await self.resolve_approval(run_id, "deny")
            elif name == "run.failed":
                raise VoiceGatewayError(
                    f"run failed: {str(event.get('error') or event)[:200]}")
            elif name == "run.cancelled":
                raise VoiceGatewayError("run was cancelled")
            elif name == "run.completed":
                if not got_any_text:
                    _LOGGER.warning("run %s completed without any text delta", run_id)
                return

        raise VoiceGatewayError(
            f"run {run_id} event stream ended without a terminal event")

    async def _events(self, run_id: str) -> AsyncIterator[RunsEvent]:
        """Iterate the run's SSE stream, skipping keepalive comments."""
        session = self._require()
        url = f"{self.base_url}/v1/runs/{run_id}/events"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise VoiceGatewayError(
                        f"event stream returned HTTP {resp.status}")
                async for raw in resp.content:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if not line or line.startswith(":"):
                        continue  # SSE keepalive comment
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        yield json.loads(payload)
                    except json.JSONDecodeError:
                        _LOGGER.debug("skipping unparseable SSE frame: %s", payload[:120])
        except aiohttp.ClientError as exc:
            raise VoiceGatewayError(
                f"event stream failed: {type(exc).__name__}: {exc}") from exc

    async def resolve_approval(self, run_id: str, choice: str) -> None:
        """Answer an approval request. `choice` is one of once / deny / always."""
        session = self._require()
        try:
            async with session.post(f"{self.base_url}/v1/runs/{run_id}/approval",
                                    json={"choice": choice}) as resp:
                await resp.read()
                _LOGGER.info("approval for run %s answered %s (HTTP %s)",
                             run_id, choice, resp.status)
        except aiohttp.ClientError as exc:
            _LOGGER.warning("could not answer approval for %s: %s", run_id, exc)

    async def steer(self, text: str, run_id: Optional[str] = None) -> bool:
        """Hand new information to a run that is already going.

        2026-09-23. When the person wakes the device mid-task and says something new, that belongs
        in the conversation the agent is already having, not in a fresh question. The API server
        exposes this as POST /v1/runs/{id}/steer, which passes the text to the live agent. It is
        cooperative: the agent decides what to do with it, which is why a stop is a separate call.

        Best effort on purpose. A steer that does not land must never break the turn in progress,
        so this returns False rather than raising, and the caller records the drop.
        """
        session = self._require()
        target = (run_id or self.last_run_id or "").strip()
        if not target:
            _LOGGER.warning("steer requested with no run id; nothing to steer")
            return False
        try:
            async with session.post(f"{self.base_url}/v1/runs/{target}/steer",
                                    json={"input": text}) as resp:
                payload = await resp.json(content_type=None)
                if resp.status != 200:
                    _LOGGER.warning("steer refused (HTTP %s): %s", resp.status,
                                    str(payload)[:200])
                    return False
        except aiohttp.ClientError as exc:
            _LOGGER.warning("steer failed: %s: %s", type(exc).__name__, exc)
            return False
        accepted = bool(payload.get("accepted"))
        _LOGGER.info("steer on run %s: %s", target, "accepted" if accepted else "not accepted")
        return accepted

    async def stop(self, run_id: Optional[str] = None) -> bool:
        """Interrupt a run. Returns True when the gateway accepted the stop."""
        run_id = run_id or self.last_run_id
        if not run_id:
            return False
        session = self._require()
        try:
            async with session.post(f"{self.base_url}/v1/runs/{run_id}/stop") as resp:
                await resp.read()
                _LOGGER.info("stop requested for run %s (HTTP %s)", run_id, resp.status)
                return resp.status == 200
        except aiohttp.ClientError as exc:
            _LOGGER.warning("stop failed for %s: %s", run_id, exc)
            return False


async def _smoke() -> int:
    """Manual preflight: python -m bridge.gateway"""
    from bridge.config import (GATEWAY_BASE_URL, GATEWAY_ENV_FILE, VOICE_INSTRUCTIONS,
                               VOICE_ROUTE_ALIAS, VOICE_SESSION_ID)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    gateway = VoiceGateway(base_url=GATEWAY_BASE_URL,
                           api_key=read_api_key(GATEWAY_ENV_FILE),
                           session_id=VOICE_SESSION_ID,
                           model=VOICE_ROUTE_ALIAS,
                           instructions=VOICE_INSTRUCTIONS,
                           run_timeout=120.0)
    async with gateway:
        print("health:", await gateway.health())
        print("\nasking...")
        answer = []
        async for delta in gateway.ask("Say exactly: voice route is working."):
            answer.append(delta)
            print(delta, end="", flush=True)
        print(f"\n\nfull answer: {''.join(answer)!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_smoke()))
