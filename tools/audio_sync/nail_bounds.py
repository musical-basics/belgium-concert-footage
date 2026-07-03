#!/usr/bin/env python3
"""Nail exact cut frames in 5D 2.mp4 near audio-estimated boundaries.

Decodes ±3.2s of 160x90 gray frames around each estimate and reports the
largest successive-frame-difference spike (the hard cut), with prominence
relative to the local median diff.
"""
import subprocess
import sys

import numpy as np

SRC = "/Users/lionelyu/Music/Belgium Concert Highlights/5D 2.mp4"
W, H, FPS = 160, 90, 25

BOUNDS = [294.8, 339.65, 370.15, 437.35, 537.75, 561.4, 599.55, 618.35,
          651.0, 669.0, 706.04, 734.85, 784.65, 819.72, 849.92, 877.45,
          910.75, 996.16, 1019.85, 1039.5, 1063.95, 1102.25, 1133.0,
          1157.0, 1166.0, 1175.0, 1190.3, 1215.95, 1243.25, 1267.36,
          1283.76, 1318.72, 1354.96, 1370.5, 1395.0]


def frames_around(t, span=3.2):
    t0 = max(0.0, t - span)
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{t0:.3f}", "-i", SRC,
           "-t", f"{2*span:.3f}", "-vf", f"scale={W}:{H}", "-pix_fmt", "gray",
           "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    n = len(raw) // (W * H)
    arr = np.frombuffer(raw[:n * W * H], np.uint8).reshape(n, H, W).astype(np.int16)
    return t0, arr


def main():
    out = []
    for b in BOUNDS:
        t0, arr = frames_around(b)
        if len(arr) < 10:
            print(f"~{b:8.2f}: decode failed")
            continue
        d = np.abs(np.diff(arr, axis=0)).mean(axis=(1, 2))
        k = int(np.argmax(d))
        med = float(np.median(d)) + 1e-6
        # cut happens between frame k and k+1 -> new clip starts at k+1
        cut_t = t0 + (k + 1) / FPS
        prom = float(d[k]) / med
        # second-highest spike for ambiguity check
        d2 = d.copy(); d2[max(0, k - 2):k + 3] = 0
        k2 = int(np.argmax(d2))
        prom2 = float(d2[k2]) / med
        flag = "OK " if prom > 6 and prom2 < prom * 0.6 else "??? "
        print(f"~{b:8.2f}: cut at {cut_t:9.3f}  prom {prom:6.1f}  (2nd {t0+(k2+1)/FPS:9.3f} prom {prom2:5.1f})  {flag}")
        out.append((b, cut_t, prom))


if __name__ == "__main__":
    main()
