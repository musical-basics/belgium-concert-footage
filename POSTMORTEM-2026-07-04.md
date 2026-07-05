# Postmortem — editor session, 2026-07-04

Every issue hit while building the thumbnail / camera-color / export features,
why it happened, how it was fixed, and what prevents it next time.

| # | Symptom | Root cause | Fix |
|---|---------|-----------|-----|
| 1 | "SyntaxError: Unexpected token '<'" in thumbnails modal | Stale server process (old code, no route) | Restart server |
| 2 | Thumbnails stuck "generating… 0/10" | Wedged + duplicate server processes | Kill strays, restart; show-existing + Regenerate UX |
| 3 | Export stuck on "PREP" (recurring) | Server/render suspended by terminal-stop signals (`nohup … &`) | Self-detach via `os.setsid` + ⟳ Restart button |
| 4 | Export washed-out vs preview | CSS preview had no real gamma → user tuned to a lying preview | Ground-truth ffmpeg stills when paused |
| 5 | SVG gamma preview still off; couldn't calibrate | Headless-screenshot color management distorts measurements | Stopped approximating; serve real frames |
| 6 | Sliders "did nothing" on paused pane | Still overlay refetched with the *saved* grade, covering live edits | `/api/grade-frame` accepts unsaved knob overrides |
| 7 | Color jumps between play and pause | ffmpeg `eq` grades limited-range luma (16–235); browser filter graded display values | Display-space LUT: `D(d) = (255·f((16+219d)/255) − 16)/219` |
| 8 | Restart endpoint: no replacement came up | Double-detach conflict, then `httpd.shutdown()` self-deadlock | Env guard; spawn-then-`os._exit`; bind retry in child |
| 9 | Killing wedged server killed in-flight render | Render is a child subprocess of the server | 409 busy-warning on restart; know the process tree before killing |

---

## 1. Stale server code → HTML parsed as JSON

**Symptom.** Clicking 🖼 Thumbs showed `SyntaxError: Unexpected token '<',
"<!DOCTYPE"… is not valid JSON`.

**Why.** The server on port 8000 predated the new `/api/thumbnails` route. The
request fell through to the HTML 404 page, and `res.json()` choked on
`<!DOCTYPE`.

**Fix.** Restart the server so it loads the new code.

**Avoid next time.**
- Any change to `editor/server.py` requires a server restart (front-end files
  only need a page reload). Now trivial: the **⟳ Restart** button in the GUI.
- Front-end fetches could check `Content-Type` before `.json()` to fail with a
  clearer message ("server is stale?") instead of a parse error.

## 2. Duplicate / zombie server processes

**Symptom.** Thumbnails stuck at "generating… 0/10"; API not responding while
*some* server process existed.

**Why.** Repeated `nohup … &` launches piled up several processes; the one
holding the port was wedged. The UI polled a dead server forever.

**Fix.** `pkill` all instances, free the port (`lsof -ti :8000 | xargs kill`),
start exactly one. UX hardened too: the modal now shows existing thumbnails
from disk instantly and only generates when none exist (↻ Regenerate to redo),
so a missed poll no longer looks like a hang.

**Avoid next time.**
- One canonical way to (re)start: `editor/restart-server.sh` or the ⟳ button —
  both kill strays and free the port first.
- Long-running jobs should persist state to disk (the thumbnails now write a
  `meta.json` sidecar) so a fresh page/server can recover without re-running.

## 3. Export stuck on "PREP" — the big one (recurred ~4×)

**Symptom.** Export button froze at "⏳ prep"; server stopped answering HTTP
entirely; render/ffmpeg appeared alive but made no progress.

**Why.** Everything was launched with `nohup python3 editor/server.py … &`
from a shell. `nohup` blocks SIGHUP but **not** the terminal-stop signals
(SIGTTIN/SIGTTOU). When the launching terminal closed or the job lost the
foreground, macOS suspended the whole process group — server, render and
ffmpeg all sat in state `T`/`TN` (stopped). A frozen server can't answer
status polls (UI stuck on "prep"), and a frozen render never finishes.
`SIGCONT` couldn't revive them (group-level stop). **This was a launch-method
bug, not an export-code bug** — which is why restarting "fixed" it every time
until the next launch re-introduced it.

**Fix (two layers).**
1. `server.py` now **self-detaches at startup** (`fork` + `os.setsid` +
   re-exec) so it is its own session leader with no controlling terminal —
   immune to those signals *no matter how it's launched*.
