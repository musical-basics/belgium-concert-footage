#!/usr/bin/env python3
"""
Local editor server for the Belgium Concert Highlights project.

- Serves the editor GUI (editor/*).
- Serves proxy videos from proxies/ WITH HTTP Range support (needed for
  smooth seeking/scrubbing of <video> elements).
- GET  /api/meta     -> clip + proxy info, fps, duration
- GET  /api/markers  -> current markers.json (performances)
- POST /api/markers  -> save markers.json

Run:  python3 editor/server.py   then open http://localhost:8000
No third-party dependencies.
"""
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDITOR_DIR = os.path.join(ROOT, "editor")
PROXY_DIR = os.path.join(ROOT, "proxies")
MARKERS_PATH = os.path.join(ROOT, "markers.json")
META_PATH = os.path.join(ROOT, "cache", "clips_meta.json")
WAVE_BIN = os.path.join(ROOT, "cache", "waveform.u8")
WAVE_META = os.path.join(ROOT, "cache", "waveform.json")

PORT = int(os.environ.get("EDITOR_PORT", "8000"))

# Logical clip ids -> proxy filename and display label. Order = track order.
CLIPS = [
    {"id": "back",       "label": "Back Camera",         "proxy": "back.mp4",       "is_audio": True},
    {"id": "livestream", "label": "Livestream Footage",  "proxy": "livestream.mp4", "is_audio": False},
    {"id": "piano",      "label": "Camera next to piano","proxy": "piano.mp4",      "is_audio": False},
]

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
}


def guess_type(path):
    return CONTENT_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # quiet

    def handle_one_request(self):
        # Browsers open speculative keep-alive connections for <video> range
        # requests and reset them without sending a request line. That reset
        # surfaces here (while reading raw_requestline), not in _copy_range, so
        # swallow it instead of letting socketserver dump a traceback.
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    # ---- helpers -----------------------------------------------------
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path):
        if not os.path.isfile(path):
            self.send_error(404, "Not found")
            return
        ctype = guess_type(path)
        size = os.path.getsize(path)
        range_header = self.headers.get("Range")
        if range_header:
            m = re.match(r"bytes=(\d*)-(\d*)", range_header)
            start = int(m.group(1)) if m and m.group(1) else 0
            end = int(m.group(2)) if m and m.group(2) else size - 1
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            self._copy_range(path, start, length)
        else:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            self._copy_range(path, 0, size)

    def _copy_range(self, path, start, length):
        chunk = 1024 * 256
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    data = f.read(min(chunk, remaining))
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client seeked away / closed

    # ---- routing -----------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "":
            return self._serve_file(os.path.join(EDITOR_DIR, "index.html"))
        if path == "/api/meta":
            return self._send_json(self._meta())
        if path == "/api/markers":
            return self._send_json(self._load_markers())
        if path == "/api/waveform":
            if os.path.isfile(WAVE_META) and os.path.isfile(WAVE_BIN):
                with open(WAVE_META) as f:
                    meta = json.load(f)
                meta["ready"] = True
                return self._send_json(meta)
            return self._send_json({"ready": False})
        if path == "/waveform.u8":
            return self._serve_file(WAVE_BIN)
        if path.startswith("/proxies/"):
            name = os.path.basename(path)
            return self._serve_file(os.path.join(PROXY_DIR, name))
        if path.startswith("/editor/"):
            name = os.path.basename(path)
            return self._serve_file(os.path.join(EDITOR_DIR, name))
        # bare static files from editor dir (app.js, styles.css)
        name = os.path.basename(path)
        candidate = os.path.join(EDITOR_DIR, name)
        if os.path.isfile(candidate):
            return self._serve_file(candidate)
        self.send_error(404, "Not found")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/markers":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 400)
            with open(MARKERS_PATH, "w") as f:
                json.dump(data, f, indent=2)
            return self._send_json({"ok": True, "saved": MARKERS_PATH})
        self.send_error(404, "Not found")

    # ---- data --------------------------------------------------------
    def _meta(self):
        meta = {}
        if os.path.isfile(META_PATH):
            with open(META_PATH) as f:
                meta = json.load(f)
        clips = []
        for c in CLIPS:
            proxy_path = os.path.join(PROXY_DIR, c["proxy"])
            clips.append({
                **c,
                "proxy_ready": os.path.isfile(proxy_path),
                "proxy_url": f"/proxies/{c['proxy']}",
            })
        return {
            "clips": clips,
            "duration": meta.get("duration", 5764.7),
            "fps": meta.get("fps", 60),
        }

    def _load_markers(self):
        if os.path.isfile(MARKERS_PATH):
            with open(MARKERS_PATH) as f:
                return json.load(f)
        return {"seed": 42, "performances": []}


def main():
    os.chdir(ROOT)
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Editor running at  http://localhost:{PORT}")
    print(f"Project root:      {ROOT}")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
