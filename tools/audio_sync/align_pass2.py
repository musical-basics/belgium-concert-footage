#!/usr/bin/env python3
"""Pass 2: exact clip boundaries + sample-accurate offsets.

Input: pass1.json (rough window->offset map), scenecuts.txt, envelopes, wavs.
Output: clips.json  [{q_in, q_out, ref_in, ref_out, delta, scores...}]
"""
import json
import re
import sys

import numpy as np

SR = 8000
FEAT_RATE = 100
BASE = "/Users/lionelyu/Music/Belgium Concert Highlights/cache/sync"

q_env = np.load(f"{BASE}/q_env.npy")
r_env = np.load(f"{BASE}/r_env.npy")


def load_wav(path):
    import scipy.io.wavfile as wf
    sr, y = wf.read(path)
    assert sr == SR
    return y.astype(np.float64) / 32768.0


def ncc_at(delta_f, t_f, W):
    """NCC of q[t:t+W] vs r[t+delta:t+delta+W] (feature frames)."""
    a = q_env[t_f:t_f + W]
    b0 = t_f + delta_f
    if b0 < 0 or b0 + W > len(r_env) or t_f + W > len(q_env):
        return -1.0
    b = r_env[b0:b0 + W]
    az = a - a.mean(); bz = b - b.mean()
    na = np.linalg.norm(az); nb = np.linalg.norm(bz)
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return float(np.dot(az, bz) / (na * nb))


def score_curve(delta_s, t0, t1, win=1.5, hop=0.1):
    """Score curve for hypothesis delta over q time range [t0, t1]."""
    d_f = int(round(delta_s * FEAT_RATE))
    W = int(win * FEAT_RATE)
    ts, ss = [], []
    t = t0
    while t <= t1:
        t_f = int(round(t * FEAT_RATE))
        ts.append(t)
        ss.append(ncc_at(d_f, t_f, W))
        t += hop
    return np.array(ts), np.array(ss)


