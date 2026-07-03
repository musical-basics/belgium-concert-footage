# 5D 2 ↔ Back Camera audio-match pipeline

Aligns the `5D 2.mp4` selects reel (23.6 min, 25fps) against the uncut
`Main Footage/Back Camera.mov` reference (96 min, 60fps) purely by audio,
producing `editor/sync.json` — 31 clips with sample-accurate offsets — and a
two-track timeline viewer at `http://localhost:8000/sync.html`.

## Method

1. **Extract** 8 kHz mono WAVs from both files (ffmpeg) into `cache/sync/`.
2. **align_pass1.py** — onset-strength envelopes (librosa, 100 Hz feature
   rate); 8s windows of the reel every 2s are matched against the whole
   reference via FFT normalized cross-correlation. Runs of constant
   `delta = ref − src` are clip candidates.
3. **align_pass2.py** — boundary refinement (two-hypothesis score-crossover
   between neighbouring deltas), scene-cut snapping, GCC-PHAT delta
   refinement, whole-clip verification score.
4. **align_pass3.py** — diagnostics: per-clip match curves exposing bad
   stretches, full-reference re-search of every unmatched region.
5. **align_pass4/5.py** — GCC-PHAT (whitened cross-correlation, robust to the
   different mics) interrogation of ambiguous regions: applause/audience
   b-roll, J-cut audio leads, black-video-with-audio segments, and the
   dropped 3514.7s clip inside old clip 22.
6. **nail_bounds.py** — exact cut frames: ±3.2s of 160×90 gray frames around
   each audio-estimated boundary; the successive-frame-difference spike is
   the hard cut (works where ffmpeg scdet fails on dark stage footage).
7. **final_verify.py** — final table (envelope NCC + 3× PHAT probes per clip)
   and writes `editor/sync.json`.

Visual confirmation: side-by-side mid-clip frames from both cameras (the
projection-screen song titles + lighting colors act as ground truth).

## Gotchas learned

- Envelope NCC dies on applause; GCC-PHAT still locks there. Use both.
- PHAT full-reference search has "attractor" false peaks at loud crowd
  moments — only consistent deltas across multiple probes count.
- Piano is periodic: PHAT can ghost-lock ±1 beat (~1.5s) — verify with the
  envelope curve.
- The reel uses J-cuts (audio switches 2–3s before the video cut) and
  contains black-video-with-audio segments (clips 0 and 10).
- Reel content before 295.68s is a different part of the evening (other
  performers) and matches nothing in the back camera.
