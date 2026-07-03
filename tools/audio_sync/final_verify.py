#!/usr/bin/env python3
"""Final verification of the 31-clip list + emit editor/sync.json."""
import json

import numpy as np
import scipy.io.wavfile as wf

SR = 8000
FEAT_RATE = 100
ROOT = "/Users/lionelyu/Music/Belgium Concert Highlights"
BASE = f"{ROOT}/cache/sync"

q_env = np.load(f"{BASE}/q_env.npy")
r_env = np.load(f"{BASE}/r_env.npy")
_, qy = wf.read(f"{BASE}/5d2_8k.wav")
_, ry = wf.read(f"{BASE}/back_8k.wav")
qy = qy.astype(np.float32) / 32768.0
ry = ry.astype(np.float32) / 32768.0

FLAGS = {0: ["black-video"], 10: ["black-video"], 4: ["partly-black"],
         21: ["audience-broll", "applause-audio"], 30: ["bows-finale"]}


def env_ncc(delta_s, t0, t1):
    d_f = int(round(delta_s * FEAT_RATE))
    a_f = int(round(t0 * FEAT_RATE))
    W = int((t1 - t0) * FEAT_RATE)
    a = q_env[a_f:a_f + W]
    b = r_env[a_f + d_f:a_f + d_f + W]
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    az, bz = a - a.mean(), b - b.mean()
    den = np.linalg.norm(az) * np.linalg.norm(bz)
    return float(np.dot(az, bz) / den) if den > 1e-9 else 0.0


def phat(t_s, delta_s, dur=8.0, search=2.0):
    n = int(dur * SR)
    i0 = int(t_s * SR)
    a = qy[i0:i0 + n]
    pad = int(search * SR)
    j0 = int((t_s + delta_s) * SR) - pad
    j1 = j0 + n + 2 * pad
    if i0 < 0 or j0 < 0 or j1 > len(ry) or len(a) < n:
        return None
    b = ry[j0:j1]
    nfft = 1 << (len(b) + n - 1).bit_length()
    A = np.fft.rfft(a, nfft)
    B = np.fft.rfft(b, nfft)
    R = B * np.conj(A)
    R /= (np.abs(R) + 1e-12)
    cc = np.fft.irfft(R, nfft)[:len(b) - n + 1]
    k = int(np.argmax(cc))
    qual = float(cc[k]) / (float(np.mean(np.abs(cc))) + 4 * float(np.std(cc)) + 1e-12)
    return (j0 + k) / SR - t_s, qual


clips = json.load(open(f"{BASE}/final_clips.json"))
out = []
print(f"{'#':>2} {'5D2 in':>8} {'5D2 out':>8} {'ref in':>8} {'ref out':>8} {'dur':>6} {'env':>6} {'phatQ':>18} flags")
for c in clips:
    i, a, b, d = c["i"], c["q_in"], c["q_out"], c["delta"]
    dur = b - a
    env = env_ncc(d, a + 1.0, b - 1.0)
    quals, errs = [], []
    for frac in (0.2, 0.5, 0.8):
        r = phat(a + frac * dur - 4, d)
        if r:
            errs.append(abs(r[0] - d))
            quals.append(r[1])
    # a PHAT probe only "locks" if its peak lands on our delta (±60ms)
    locked = [q for e, q in zip(errs, quals) if e < 0.06]
    flags = FLAGS.get(i, [])
    rec = {"i": i, "src_in": round(a, 3), "src_out": round(b, 3),
           "ref_in": round(a + d, 3), "ref_out": round(b + d, 3),
           "delta": round(d, 4), "dur": round(dur, 3),
           "env_score": round(env, 3),
           "phat_locks": len(locked), "phat_q": round(max(quals), 2) if quals else None,
           "flags": flags}
    out.append(rec)
    print(f"{i:>2} {a:8.2f} {b:8.2f} {a+d:8.2f} {b+d:8.2f} {dur:6.1f} {env:6.3f} "
          f"{len(locked)}/3 lock maxQ {max(quals):4.2f}  {','.join(flags)}")

doc = {
    "generated": "audio-match pipeline (onset-envelope NCC + GCC-PHAT), 2026-07-03",
    "query": "5D 2.mp4",
    "reference": "Main Footage/Back Camera.mov",
    "ref_duration": 5764.7,
    "query_duration": 1414.64,
    "unmatched_src": [
        {"src_in": 0.0, "src_out": 295.68,
         "note": "pre-concert acts (speaker, violinist, other pianist) — not on back camera"},
        {"src_in": 1412.12, "src_out": 1414.64, "note": "black tail"},
    ],
    "clips": out,
}
with open(f"{ROOT}/editor/sync.json", "w") as f:
    json.dump(doc, f, indent=1)
print(f"\nwrote editor/sync.json  ({len(out)} clips, "
      f"{sum(c['dur'] for c in out):.1f}s placed of {1414.64:.1f}s reel)")