def main():
    res = json.load(open(f"{BASE}/pass1.json"))

    # ---- build strong runs
    runs = []
    cur = None
    for x in res:
        if cur and abs(x["delta"] - cur["deltas"][-1]) <= 0.3:
            cur["q1"] = x["q"] + 8
            cur["deltas"].append(x["delta"])
            cur["scores"].append(x["score"])
        else:
            cur = {"q0": x["q"], "q1": x["q"] + 8,
                   "deltas": [x["delta"]], "scores": [x["score"]]}
            runs.append(cur)
    strong = []
    for r in runs:
        med = float(np.median(r["scores"]))
        if (len(r["scores"]) >= 3 and med >= 0.70) or (len(r["scores"]) >= 2 and med >= 0.80):
            strong.append({"q0": r["q0"], "q1": r["q1"],
                           "delta": float(np.median(r["deltas"])),
                           "score": med, "n": len(r["scores"])})
    # merge strong runs with ~same delta (window-straddle artifacts between them)
    merged = []
    for s in strong:
        if merged and abs(s["delta"] - merged[-1]["delta"]) <= 0.6 and s["q0"] - merged[-1]["q1"] <= 30:
            m = merged[-1]
            m["q1"] = s["q1"]
            m["delta"] = (m["delta"] * m["n"] + s["delta"] * s["n"]) / (m["n"] + s["n"])
            m["n"] += s["n"]
            m["score"] = max(m["score"], s["score"])
        else:
            merged.append(dict(s))
    print(f"{len(merged)} candidate clips", file=sys.stderr)

    # ---- scene cuts
    cuts = [float(m) for m in re.findall(r"pts_time:([0-9.]+)", open(f"{BASE}/scenecuts.txt").read())]

    def snap(t, tol=2.0, lo=None, hi=None):
        cand = [c for c in cuts if abs(c - t) <= tol and (lo is None or c > lo) and (hi is None or c < hi)]
        return (min(cand, key=lambda c: abs(c - t)), True) if cand else (t, False)

    # ---- boundaries between consecutive clips: score-curve crossover
    clips = []
    for i, m in enumerate(merged):
        clips.append({"delta": m["delta"], "ev0": m["q0"], "ev1": m["q1"], "score": m["score"]})

    bounds = []  # boundary time between clip i and i+1 (in q time)
    for i in range(len(clips) - 1):
        a, b = clips[i], clips[i + 1]
        t0 = max(0.0, min(a["ev1"], b["ev0"]) - 12)
        t1 = min(len(q_env) / FEAT_RATE - 2, max(a["ev1"], b["ev0"]) + 12)
        ts, sa = score_curve(a["delta"], t0, t1)
        _, sb = score_curve(b["delta"], t0, t1)
        # window [t, t+1.5] belongs to A while score_a high; boundary = first
        # SUSTAINED handover from A to B in the smoothed diff.
        k = 5
        ker = np.ones(k) / k
        diff = np.convolve(sb - sa, ker, mode="same")
        cands = []
        for j in range(3, len(ts) - 4):
            if diff[j] < 0 <= diff[j + 1]:
                before = float(np.mean(diff[max(0, j - 8):j + 1]))
                after = float(np.mean(diff[j + 1:j + 9]))
                cands.append((after - before, ts[j + 1]))
        if cands:
            contrast, cross = max(cands)
            cross += 0.75  # crossing is where straddling window flips; centre it
        else:
            cross = ts[int(np.argmax(np.gradient(diff)))] + 0.75
        snapped, ok = snap(cross, tol=1.6)
        bounds.append({"t": snapped, "snapped": ok, "raw": round(float(cross), 2)})

    # ---- first clip start / last clip end
    first, last = clips[0], clips[-1]
    ts, ss = score_curve(first["delta"], max(0, first["ev0"] - 20), first["ev0"] + 10)
    good = ts[ss > 0.5]
    start = float(good[0]) if len(good) else first["ev0"]
    start, s_ok = snap(start, tol=2.5)

    qdur = len(q_env) / FEAT_RATE
    ts, ss = score_curve(last["delta"], last["ev1"] - 10, min(qdur - 1.6, last["ev1"] + 30))
    good = ts[ss > 0.5]
    end = float(good[-1]) + 1.5 if len(good) else last["ev1"]
    end, e_ok = snap(end, tol=2.5)

    # ---- assemble clip list with boundaries
    q_in = [start] + [b["t"] for b in bounds]
    q_out = [b["t"] for b in bounds] + [end]

    # ---- sample-accurate delta refinement on raw audio
    qy = load_wav(f"{BASE}/5d2_8k.wav")
    ry = load_wav(f"{BASE}/back_8k.wav")

    def refine(delta_s, t_s, dur=10.0, search=2.0):
        """GCC-PHAT between 5D2 audio at t_s and ref audio around estimate.

        Whitened cross-correlation is robust to the very different mic/room
        colouration of the two cameras. Returns (refined delta, peak quality)
        where quality = peak / (mean + 4*std) of the correlation in-window.
        """
        n = int(dur * SR)
        i0 = int(t_s * SR)
        a = qy[i0:i0 + n]
        pad = int(search * SR)
        j0 = int((t_s + delta_s) * SR) - pad
        j1 = j0 + n + 2 * pad
        if i0 < 0 or j0 < 0 or j1 > len(ry) or len(a) < n:
            return None
        b = ry[j0:j1]
        nfft = 1
        while nfft < len(b) + n:
            nfft *= 2
        A = np.fft.rfft(a, nfft)
        B = np.fft.rfft(b, nfft)
        R = B * np.conj(A)
        R /= (np.abs(R) + 1e-12)
        cc = np.fft.irfft(R, nfft)[:len(b) - n + 1]
        k = int(np.argmax(cc))
        peak = float(cc[k])
        quality = peak / (float(np.mean(np.abs(cc))) + 4 * float(np.std(cc)) + 1e-12)
        return (j0 + k) / SR - t_s, quality

    out = []
    for i, c in enumerate(clips):
        qi, qo = q_in[i], q_out[i]
        dur = qo - qi
        probes = []
        for frac in (0.15, 0.5, 0.85):
            t = qi + frac * dur
            rr = refine(c["delta"], t)
            if rr:
                probes.append((t, rr[0], rr[1]))
        deltas = [p[1] for p in probes if p[2] > 1.0]
        if deltas:
            d = float(np.median(deltas))
        else:
            d = c["delta"]
        spread = (max(deltas) - min(deltas)) if len(deltas) >= 2 else None
        # whole-clip envelope verification at the final delta
        d_f = int(round(d * FEAT_RATE))
        t_f = int(round((qi + 0.5) * FEAT_RATE))
        W = max(int((dur - 1.0) * FEAT_RATE), 100)
        vscore = ncc_at(d_f, t_f, W)
        out.append({
            "q_in": round(qi, 3), "q_out": round(qo, 3),
            "delta": round(d, 4),
            "ref_in": round(qi + d, 3), "ref_out": round(qo + d, 3),
            "dur": round(dur, 3),
            "pass1_score": round(c["score"], 3),
            "verify": round(vscore, 3),
            "probe_scores": [round(p[2], 2) for p in probes],
            "probe_deltas": [round(p[1], 4) for p in probes],
            "drift_spread": round(spread, 4) if spread is not None else None,
        })

    json.dump({"clips": out,
               "bounds": bounds,
               "start_snapped": s_ok, "end_snapped": e_ok},
              open(f"{BASE}/clips.json", "w"), indent=1)
    for i, c in enumerate(out):
        print(f"[{i:2d}] q {c['q_in']:8.2f}-{c['q_out']:8.2f} ({c['dur']:6.2f}s) -> ref {c['ref_in']:8.2f}-{c['ref_out']:8.2f}  d={c['delta']:9.3f}  verify {c['verify']:.3f}  gcc {c['probe_scores']} spread {c['drift_spread']}")


if __name__ == "__main__":
    main()
