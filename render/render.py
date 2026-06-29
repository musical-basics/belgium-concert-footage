#!/usr/bin/env python3
"""
Render performance videos from markers.json.

For each performance:
  1. detect audio transitions in its window (librosa, via Back Camera audio)
  2. build a deterministic cut plan (3-5s cuts, snapped to audio) -> plan.py
  3. cut each segment from the matching ORIGINAL full-res camera file
  4. concat the segments (hard cuts) and mux the continuous audio bed

Same seed in markers.json  ->  identical cut plan  ->  identical render.

Usage:
  python3 render/render.py                 # render all performances
  python3 render/render.py --only 1,3      # render performances #1 and #3 (1-based)
  python3 render/render.py --dry-run       # print plans, write plan JSON, no encode
  python3 render/render.py --encoder x264  # bit-exact CPU encode (default: videotoolbox)
  python3 render/render.py --audio livestream   # override audio bed source
"""
import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audio_cuts import detect_transitions          # noqa: E402
from plan import build_segments                     # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKERS = os.path.join(ROOT, "markers.json")
OUT_DIR = os.path.join(ROOT, "output")
SEG_DIR = os.path.join(ROOT, "cache", "segments")

SOURCES = {
    "back": "Main Footage/Back Camera.mov",
    "livestream": "Main Footage/Livestream Footage.mov",
    "piano": "Main Footage/camera next to piano.mov",
}
W, H, FPS = 1920, 1080, 60


def src_path(cam):
    return os.path.join(ROOT, SOURCES[cam])


def slugify(s):
    s = re.sub(r"[^\w\s-]", "", s or "").strip().lower()
    return re.sub(r"[\s_-]+", "-", s) or "untitled"


def run(cmd):
    subprocess.run(cmd, check=True)


def encoder_args(encoder):
    if encoder == "x264":
        # deterministic, bit-exact across runs (single-threaded)
        return ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-x264-params", "threads=1", "-pix_fmt", "yuv420p"]
    # default: fast Apple hardware encoder
    return ["-c:v", "h264_videotoolbox", "-b:v", "10M", "-pix_fmt", "yuv420p"]


VF = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
      f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps={FPS},format=yuv420p,setpts=PTS-STARTPTS")

# Per-camera color correction, prepended to the common VF. The Back Camera is
# underexposed vs the other two, so lift its midtones with gamma (preserves the
# deep blacks / saturated stage lights better than a flat brightness offset).
# Tune the numbers here if it needs more/less.
CAMERA_EQ = {
    "back": "eq=contrast=1.08:gamma=1.30:saturation=1.05",
}


def vf_for(cam):
    eq = CAMERA_EQ.get(cam)
    return f"{eq},{VF}" if eq else VF


def render_performance(perf, index, seed, audio_cam, encoder, dry):
    t_in, t_out = float(perf["in"]), float(perf["out"])
    title = perf.get("title", "Untitled")
    name = f"{index+1:02d}_{slugify(title)}"
    print(f"\n=== #{index+1}  {title} — {perf.get('composer','')}  "
          f"[{t_in:.2f} -> {t_out:.2f}, {t_out-t_in:.1f}s] ===")

    transitions, _beats = detect_transitions(src_path(audio_cam), t_in, t_out)
    segments, stats = build_segments(t_in, t_out, transitions, seed, index)
    print(f"  plan: {stats['segments']} segments  "
          f"({stats['audio_cuts']} audio-snapped, {stats['heuristic_cuts']} heuristic cuts)")

    plan = {
        "performance": index + 1, "title": title, "composer": perf.get("composer", ""),
        "in": t_in, "out": t_out, "seed": seed, "audio_source": audio_cam,
        "stats": stats, "segments": segments,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, name + ".plan.json"), "w") as f:
        json.dump(plan, f, indent=2)

    if dry:
        for s in segments:
            print(f"    {s['start']:8.2f} -> {s['end']:8.2f}  "
                  f"{s['camera']:10s} [{s['cut_type']}]")
        return

    # 1) cut each segment from its camera's original file
    seg_dir = os.path.join(SEG_DIR, name)
    os.makedirs(seg_dir, exist_ok=True)
    listfile = os.path.join(seg_dir, "concat.txt")
    enc = encoder_args(encoder)
    with open(listfile, "w") as lf:
        for s in segments:
            out = os.path.join(seg_dir, f"seg_{s['index']:04d}.mp4")
            run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-hwaccel", "videotoolbox",
                 "-ss", f"{s['start']:.3f}", "-i", src_path(s["camera"]),
                 "-t", f"{s['duration']:.3f}",
                 "-an", "-dn", "-vf", vf_for(s["camera"]), *enc,
                 "-r", str(FPS), "-g", str(FPS), "-write_tmcd", "0",
                 "-video_track_timescale", "60000", out])
            lf.write(f"file '{os.path.abspath(out)}'\n")
            print(f"    cut seg {s['index']+1}/{len(segments)} [{s['camera']}]   ", end="\r")
    print()

    # 2) concat video (identical params -> stream copy), continuous audio bed
    video_only = os.path.join(seg_dir, "video.mp4")
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy", video_only])

    audio_only = os.path.join(seg_dir, "audio.m4a")
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-ss", f"{t_in:.3f}", "-i", src_path(audio_cam), "-t", f"{t_out-t_in:.3f}",
         "-vn", "-c:a", "aac", "-b:a", "256k", "-ar", "48000", audio_only])

    out_path = os.path.join(OUT_DIR, name + ".mp4")
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", video_only, "-i", audio_only,
         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "copy",
         "-dn", "-write_tmcd", "0", "-movflags", "+faststart", "-shortest", out_path])
    print(f"  ✓ {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--markers", default=MARKERS)
    ap.add_argument("--only", default="", help="comma list of 1-based indices")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--encoder", choices=["videotoolbox", "x264"], default="videotoolbox")
    ap.add_argument("--audio", default=None, help="override audio source camera id")
    args = ap.parse_args()

    if not os.path.isfile(args.markers):
        sys.exit(f"No markers file at {args.markers}. Mark performances in the editor first.")
    with open(args.markers) as f:
        data = json.load(f)

    seed = int(data.get("seed", 42))
    audio_cam = args.audio or data.get("audio_source", "back")
    perfs = data.get("performances", [])
    if not perfs:
        sys.exit("No performances in markers.json.")

    only = {int(x) for x in args.only.split(",") if x.strip()} if args.only else None
    print(f"seed={seed}  audio={audio_cam}  encoder={args.encoder}  "
          f"performances={len(perfs)}" + (f"  only={sorted(only)}" if only else ""))

    for i, perf in enumerate(perfs):
        if only and (i + 1) not in only:
            continue
        render_performance(perf, i, seed, audio_cam, args.encoder, args.dry_run)

    print("\nDone." + ("  (dry run — no video encoded)" if args.dry_run else f"  Output in {OUT_DIR}/"))


if __name__ == "__main__":
    main()
