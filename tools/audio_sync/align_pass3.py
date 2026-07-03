#!/usr/bin/env python3
"""Pass 3: per-clip match curves + full-search of bad stretches.

For every clip in clips.json: score curve at its delta (2s win, 0.5s hop).
Sustained low stretches (and the unmatched head/tail regions) get a fresh
full-reference search with 4s windows to see if some other delta matches.
"""
import json
import sys

import numpy as np
import scipy.signal

FEAT_RATE = 100
BASE = "/Users/lionelyu/Music/Belgium Concert Highlights/cache/sync"

q_env = np.load(f"{BASE}/q_env.npy")
r_env = np.load(f"{BASE}/r_env.npy")
QDUR = len(q_env) / FEAT_RATE

# sliding stats of reference reused across searches
def make_ref_stats(W):
    csum = np.concatenate(([0.0], np.cumsum(r_env)))
    csum2 = np.concatenate(([0.0], np.cumsum(r_env * r_env)))
    rs = csum[W:] - csum[:-W]
    rs2 = csum2[W:] - csum2[:-W]
    rstd = np.sqrt(np.maximum(rs2 - rs * rs / W, 1e-12))
    return rstd


def ncc_at(delta_f, t_f, W):
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


def full_search(t_s, win_s, rstd_cache={}):
    W = int(win_s * FEAT_RATE)
    if W not in rstd_cache:
        rstd_cache[W] = make_ref_stats(W)
    rstd = rstd_cache[W]
    t_f = int(round(t_s * FEAT_RATE))
    w = q_env[t_f:t_f + W]
    if len(w) < W:
        return None
    wz = w - w.mean()
    wn = np.linalg.norm(wz)
    if wn < 1e-9:
        return None
    corr = scipy.signal.fftconvolve(r_env, wz[::-1], mode="valid")
    ncc = corr / (rstd * wn)
    best = int(np.argmax(ncc))
    return best / FEAT_RATE - t_s, float(ncc[best])


def main():
    data = json.load(open(f"{BASE}/clips.json"))
    clips = data["clips"]

    print("=== per-clip match curves (2s windows @ delta; '.'<0.3  '-'<0.6  '+'<0.8  '#'>=0.8) ===")
    bad_stretches = []
    for i, c in enumerate(clips):
        d_f = int(round(c["delta"] * FEAT_RATE))
        W = 2 * FEAT_RATE
        ts = np.arange(c["q_in"], c["q_out"] - 2.0, 0.5)
        ss = np.array([ncc_at(d_f, int(round(t * FEAT_RATE)), W) for t in ts])
        chars = "".join("#" if s >= 0.8 else "+" if s >= 0.6 else "-" if s >= 0.3 else "." for s in ss)
        print(f"[{i:2d}] q {c['q_in']:8.2f} d={c['delta']:9.3f} v={c['verify']:.3f} |{chars}|")
        # sustained bad stretches (>= 5s below 0.3)
        bad = ss < 0.3
        j = 0
        while j < len(bad):
            if bad[j]:
                k = j
                while k < len(bad) and bad[k]:
                    k += 1
                if (k - j) * 0.5 >= 5.0:
                    bad_stretches.append((i, float(ts[j]), float(ts[min(k, len(ts) - 1)]) + 2.0))
                j = k
            else:
                j += 1

    # unmatched regions outside clips
    print("\n=== unmatched regions (before first / between / after last) ===")
    regions = []
    if clips[0]["q_in"] > 5:
        regions.append((None, 0.0, clips[0]["q_in"]))
    for a, b in zip(clips, clips[1:]):
        if b["q_in"] - a["q_out"] > 1.0:
            regions.append((None, a["q_out"], b["q_in"]))
    if QDUR - clips[-1]["q_out"] > 3:
        regions.append((None, clips[-1]["q_out"], QDUR))
    for r in regions:
        print(f"  gap q {r[1]:.1f} - {r[2]:.1f} ({r[2]-r[1]:.1f}s)")

    print("\n=== full-search of bad stretches + gaps (4s windows, 1s hop) ===")
    for src, t0, t1 in bad_stretches + regions:
        tag = f"clip {src}" if src is not None else "gap"
        print(f"--- {tag}: q {t0:.1f}-{t1:.1f}")
        t = t0
        while t < min(t1, QDUR - 4.2):
            r = full_search(t, 4.0)
            if r:
                print(f"    q={t:8.1f}  best delta {r[0]:9.2f}  ref {t + r[0]:8.1f}  s={r[1]:.3f}")
            t += 1.0


if __name__ == "__main__":
    main()
