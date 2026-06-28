#!/usr/bin/env python3
"""
Deterministic cut-plan generation.

For a performance [t_in, t_out] and a seed, produce a list of segments, each
assigned to one of the three cameras. Cut spacing is 3-5s (seeded), and each
base cut is SNAPPED to a nearby audio transition when one exists within the
snap window -> "SOME cut points match audio transitions", the rest follow the
3-5s heuristic. No camera repeats back-to-back.

Same (seed, in, out, transitions) -> identical plan, every run.
"""
import random

CAMERAS = ["back", "livestream", "piano"]

# Tunables (kept here so the render is reproducible & inspectable).
SEG_LO = 3.0          # min spacing between cuts (heuristic)
SEG_HI = 5.0          # max spacing between cuts (heuristic)
SNAP_WINDOW = 0.75    # snap a base cut to an audio transition within +/- this
MIN_LEN = 2.0         # never produce a segment shorter than this


def _rng(seed, index):
    # str seed -> stable across processes (unlike hash()); deterministic.
    return random.Random(f"{seed}:{index}")


def build_segments(t_in, t_out, transitions, seed, index,
                   first_camera=None):
    rng = _rng(seed, index)
    total = t_out - t_in

    # 1) base cut times via seeded 3-5s spacing, snapped to audio where possible
    from audio_cuts import nearest
    cuts = []          # (time, type)
    t = t_in
    while True:
        nt = t + rng.uniform(SEG_LO, SEG_HI)
        if nt >= t_out - MIN_LEN:
            break
        snapped, ctype = nt, "heuristic"
        cand = nearest(transitions, nt, SNAP_WINDOW)
        if cand is not None and (cand - t) >= MIN_LEN and (t_out - cand) >= MIN_LEN:
            snapped, ctype = round(cand, 3), "audio"
        cuts.append((round(snapped, 3), ctype))
        t = snapped

    # 2) boundaries -> segments
    bounds = [t_in] + [c[0] for c in cuts] + [t_out]
    cut_types = ["start"] + [c[1] for c in cuts]

    segments = []
    prev = first_camera
    for k in range(len(bounds) - 1):
        choices = [c for c in CAMERAS if c != prev] or CAMERAS
        cam = rng.choice(choices)
        prev = cam
        segments.append({
            "index": k,
            "start": round(bounds[k], 3),
            "end": round(bounds[k + 1], 3),
            "duration": round(bounds[k + 1] - bounds[k], 3),
            "camera": cam,
            "cut_type": cut_types[k],   # how THIS segment's start cut was chosen
        })

    n_audio = sum(1 for s in segments if s["cut_type"] == "audio")
    stats = {
        "segments": len(segments),
        "total": round(total, 3),
        "audio_cuts": n_audio,
        "heuristic_cuts": len(segments) - 1 - n_audio,
    }
    return segments, stats


if __name__ == "__main__":
    segs, st = build_segments(100.0, 240.0, [123.4, 156.7, 190.2], seed=42, index=0)
    print(st)
    for s in segs:
        print(f"  {s['start']:8.3f} -> {s['end']:8.3f}  {s['camera']:10s} [{s['cut_type']}]")
