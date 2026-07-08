# Reels Production Interface

A portrait (1080×1920) reels editor that stacks the three stationary cameras
into one vertical frame and lets you cut, frame, grade, and title the show as a
single compound clip. Lives at `/reels.html` (served by `editor/server.py`),
driven by `editor/reels.js`, rendered by `render/style_reels.py`.

The design invariant: **the preview IS the export.** All pane geometry is
computed in true output pixels (the stage is a real 1080×1920 div scaled down
with a CSS transform), and the framing/color/title math mirrors the render
script exactly, so what you see on stage is what burns into the file.

---

## Compound clip & editing

The whole reel is one compound clip — an ordered list of concert-time
`segments` played back-to-back with no gaps. **Every edit hits all three
stacked cameras and the audio together**, and the reel ripples closed.

- **Split at playhead** (`S` / ✂ button) — cuts the compound clip in two at the
  playhead across all cameras + audio. Guards against splitting too close to an
  existing cut.
- **Trim edges** — drag a clip's left/right handle on the timeline. Trims are
  in concert time and snap to markers (Shift disables).
- **Delete segment** (`⌫` / ✕ button) — removes the selected segment; the rest
  of the reel closes up. Disabled when only one segment remains.
- **Move / reorder clips** — drag a clip body on the timeline to any cut
  boundary (shows an insertion caret + a ghost of the clip). Also `[` / `]` to
  nudge the selected clip earlier/later.
- **Duplicate clips** — ⌥-drag a clip body drops a *copy* at the target
  boundary, leaving the original in place (green ghost = duplicate).
- **Shared-cut edge selection** — at an internal cut, whichever side of the line
  the cursor is on decides whether you grab the left clip's OUT or the right
  clip's IN, so both edges are always reachable.
- **Play from clip start** — pressing play with a segment selected starts from
  that segment's beginning (unless the playhead is already inside it).

## Camera framing (per camera)

Each camera gets its own zoom + X/Y pan within its pane, using the exact
cover-fit + pan-crop formula the renderer uses.

- **Drag a pane** on the preview to reposition · **wheel / pinch** to zoom ·
  **double-click** to reset that pane.
- **Sliders** for zoom / X / Y in the Camera framing card. X/Y ranges track the
  reachable pan at the current zoom (zoom in to unlock more travel; sliders
  disable when there's no travel).
- **Reorder the camera stack** — ▲ / ▼ buttons per row move a camera up/down in
  the vertical stack (top → middle → bottom) without reloading video.
- **Reset** per-camera (↺) or **Reset all**.

## Camera color grades (shared with the main editor)

The **same** per-camera `{brightness, gamma, contrast, saturation}` grade the
marker editor uses — one document (`/api/camera-grades`) shared by both the reel
export and the main highlights render.

- Live pane preview replicates ffmpeg's `eq` via an SVG LUT filter on the
  limited-range luma, so the preview matches the export.
- Per-camera sliders, per-camera reset (↺), reset all.
- Explicit **Save color** (grades are project-wide); sends all cameras
  (including ones not shown here, e.g. the 5D 2) so nothing is dropped.

## Titles (text overlays burned into the reel)

Titles are **reel-time** `{text, subtitle, in, out, x, y, scale, wrap}` — a
title says "show this from reel-time in..out," fully independent of the clips
beneath it. This is deliberate: duplicating/cutting/reordering a clip never
duplicates or moves a title on its own.

- **Create** (`T` / `⌘T` / ＋ button) at the playhead; new titles inherit the
  saved default styling.
- **Text + subtitle**, multi-line (Enter = line break), with a per-title
  **wrap** toggle (auto-wrap long lines vs. keep exact breaks).
- **Position** by dragging the block on the preview — FCP-style center snapping
  (hold Shift for free placement). ⌖ recenters.
- **Timing** by dragging the title bar's edges on the timeline's **purple lane**
  (retimes in reel seconds); drag the body to slide it; ⌥-drag the body to
  duplicate.
- **Font size** — per-title `A− / A+`, plus a global **Font** scale in the card
  header that scales all titles.
- **Save as default** (☆) — stores this title's position/size/wrap (not
  text/timing) in localStorage so the next Create title inherits that look.
