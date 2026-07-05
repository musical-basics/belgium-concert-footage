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
import hmac
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDITOR_DIR = os.path.join(ROOT, "editor")
PROXY_DIR = os.path.join(ROOT, "proxies")
OUT_DIR = os.path.join(ROOT, "output")
THUMBS_DIR = os.path.join(OUT_DIR, "thumbs")   # generated performance thumbnails
RENDER_SCRIPT = os.path.join(ROOT, "render", "render.py")
DB_PATH = os.path.join(ROOT, "markers.db")
MARKERS_PATH = os.path.join(ROOT, "markers.json")  # JSON mirror for the render pipeline
META_PATH = os.path.join(ROOT, "cache", "clips_meta.json")
WAVE_BIN = os.path.join(ROOT, "cache", "waveform.u8")
WAVE_META = os.path.join(ROOT, "cache", "waveform.json")
TRANSCRIPT_PATH = os.path.join(ROOT, "cache", "transcript.json")
SYNC_JSON = os.path.join(EDITOR_DIR, "sync.json")   # 5D 2 live-camera coverage map

PORT = int(os.environ.get("EDITOR_PORT", "8000"))

# Shared-secret auth. When EDITOR_TOKEN is set (production/public deploy), every
# /api/* request, proxy video, and waveform must present the token — via
# "Authorization: Bearer <token>", an "X-Auth-Token" header, or a "?token=..."
# query param (needed for <video>/<img> which can't set headers). Left unset in
# local dev -> no auth. The static shell (index.html/app.js/css) stays open so
# the login prompt can load; it holds no secrets on its own.
AUTH_TOKEN = os.environ.get("EDITOR_TOKEN", "").strip()

# Logical clip ids -> proxy filename and display label. Order = track order.
CLIPS = [
    {"id": "back",       "label": "Back Camera",         "proxy": "back.mp4",       "is_audio": True},
    {"id": "livestream", "label": "Livestream Footage",  "proxy": "livestream.mp4", "is_audio": False},
    {"id": "piano",      "label": "Camera next to piano","proxy": "piano.mp4",      "is_audio": False},
    # Roving live camera. Only covers parts of the concert; the editor pane
    # follows the playhead through the audio-matched map in editor/sync.json
    # and the renderer prioritizes it wherever it has footage (render/plan.py).
    {"id": "5d2",        "label": "5D 2 (live)",         "proxy": "5d2.mp4",        "is_audio": False,
     "live": True},
]

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def guess_type(path):
    return CONTENT_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def slugify(s):
    # Mirror render.py's slugify so predicted output filenames match what a
    # render actually writes (NN_slug.mp4).
    s = re.sub(r"[^\w\s-]", "", s or "").strip().lower()
    return re.sub(r"[\s_-]+", "-", s) or "untitled"


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
    "title_scale": 1.0,
}


