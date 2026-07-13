# Architecture — personal multi-style video editor

A local-first video editor built for collaboration between a human and AI
coding agents. One Python backend owns all data and rendering; two frontends
(classic `/` and v2 `/v2`) drive it; **styles** turn the same marked-up concert
into different videos.

```
project.json          which footage/cameras this project uses (swap per concert)
markers.db            source of truth (SQLite) ── mirrored to ──> markers.json
editor/server.py      the only server: API + media streaming + render jobs
editor/  (v1)         classic vanilla-JS editor at /   (timeline, marking, color)
ui/      (v2)         Vite + Svelte SPA at /v2         (panels migrate here)
render/render.py      style: "highlights"  — landscape per-performance export
render/style_*.py     more styles (applause_ranker, …)
```

## Concepts

### Project (fresh concert = new project.json)
`project.json` declares the footage: `cameras` (id/label/source/proxy/
`is_audio`/`live`), `fps`, `duration`, `audio_bed`, `sync_json`. `server.py`
(CLIPS) and `render.py` (SOURCES/AUDIO_BED/FPS) both read it. To reuse the
editor for a new concert: new folder of footage, new `project.json`, regenerate
proxies (`render/make_proxies.sh`) + waveform (`render/make_waveform.py`), and
mark away. Nothing in the code references Belgium-specific paths (the old
values remain only as boot fallbacks).

### Markers (the data every style shares)
Stored in `markers.db`, mirrored to `markers.json` (what render scripts read):
- `performances` — the pieces: `{title, composer, in, out, camera_weights?, kenburns?, camera_overrides?}`
  (`camera_weights` = optional `{camera_id: relative weight}` steering the
  auto-cut's screen-time mix for that piece; weights are compensated for the
  no-back-to-back-repeat rule so the actual share matches the numbers; 0 =
  never use that camera; ≤2 cameras or >50% targets degrade to what the
  no-repeat rule permits — see `render/plan.py`; `kenburns` = optional list of
  camera ids whose cuts get a mild alternating zoom in/out, render-only —
  see `kb_vf_for` in `render/render.py`; `camera_overrides` = optional list of
  `{start, end, camera?, kb?}` (concert seconds) manual per-cut picks made in
  the v1 editor's CAM timeline lane after seeing the seeded plan — `camera`
  swaps the angle, `kb` `"in"|"out"|"none"` forces/kills that cut's Ken Burns
  move regardless of the `kenburns` list — applied as a final pass over the
  seeded assignment, matched by segment midpoint, never touching forced 5D 2
  coverage — see `build_segments` in `render/plan.py`)
- `titles` — burned-in text overlays
- `regions` — **generic style regions**: `{kind, perf, in, out, rank, cam}`
  - `kind: "applause"` — crowd reaction for performance `perf`; `rank` 1–10
  - `kind: "highlight"` — the 3–5s showcase moment of `perf`
  - new styles add new kinds — **no schema change needed**
- `camera_grades` — per-camera color (brightness/gamma/contrast/saturation)

Saves use **preserve-if-absent**: a client that posts a payload without a key
(e.g. the v1 editor doesn't know about a future key) never wipes that data.

### Styles (one dataset → many videos)
A style = a render recipe. Registry: `STYLES` in `editor/server.py`.

| id | script | output |
|---|---|---|
| `highlights` | `render/render.py --only N` | landscape 1080p per performance |
| `applause_ranker` | `render/style_applause_ranker.py` | portrait 1080×1920 Short: 3–5s of each piece + its applause + big "7/10" rank, rank-ascending |

**Adding a style** (agent checklist):
1. Write `render/style_<name>.py`. Read `markers.json`; reuse helpers from
   `render.py` (`import render as R`): `R.src_path(cam)`, `R.CAMERA_GRADES` +
   `R.eq_filter` (color), `R.encoder_args/resolve_encoder`, `R.AUDIO_BED`,
   `R.TITLE_FONT`, `R.run`. Print `cut seg X/Y` while cutting and `✓ <file>.mp4`
   when done — that stdout protocol is what gives you a progress bar for free.
2. Register it in `STYLES` (label, script, `output`, `needs`).
3. If it needs new marked data, use a new region `kind` via `/api/regions`
   (no schema change) and, optionally, add marking affordances to the UI.
4. It appears automatically in the v2 Styles page (`/v2`) with export +
   progress + Open; `POST /api/export {"style": "<id>"}` runs it.

### The two frontends (strangler migration)
- **v1 (`editor/`, served at `/`)** — the full classic editor: canvas timeline,
  waveform, performance/title/5D2 marking, color grading with exact ffmpeg
  preview, thumbnails, exports. Still the tool for *marking*.
- **v2 (`ui/`, served at `/v2`)** — Svelte 5 + Vite SPA. Panels migrate here
  one at a time; new panel-style features should be built here first. Current
  panels: **Styles** (readiness table, rank editing, style exports).
  - Dev: `cd ui && npm run dev` → http://localhost:5173 (proxies to :8000).
  - Ship: `cd ui && npm run build` (ui/dist is committed; the Python server
    serves it, so remote boxes need no node).
  - All server calls go through `ui/src/lib/api.js` — the whole API surface in
    one greppable file. Don't fetch() from components.
- Migration order suggestion: Styles ✓ → Performances list → Color panel →
  Titles → timeline last (it's the hardest; keep v1 until v2's timeline is
  genuinely better).

## API quick reference
```
GET  /api/markers            everything (performances, titles, regions, grades)
POST /api/markers            full save (preserve-if-absent for missing keys)
GET/POST/DELETE /api/regions additive region CRUD (+ POST /api/regions/update)
GET/POST /api/titles         additive title CRUD
GET  /api/styles             style registry + job progress + outputs on disk
POST /api/export             {"index": N} highlights | {"style": id} any style
GET  /api/camera-grades      color grade spec; POST saves
GET  /api/grade-frame        exact ffmpeg-graded still (WYSIWYG preview)
GET  /api/thumbnails?index=N contact sheet; POST generates
POST /api/restart            spawn detached replacement server (409 if render busy)
```

## Operational notes (hard-won — see POSTMORTEM-2026-07-04.md)
- Start/restart the server with `editor/restart-server.sh` or the ⟳ button.
  It self-detaches (`os.setsid`) — a bare `nohup … &` used to freeze the whole
  process tree (state `T`, exports stuck on "PREP").
- Renders are **children of the server**; restarting kills them (the API warns).
- Color previews: playback panes use a limited-range-corrected LUT filter;
  the paused frame and thumbnails are *actual ffmpeg output* — that is the
  ground truth, never "improve" it back to a pure CSS approximation.
- Verify color/video questions with `ffprobe signalstats` on files, never with
  headless-browser screenshots.
