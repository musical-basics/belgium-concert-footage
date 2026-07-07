#!/usr/bin/env python3
"""
Deterministic cut-plan generation.

For a performance [t_in, t_out] and a seed, produce a list of segments, each
assigned to a camera. Cut spacing is 3-5s (seeded), and each base cut is
SNAPPED to a nearby audio transition when one exists within the snap window
-> "SOME cut points match audio transitions", the rest follow the 3-5s
heuristic. No camera repeats back-to-back.

Live-camera priority: when `live_clips` intervals are supplied (the roving
"5D 2" camera, audio-matched onto the concert timeline by tools/audio_sync),
any part of the performance covered by live footage is shown from the live
camera as ONE uncut segment — its edges become forced cuts. The 3-camera
alternation only fills the gaps the live camera didn't capture.

Same (seed, in, out, transitions, live_clips) -> identical plan, every run.
"""
import random

CAMERAS = ["back", "livestream", "piano"]
LIVE_CAMERA = "5d2"

# Tunables (kept here so the render is reproducible & inspectable).
SEG_LO = 3.0          # min spacing between cuts (heuristic)
SEG_HI = 5.0          # max spacing between cuts (heuristic)
SNAP_WINDOW = 0.75    # snap a base cut to an audio transition within +/- this
MIN_LEN = 2.0         # never produce a segment shorter than this


def _rng(seed, index):
    # str seed -> stable across processes (unlike hash()); deterministic.
    return random.Random(f"{seed}:{index}")


def _usable_live(t_in, t_out, live_clips):
    """Clip the live intervals to the performance window and enforce MIN_LEN:
    an interval edge is never allowed to leave a sub-MIN_LEN stationary sliver
    against t_in/t_out (shrink the live side instead — the live source has no
    footage beyond its own edges, so it can never be extended)."""
    out = []
    for c in live_clips or []:
        a, b, delta = max(c["ref_in"], t_in), min(c["ref_out"], t_out), c["delta"]
        if 0 < a - t_in < MIN_LEN:
            a = t_in + MIN_LEN
        if 0 < t_out - b < MIN_LEN:
            b = t_out - MIN_LEN
        if b - a >= MIN_LEN:
            out.append({"a": round(a, 3), "b": round(b, 3), "delta": delta})
    out.sort(key=lambda c: c["a"])
    return out


def _weights_for_no_repeat(target):
    """Compensate pick-weights for the no-back-to-back rule so the LONG-RUN
    screen share matches the user's targets.

    Picking `j != prev` with probability ∝ w_j is a Markov chain whose
    stationary distribution is π_i ∝ w_i·(S − w_i)  (S = Σw). Naively using the
    targets as pick-weights therefore under-serves heavy cameras (ask 50%, get
    ~40%). We fixed-point iterate w until π ≈ target.

    Notes: with ≤2 cameras the chain is forced alternation (50/50 no matter
    what), so targets are returned as-is; and no camera can exceed 50% of cuts
    under a strict no-repeat rule, so a >50% target saturates near 50%."""
    cams = list(target)
    if len(cams) <= 2:
        return dict(target)
    tot = sum(target.values())
    t = {c: target[c] / tot for c in cams}
    w = dict(t)
    for _ in range(300):
        S = sum(w.values())
        pi = {c: w[c] * (S - w[c]) for c in cams}
        Z = sum(pi.values()) or 1.0
        drift = 0.0
        for c in cams:
            p = pi[c] / Z
            drift = max(drift, abs(p - t[c]))
            w[c] *= (t[c] / max(p, 1e-9)) ** 0.5
            w[c] = min(max(w[c], 1e-6), 1e6)      # keep boundary targets sane
        m = max(w.values())
        for c in cams:
            w[c] /= m
        if drift < 1e-4:
            break
    return w


