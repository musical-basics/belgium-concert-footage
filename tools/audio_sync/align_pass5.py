#!/usr/bin/env python3
"""Pass 5: long-window PHAT hypothesis tests on the applause/b-roll regions."""
import numpy as np
import scipy.io.wavfile as wf

SR = 8000
BASE = "/Users/lionelyu/Music/Belgium Concert Highlights/cache/sync"
_, qy = wf.read(f"{BASE}/5d2_8k.wav")
_, ry = wf.read(f"{BASE}/back_8k.wav")
qy = qy.astype(np.float32) / 32768.0
ry = ry.astype(np.float32) / 32768.0


def phat_local(t_s, delta_s, dur, search=2.5):
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


def sweep(name, t0, t1, hyps, dur, step):
    print(f"\n--- {name}  win={dur}s")
    t = t0
    while t <= t1 - dur:
        row = [f"q={t:8.1f}"]
        for h in hyps:
            r = phat_local(t, h, dur)
            row.append(f"h{h:9.3f}: d={r[0]:9.3f} Q={r[1]:.2f}" if r else f"h{h:9.3f}: ---")
        print("  ".join(row), flush=True)
        t += step


sweep("clip7/8 applause zone", 646, 706, [1191.793, 1281.894], 12, 2)
sweep("gap before 21 (post-Gallop applause)", 1128, 1160, [3002.215, 3245.531], 12, 2)
sweep("1163-1180 zone", 1162, 1179, [3245.531, 968.5, 3391.418], 8, 1.5)
sweep("finale/bows tail", 1366, 1400, [3999.884, 4334.974], 14, 2)
sweep("17/18 handover", 1026, 1046, [2807.53, 2839.081], 6, 1)
sweep("22a start", 1172, 1182, [3391.418], 6, 1)