_SCHEMA = """
    CREATE TABLE IF NOT EXISTS project (
        id           INTEGER PRIMARY KEY CHECK (id = 1),
        seed         INTEGER NOT NULL DEFAULT 42,
        project      TEXT,
        fps          REAL,
        duration     REAL,
        audio_source TEXT,
        title_scale  REAL DEFAULT 1.0,
        -- Per-camera color grade as a JSON object {cam: {brightness,gamma,
        -- contrast,saturation}}. NULL = use render.py's baked-in defaults.
        camera_grades TEXT
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
        y_pos    REAL,
        scale    REAL
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
    # Migrate older DBs whose titles table predates the x/y/scale columns.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(titles)")}
    for col in ("x_pos", "y_pos", "scale"):
        if col not in cols:
            conn.execute(f"ALTER TABLE titles ADD COLUMN {col} REAL")
    # ...and the project's global title font scale + per-camera color grades.
    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(project)")}
    if "title_scale" not in pcols:
        conn.execute("ALTER TABLE project ADD COLUMN title_scale REAL")
    if "camera_grades" not in pcols:
        conn.execute("ALTER TABLE project ADD COLUMN camera_grades TEXT")
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
                    if existing.get("camera_grades"):
                        seed["camera_grades"] = existing["camera_grades"]
                    _write_performances(conn, existing.get("performances", []))
                    _write_titles(conn, existing.get("titles", []))
                    print(f"Seeded markers.db from {MARKERS_PATH} "
                          f"({len(existing.get('performances', []))} performances, "
                          f"{len(existing.get('titles', []))} titles)")
                except Exception as e:
                    print(f"Could not seed from markers.json: {e}")
            cg = seed.get("camera_grades")
            conn.execute(
                "INSERT INTO project "
                "(id, seed, project, fps, duration, audio_source, camera_grades) "
                "VALUES (1, ?, ?, ?, ?, ?, ?)",
                (seed["seed"], seed["project"], seed["fps"], seed["duration"],
                 seed["audio_source"], json.dumps(cg) if cg else None),
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
        "INSERT INTO titles (ordinal, text, subtitle, in_s, out_s, x_pos, y_pos, scale) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(i, t.get("text"), t.get("subtitle"), t.get("in"), t.get("out"),
          t.get("x"), t.get("y"), t.get("scale"))
         for i, t in enumerate(titles)],
    )


def _load(conn):
    """Read the full project payload (meta + performances + titles)."""
    prow = conn.execute(
        "SELECT seed, project, fps, duration, audio_source, title_scale, camera_grades "
        "FROM project WHERE id = 1"
    ).fetchone()
    meta = dict(prow) if prow else dict(DEFAULT_PROJECT)
    if meta.get("title_scale") is None:      # NULL on rows predating the column
        meta["title_scale"] = 1.0
    # camera_grades is stored as a JSON string; expose it as an object (or drop
    # the key when unset so the render/thumbnails fall back to defaults).
    cg = meta.pop("camera_grades", None)
    if cg:
        try:
            meta["camera_grades"] = json.loads(cg) if isinstance(cg, str) else cg
        except (TypeError, ValueError):
            pass
    meta["performances"] = [
        {"title": r["title"], "composer": r["composer"], "in": r["in_s"], "out": r["out_s"]}
        for r in conn.execute(
            "SELECT title, composer, in_s, out_s FROM performances ORDER BY ordinal"
        )
    ]
    meta["titles"] = [
        {"text": r["text"], "subtitle": r["subtitle"], "in": r["in_s"], "out": r["out_s"],
         "x": r["x_pos"], "y": r["y_pos"], "scale": r["scale"]}
        for r in conn.execute(
            "SELECT text, subtitle, in_s, out_s, x_pos, y_pos, scale FROM titles ORDER BY ordinal"
        )
    ]
    return meta


def db_load():
    with db_connect() as conn:
        return _load(conn)


# ---- 5D 2 live-camera coverage (sync.json) ---------------------------
# sync.json is the audio-matched map of the 5D 2 reel onto the concert timeline.
# render.py reads clips[].{ref_in,ref_out,delta} directly to prioritize the live
# camera, so the editor persists trims/cuts straight into that file. To keep the
# original matched extent as the outer limit (so a trim is reversible), the first
# edit of a clip records its matched bounds in an `orig` block; ref_in/ref_out
# then hold the *effective* (trimmed) window and can never leave `orig`.
_SYNC_LOCK = threading.Lock()


def _sync_load():
    with open(SYNC_JSON) as f:
        return json.load(f)


def _clip_orig(c):
    """The matched (max) bounds of a clip — the `orig` block if present, else the
    clip's own bounds (an as-yet-unedited clip is at full extent)."""
    o = c.get("orig")
    if o:
        return o["src_in"], o["src_out"], o["ref_in"], o["ref_out"]
    return c["src_in"], c["src_out"], c["ref_in"], c["ref_out"]


def save_live_clips(clips):
    """Replace sync.json's `clips` with `clips` (from the editor), clamping every
    clip to its own matched envelope and (re)deriving src bounds from ref+delta.
    Returns the normalized clip list. Serialized so concurrent saves can't race."""
    with _SYNC_LOCK:
        doc = _sync_load()
        clean = []
        for raw in clips:
            delta = float(raw["delta"])
            osi, oso, ori, oro = (float(raw["orig"]["src_in"]), float(raw["orig"]["src_out"]),
                                  float(raw["orig"]["ref_in"]), float(raw["orig"]["ref_out"]))
            # effective ref window, clamped inside the matched envelope
            ri = max(ori, min(float(raw["ref_in"]), oro))
            ro = min(oro, max(float(raw["ref_out"]), ri))
            c = {
                "i": raw["i"],
                "src_in": round(ri - delta, 3), "src_out": round(ro - delta, 3),
                "ref_in": round(ri, 3), "ref_out": round(ro, 3),
                "delta": round(delta, 3),
                "dur": round(ro - ri, 3),
                "orig": {"src_in": round(osi, 3), "src_out": round(oso, 3),
                         "ref_in": round(ori, 3), "ref_out": round(oro, 3)},
                "flags": list(raw.get("flags") or []),
            }
            for k in ("env_score", "phat_locks", "phat_q"):
                if raw.get(k) is not None:
                    c[k] = raw[k]
            clean.append(c)
        clean.sort(key=lambda c: c["ref_in"])
        doc["clips"] = clean
        doc["edited"] = "trimmed/cut in editor"
        tmp = SYNC_JSON + ".tmp"
        with open(tmp, "w") as f:
            json.dump(doc, f, indent=1)
        os.replace(tmp, SYNC_JSON)
        return clean


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
            info["phase"] = "preparing"        # analysis + cut plan, before encoding
            info["progress"] = 0
            # -u => unbuffered, so render.py's "cut seg X/Y" progress arrives live
            # (each \r update as its own line) instead of one burst at the end.
            proc = subprocess.Popen(
                [sys.executable, "-u", RENDER_SCRIPT, "--only", str(index)],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
            last = ""
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                last = line
                info["line"] = line
                m = re.search(r"cut seg (\d+)/(\d+)", line)
                if m:                          # encoding segments = the bulk of a render
                    cur, total = int(m.group(1)), int(m.group(2))
                    info["progress"] = round(cur / total * 100) if total else 0
                    info["phase"] = "finishing" if cur >= total else "cutting"
                elif "✓" in line and ".mp4" in line:
                    fm = re.search(r"(\S+\.mp4)\s*$", line)
                    if fm:
                        info["file"] = fm.group(1)
                    info["progress"] = 100
            proc.wait()
            info["code"] = proc.returncode
            if proc.returncode == 0:
                info["status"] = "done"
                info["progress"], info["phase"] = 100, "done"
            else:
                info["status"] = "error"
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
                           "progress": 0, "phase": "queued",
                           "file": None, "error": None, "line": ""}
    threading.Thread(target=_export_worker, args=(index,), daemon=True).start()
    return True


