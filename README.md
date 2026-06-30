# Belgium Concert — Performance Marker & Multi-Cam Render Pipeline

Mark each performance across 3 synced camera angles in a browser editor, then
auto-generate a multi-cam performance video per song — cutting between the 3
angles every 3–5 seconds, with **some cuts snapped to musical transitions**,
fully reproducible from a seed.

```
Main Footage/           # original 3 camera files (1280x720, 60fps, 96 min)
  Back Camera.mov        ← audio bed for the final renders
  Livestream Footage.mov
  camera next to piano.mov
proxies/                # small 640x360 proxies for the editor (auto-generated)
editor/                 # the marking GUI + local server
render/                 # the deterministic render pipeline
markers.json            # your marked performances (written by the GUI)
output/                 # rendered performance videos + per-song plan JSON
cache/                  # proxy log, audio-transition cache, temp segments
```

---

## 1. One-time setup — build the editor proxies

The originals are 7 GB each; the editor plays small proxies instead so 3-up
playback stays smooth. This runs once (~40 min total, 7× realtime):

```bash
bash render/make_proxies.sh          # writes proxies/back.mp4, livestream.mp4, piano.mp4
tail -f cache/proxy_build.log        # watch progress
```

Already running in the background from setup — check the log; when it prints
`ALL PROXIES DONE` you're set. The editor works as each proxy finishes (reload
the page to pick up newly-finished angles).

The timeline waveform is precomputed once from the Back Camera audio:

```bash
python3 render/make_waveform.py      # writes cache/waveform.u8 (~0.6 MB)
```

(Also already done during setup.)

## 2. Mark the performances

```bash
python3 editor/server.py             # serves http://localhost:8000
```

Open **http://localhost:8000**. You'll see the 3 angles stacked and synced.

- **Space** play/pause · **← / →** step one frame · **Shift+← / →** jump ±5s
- The timeline shows the **audio waveform** so you can see exactly where pieces
  start/end (silence/applause gaps between performances are obvious).
- **Zoom:** mouse-wheel over the waveform (zooms at the cursor), or **+ / −**
  keys / buttons; **0** or **Fit** returns to the whole concert.
- **Navigate:** drag the slim **minimap** strip below the waveform to pan; when
  playing, the view auto-scrolls to follow the playhead.
- Scrub by dragging on the waveform; click a performance block to select it.
- **I** = mark In, **O** = mark Out at the playhead (orange range previews).
- Fill in **Title** + **Composer**, click **Save performance** (or **Enter** to
  jump to the title field). It appears as a block on the timeline and in the list.
- Click any block/list item to **edit** it; **P** previews the selected one.
- Set the **Seed** (controls the random-but-reproducible cut pattern).
- Click **Save markers** (top right) to write `markers.json`.

### Titles (on-screen text overlays)

Add text that burns into the final render — a song title card, a lower-third,
whatever — and drag its handles to set exactly how long it shows, like a video
editor:

- Click **+ Create title** (or press **T**) to drop a title at the playhead. It
  appears as a **purple block in the title lane** at the top of the timeline.
- **Drag the title's left/right handles** to set its start/end (= how long the
  text is on screen); **drag the body of the block to slide the whole title**
  to a different point in the timeline.
- A **live preview** of the title is overlaid on the video as a lower-third
  whenever the playhead is inside its window — scrub through it to see exactly
  how the burned-in render will look (text, position, fade).
- Edit the **title text** and an optional **subtitle** (e.g. composer) in the
  **Titles** card on the right; changes save automatically and update the
  preview live.
- Titles are stored in `markers.json` under `"titles"` and rendered as a
  centered lower-third (white text, soft outline, 0.4 s fade in/out). A title
  is drawn over whichever performance render(s) its window falls in.

The `Audio:` dropdown only changes which angle you *hear while marking* — the
final render's audio bed is set separately (default: Back Camera).

## 3. Render the performance videos

```bash
python3 render/render.py                 # render every performance
python3 render/render.py --dry-run       # print the cut plan only, no encoding
python3 render/render.py --only 1,3      # just performances #1 and #3 (1-based)
python3 render/render.py --audio livestream   # use a different audio bed
python3 render/render.py --encoder x264  # bit-exact CPU encode (slower)
```

Each performance →
- `output/NN_title.mp4` — the finished multi-cam video (1920×1080, 60fps), with
  any overlapping **titles burned in** as lower-thirds
- `output/NN_title.plan.json` — the exact cut list (every segment, camera, and
  whether its cut was `audio`-snapped or `heuristic`) plus the `titles` overlaid
  on this performance (with clip-local start/end times)

Performances with no titles over them are stream-copied (fast); a performance
that has titles is re-encoded once to burn them in.

---

## How the cutting works

For each performance window `[in, out]`:

1. **Audio analysis** (`render/audio_cuts.py`) — librosa extracts the window's
   audio (from the Back Camera bed) and finds prominent **onsets** (phrase/chord
   changes) plus the beat grid.
2. **Cut plan** (`render/plan.py`) — walks the window placing cuts every
   **3–5 s** (seeded). Each base cut **snaps to a nearby audio transition** if one
   sits within ±0.75 s, else it stays on the heuristic time. No camera repeats
   back-to-back; no segment shorter than 2 s.
3. **Render** (`render/render.py`) — cuts each segment from the matching
   *original* full-res file, concatenates them with hard cuts, and lays the
   continuous audio bed over the top.

**Determinism:** the same `seed` + `in/out` always produce the same plan, so the
same render. Change the seed (in the GUI) to roll a different cut pattern for the
same markers. The hardware (`videotoolbox`) encoder is visually identical run to
run; use `--encoder x264` if you need bit-exact files.

### Tuning

Edit the constants at the top of `render/plan.py`:

| constant | meaning | default |
|----------|---------|---------|
| `SEG_LO` / `SEG_HI` | cut spacing range (seconds) | 3.0 / 5.0 |
| `SNAP_WINDOW` | how close an audio onset must be to capture a cut | 0.75 s |
| `MIN_LEN` | shortest allowed segment | 2.0 s |
| `CAMERAS` | camera ids / order | back, livestream, piano |