- **Ripple with clip edits** — titles are anchored to the footage under their
  start, so trimming/deleting/moving/duplicating clips re-derives title
  positions correctly, including titles *inside* a trimmed clip (a left-edge
  trim slides them with the footage; deleted-footage titles re-anchor to the
  replacement clip's start).
- **Emoji + WYSIWYG render** — all titles are rendered to PNG overlays via
  Pillow at export (ffmpeg drawtext can't do color emoji), so the burned-in
  titles match the browser preview exactly.

## Timeline

Canvas timeline with a cached static layer (ruler, blocks, waveform, markers,
titles, selection) blitted per-frame with only the playhead/cursor drawn live —
affordable at 60fps even with a 1px-resolution waveform.

- **Waveform** per block (mirrored, two-tone: dim = peak, bright = mean).
- **Markers** — drop at playhead (`M` / ⚑), shown as amber flags; click a flag
  to select (park playhead on it), `⌫` deletes. Scrubbing and trims **snap** to
  markers and cut boundaries (Shift disables).
- **Purple title lane** across the top for title bars.
- **Hover cursor** shows where a click would land with output + concert
  timecodes (turns amber when snapped).
- **Zoom / pan** — buttons, `+ / −`, `⌘+ / ⌘−` (anchored at playhead), `0` / `⌘0`
  fit, wheel to zoom, trackpad-X to pan.

## Reel projects

Each project is a complete independent cut (segments, framing, markers, titles).

- **Switch** via the Reel dropdown · **New** (starts from the entire show as one
  compound clip) · **Rename** (✎ or double-click; also renames the export file)
  · **Delete** (removes the cut list only — footage untouched).
- Names are required (re-prompts on blank) and become the export filename
  (`reel_<slug>.mp4`).

## Undo / redo / history

- **Undo** (`⌘Z`) / **Redo** (`⇧⌘Z`, also `Ctrl+Y`), buttons labeled with the
  action name. Linear history (new edit clears redo).
- Continuous gestures (drags, wheel, slider sweeps) collapse into one undo step.
- **History panel** (🕘) lists every step newest-first; click any step to jump
  directly there (undone steps shown struck-through above the current line).
- Cap: 200 steps.

## Persistence (durable)

Stored in SQLite (`markers.db`) with backup tables; `reels.json` is a mirror the
server keeps in sync (never deleted).

- **Save** button + `⌘S` (window capture-phase handler so it beats browser
  Save-Page).
- **Autosave** — debounced 300ms after any edit.
- **Unload flush** — `sendBeacon` (with sync-XHR fallback) on beforeunload /
  pagehide / tab-hidden, so a cut made a fraction of a second before a refresh
  isn't lost.
- Save button reflects state: dirty • / Saving… / ✓ Saved / ⚠ error.

## Export

- **Export reel** (🎬) renders 1080×1920, all segments, mastered audio bed.
- Split-button menu: **Export (replace)** overwrites this reel's file ·
  **Export as new** writes a numbered file (`reel_<slug>_2`, `_3`, …) keeping
  the previous one.
- **Progress bar** below the topbar tracks every phase, including the previously
  silent finishing phase: preparing → cutting clips % → rendering titles %
  (real % from Pillow renders) → burning titles / mux (indeterminate slide) →
  done. Polled at 700ms.
- **▶ Open** and **📁 Finder** appear when a render finishes (reveal-in-Finder
  via `open -R`).

## Collapsible / reorderable panels

Every side card (Reel, Camera framing, Camera color, Titles) is collapsible
(click header or ▾) and drag-reorderable by its ⋮⋮ grip. Collapse state and
order persist per-device in localStorage.

## Keyboard reference

| Key | Action |
|-----|--------|
| Space | Play / pause |
| ← → | Frame back / forward |
| Shift+← → | ±5s |
| `S` | Split at playhead |
| `M` | Add marker |
| `T` / `⌘T` | Create title |
| `[` `]` | Move selected clip earlier / later |
| `⌫` | Delete selection (marker / title / segment) |
| `Esc` | Deselect |
| `⌘Z` / `⇧⌘Z` | Undo / redo |
| `⌘S` | Save |
| `+` `−` / `0` | Zoom / fit |
| `⌘+` `⌘−` / `⌘0` | Zoom at playhead / fit |
| ⌥-drag clip | Duplicate clip |
| ⌥-drag title | Duplicate title |
| Shift while dragging | Disable snapping |