2. In-GUI **⟳ Restart** button (`POST /api/restart`): spawns a fully-detached
   replacement, `os._exit()`s to free the port instantly, replacement retries
   the bind; page auto-reloads. Refuses with 409 while an export runs (see #9)
   unless forced.

**Avoid next time.**
- Diagnose a "hung" process with `ps -o stat=` **first**: a `T` state means
  suspended, not deadlocked — no amount of code-fixing helps.
- Daemons must own their session (`setsid`). Bare `nohup … &` is not a daemon.

## 4. Export washed-out while the preview looked fine

**Symptom.** Back Camera tuned to look good in the editor (brightness 0.42,
gamma 2.18) exported nearly white.

**Why.** The pane preview used a CSS filter, and **CSS has no gamma
primitive** — gamma was faked as a flat brightness multiplier. ffmpeg's gamma
is a power curve that lifts darks enormously (a dark frame went 33→216 mean
luma; the CSS fake showed ~2×). The user tuned against a preview that
understated the grade. The export applied the grade exactly once and was
"correct" — the preview lied.

**Fix.** Evolved through #5 into the ground-truth approach: when paused, each
pane overlays a **real ffmpeg-graded frame** from `GET /api/grade-frame`
(same `eq` as the render; verified pixel-identical, YAVG 80.115 = 80.115).

**Avoid next time.**
- For any "preview must match output" feature, render the preview **with the
  same engine as the output** (here: ffmpeg itself). Approximations belong
  only where exactness is impossible (live playback).
- When a user reports "output ≠ preview," measure both (`signalstats` YAVG)
  before assuming the output pipeline double-applies something — here the
  export was innocent.

## 5. Couldn't calibrate the SVG filter via headless screenshots

**Symptom.** SVG-gamma preview measured *over* ffmpeg on synthetic gray but
*under* on real footage — the same filter can't do both; calibration went in
circles.

**Why.** Headless-Chrome screenshots pass through Chrome's own color
management, distorting measurements unpredictably. The oracle was broken, not
(only) the filter.

**Fix.** Abandoned pixel-perfect browser filtering as a goal; the paused
ground-truth still (#4) is exact by construction. (The playback filter was
later made near-exact anyway once the *real* math error surfaced — see #7.)

**Avoid next time.**
- Don't calibrate color through a screenshot pipeline you don't control.
  Compare at the source of truth (ffmpeg output bytes / `ffprobe signalstats`
  on files, not screenshots).
- If two measurements of one deterministic function disagree in direction,
  suspect the measuring instrument.

## 6. Sliders "did nothing" on a paused pane

**Symptom.** Dragging Back-Camera sliders left the pane unchanged.

**Why.** Self-inflicted by #4's overlay: dragging updated the live filter on
the `<video>` underneath, then the debounced still-refresh fetched a frame
using the **last-saved** grade and covered the pane with the old look again.

**Fix.** `/api/grade-frame` accepts optional
`brightness/gamma/contrast/saturation` params (validated + clamped like the
save path); the client passes whatever grade each pane is currently
previewing, saved or not. Drag → approximate filter live → exact frame at the
slider values ~200 ms after settling.

**Avoid next time.**
- When adding an overlay that covers a live element, every code path that
  changes the underlying look must also invalidate/refresh the overlay —
  with the *same parameters* the user is seeing, not persisted state.

## 7. Color jumps between play and pause

**Symptom.** Playback showed one color; pausing snapped to another (the
paused one matching the export).

**Why.** The genuinely subtle one: ffmpeg's `eq` applies its LUT to the
video's **limited-range luma bytes (16–235)**, while the browser filter
operated on decoded **display values (0–255)**. Same curve, shifted input —
biggest error in the darks. Found by predicting ffmpeg's output byte-for-byte
and testing hypotheses until 4/4 probes matched exactly (srcY 44/71/126/180 →
101/129/173/206).

**Fix.** The playback filter is now a single `feComponentTransfer
type="table"` LUT sampling the display-space curve
`D(d) = (clamp₂₅₅(255·f((16+219d)/255)) − 16)/219` where `f` is ffmpeg's
contrast→brightness→gamma. Luma now matches the export exactly; chroma stays a
close RGB approximation; the paused still remains authoritative.

**Avoid next time.**
- Video color range (limited vs full) is a first-class suspect in any
  "video looks different here vs there" bug.
- Validate curve math against a handful of *known byte values* through the
  real pipeline before shipping — it's what cracked this immediately.

## 8. Restart endpoint: replacement never came up

**Symptom.** `POST /api/restart` returned ok, old server exited, port stayed
dead.

**Why.** Two stacked bugs: (a) the replacement re-ran the self-detach fork
while already a session leader — the double-detach killed it (fixed with an
env-var guard); (b) `httpd.shutdown()` called from a thread spawned by a
request handler **self-deadlocks** (it waits for `serve_forever` to stop,
which waits for the handler that triggered it).

**Fix.** Skip `shutdown()` entirely: spawn the detached replacement, then
`os._exit(0)` (frees the socket instantly); the replacement retries its bind
for ~6 s. Found by instrumenting the worker with file logging to see exactly
where it hung.

**Avoid next time.**
- Never call `httpd.shutdown()` from a request-handling thread.
- For self-restart, "spawn replacement + hard-exit + bind-retry in the child"
  is the reliable shape.
- When a background worker dies silently, add file-based logging before
  theorizing.

## 9. Killing the wedged server killed the in-flight render

**Symptom.** After force-killing a frozen server, the running export was gone.

**Why.** The render is a `subprocess` **child** of the server; killing the
parent (and its group) took the render down. An interrupted render leaves the
previous output file on disk — easy to mistake for the new one.

**Fix / mitigation.** The restart endpoint now returns **409 with the busy
export list** and requires an explicit force. The UI warns "restarting will
KILL that render."

**Avoid next time.**
- Map the process tree (`ps -o ppid=`) before killing anything.
- Check output-file mtimes against expectations after any interrupted job.

---

## Recurring themes

1. **Process management caused more pain than code.** Stale/duplicated/
   suspended processes produced four separate "bugs." One canonical,
   self-detaching launch path (now built-in) removes the entire class.
2. **A preview that approximates the output will eventually mislead a
   decision.** The fix that stuck was serving the *actual* output (ffmpeg
   frames), not a better approximation.
3. **Verify with numbers at the source of truth.** `ffprobe signalstats` on
   real files settled every color question; screenshots and eyeballs misled.
4. **`T` in `ps` state output = suspended.** Check it before debugging
   "hangs."
5. **UI stuck on a status ≠ backend stuck.** Every "stuck on PREP/generating"
   was a dead status channel, while the work itself was either fine or frozen
   for unrelated reasons. Poll paths should surface "server unreachable"
   distinctly from "job in progress."