# ---- performance thumbnails ------------------------------------------
# "Generate thumbnail" pulls 10 full-res still frames spread across a
# performance, cycling through the camera angles (the roving 5D 2 where it has
# live coverage, otherwise the 3 stationary cameras) so you get a varied
# contact-sheet to pick a poster frame from — no render required. Frames are
# graded with the SAME per-camera eq as the final render (render.CAMERA_GRADES,
# incl. any editor override) so what you see matches the export. Written to
# output/thumbs/NN/.

# render.py owns the source-file map, per-camera grade, and the 5D 2 live map;
# import lazily so a broken render module can't stop the editor from booting.
_RENDER_MOD = None


def _render_mod():
    global _RENDER_MOD
    if _RENDER_MOD is None:
        sys.path.insert(0, os.path.join(ROOT, "render"))
        import render as _r
        _RENDER_MOD = _r
    return _RENDER_MOD


# id -> label, mirroring CLIPS, for captioning thumbnails.
_CAM_LABEL = {c["id"]: c["label"] for c in CLIPS}

# ---- per-camera color grades -----------------------------------------
_GRADE_KEYS = ("brightness", "gamma", "contrast", "saturation")
# Sane UI/validation ranges (also enforced server-side so a bad POST can't
# produce a broken ffmpeg filter). ffmpeg eq: brightness -1..1, others 0..3ish.
_GRADE_BOUNDS = {
    "brightness": (-1.0, 1.0),
    "gamma": (0.1, 3.0),
    "contrast": (0.0, 3.0),
    "saturation": (0.0, 3.0),
}


def _camera_grades():
    """Current per-camera grade overrides from the DB (or {} if none saved)."""
    try:
        return db_load().get("camera_grades") or {}
    except Exception:
        return {}


def camera_grade_defaults():
    """The baked-in defaults from render.py, as a plain dict (for the API so the
    editor can show/reset to them)."""
    r = _render_mod()
    return {cam: dict(g) for cam, g in r.CAMERA_GRADE_DEFAULTS.items()}


def clean_camera_grades(incoming):
    """Validate + clamp a {cam: {knob: value}} payload against known cameras and
    bounds. Drops unknown cameras/knobs and non-numeric values. Returns a clean
    dict (possibly empty)."""
    r = _render_mod()
    known = set(r.CAMERA_GRADE_DEFAULTS)
    out = {}
    for cam, g in (incoming or {}).items():
        if cam not in known or not isinstance(g, dict):
            continue
        clean = {}
        for key in _GRADE_KEYS:
            if key not in g or g[key] is None:
                continue
            try:
                v = float(g[key])
            except (TypeError, ValueError):
                continue
            lo, hi = _GRADE_BOUNDS[key]
            clean[key] = max(lo, min(hi, v))
        if clean:
            out[cam] = clean
    return out

# thumbnail generation jobs, keyed by 1-based performance index (like _EXPORTS).
_THUMBS = {}
_THUMBS_LOCK = threading.Lock()
_THUMBS_RUN_LOCK = threading.Lock()
N_THUMBS = 10          # frames per performance


def _thumb_shots(t_in, t_out, r):
    """Pick N_THUMBS (concert_time, camera, source_time) shots across a
    performance. 5D 2 wins wherever it has live coverage of the moment;
    otherwise the 3 stationary cameras rotate so the sheet spans angles."""
    live = r.load_live_clips()

    def live_at(t):
        for c in live:
            if c["ref_in"] <= t < c["ref_out"]:
                return c
        return None

    span = t_out - t_in
    # Every non-live camera is a valid angle (including "back", which doubles as
    # the audio source). Only the roving 5D 2 is handled via live coverage.
    stationary = [c["id"] for c in CLIPS if not c.get("live", False)] or ["back"]
    # Sample at the mid-point of N equal slices (avoids the exact in/out frames,
    # which are often a cut or black).
    shots = []
    rot = 0
    for k in range(N_THUMBS):
        t = t_in + span * (k + 0.5) / N_THUMBS
        cov = live_at(t)
        if cov is not None:
            cam, src = "5d2", t - cov["delta"]   # src = ref - delta
        else:
            cam = stationary[rot % len(stationary)]
            rot += 1
            src = t
        shots.append((round(t, 3), cam, round(max(0.0, src), 3)))
    return shots


