"""Local HTTP endpoints for the viewer and document uploads."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
from pathlib import Path
import select
import tempfile
import threading
import time
import urllib.parse

from .rendering import render_file
from .session_lifetime import ViewerLifetime
from .web_geometry import MAX_INPUT, prepare_geometry


# Explicit routes keep package resources private unless listed here.
STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/viewer.css": ("viewer.css", "text/css; charset=utf-8"),
    "/static/viewer.js": ("viewer.js", "text/javascript; charset=utf-8"),
    "/static/cross-probe.js": ("cross-probe.js", "text/javascript; charset=utf-8"),
    "/static/net-labels.js": ("net-labels.js", "text/javascript; charset=utf-8"),
    "/static/viewer-3d.js": ("viewer-3d.js", "text/javascript; charset=utf-8"),
    "/static/cad-controls.js": ("cad-controls.js", "text/javascript; charset=utf-8"),
    "/static/board-surface.js": ("board-surface.js", "text/javascript; charset=utf-8"),
    "/static/studio-3d.js": ("studio-3d.js", "text/javascript; charset=utf-8"),
    "/static/step-loader.js": ("step-loader.js", "text/javascript; charset=utf-8"),
    "/static/step-worker.js": ("step-worker.js", "text/javascript; charset=utf-8"),
    "/static/occt-import-js.js": ("occt-import-js.js", "text/javascript; charset=utf-8"),
    "/static/occt-import-js.wasm": ("occt-import-js.wasm", "application/wasm"),
    "/static/OCCT-LICENSE.txt": ("OCCT-LICENSE.txt", "text/plain; charset=utf-8"),
    "/static/OCCT-IMPORT-LICENSE.txt": ("OCCT-IMPORT-LICENSE.txt", "text/plain; charset=utf-8"),
    "/static/layout-webgl.js": ("layout-webgl.js", "text/javascript; charset=utf-8"),
    "/static/layout-shaders.js": ("layout-shaders.js", "text/javascript; charset=utf-8"),
    "/static/schematic-style.js": ("schematic-style.js", "text/javascript; charset=utf-8"),
    "/static/measurement-tools.js": ("measurement-tools.js", "text/javascript; charset=utf-8"),
    "/static/rectangle-selection.js": ("rectangle-selection.js", "text/javascript; charset=utf-8"),
    "/static/viewer-session.js": ("viewer-session.js", "text/javascript; charset=utf-8"),
    "/static/three.module.min.js": ("three.module.min.js", "text/javascript; charset=utf-8"),
    "/static/three.core.min.js": ("three.core.min.js", "text/javascript; charset=utf-8"),
    "/static/OrbitControls.js": ("OrbitControls.js", "text/javascript; charset=utf-8"),
    "/static/THREE-LICENSE.txt": ("THREE-LICENSE.txt", "text/plain; charset=utf-8"),
}


class ViewerServer(ThreadingHTTPServer):
    def __init__(self, address, handler, *, grace=3.0):
        super().__init__(address, handler)
        self.viewer_lifetime = ViewerLifetime(self.shutdown, grace)

    def shutdown(self):
        self.viewer_lifetime.close()
        super().shutdown()

    def server_close(self):
        if lifetime := getattr(self, "viewer_lifetime", None):
            lifetime.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    initial: dict | None = None  # board given on the command line, served once

    def log_message(self, *_):  # keep the console quiet
        pass

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: dict) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def do_GET(self) -> None:
        url = urllib.parse.urlparse(self.path)
        if url.path == "/session":
            self._viewer_session()
        elif url.path in STATIC_ROUTES:
            filename, content_type = STATIC_ROUTES[url.path]
            body = files("cadview").joinpath("static", filename).read_bytes()
            self._send(200, body, content_type)
        elif url.path == "/initial":
            data, Handler.initial = Handler.initial, None
            self._json(200, data or {})
        else:
            self._send(404, b"not found", "text/plain")

    def _viewer_session(self):
        host = self.headers.get("Host")
        port = self.server.server_address[1]
        if (host not in {f"127.0.0.1:{port}", f"localhost:{port}"}
                or self.headers.get("Origin", f"http://{host}") != f"http://{host}"
                or self.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin"
                or self.headers.get("Accept") != "text/event-stream"):
            self._json(403, {"error": "A session must connect from this viewer."})
            return
        lifetime = getattr(self.server, "viewer_lifetime", None)
        connection = lifetime.connect() if lifetime else None
        if connection is None:
            self._send(204, b"", "text/event-stream")  # Stop EventSource retries.
            return
        self.close_connection = True
        try:
            self.connection.settimeout(2)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"retry: 500\ndata: connected\n\n")
            self.wfile.flush()
            heartbeat = time.monotonic() + 5
            while not lifetime.stopping.is_set():
                readable, _, _ = select.select([self.connection], [], [], .5)
                # This is a one-way HTTP stream. Readability means EOF or an
                # unexpected pipelined request; neither keeps a viewer alive.
                if readable:
                    break
                if time.monotonic() >= heartbeat:
                    self.wfile.write(b": alive\n\n")
                    self.wfile.flush()
                    heartbeat = time.monotonic() + 5
        except (OSError, ValueError):
            pass  # Tab/window closed, browser exited, or backend is stopping.
        finally:
            lifetime.disconnect(connection)

    def do_POST(self) -> None:
        url = urllib.parse.urlparse(self.path)
        if url.path == "/shutdown":
            host = f"127.0.0.1:{self.server.server_address[1]}"
            if (self.headers.get("Host") != host or self.headers.get("Origin") != f"http://{host}"
                    or self.headers.get("X-Viewer-Request") != "shutdown"):
                self._json(403, {"error": "Quit must be requested from this viewer."})
                return
            self._json(200, {"stopped": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if url.path == "/geometry":
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= MAX_INPUT:
                    self.close_connection = True
                    self._json(413, {"error": "Invalid layout geometry request size"})
                    return
                body = self.rfile.read(length)
                lifetime = getattr(self.server, "viewer_lifetime", None)
                geometry = (prepare_geometry(body, stopping=lifetime.stopping) if lifetime else prepare_geometry(body))
                if geometry is None:
                    self._json(503, {"error": "Another layout is being prepared"})
                else:
                    self._send(200, geometry, "application/octet-stream")
            except (BrokenPipeError, ConnectionResetError):
                pass  # The tab was closed while its isolated compiler finished.
            except Exception as exc:
                try:
                    self._json(422, {"error": str(exc)})
                except (BrokenPipeError, ConnectionResetError):
                    pass
            return
        if url.path != "/render":
            self._send(404, b"not found", "text/plain")
            return
        q = urllib.parse.parse_qs(url.query)
        get = lambda k, d="": q.get(k, [d])[0]
        name = urllib.parse.unquote(self.headers.get("X-Filename", "board.PcbDoc"))
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            webgl = self.headers.get("X-WebGL") == "1"
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / (Path(name).name or "board.PcbDoc")
                path.write_bytes(body)
                data = render_file(
                    path,
                    side=get("side", "all"),
                    clip=get("clip") == "1",
                    pour_color=get("pour") or None,
                    polygon_outlines=get("outlines") == "1",
                    **({"native": True} if webgl else {}),
                )
            if webgl:
                data["webgl"] = True
            self._json(200, data)
        except Exception as exc:  # report parser/renderer errors to the page
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
