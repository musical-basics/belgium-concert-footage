# Playbook: aligning an edited selects reel to an uncut reference by audio

*Written 2026-07-03 after aligning `5D 2.mp4` (23.6 min selects reel) to
`Back Camera.mov` (96 min uncut) — 31 clips, sample-accurate, fully verified.
Audience: a future AI agent (or human) doing this again on new footage.
Concrete scripts for each pass live next to this file; adapt paths/constants.*

## The problem shape

You have:
- **Reference**: one long, uncut recording (camera or audio recorder) covering
  an entire event.
- **Query**: an edited reel — a sequence of hard-cut clips selected from the
  same event, possibly reordered, with unknown boundaries, shot on a
  different device with a different microphone.

You want: for every clip in the query, its exact source range and its exact
position on the reference timeline (`delta = ref_time − query_time`,
constant within a clip). Plus honest flags for anything that doesn't match.

Total wall time for ~90 min reference / ~24 min query: **under an hour**,
almost all of it thinking, not computing. Each pass below runs in seconds to
a few minutes on a laptop.

## Core insight

Don't detect cuts first and then match clips. Instead **estimate the
alignment offset as a function of query time** — offset jumps are the cuts,
and the plateau values are the alignments. You get segmentation and matching
from one computation, and every later pass is just sharpening the same map.

Three signals, used for what each is good at:

| Signal | Good at | Fails at |
|---|---|---|
| Onset-envelope NCC (librosa onset strength @100 Hz, FFT cross-correlation) | music, speech; global search over the whole reference | applause/crowd noise, silence |
| GCC-PHAT on raw audio (whitened cross-correlation, 8 kHz) | applause, different mics/rooms, ms-accurate refinement | periodic music (ghost-locks ±1 beat), global search (false attractors) |
| Frame-difference spikes on tiny grayscale frames | exact cut frame, even in dark footage where ffmpeg `scdet` fails | slow fades, black-to-black cuts |

**Never trust one signal alone.** Every conclusion below required two.

## Step 0 — Probe and prep (2 min)

```bash
ffprobe -v error -show_entries format=duration:stream=codec_type,r_frame_rate,sample_rate <file>
ffmpeg -y -v error -i QUERY -vn -ac 1 -ar 8000 cache/sync/q_8k.wav
ffmpeg -y -v error -i REF   -vn -ac 1 -ar 8000 cache/sync/r_8k.wav
```
8 kHz mono is plenty; it keeps a 96-min reference at 46M samples so
full-reference FFTs stay cheap. Note the fps of each source now (frame
quantization matters at the end). Sanity-check durations against whatever
the user told you ("18 minutes of selects" ⇒ expect ≈18 min of matched
content; if the reel is longer, the rest is probably unmatched preamble).

Also start these in the background immediately — you'll want them later:
- a low-res proxy of the query for the viewer (`scale=640:360,fps=30`)
- `blackdetect` over the query (`-vf blackdetect=d=0.5:pix_th=0.08`)
- ffmpeg scene detect (`select='gt(scene,0.15)'`) — low value on dark
  footage, but free candidates for boundary snapping

## Pass 1 — Global offset map (align_pass1.py)

Onset envelopes for both files (hop = 80 samples @8 kHz → 100 Hz feature
rate). Slide an **8s window every 2s** over the query envelope; for each,
normalized cross-correlation against the *entire* reference envelope via
`scipy.signal.fftconvolve` (precompute the reference's sliding mean/std with
cumsums for a true NCC). Record `(q_time, best_ref_time, delta, score)`.

Read the output as **runs of constant delta**:
- score ≥ ~0.85 sustained over ≥3 windows → real clip, delta correct to ±10 ms
- score ~0.5–0.7 with delta changing every window → unmatched content (noise
  matches; ignore)
- short runs (n=2) at ~0.7 → suspicious; keep as *candidates* only
- a selects reel is usually **chronological**: deltas should be
  non-decreasing. A violation = probably a false lock; investigate.

Beware one pseudo-match signature: if `best_ref_time` stays **fixed** while
`q_time` advances (delta decreasing by 1 per second), the query audio is
stationary noise being attracted to one loud reference moment. Not a match.

## Pass 2 — Boundaries and refined deltas (align_pass2.py)

For each adjacent clip pair (deltas dA, dB), compute two **hypothesis score
curves** over the transition zone: NCC of a short window (1.5s) at exactly dA
and exactly dB, every 0.1s. The boundary is where the smoothed difference
`s_B − s_A` makes its **first sustained** sign flip — take the crossing with
the greatest before/after contrast, NOT the last crossing (a bug I hit: late
noise crossings squeezed real clips to slivers).

Refine each clip's delta with GCC-PHAT at 3 probe points (20%/50%/80%):

```python
R = FFT(ref_chunk) * conj(FFT(query_chunk)); R /= |R|      # whiten
cc = IFFT(R); quality = peak / (mean(|cc|) + 4*std(cc))
```

- quality > ~1.5 **and** peak within 60 ms of the envelope delta = lock.
- The 3 probes agreeing within ~20 ms also proves there's **no clock drift**
  worth worrying about (if they disagree systematically and monotonically,
  the devices drift — then fit delta(t) linearly per clip instead).
- Verify each final clip with whole-clip envelope NCC. **< 0.5 means
  something inside the clip is wrong** — do not ship it; diagnose.

## Pass 3 — Diagnose every low scorer (align_pass3.py)

For each clip, print a per-2s **match-curve strip** at its delta
(`#`≥0.8 `+`≥0.6 `-`≥0.3 `.`<0.3). This one visualization found every real
problem in the project:

