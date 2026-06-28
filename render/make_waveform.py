#!/usr/bin/env python3
"""
Precompute a compact waveform peaks file for the editor timeline.

Decodes the audio-bed source (Back Camera) to mono PCM and reduces it to one
abs-peak per (1/PPS) second, stored as a Uint8 array (cache/waveform.u8) plus
metadata (cache/waveform.json). The GUI loads this once and re-samples it for
whatever zoom level is on screen.
"""
import json
import os
import subprocess
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "Main Footage", "Back Camera.mov")
OUT_BIN = os.path.join(ROOT, "cache", "waveform.u8")
OUT_META = os.path.join(ROOT, "cache", "waveform.json")

SR = 22050          # decode sample rate
PPS = 100           # peaks per second (10ms resolution)


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else SRC
    os.makedirs(os.path.dirname(OUT_BIN), exist_ok=True)
    print(f"decoding audio from {src} ...")
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", src, "-ac", "1", "-ar", str(SR),
         "-f", "s16le", "-"],
        stdout=subprocess.PIPE,
    )
    raw = proc.stdout.read()
    proc.wait()
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    print(f"{len(samples):,} samples ({len(samples)/SR/60:.1f} min)")

    bucket = SR // PPS
    n = len(samples) // bucket
    trimmed = samples[:n * bucket].reshape(n, bucket)
    peaks = np.abs(trimmed).max(axis=1)
    peak_max = max(float(peaks.max()), 1.0)
    peaks = np.clip(peaks / peak_max * 255.0, 0, 255).astype(np.uint8)

    peaks.tofile(OUT_BIN)
    with open(OUT_META, "w") as f:
        json.dump({
            "peaks_per_second": PPS,
            "count": int(n),
            "duration": n / PPS,
            "sample_rate": SR,
        }, f)
    print(f"wrote {n:,} peaks -> {OUT_BIN} ({os.path.getsize(OUT_BIN)/1e6:.1f} MB)")
    print("WAVEFORM DONE")


if __name__ == "__main__":
    main()
