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
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDITOR_DIR = os.path.join(ROOT, "editor")
PROXY_DIR = os.path.join(ROOT, "proxies")
OUT_DIR = os.path.join(ROOT, "output")
RENDER_SCRIPT = os.path.join(ROOT, "render", "render.py")
DB_PATH = os.path.join(ROOT, "markers.db")
MARKERS_PATH = os.path.join(ROOT, "markers.json")  # JSON mirror for the render pipeline
META_PATH = os.path.join(ROOT, "cache", "clips_meta.json")
WAVE_BIN = os.path.join(ROOT, "cache", "waveform.u8")
WAVE_META = os.path.join(ROOT, "cache", "waveform.json")
TRANSCRIPT_PATH = os.path.join(ROOT, "cache", "transcript.json")

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


# ---- persistence (SQLite, source of truth) ---------------------------
# markers.db is the durable store. Each save also mirrors to markers.json so
# render/render.py keeps reading a plain file. A new connection is opened per
# call (sqlite3 connections aren't shareable across ThreadingHTTPServer
# threads); _DB_LOCK serializes writes so concurrent saves can't interleave.
_DB_LOCK = threading.Lock()

DEFAULT_PROJECT = {
    "seed": 42,
    "project": "Belgium Concert Highlights",
    "fps": 60,
    "duration": 5764.7,
    "audio_source": "back",
}


_SCHEMA = """
    CREATE TABLE IF NOT EXISTS project (
        id           INTEGER PRIMARY KEY CHECK (id = 1),
        seed         INTEGER NOT NULL DEFAULT 42,
        project      TEXT,
        fps          REAL,
        duration     REAL,
        audio_source TEXT
    );
    CREATE TABLE IF NOT EXISTS performances (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        ordinal  INTEGER NOT NULL,
        title    TEXT,
        composer TEXT,
        in_s     REAL NOT NULL,
        out_s    REAL NOT NULL
    );
    -- On-screen text overlays ("titles"), each shown over the final render for
    -- its [in_s, out_s] window (global concert seconds). `subtitle` is an
    -- optional second line. Burned in by render/render.py via drawtext.
    CREATE TABLE IF NOT EXISTS titles (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        ordinal  INTEGER NOT NULL,
        text     TEXT,
        subtitle TEXT,
        in_s     REAL NOT NULL,
        out_s    REAL NOT NULL,
        x_pos    REAL,
        y_pos    REAL
    );
    -- Each row is a full JSON snapshot of the project + all regions, taken
    -- automatically every BACKUP_EVERY writes. `day` is the local YYYY-MM-DD
    -- the snapshot was taken, used by the prune step to keep one-per-day.
    CREATE TABLE IF NOT EXISTS region_backups (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT    NOT NULL,
        day        TEXT    NOT NULL,
        write_no   INTEGER NOT NULL,
        n_regions  INTEGER NOT NULL,
        payload    TEXT    NOT NULL
    );
    -- Singleton counter of total saves, so "every 5th write" survives restarts.
    CREATE TABLE IF NOT EXISTS backup_state (
        id          INTEGER PRIMARY KEY CHECK (id = 1),
        write_count INTEGER NOT NULL DEFAULT 0
    );
"""

# Backup cadence + retention.
BACKUP_EVERY = 5     # take a snapshot on every Nth save
BACKUP_KEEP_RECENT = 100  # keep this many newest snapshots verbatim; older ones
                          # collapse to one-per-day (the "eat its own tail" loop)


def db_connect():
    # Ensure the schema on every connection (cheap, idempotent) so the tables
    # exist even if markers.db was deleted/moved while the server is running —
    # connections are per-request, so we can't rely on a one-time startup init.
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    # Migrate older DBs whose titles table predates the x/y position columns.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(titles)")}
    for col in ("x_pos", "y_pos"):
        if col not in cols:
            conn.execute(f"ALTER TABLE titles ADD COLUMN {col} REAL")
    return conn


