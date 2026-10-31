"""The graphical installer: a local web UI served by the bundled Python.

Loopback only. The window is a browser or a `pywebview` shell pointed at it; either way the same
`Session` does the work, so the GUI cannot drift from the console path.
"""
from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .session import Session

WEB_ROOT = Path(__file__).resolve().parent / "web"


class Handler(BaseHTTPRequestHandler):
    session: Session  # set by serve()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        pass  # a desktop installer should not narrate HTTP requests

    # -- helpers ------------------------------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}

    # -- routes -------------------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path in ("/", "/index.html"):
            page = WEB_ROOT / "index.html"
            if not page.exists():  # pragma: no cover - packaging error
                self._json({"error": "the interface file is missing from the package"}, 500)
                return
            self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            return
        if self.path == "/api/status":
            self._json(self.session.status())
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        body = self._read_json()
        try:
            if self.path == "/api/gateway":
                self._json(
                    self.session.install_gateway(
                        str(body.get("dial_address", "")),
                        registry_port=int(body.get("registry_port", 10701)),
                        tls_port=int(body.get("tls_port", 10702)),
                        skip_hermes=bool(body.get("skip_hermes", False)),
                        dry_run=body.get("dry_run"),
                    )
                )
                return
            if self.path == "/api/inspect":
                self._json(self.session.inspect_gateway(str(body.get("host", "")),
                                                        int(body.get("port", 10702))))
                return
            if self.path == "/api/pair":
                self._json(
                    self.session.pair(
                        str(body.get("host", "")),
                        int(body.get("port", 10702)),
                        str(body.get("words", "")),
                        str(body.get("device_name", "")),
                        expected_code=str(body.get("expected_code", "")),
                        remember=bool(body.get("remember", False)),
                    )
                )
                return
            if self.path == "/api/flash":
                device_id = str(body.pop("device_id", ""))
                self._json(self.session.flash(device_id, **body))
                return
        except (TypeError, ValueError) as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
            return
        self._json({"error": "not found"}, 404)


def serve(session: Session, *, port: int = 0, open_browser: bool = True) -> int:
    """Run the interface until interrupted. Returns a process exit code."""
    handler = type("BoundHandler", (Handler,), {"session": session})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"  Hermes voice installer is running at {url}")
    print("  Press Ctrl+C when you are finished.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Closed.")
    finally:
        server.server_close()
    return 0