def _extract_thumb(r, cam, src_t, dst):
    """One JPEG frame at src_t from camera `cam`, at the render's output
    resolution (render.W x render.H, 1080p) and graded + letterboxed exactly
    like the export so a thumbnail matches the final frame. Fast seek (-ss
    before -i) + a single frame."""
    eq = r.eq_filter(r.CAMERA_GRADES.get(cam))
    # Same scale/pad framing render.py uses (W/H, keep aspect, centre-pad), with
    # the per-camera grade prepended. Drop the video-only fps/setpts bits.
    scale_pad = (f"scale={r.W}:{r.H}:force_original_aspect_ratio=decrease,"
                 f"pad={r.W}:{r.H}:(ow-iw)/2:(oh-ih)/2")
    vf = f"{eq},{scale_pad}" if eq else scale_pad
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-ss", f"{src_t:.3f}", "-i", r.src_path(cam),
         "-frames:v", "1", "-vf", vf, "-q:v", "2", dst],
        check=True)


def _thumbs_worker(index, perf):
    info = _THUMBS[index]
    try:
        with _THUMBS_RUN_LOCK:                  # serialize; ffmpeg is disk-heavy
            info["status"] = "running"
            r = _render_mod()
            r.apply_camera_grades(_camera_grades())   # honor the saved grade
            out_dir = os.path.join(THUMBS_DIR, f"{index:02d}")
            os.makedirs(out_dir, exist_ok=True)
            # Clear any stale frames from a previous run of this performance.
            for old in os.listdir(out_dir):
                if old.endswith(".jpg"):
                    os.remove(os.path.join(out_dir, old))
            shots = _thumb_shots(perf["in"], perf["out"], r)
            thumbs = []
            for k, (t, cam, src) in enumerate(shots):
                name = f"{k:02d}.jpg"
                try:
                    _extract_thumb(r, cam, src, os.path.join(out_dir, name))
                except subprocess.CalledProcessError:
                    continue                    # skip a frame ffmpeg couldn't grab
                thumbs.append({
                    "url": f"/thumbs/{index:02d}/{name}",
                    "t": t, "camera": cam,
                    "camera_label": _CAM_LABEL.get(cam, cam),
                })
                info["done"] = len(thumbs)
            info["thumbs"] = thumbs
            info["status"] = "done" if thumbs else "error"
            if not thumbs:
                info["error"] = "no frames could be extracted"
            else:
                # Sidecar so existing thumbs (with captions) survive a restart
                # and can be shown without regenerating.
                with open(os.path.join(out_dir, "meta.json"), "w") as f:
                    json.dump({"thumbs": thumbs}, f)
    except Exception as e:
        info["status"] = "error"
        info["error"] = str(e)


def thumbs_on_disk(index):
    """The thumbnails already generated for a performance (1-based), read from
    the meta.json sidecar; falls back to scanning *.jpg if it's missing. Empty
    list if none exist. Lets the editor show existing frames after a restart
    without re-running ffmpeg."""
    out_dir = os.path.join(THUMBS_DIR, f"{index:02d}")
    if not os.path.isdir(out_dir):
        return []
    meta = os.path.join(out_dir, "meta.json")
    if os.path.isfile(meta):
        try:
            with open(meta) as f:
                thumbs = json.load(f).get("thumbs", [])
            # Keep only entries whose jpg is actually present.
            return [t for t in thumbs
                    if os.path.isfile(os.path.join(out_dir, os.path.basename(t["url"])))]
        except Exception:
            pass
    # No/broken sidecar: reconstruct a bare list from the jpg files.
    jpgs = sorted(fn for fn in os.listdir(out_dir) if fn.endswith(".jpg"))
    return [{"url": f"/thumbs/{index:02d}/{fn}", "t": None,
             "camera": None, "camera_label": ""} for fn in jpgs]


def start_thumbs(index):
    """Kick a background thumbnail job for a performance (1-based). Returns
    (started, error): started=False if one is already queued/running."""
    try:
        with db_connect() as conn:
            perfs = _load(conn)["performances"]
    except Exception as e:
        return False, str(e)
    if not (1 <= index <= len(perfs)):
        return False, "no such performance"
    perf = perfs[index - 1]
    with _THUMBS_LOCK:
        cur = _THUMBS.get(index)
        if cur and cur["status"] in ("queued", "running"):
            return False, None
        _THUMBS[index] = {"status": "queued", "done": 0, "total": N_THUMBS,
                          "thumbs": [], "error": None}
    threading.Thread(target=_thumbs_worker, args=(index, perf), daemon=True).start()
    return True, None


def thumbs_status(index):
    """Snapshot of one performance's thumbnail state. If a job for this
    performance ran this session, report it; otherwise fall back to whatever is
    already on disk (status 'done' with those frames, or 'idle' if none) so a
    fresh page/server still shows existing thumbnails."""
    with _THUMBS_LOCK:
        info = _THUMBS.get(index)
        if info and info["status"] in ("queued", "running"):
            return {"status": info["status"], "done": info.get("done", 0),
                    "total": info.get("total", N_THUMBS),
                    "thumbs": info.get("thumbs", []), "error": info.get("error")}
        if info and info["status"] == "error":
            return {"status": "error", "done": info.get("done", 0),
                    "total": info.get("total", N_THUMBS),
                    "thumbs": info.get("thumbs", []), "error": info.get("error")}
    # No active/errored job -> reflect the disk (this covers 'done this session'
    # AND thumbnails from a previous session/server).
    disk = thumbs_on_disk(index)
    return {"status": "done" if disk else "idle",
            "done": len(disk), "total": N_THUMBS, "thumbs": disk, "error": None}