def db_init():
    """Seed from markers.json on first run so existing work isn't lost when
    migrating off the flat file. Schema itself is ensured in db_connect()."""
    with db_connect() as conn:
        row = conn.execute("SELECT id FROM project WHERE id = 1").fetchone()
        if row is None:
            seed = dict(DEFAULT_PROJECT)
            if os.path.isfile(MARKERS_PATH):
                try:
                    with open(MARKERS_PATH) as f:
                        existing = json.load(f)
                    seed.update({k: existing[k] for k in DEFAULT_PROJECT if k in existing})
                    _write_performances(conn, existing.get("performances", []))
                    _write_titles(conn, existing.get("titles", []))
                    print(f"Seeded markers.db from {MARKERS_PATH} "
                          f"({len(existing.get('performances', []))} performances, "
                          f"{len(existing.get('titles', []))} titles)")
                except Exception as e:
                    print(f"Could not seed from markers.json: {e}")
            conn.execute(
                "INSERT INTO project (id, seed, project, fps, duration, audio_source) "
                "VALUES (1, ?, ?, ?, ?, ?)",
                (seed["seed"], seed["project"], seed["fps"], seed["duration"],
                 seed["audio_source"]),
            )


def _write_performances(conn, perfs):
    conn.execute("DELETE FROM performances")
    conn.executemany(
        "INSERT INTO performances (ordinal, title, composer, in_s, out_s) "
        "VALUES (?, ?, ?, ?, ?)",
        [(i, p.get("title"), p.get("composer"), p.get("in"), p.get("out"))
         for i, p in enumerate(perfs)],
    )


def _write_titles(conn, titles):
    conn.execute("DELETE FROM titles")
    conn.executemany(
        "INSERT INTO titles (ordinal, text, subtitle, in_s, out_s, x_pos, y_pos) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(i, t.get("text"), t.get("subtitle"), t.get("in"), t.get("out"),
          t.get("x"), t.get("y"))
         for i, t in enumerate(titles)],
    )


def _load(conn):
    """Read the full project payload (meta + performances + titles)."""
    prow = conn.execute(
        "SELECT seed, project, fps, duration, audio_source FROM project WHERE id = 1"
    ).fetchone()
    meta = dict(prow) if prow else dict(DEFAULT_PROJECT)
    meta["performances"] = [
        {"title": r["title"], "composer": r["composer"], "in": r["in_s"], "out": r["out_s"]}
        for r in conn.execute(
            "SELECT title, composer, in_s, out_s FROM performances ORDER BY ordinal"
        )
    ]
    meta["titles"] = [
        {"text": r["text"], "subtitle": r["subtitle"], "in": r["in_s"], "out": r["out_s"],
         "x": r["x_pos"], "y": r["y_pos"]}
        for r in conn.execute(
            "SELECT text, subtitle, in_s, out_s, x_pos, y_pos FROM titles ORDER BY ordinal"
        )
    ]
    return meta


def db_load():
    with db_connect() as conn:
        return _load(conn)


def _bump_write_count(conn):
    """Increment and return the persistent total-saves counter."""
    conn.execute(
        "INSERT INTO backup_state (id, write_count) VALUES (1, 1) "
        "ON CONFLICT(id) DO UPDATE SET write_count = write_count + 1"
    )
    return conn.execute("SELECT write_count FROM backup_state WHERE id = 1").fetchone()[0]


def _create_backup(conn, snapshot, write_no, now):
    conn.execute(
        "INSERT INTO region_backups (created_at, day, write_no, n_regions, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        (now.isoformat(timespec="seconds"), now.strftime("%Y-%m-%d"), write_no,
         len(snapshot.get("performances", [])), json.dumps(snapshot)),
    )


