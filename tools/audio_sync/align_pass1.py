#!/usr/bin/env python3
"""Pass 1: rough audio alignment of 5D 2 against Back Camera.

Slides 8s onset-envelope windows (2s hop) over the 5D2 audio and finds, for
each, the best-matching position in the full back-camera envelope via
FFT normalized cross-correlation. Prints one line per window:
  qtime  best_ref_time  delta(ref-q)  ncc_score
"""
import json
import sys

import numpy as np
import librosa
import scipy.signal

SR = 8000
HOP = 80          # 100 Hz feature rate
FEAT_RATE = SR // HOP
WIN_S = 8.0
HOP_S = 2.0

BASE = "/Users/lionelyu/Music/Belgium Concert Highlights/cache/sync"


def onset_env(path):
    y, _ = librosa.load(path, sr=SR, mono=True)
    env = librosa.onset.onset_strength(y=y, sr=SR, hop_length=HOP)
    return env.astype(np.float64)


def main():
    print("loading + onset envelopes...", file=sys.stderr, flush=True)
    q = onset_env(f"{BASE}/5d2_8k.wav")
    r = onset_env(f"{BASE}/back_8k.wav")
    np.save(f"{BASE}/q_env.npy", q)
    np.save(f"{BASE}/r_env.npy", r)
    print(f"q {len(q)} frames, r {len(r)} frames", file=sys.stderr, flush=True)

    # sliding stats of reference for NCC denominator
    W = int(WIN_S * FEAT_RATE)
    hopw = int(HOP_S * FEAT_RATE)
    csum = np.concatenate(([0.0], np.cumsum(r)))
    csum2 = np.concatenate(([0.0], np.cumsum(r * r)))
    n_pos = len(r) - W + 1
    rs = csum[W:] - csum[:-W]          # sliding sums, len n_pos
    rs2 = csum2[W:] - csum2[:-W]
    rvar = rs2 - rs * rs / W
    rstd = np.sqrt(np.maximum(rvar, 1e-12))

    results = []
    for start in range(0, len(q) - W, hopw):
        w = q[start:start + W]
        wz = w - w.mean()
        wn = np.linalg.norm(wz)
        if wn < 1e-9:
            continue
        corr = scipy.signal.fftconvolve(r, wz[::-1], mode="valid")  # len n_pos
        ncc = corr / (rstd * wn)
        best = int(np.argmax(ncc))
        score = float(ncc[best])
        qt = start / FEAT_RATE
        rt = best / FEAT_RATE
        results.append({"q": round(qt, 2), "ref": round(rt, 2),
                        "delta": round(rt - qt, 2), "score": round(score, 3)})
        if len(results) % 50 == 0:
            print(f"...{len(results)} windows", file=sys.stderr, flush=True)

    with open(f"{BASE}/pass1.json", "w") as f:
        json.dump(results, f)
    # human-readable dump
    for x in results:
        print(f"q={x['q']:8.2f} ref={x['ref']:8.2f} d={x['delta']:9.2f} s={x['score']:.3f}")


if __name__ == "__main__":
    main()
