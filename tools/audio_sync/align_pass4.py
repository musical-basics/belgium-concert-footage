#!/usr/bin/env python3
"""Pass 4: GCC-PHAT interrogation of ambiguous regions.

Two modes per probe:
  - local: PHAT peak near a hypothesis delta (±2.5s) -> confirms/updates
  - full: PHAT against the ENTIRE reference -> finds matches envelope missed
Quality metric: peak / (mean(|cc|) + 4*std(cc)); > ~1.5 = confident.
"""
import json
import sys

import numpy as np
import scipy.io.wavfile as wf

SR = 8000
BASE = "/Users/lionelyu/Music/Belgium Concert Highlights/cache/sync"

_, qy = wf.read(f"{BASE}/5d2_8k.wav")
_, ry = wf.read(f"{BASE}/back_8k.wav")
qy = qy.astype(np.float32) / 32768.0
ry = ry.astype(np.float32) / 32768.0

# --- precompute full-reference FFT once
NFFT_FULL = 1 << (len(ry) + SR * 12 - 1).bit_length()
print(f"precomputing ref FFT (nfft={NFFT_FULL})...", file=sys.stderr, flush=True)
RY_F = np.fft.rfft(ry, NFFT_FULL)


def phat_local(t_s, delta_s, dur=8.0, search=2.5):
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


def phat_full(t_s, dur=8.0):
    n = int(dur * SR)
    i0 = int(t_s * SR)
    a = qy[i0:i0 + n]
    if len(a) < n:
        return None
    A = np.fft.rfft(a, NFFT_FULL)
    R = RY_F * np.conj(A)
    R /= (np.abs(R) + 1e-12)
    cc = np.fft.irfft(R, NFFT_FULL)[:len(ry) - n + 1]
    k = int(np.argmax(cc))
    qual = float(cc[k]) / (float(np.mean(np.abs(cc))) + 4 * float(np.std(cc)) + 1e-12)
    return k / SR - t_s, qual


def probe_region(name, t0, t1, hyps=(), step=3.0, dur=6.0):
    print(f"\n--- {name}: q {t0:.1f}-{t1:.1f}  hyps={list(hyps)}")
    t = t0
    while t < t1 - dur + 0.5:
        row = [f"q={t:8.1f}"]
        for h in hyps:
            r = phat_local(t, h, dur=dur)
            if r:
                row.append(f"h{h:.0f}: d={r[0]:9.3f} Q={r[1]:.2f}")
        rf = phat_full(t, dur=dur)
        if rf:
            row.append(f"FULL: d={rf[0]:9.3f} (ref {t + rf[0]:7.1f}) Q={rf[1]:.2f}")
        print("  ".join(row), flush=True)
        t += step


if __name__ == "__main__":
    # 1. clip 7 tail: attached to 1191.79 or elsewhere?
    probe_region("clip7-tail", 648, 672, hyps=[1191.793])
    # 2. boundary 17/18 zone + possible 2993 mini-clip + 2839 start
    probe_region("17/18-zone", 1028, 1044, hyps=[2807.510, 2839.081, 2993.10], step=1.5)
    # 3. clip 20 bad tail + clip 21 start
    probe_region("20-tail", 1130, 1158, hyps=[3002.215, 3245.531])
    # 4. pre-clip-22: possible 967.91 mini-clip
    probe_region("967-miniclip", 1164, 1182, hyps=[967.91, 3391.438], step=1.5)
    # 5. clip22 split zone
    probe_region("22-split", 1188, 1202, hyps=[3391.438, 3514.72], step=1.5)
    # 6. clip 28 tail + file tail
    probe_region("28-tail+end", 1368, 1413, hyps=[3999.884])
    # 7. first clip start sanity
    probe_region("clip0-start", 292, 300, hyps=[-194.152], step=1.0, dur=4.0)