def _prune_backups(conn):
    """Self-healing retention: keep the newest BACKUP_KEEP_RECENT snapshots as-is,
    then for everything older keep only the most recent snapshot per day (so write
    days stay represented forever while the dense recent history is bounded)."""
    rows = conn.execute(
        "SELECT id, day FROM region_backups ORDER BY id DESC"
    ).fetchall()
    older = rows[BACKUP_KEEP_RECENT:]  # rows beyond the recent window, newest-first
    seen_days, to_delete = set(), []
    for r in older:
        if r["day"] in seen_days:
            to_delete.append(r["id"])      # a newer snapshot already represents this day
        else:
            seen_days.add(r["day"])        # keep this one as the day's survivor
    if to_delete:
        conn.executemany(
            "DELETE FROM region_backups WHERE id = ?", [(i,) for i in to_delete]
        )
    return len(to_delete)


def list_backups():
    """Backup metadata, newest first (payload omitted to keep the list light)."""
    with db_connect() as conn:
        return [
            {"id": r["id"], "created_at": r["created_at"], "day": r["day"],
             "write_no": r["write_no"], "n_regions": r["n_regions"]}
            for r in conn.execute(
                "SELECT id, created_at, day, write_no, n_regions "
                "FROM region_backups ORDER BY id DESC"
            )
        ]


def restore_backup(backup_id):
    """Re-save the project from a stored snapshot. Goes through db_save so the
    restore is itself mirrored to markers.json and counts as a normal write."""
    with _DB_LOCK:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT payload FROM region_backups WHERE id = ?", (backup_id,)
            ).fetchone()
            if row is None:
                return None
            # Safety snapshot of the live state first, so a restore is itself
            # reversible even when we're not on a BACKUP_EVERY boundary.
            cur = conn.execute(
                "SELECT write_count FROM backup_state WHERE id = 1"
            ).fetchone()
            _create_backup(conn, _load(conn), cur[0] if cur else 0, datetime.now())
            _prune_backups(conn)
        payload = json.loads(row["payload"])
    return db_save(payload)


# ---- single-performance export jobs ----------------------------------
# Each editor "Export" button kicks `render/render.py --only N` as a subprocess.
# Renders are heavy, so _EXPORT_RUN_LOCK serializes them (extra requests queue);
# job state lives in _EXPORTS keyed by 1-based performance index for status polls.
_EXPORTS = {}
_EXPORTS_LOCK = threading.Lock()
_EXPORT_RUN_LOCK = threading.Lock()