def output_file_map():
    """Map 1-based performance index -> existing output filename, so the editor
    can show an 'open' affordance for any performance already rendered on disk
    (not just ones exported this session). Uses render.py's exact naming."""
    files = {}
    try:
        with db_connect() as conn:
            perfs = _load(conn)["performances"]
    except Exception:
        perfs = []
    for i, p in enumerate(perfs):
        name = f"{i+1:02d}_{slugify(p.get('title'))}.mp4"
        if os.path.isfile(os.path.join(OUT_DIR, name)):
            files[i + 1] = name
    return files


def render_plans():
    """The cut plans the renderer wrote (output/*.plan.json), trimmed to what the
    editor needs to show which camera is on screen at any moment: each plan's
    global [in, out] window plus its segments (clip-local start/end + camera).
    Lets the GUI mirror the deterministic edit without re-running audio analysis."""
    plans = []
    if not os.path.isdir(OUT_DIR):
        return plans
    for fn in sorted(os.listdir(OUT_DIR)):
        if not fn.endswith(".plan.json"):
            continue
        try:
            with open(os.path.join(OUT_DIR, fn)) as f:
                p = json.load(f)
            plans.append({
                "in": p.get("in"), "out": p.get("out"),
                "performance": p.get("performance"), "title": p.get("title"),
                "segments": [{"start": s["start"], "end": s["end"], "camera": s["camera"]}
                             for s in p.get("segments", [])],
            })
        except Exception:
            continue          # skip a malformed/partial plan file
    return plans


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
                "progress": info.get("progress", 0),
                "phase": info.get("phase", ""),
                "file": os.path.basename(info["file"]) if info.get("file") else None,
                "error": info.get("error"),
                "line": info.get("line", ""),
            }
        return out


def _persist(conn, data):
    """Write a full project payload into an open connection (project + regions),
    plus the periodic backup. Caller holds _DB_LOCK and the transaction."""
    cg = data.get("camera_grades")
    conn.execute(
        "INSERT INTO project "
        "(id, seed, project, fps, duration, audio_source, title_scale, camera_grades) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "seed=excluded.seed, project=excluded.project, fps=excluded.fps, "
        "duration=excluded.duration, audio_source=excluded.audio_source, "
        "title_scale=excluded.title_scale, camera_grades=excluded.camera_grades",
        (data.get("seed", 42), data.get("project"), data.get("fps"),
         data.get("duration"), data.get("audio_source"),
         data.get("title_scale", 1.0),
         json.dumps(cg) if cg else None),
    )
    _write_performances(conn, data.get("performances", []))
    _write_titles(conn, data.get("titles", []))
    # Snapshot every Nth write, then trim per the retention policy. Done inside
    # the same transaction so a backup never reflects half a save.
    count = _bump_write_count(conn)
    if count % BACKUP_EVERY == 0:
        _create_backup(conn, _load(conn), count, datetime.now())
        _prune_backups(conn)


def _mirror_markers():
    """Write the canonical view back out to markers.json for the render pipeline."""
    mirror = db_load()
    tmp = MARKERS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(mirror, f, indent=2)
    os.replace(tmp, MARKERS_PATH)
    return mirror


def db_save(data):
    """Persist a full project payload, then mirror to markers.json."""
    with _DB_LOCK:
        with db_connect() as conn:
            _persist(conn, data)
        return _mirror_markers()


def db_mutate(fn):
    """Atomic read-modify-write of the whole project under the lock: fn(data)
    mutates the loaded payload in place, then it's persisted + mirrored. Used by
    the additive title API so concurrent agent/editor writes can't clobber."""
    with _DB_LOCK:
        with db_connect() as conn:
            data = _load(conn)
            fn(data)
            _persist(conn, data)
        return _mirror_markers()