def build_segments(t_in, t_out, transitions, seed, index,
                   first_camera=None, live_clips=None, camera_weights=None):
    """camera_weights: optional {camera_id: relative_weight} for the stationary
    cameras (e.g. {"back": 25, "livestream": 25, "piano": 50}). Weight 0 (or
    negative) removes that camera from this performance entirely. When absent,
    the ORIGINAL unweighted rng.choice path runs, so existing seeds keep
    producing byte-identical plans."""
    rng = _rng(seed, index)
    total = t_out - t_in
    live = _usable_live(t_in, t_out, live_clips)

    # Stationary-camera pool + weights for this performance. Unknown ids in the
    # weights map are ignored; cameras missing from the map default to weight 1.
    weights = None
    pool = CAMERAS
    if camera_weights:
        target = {c: max(float(camera_weights.get(c, 1)), 0.0) for c in CAMERAS}
        pool = [c for c in CAMERAS if target[c] > 0] or CAMERAS
        weights = _weights_for_no_repeat({c: target[c] for c in pool})

    def live_at(t):
        for c in live:
            if c["a"] <= t < c["b"]:
                return c
        return None

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

    # 2) drop heuristic/audio cuts that fall inside live coverage (a live
    #    stretch plays as one uncut shot) or within MIN_LEN of a live edge
    #    (would leave a sliver segment against the forced cut)
    def near_live_edge(ct):
        for c in live:
            if abs(ct - c["a"]) < MIN_LEN or abs(ct - c["b"]) < MIN_LEN:
                return True
        return False
    cuts = [(ct, ty) for ct, ty in cuts
            if live_at(ct) is None and not near_live_edge(ct)]

    # 3) boundaries = surviving cuts + forced live edges
    bound_map = {round(ct, 3): ty for ct, ty in cuts}
    for c in live:
        for edge in (c["a"], c["b"]):
            if t_in < edge < t_out:
                bound_map[round(edge, 3)] = "live"
    bounds = [t_in] + sorted(bound_map) + [t_out]
    cut_types = ["start"] + [bound_map[b] for b in sorted(bound_map)]

    # 4) assign cameras: live coverage wins; gaps alternate the 3 stationary
    #    cameras with no back-to-back repeats
    segments = []
    prev = first_camera
    for k in range(len(bounds) - 1):
        mid = (bounds[k] + bounds[k + 1]) / 2
        cov = live_at(mid)
        if cov is not None:
            cam = LIVE_CAMERA
        elif weights is None:
            choices = [c for c in CAMERAS if c != prev] or CAMERAS
            cam = rng.choice(choices)
        else:
            choices = [c for c in pool if c != prev] or pool
            ws = [max(weights.get(c, 1.0), 0.0001) for c in choices]
            cam = rng.choices(choices, weights=ws, k=1)[0]
        prev = cam
        seg = {
            "index": k,
            "start": round(bounds[k], 3),
            "end": round(bounds[k + 1], 3),
            "duration": round(bounds[k + 1] - bounds[k], 3),
            "camera": cam,
            "cut_type": cut_types[k],   # how THIS segment's start cut was chosen
        }
        if cov is not None:
            # ref -> live-source mapping for the renderer (src = ref - delta)
            seg["delta"] = cov["delta"]
        segments.append(seg)

    n_audio = sum(1 for s in segments if s["cut_type"] == "audio")
    n_live = sum(1 for s in segments if s["camera"] == LIVE_CAMERA)
    stats = {
        "segments": len(segments),
        "total": round(total, 3),
        "audio_cuts": n_audio,
        "heuristic_cuts": sum(1 for s in segments if s["cut_type"] == "heuristic"),
        "live_segments": n_live,
        "live_seconds": round(sum(s["duration"] for s in segments
                                  if s["camera"] == LIVE_CAMERA), 3),
    }
    return segments, stats


if __name__ == "__main__":
    live_demo = [{"ref_in": 120.0, "ref_out": 165.0, "delta": -50.0}]
    segs, st = build_segments(100.0, 240.0, [123.4, 156.7, 190.2], seed=42, index=0,
                              live_clips=live_demo)
    print(st)
    for s in segs:
        extra = f"  (src {s['start']-s['delta']:.3f})" if "delta" in s else ""
        print(f"  {s['start']:8.3f} -> {s['end']:8.3f}  {s['camera']:10s} [{s['cut_type']}]{extra}")