def _export_worker(index):
    info = _EXPORTS[index]
    try:
        with _EXPORT_RUN_LOCK:                 # one heavy render at a time
            info["status"] = "running"
            info["started"] = time.time()
            proc = subprocess.Popen(
                [sys.executable, RENDER_SCRIPT, "--only", str(index)],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
            last = ""
            for line in proc.stdout:           # \r progress arrives as its own lines
                line = line.strip()
                if not line:
                    continue
                last = line
                info["line"] = line
                m = re.search(r"(\S+\.mp4)\s*$", line)
                if m and "✓" in line:
                    info["file"] = m.group(1)
            proc.wait()
            info["code"] = proc.returncode
            info["status"] = "done" if proc.returncode == 0 else "error"
            if proc.returncode != 0:
                info["error"] = last or f"render exited {proc.returncode}"
    except Exception as e:
        info["status"] = "error"
        info["error"] = str(e)
    finally:
        info["ended"] = time.time()


def start_export(index):
    """Kick a background render of one performance (1-based). Returns False if
    that performance is already queued/running."""
    with _EXPORTS_LOCK:
        cur = _EXPORTS.get(index)
        if cur and cur["status"] in ("queued", "running"):
            return False
        _EXPORTS[index] = {"status": "queued", "requested": time.time(),
                           "file": None, "error": None, "line": ""}
    threading.Thread(target=_export_worker, args=(index,), daemon=True).start()
    return True


def exports_status():
    """Snapshot of every export job: status, elapsed seconds, output filename."""
    with _EXPORTS_LOCK:
        out = {}
        for idx, info in _EXPORTS.items():
            start = info.get("started") or info.get("requested")
            elapsed = round((info.get("ended") or time.time()) - start) if start else 0
            out[idx] = {
                "status": info["status"],
                "elapsed": elapsed,
                "file": os.path.basename(info["file"]) if info.get("file") else None,
                "error": info.get("error"),
                "line": info.get("line", ""),
            }
        return out


def db_save(data):
    """Persist a full project payload, then mirror to markers.json."""
    with _DB_LOCK:
        with db_connect() as conn:
            conn.execute(
                "INSERT INTO project (id, seed, project, fps, duration, audio_source) "
                "VALUES (1, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "seed=excluded.seed, project=excluded.project, fps=excluded.fps, "
                "duration=excluded.duration, audio_source=excluded.audio_source",
                (data.get("seed", 42), data.get("project"), data.get("fps"),
                 data.get("duration"), data.get("audio_source")),
            )
            _write_performances(conn, data.get("performances", []))
            _write_titles(conn, data.get("titles", []))
            # Snapshot every Nth write, then trim per the retention policy. Done
            # inside the same transaction so a backup never reflects half a save.
            count = _bump_write_count(conn)
            if count % BACKUP_EVERY == 0:
                _create_backup(conn, _load(conn), count, datetime.now())
                _prune_backups(conn)
        # Mirror the canonical view back out for the render pipeline.
        mirror = db_load()
        tmp = MARKERS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(mirror, f, indent=2)
        os.replace(tmp, MARKERS_PATH)
        return mirror


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
        if path == "/api/backups":
            return self._send_json({"backups": list_backups()})
        if path == "/api/exports":
            return self._send_json({"exports": exports_status()})
        if path == "/api/waveform":
            if os.path.isfile(WAVE_META) and os.path.isfile(WAVE_BIN):
                with open(WAVE_META) as f:
                    meta = json.load(f)
                meta["ready"] = True
                return self._send_json(meta)
            return self._send_json({"ready": False})
        if path == "/waveform.u8":
            return self._serve_file(WAVE_BIN)
        if path == "/api/transcript":
            # Time-coded caption transcript. Grows as transcribe.py finishes
            # each of its 10 chunks; "ready" stays False until the file exists.
            if os.path.isfile(TRANSCRIPT_PATH):
                with open(TRANSCRIPT_PATH) as f:
                    data = json.load(f)
                data["ready"] = True
                return self._send_json(data)
            return self._send_json({"ready": False, "segments": []})
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
            try:
                db_save(data)
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 500)
            return self._send_json({"ok": True, "saved": DB_PATH})
        if path == "/api/backups/restore":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                backup_id = int(json.loads(raw.decode("utf-8")).get("id"))
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 400)
            restored = restore_backup(backup_id)
            if restored is None:
                return self._send_json({"ok": False, "error": "no such backup"}, 404)
            return self._send_json({"ok": True, "restored": restored})
        if path == "/api/export":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                index = int(json.loads(raw.decode("utf-8")).get("index"))
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 400)
            started = start_export(index)
            return self._send_json({"ok": True, "index": index, "started": started})
        if path == "/api/open":
            # Open a finished render in the default player. Restricted to OUT_DIR.
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                fname = os.path.basename(json.loads(raw.decode("utf-8")).get("file", ""))
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 400)
            target = os.path.join(OUT_DIR, fname)
            if not fname or not os.path.isfile(target):
                return self._send_json({"ok": False, "error": "no such file"}, 404)
            try:
                subprocess.Popen(["open", target])
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 500)
            return self._send_json({"ok": True})
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
        return db_load()


def main():
    os.chdir(ROOT)
    db_init()
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Editor running at  http://localhost:{PORT}")
    print(f"Markers DB:        {DB_PATH}")
    print(f"Project root:      {ROOT}")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