def _clean_title(t):
    """Validate + normalize one title from the agent API. Requires numeric in/out
    (out > in); text/subtitle/x/y/scale optional. Raises ValueError on bad input."""
    if not isinstance(t, dict):
        raise ValueError("each title must be a JSON object")
    try:
        tin, tout = float(t["in"]), float(t["out"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("title needs numeric 'in' and 'out' (seconds)")
    if tout <= tin:
        raise ValueError("title 'out' must be greater than 'in'")

    def _opt(key):
        v = t.get(key)
        return None if v is None else float(v)

    clean = {
        "text": str(t.get("text") or ""),
        "subtitle": str(t.get("subtitle") or ""),
        "in": round(tin, 3), "out": round(tout, 3),
        "x": _opt("x"), "y": _opt("y"),
    }
    if t.get("scale") is not None:
        clean["scale"] = float(t["scale"])
    return clean


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

    # ---- auth --------------------------------------------------------
    def _authed(self):
        """True if auth is disabled (no EDITOR_TOKEN) or the request carries the
        token via Authorization: Bearer / X-Auth-Token / ?token=."""
        if not AUTH_TOKEN:
            return True
        presented = None
        h = self.headers.get("Authorization", "")
        if h.startswith("Bearer "):
            presented = h[7:].strip()
        if presented is None:
            presented = self.headers.get("X-Auth-Token", "").strip() or None
        if presented is None:
            q = urllib.parse.urlparse(self.path).query
            presented = urllib.parse.parse_qs(q).get("token", [""])[0] or None
        if presented is None:
            # Cookie 'et' — lets the browser authorize <video>/<img>/media that
            # can't carry an Authorization header. Set by editor/auth.js.
            cookie = self.headers.get("Cookie", "")
            for part in cookie.split(";"):
                k, _, v = part.strip().partition("=")
                if k == "et" and v:
                    presented = v
                    break
        return presented is not None and hmac.compare_digest(presented, AUTH_TOKEN)

    def _require_auth(self):
        """Gate a protected route; sends 401 and returns False when unauthorized."""
        if self._authed():
            return True
        self._send_json({"ok": False, "error": "unauthorized"}, 401)
        return False

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
        # Gate data + media; leave the static shell open so the login can load.
        if (path.startswith("/api/") or path.startswith("/proxies/")
                or path.startswith("/thumbs/")
                or path == "/waveform.u8") and not self._require_auth():
            return
        if path == "/" or path == "":
            return self._serve_file(os.path.join(EDITOR_DIR, "index.html"))
        if path == "/thumb-view":
            # Standalone image page: full-size thumbnail + a "Show in Finder"
            # button. Static shell (like the editor); the <img> and the reveal
            # call authorize via the same cookie the gallery uses.
            return self._serve_thumb_view()
        if path == "/api/meta":
            return self._send_json(self._meta())
        if path == "/api/markers":
            return self._send_json(self._load_markers())
        if path == "/api/titles":
            # Agent-friendly read of just the title cards.
            return self._send_json({"titles": db_load().get("titles", [])})
        if path == "/api/backups":
            return self._send_json({"backups": list_backups()})
        if path == "/api/camera-grades":
            # Per-camera color grade: the render defaults, plus any saved
            # override, plus the labels — everything the Color modal needs.
            return self._send_json({
                "cameras": [{"id": c["id"], "label": c["label"]} for c in CLIPS],
                "keys": list(_GRADE_KEYS),
                "bounds": _GRADE_BOUNDS,
                "defaults": camera_grade_defaults(),
                "grades": _camera_grades(),
            })
        if path == "/api/exports":
            return self._send_json({"exports": exports_status(),
                                    "files": output_file_map()})
        if path == "/api/plans":
            return self._send_json({"plans": render_plans()})
        if path == "/api/thumbnails":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                index = int(q.get("index", [""])[0])
            except (ValueError, TypeError):
                return self._send_json({"ok": False, "error": "index (int) required"}, 400)
            return self._send_json(thumbs_status(index))
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
        if path.startswith("/thumbs/"):
            # /thumbs/NN/KK.jpg — a generated performance thumbnail. Restrict to
            # THUMBS_DIR (no path traversal) and only serve real image files.
            rel = os.path.normpath(path[len("/thumbs/"):]).lstrip("/")
            target = os.path.normpath(os.path.join(THUMBS_DIR, rel))
            if not target.startswith(THUMBS_DIR + os.sep):
                return self.send_error(404, "Not found")
            return self._serve_file(target)
        if path.startswith("/editor/"):
            name = os.path.basename(path)
            return self._serve_file(os.path.join(EDITOR_DIR, name))
        # bare static files from editor dir (app.js, styles.css)
        name = os.path.basename(path)
        candidate = os.path.join(EDITOR_DIR, name)
        if os.path.isfile(candidate):
            return self._serve_file(candidate)
        self.send_error(404, "Not found")

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not self._require_auth():          # all POSTs mutate -> always gated
            return
        if path == "/api/markers":
            try:
                data = self._read_json()
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 400)
            try:
                db_save(data)
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 500)
            return self._send_json({"ok": True, "saved": DB_PATH})
        if path == "/api/live-clips":
            # Persist trimmed/split 5D 2 coverage back to editor/sync.json so the
            # renderer (which reads that file) honors the new live-camera windows.
            try:
                body = self._read_json()
                clips = body["clips"] if isinstance(body, dict) else body
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 400)
            try:
                saved = save_live_clips(clips)
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 500)
            return self._send_json({"ok": True, "clips": saved, "saved": SYNC_JSON})
        if path == "/api/titles":
            # Additive title API for the external agent: append one title (a bare
            # object) or many ({"titles":[...]}), without touching anything else.
            try:
                body = self._read_json()
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 400)
            incoming = body.get("titles") if isinstance(body, dict) and "titles" in body else [body]
            try:
                clean = [_clean_title(t) for t in incoming]
            except ValueError as e:
                return self._send_json({"ok": False, "error": str(e)}, 400)
            box = {}
            def _add(data):
                data["titles"] = (data.get("titles") or []) + clean
                data["titles"].sort(key=lambda t: t.get("in", 0))
                box["titles"] = data["titles"]
            db_mutate(_add)
            return self._send_json({"ok": True, "added": len(clean), "titles": box["titles"]})
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
        if path == "/api/camera-grades":
            # Save the per-camera color grade (project-wide). Body: {"grades":
            # {cam:{knob:val}}} — validated/clamped, then persisted + mirrored to
            # markers.json so the render and thumbnails pick it up.
            try:
                body = self._read_json()
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 400)
            grades = clean_camera_grades(body.get("grades") if isinstance(body, dict) else None)
            box = {}
            def _set(data):
                if grades:
                    data["camera_grades"] = grades
                else:
                    data.pop("camera_grades", None)   # empty => back to defaults
                box["grades"] = grades
            db_mutate(_set)
            return self._send_json({"ok": True, "grades": box["grades"]})
        if path == "/api/restart":
            # Restart the server by spawning a fully-detached replacement, then
            # exiting this process. Refuse (unless ?force) while an export is
            # running, since the render is our child and would be killed with us.
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            force = q.get("force", ["0"])[0] in ("1", "true", "yes")
            with _EXPORTS_LOCK:
                busy = [i for i, j in _EXPORTS.items()
                        if j.get("status") in ("queued", "running")]
            if busy and not force:
                return self._send_json(
                    {"ok": False, "busy": busy,
                     "error": f"export(s) {busy} running — restarting would kill "
                              f"the render. Pass force=1 to restart anyway."}, 409)
            self._send_json({"ok": True, "restarting": True})
            _do_restart()      # shuts down + spawns replacement after we respond
            return
        if path == "/api/export":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                index = int(json.loads(raw.decode("utf-8")).get("index"))
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 400)
            started = start_export(index)
            return self._send_json({"ok": True, "index": index, "started": started})
        if path == "/api/thumbnails":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                index = int(json.loads(raw.decode("utf-8")).get("index"))
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 400)
            started, err = start_thumbs(index)
            if err:
                return self._send_json({"ok": False, "error": err}, 400)
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
        if path == "/api/reveal":
            # Reveal a generated thumbnail in Finder (open -R). Takes the same
            # "/thumbs/NN/KK.jpg" url the gallery serves; restricted to THUMBS_DIR
            # with the same traversal guard as the file route.
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                url = str(json.loads(raw.decode("utf-8")).get("url", ""))
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 400)
            if not url.startswith("/thumbs/"):
                return self._send_json({"ok": False, "error": "not a thumbnail"}, 400)
            rel = os.path.normpath(urllib.parse.unquote(url[len("/thumbs/"):])).lstrip("/")
            target = os.path.normpath(os.path.join(THUMBS_DIR, rel))
            if not target.startswith(THUMBS_DIR + os.sep) or not os.path.isfile(target):
                return self._send_json({"ok": False, "error": "no such file"}, 404)
            try:
                subprocess.Popen(["open", "-R", target])
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 500)
            return self._send_json({"ok": True})
        self.send_error(404, "Not found")

    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        if not self._require_auth():
            return
        if path == "/api/titles":
            # Remove one title by 0-based index (?index=N) from the sorted list.
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                idx = int(q.get("index", [""])[0])
            except (ValueError, TypeError):
                return self._send_json({"ok": False, "error": "index (int) required"}, 400)
            box = {"ok": False}
            def _del(data):
                titles = data.get("titles") or []
                if 0 <= idx < len(titles):
                    titles.pop(idx)
                    data["titles"] = titles
                    box["ok"] = True
                box["titles"] = data.get("titles", [])
            db_mutate(_del)
            if not box["ok"]:
                return self._send_json({"ok": False, "error": "index out of range"}, 404)
            return self._send_json({"ok": True, "titles": box["titles"]})
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

    def _serve_thumb_view(self):
        """Standalone page for one thumbnail: the image full-size plus a
        'Show in Finder' button (which POSTs the url to /api/reveal)."""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        url = (q.get("url", [""])[0] or "").strip()
        # Only allow our own thumbnail urls; anything else -> a plain message.
        safe = url.startswith("/thumbs/") and not (".." in url)
        img_url = url if safe else ""
        name = url.rsplit("/", 1)[-1] if safe else ""
        # Escape for safe embedding in HTML/JS string contexts.
        u_html = html.escape(img_url, quote=True)
        # Valid JS string literal; also break any "</script>" so it can't close
        # the inline <script> block early (json.dumps escapes quotes, not "</").
        u_js = json.dumps(img_url).replace("</", "<\\/")
        n_html = html.escape(name, quote=True)
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{n_html or 'Thumbnail'}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin: 0; background: #0b0d12; color: #e6e8ee;
    font: 13px/1.5 -apple-system, system-ui, sans-serif;
    display: flex; flex-direction: column; min-height: 100vh; }}
  header {{ display: flex; align-items: center; gap: 12px; padding: 10px 14px;
    border-bottom: 1px solid #262b36; background: #12151c; }}
  header .name {{ font-weight: 600; }}
  header .spacer {{ flex: 1; }}
  button {{ font: inherit; color: #e6e8ee; background: #1b1f28;
    border: 1px solid #3a4150; border-radius: 7px; padding: 6px 12px; cursor: pointer; }}
  button:hover {{ border-color: #60a5fa; }}
  #status {{ color: #8b93a3; font-size: 12px; }}
  main {{ flex: 1; display: flex; align-items: center; justify-content: center; padding: 16px; }}
  img {{ max-width: 100%; max-height: 88vh; border: 1px solid #262b36; border-radius: 8px; }}
  .empty {{ color: #8b93a3; padding: 40px; text-align: center; }}
</style></head>
<body>
<header>
  <span class="name">{n_html or 'Thumbnail'}</span>
  <span id="status"></span>
  <span class="spacer"></span>
  <button id="reveal"{'' if safe else ' disabled'}>📂 Show in Finder</button>
</header>
<main>
  {f'<img src="{u_html}" alt="{n_html}" />' if safe else '<div class="empty">No thumbnail specified.</div>'}
</main>
<script>
const URL_ = {u_js};
const st = document.getElementById('status');
const btn = document.getElementById('reveal');
if (btn) btn.onclick = async () => {{
  try {{
    const r = await fetch('/api/reveal', {{ method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ url: URL_ }}) }}).then(r => r.json());
    st.textContent = r.ok ? '✓ revealed in Finder' : ('⚠ ' + (r.error || 'failed'));
  }} catch (e) {{ st.textContent = '⚠ ' + e; }}
}};
</script>
</body></html>"""
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


# ---- self-detach + restart -------------------------------------------
# Running the editor from a shell (nohup ... &) leaves it in that shell's process
# group, so when the terminal closes / the job loses the foreground, macOS
# suspends it with a terminal-stop signal (SIGTTIN/SIGTTOU) -> the server freezes
# (state "T"), stops answering, and the UI hangs (export stuck on "PREP"). nohup
# does NOT block those signals. Detaching into our own session does. We do it once
# at startup so a normally-launched server can never be frozen this way.
#
# Guard env var so the re-exec'd child (already a session leader) doesn't loop.
_DETACHED_ENV = "EDITOR_DETACHED"


def detach_session():
    """Re-exec into a new session (os.setsid) unless we're already a session
    leader or detaching was disabled. No-op on platforms without setsid."""
    if os.environ.get(_DETACHED_ENV) == "1":
        return
    if not hasattr(os, "setsid"):
        return
    try:
        if os.getpid() == os.getsid(0):     # already our own session leader
            os.environ[_DETACHED_ENV] = "1"
            return
    except OSError:
        pass
    # Fork so the child can setsid (a process group leader can't). The parent
    # exits immediately; the child becomes the real, detached server.
    try:
        if os.fork() > 0:
            os._exit(0)                     # parent leaves
    except OSError:
        return                              # fork failed -> run un-detached
    os.setsid()
    os.environ[_DETACHED_ENV] = "1"
    # Re-exec a fresh interpreter so we start clean in the new session.
    os.execv(sys.executable, [sys.executable] + sys.argv)


def spawn_replacement():
    """Start a brand-new, fully-detached server process (same interpreter,
    script, and port). Used by /api/restart: the new process binds the port
    once we release it. Returns the child pid."""
    env = dict(os.environ)
    env[_DETACHED_ENV] = "1"                # start_new_session already detaches;
                                            # skip the in-process fork/setsid path
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__)],
        cwd=ROOT, env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)            # its own session -> never frozen
    return proc.pid


def _do_restart():
    """Spawn a detached replacement, then hard-exit so the OS frees the port
    instantly (which lets the replacement's bind succeed). Run in a short-lived
    thread so the triggering request can return first.

    Note: we deliberately do NOT call httpd.shutdown()/server_close() — calling
    shutdown() from a thread started by a request handler self-deadlocks (it
    waits for serve_forever to stop, which waits for this handler to finish).
    os._exit() frees the socket immediately anyway."""
    def worker():
        time.sleep(0.4)                     # let the /api/restart response flush
        try:
            spawn_replacement()             # child retries bind until we exit
        except Exception:
            pass
        os._exit(0)                         # instantly frees the port
    threading.Thread(target=worker, daemon=True).start()


_HTTPD = {}   # holds the live server so /api/restart can shut it down


def main():
    os.chdir(ROOT)
    detach_session()          # never freeze from terminal-stop signals
    db_init()
    # allow_reuse_address must be set before bind so a restart's replacement can
    # rebind the port immediately (no TIME_WAIT stall). It's a class attr.
    ThreadingHTTPServer.allow_reuse_address = True
    # Retry the bind briefly: on a restart the replacement starts before the old
    # process has fully released the port, so give it a couple seconds to free up.
    httpd = None
    for attempt in range(40):               # ~6s max (40 * 0.15s)
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
            break
        except OSError:
            time.sleep(0.15)
    if httpd is None:                       # last try, let the error surface
        httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    _HTTPD["srv"] = httpd
    print(f"Editor running at  http://localhost:{PORT}  (pid {os.getpid()})")
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