- strip good then all-dots at the end → clip ends earlier than you thought;
  the tail is something else
- dots at the start → boundary too early / previous clip extends further
- dots in the middle of a "clip" → hidden cut; a whole clip was swallowed
  (threshold dropped it in pass 1 — here it was a real clip at delta 3514.7
  inside what looked like one clip)
- mediocre `+` throughout but PHAT locks → quiet music / different mic
  coloration; fine, keep it

Then full-search every bad stretch and every gap again with 4s windows.

## Pass 4/5 — Interrogate the ambiguous with PHAT (align_pass4/5.py)

Rules learned the hard way:

1. **Full-reference PHAT search is a trap**: loud crowd moments act as
   universal attractors (here, refs ≈3168/4360/5265/5726 "matched"
   everything with Q≈2.5–3.5). Only *consistent deltas across ≥3 probe
   positions* mean anything; a single Q<4 full-search hit means nothing.
2. **Envelope silence ≠ no match.** Applause kills envelope NCC. Before
   declaring a region unmatched, PHAT-test it at the *neighboring clips'
   deltas* with long (12–14s) windows. Here, three "unmatched gaps" were
   audience b-roll shots whose audio continued at the next/previous clip's
   delta — the reel editor had laid consecutive shots over continuous audio.
3. Domain logic is evidence: if the reel shows an audience shot right after
   a song, its audio is the applause after that song — compute what delta
   that implies and test it directly. Cheapest hypothesis wins.
4. Watch for **J-cuts / L-cuts**: audio switching 2–3s before/after the
   video cut is normal editing. Video cut position (from frame diffs) is the
   clip boundary for placement; the audio handover point will differ. Don't
   let the audio crossover pull the boundary off the video cut.
5. PHAT on solo piano can **ghost-lock exactly ±1 beat** (~1.5s here). If a
   PHAT delta sits a suspiciously musical interval away from the envelope
   delta, the envelope is right.

## Nail the exact cut frames (nail_bounds.py)

Around each audio-estimated boundary, decode ±3.2s of 160×90 grayscale
frames, take the mean absolute successive-frame difference, and pick the
biggest spike (report prominence = spike/median, plus the runner-up).
Prominence >6 with no near-tie = solid cut frame. This works on footage so
dark that ffmpeg's scene detector returns nothing. Ambiguous ones (two close
spikes, prom <5) get resolved by the fine PHAT sweeps (0.5s step, 4s window,
both deltas) — the audio handover brackets which video spike is the cut.

## Verification — the part that makes it real

Numeric: final table with whole-clip envelope NCC + 3 PHAT probes/clip
(final_verify.py). Demand an explanation for every score below ~0.7 —
acceptable explanations are *applause*, *black video*, *bows/crowd*; each
got a flag in the output JSON. "Dunno" is not acceptable.

**Visual (do not skip — it caught the only remaining doubt):** for every
clip, extract a mid-clip frame from the query AND the reference at
`mid + delta`, tile them side-by-side (ffmpeg hstack/vstack sheets), and
*look at them*. Different angles of the same stage verify instantly via:
- projected slide text (matched exact song titles from two angles)
- stage lighting color (here it cycled pink→blue→red; both cameras showed
  the same phase at the mapped instant — that's sub-cycle precision for free)
- performer position / instrument / who's on stage

Also build a whole-reel contact sheet early (1 frame/4s, tiled). Ten seconds
of looking at it explained: the pre-roll that matches nothing (different
performers), black-with-audio segments, and audience b-roll — before any of
those cost analysis time.

By ear: the viewer's "Both" audio mode plays the two tracks mixed; correct
sync sounds like room reverb, an error sounds like slap echo (40 ms = one
25fps frame is clearly audible as color, 200 ms as echo).

## Things that will look like bugs but are content

- **Black video with live audio** (two clips here) — the reel deliberately
  carried audio over black. Audio-place them; flag `black-video`.
- Reel preamble from a different part of the event → matches nothing, ever.
  Trust the user's hint about where content starts; confirm, don't assume.
- The projection screen showing a song title **after** that song ended
  (slides persist through applause) — don't use slide text alone to reject
  a match, use it with timing.

## Output contract

Emit one JSON: per clip `{src_in, src_out, ref_in, ref_out, delta,
env_score, phat_locks, flags[]}` + explicit `unmatched_src` ranges with
human-readable notes. Check before shipping: ref ranges strictly increasing
and non-overlapping (for a chronological reel), total placed duration ≈ what
the user predicted, and **every** second of the query is either placed or in
`unmatched_src` with a reason.

## Checklist (condensed)

1. Probe files; extract 8 kHz mono WAVs; start proxy + blackdetect + scdet
   in background; contact-sheet the query and LOOK at it.
2. Pass 1: envelope NCC map, 8s/2s → runs of constant delta.
3. Pass 2: crossover boundaries (first *sustained* flip), PHAT-refine deltas
   (3 probes; checks drift too), whole-clip verify.
4. Pass 3: match-curve strips; diagnose every `.`-stretch and every gap.
5. Pass 4/5: PHAT hypothesis tests on ambiguous regions using *neighbor
   deltas*; respect the attractor/ghost-lock/J-cut rules.
6. nail_bounds: frame-diff the exact cut frames.
7. final_verify: table + JSON; explain every low score or fix it.
8. Visual sheets: side-by-side frames for **all** clips; zoom any doubt.
9. Ship JSON + viewer; save gotchas to memory.
