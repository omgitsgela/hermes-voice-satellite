"""The graphical installer: a local web UI served by the bundled Python.

Loopback only. The window is a browser or a `pywebview` shell pointed at it; either way the same
`Session` does the work, so the GUI cannot drift from the console path.
"""
from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .session import Session

WEB_ROOT = Path(__file__).resolve().parent / "web"


class Handler(BaseHTTPRequestHandler):
    session: Session  # set by serve()
    token: str = ""   # set by serve(): per-launch, unguessable

    #: Requests that change the machine must prove they came from the page this process served.
    #: Binding to loopback is not authorisation: any page in the same browser can POST to it, and a
    #: DNS-rebinding request can carry a hostile Host header. A token, a Host check and a strict
    #: content type close all three.
    ALLOWED_HOSTS = ("127.0.0.1", "localhost", "[::1]")

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in self.ALLOWED_HOSTS

    def _token_ok(self) -> bool:
        supplied = self.headers.get("X-Installer-Token") or ""
        return bool(self.token) and secrets.compare_digest(supplied, self.token)

    def _guard(self) -> bool:
        if not self._host_ok():
            self._json({"error": "unexpected Host header"}, 403)
            return False
        if not self._token_ok():
            self._json({"error": "missing or wrong installer token"}, 403)
            return False
        return True

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
            html = page.read_text().replace(
                "</head>", f'<script>window.INSTALLER_TOKEN="{self.token}";</script></head>', 1
            )
            self._send(200, html.encode(), "text/html; charset=utf-8")
            return
        if self.path == "/api/status":
            if not self._guard():
                return
            self._json(self.session.status())
            return
        if self.path == "/api/ports":
            if not self._guard():
                return
            self._json({"ok": True, "ports": self.session.usb_ports()})
            return
        if self.path == "/api/mounts":
            if not self._guard():
                return
            self._json({"ok": True, "mounts": self.session.card_mounts()})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if not self._guard():
            return
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            # A form-encoded or text/plain body can be sent cross-origin without a preflight, which
            # is exactly how a hostile page would reach these endpoints.
            self._json({"error": "expected application/json"}, 415)
            return
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
                        device_id=str(body.get("device_id") or "") or None,
                        expected_code=str(body.get("expected_code", "")),
                        remember=bool(body.get("remember", False)),
                    )
                )
                return
            if self.path == "/api/flash":
                # `device_profile` selects the driver; `device_id` is the board's own identity.
                profile_id = str(body.pop("device_profile", "") or body.pop("device_id", ""))
                if body.get("stage_only"):
                    body.pop("stage_only", None)
                    self._json(self.session.flash(profile_id, **body))
                else:
                    self._json(self.session.install_device(profile_id, **body))
                return
            if self.path == "/api/device-id":
                self._json(self.session.device_identity(
                    str(body.get("profile", "")), port=body.get("port")
                ))
                return
        except (TypeError, ValueError) as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
            return
        self._json({"error": "not found"}, 404)


def serve(session: Session, *, port: int = 0, open_browser: bool = True) -> int:
    """Run the interface until interrupted. Returns a process exit code."""
    handler = type("BoundHandler", (Handler,),
                   {"session": session, "token": secrets.token_urlsafe(32)})
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
        # Paired credentials live in a temporary directory; the session that owns them ends here.
        session.close()
    return 0
