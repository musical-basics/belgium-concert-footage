#!/usr/bin/env python3
"""
Audio-transition detection for a performance window.

Given the reference audio source and an [t_in, t_out] window, returns a sorted
list of candidate "transition" timestamps (absolute seconds) where a video cut
would land naturally on the music — strong onsets (phrase/chord changes) plus
beat positions, de-duplicated.

We extract the window with ffmpeg first (fast, frame-accurate input seek) so
librosa never has to decode 90 minutes of audio to reach a late offset.
Results are cached under cache/transitions/.
"""
import hashlib
import json
import os
import subprocess
import tempfile

import numpy as np
import librosa

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, "cache", "transitions")
SR = 22050


def _cache_key(source, t_in, t_out):
    h = hashlib.sha1(f"{source}|{t_in:.3f}|{t_out:.3f}|v2".encode()).hexdigest()[:16]
    return os.path.join(CACHE_DIR, h + ".json")


def _extract_wav(source_path, t_in, dur, out_wav):
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{t_in:.3f}", "-i", source_path, "-t", f"{dur:.3f}",
        "-vn", "-ac", "1", "-ar", str(SR), out_wav,
    ]
    subprocess.run(cmd, check=True)


def detect_transitions(source_path, t_in, t_out, use_cache=True):
    """Return (transitions, beats) as lists of absolute-second floats."""
    key = _cache_key(source_path, t_in, t_out)
    if use_cache and os.path.isfile(key):
        with open(key) as f:
            d = json.load(f)
        return d["transitions"], d["beats"]

    dur = t_out - t_in
    os.makedirs(CACHE_DIR, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        wav = os.path.join(td, "seg.wav")
        _extract_wav(source_path, t_in, dur, wav)
        y, sr = librosa.load(wav, sr=SR, mono=True)

    # Onset strength envelope -> prominent onsets (phrase/chord boundaries).
    hop = 512
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    onset_frames = librosa.onset.onset_detect(
        onset_envelope=onset_env, sr=sr, hop_length=hop,
        backtrack=True, delta=0.18, wait=int(0.30 * sr / hop),
    )
    onsets = librosa.frames_to_time(onset_frames, sr=sr, hop_length=hop)

    # Keep only the stronger half of onsets so cuts land on salient moments.
    if len(onset_frames):
        strengths = onset_env[np.clip(onset_frames, 0, len(onset_env) - 1)]
        thresh = np.median(strengths)
        onsets = onsets[strengths >= thresh]

    # Beat grid (downbeat-ish anchors for rhythmic cuts).
    try:
        _, beat_frames = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr, hop_length=hop)
        beats = librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop)
    except Exception:
        beats = np.array([])

    transitions = sorted(float(t_in + t) for t in onsets)
    beats = sorted(float(t_in + t) for t in beats)

    if use_cache:
        with open(key, "w") as f:
            json.dump({"transitions": transitions, "beats": beats}, f)
    return transitions, beats


def nearest(sorted_times, target, window):
    """Nearest value to target within +/- window, else None."""
    if not sorted_times:
        return None
    best, bestd = None, window + 1
    # linear is fine (lists are small per window)
    for t in sorted_times:
        d = abs(t - target)
        if d < bestd:
            best, bestd = t, d
        elif t > target and d > bestd:
            break
    return best if bestd <= window else None


if __name__ == "__main__":
    import sys
    src, a, b = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
    tr, bt = detect_transitions(src, a, b, use_cache=False)
    print(f"{len(tr)} transitions, {len(bt)} beats in [{a},{b}]")
    print("first 20 transitions:", [round(x, 2) for x in tr[:20]])
