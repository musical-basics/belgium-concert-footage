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
import platform
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
    "5d2": "5D 2.mp4",   # roving live camera; timeline mapping via SYNC_JSON
}

# Audio-matched map of the 5D 2 selects reel onto the concert timeline
# (produced by tools/audio_sync/, see its PLAYBOOK.md). When present, any
# performance range covered by a usable 5D 2 clip is rendered from that
# camera (the "live feed" wins); the 3 stationary cameras fill the gaps.
SYNC_JSON = os.path.join(ROOT, "editor", "sync.json")

# Trim this far inside each matched clip's edges so a cut can never spill a
# frame of the neighbouring (different) shot at a 25fps clip boundary.
LIVE_EDGE_PAD = 0.1


def load_live_clips():
    """Usable live-camera intervals on the concert timeline, or [] if the
    sync map (or the source file) is missing. Black-video clips are excluded —
    they carry concert audio but show nothing."""
    if not os.path.isfile(SYNC_JSON) or not os.path.isfile(src_path("5d2")):
        return []
    with open(SYNC_JSON) as f:
        doc = json.load(f)
    out = []
    for c in doc.get("clips", []):
        if "black-video" in (c.get("flags") or []):
            continue
        a = c["ref_in"] + LIVE_EDGE_PAD
        b = c["ref_out"] - LIVE_EDGE_PAD
        if b - a > 1.0:
            out.append({"ref_in": round(a, 3), "ref_out": round(b, 3),
                        "delta": c["delta"]})
    return out

# Final audio bed muxed into every export: the mixed/mastered edit that fully
# replaces the camera scratch audio. Same length & timeline as the clips, used
# at face-value alignment (file 0:00 == concert 0:00), so each performance is
# cut from the same [t_in, t_out] window. Cut *detection* still reads the camera
# bed, so the visual edit is unchanged. Falls back to the camera if absent.
AUDIO_BED = os.path.join(ROOT, "Audio Edit Belgium Concert Highlights.wav")

W, H, FPS = 1920, 1080, 60

# Font used to burn in the on-screen titles (drawtext). Cross-platform: honour
# $TITLE_FONT, else pick the first font that exists (macOS Arial, common Linux
# DejaVu/Liberation). Install one on the render box if none are present.
_FONT_CANDIDATES = [
    os.environ.get("TITLE_FONT", ""),
    "/System/Library/Fonts/Supplemental/Arial.ttf",          # macOS
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",       # Debian/Ubuntu
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",                   # Arch
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",                # Fedora
]
TITLE_FONT = next((p for p in _FONT_CANDIDATES if p and os.path.isfile(p)),
                  "/System/Library/Fonts/Supplemental/Arial.ttf")


# ---- encoder / hardware acceleration (platform-aware) ----------------
_NVENC = None


def has_nvenc():
    """True if this ffmpeg build exposes the NVIDIA h264 encoder (cached)."""
    global _NVENC
    if _NVENC is None:
        try:
            out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                 capture_output=True, text=True).stdout
            _NVENC = "h264_nvenc" in out
        except Exception:
            _NVENC = False
    return _NVENC


def resolve_encoder(name):
    """Turn 'auto' (or None) into a concrete encoder for this machine: Apple
    videotoolbox on macOS, NVENC if the GPU/ffmpeg supports it, else CPU x264."""
    name = name or os.environ.get("RENDER_ENCODER", "auto")
    if name and name != "auto":
        return name
    if platform.system() == "Darwin":
        return "videotoolbox"
    return "nvenc" if has_nvenc() else "x264"


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
    if encoder == "nvenc":
        # NVIDIA hardware encoder (Linux GPU boxes)
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-b:v", "10M", "-pix_fmt", "yuv420p"]
    # Apple hardware encoder (macOS)
    return ["-c:v", "h264_videotoolbox", "-b:v", "10M", "-pix_fmt", "yuv420p"]


def decode_hwaccel(encoder):
    """Optional -hwaccel flags for the decode side of segment cuts. Skipped on
    CPU/x264 so a machine with no GPU still works."""
    if platform.system() == "Darwin":
        return ["-hwaccel", "videotoolbox"]
    if encoder == "nvenc":
        return ["-hwaccel", "cuda"]
    return []


VF = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
      f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps={FPS},format=yuv420p,setpts=PTS-STARTPTS")

# Per-camera color correction, prepended to the common VF. The Back Camera is
# underexposed vs the other two, so lift its midtones with gamma (preserves the
# deep blacks / saturated stage lights better than a flat brightness offset).
# Tune the numbers here if it needs more/less.
CAMERA_EQ = {
    "back": "eq=contrast=1.18:gamma=1.62:saturation=1.05",
}


def vf_for(cam):
    eq = CAMERA_EQ.get(cam)
    return f"{eq},{VF}" if eq else VF


def _fade_alpha(a, b, fd):
    """drawtext alpha expression: fade in over `fd`s after a, hold, fade out
    over `fd`s before b. `t` is the output (local) timestamp in seconds."""
    return (f"alpha='if(lt(t,{a:.3f}),0,"
            f"if(lt(t,{a + fd:.3f}),(t-{a:.3f})/{fd:.3f},"
            f"if(lt(t,{b - fd:.3f}),1,"
            f"if(lt(t,{b:.3f}),({b:.3f}-t)/{fd:.3f},0))))'")


# Word-wrap widths (max characters per line) for the burned-in titles. drawtext
# has no auto-wrap, so we wrap here; the editor preview wraps with the SAME
# limits so what you see while marking matches the render. Sized so a full line
# fills ~the centre 80% of the 1920px frame at the fonts below.
TITLE_MAIN_MAX_CHARS = 40
TITLE_SUB_MAX_CHARS = 56

# Default normalized position (centre of the text block) when a title has no
# explicit x/y — horizontally centred, lower third. Editor uses the same values.
TITLE_DEF_X = 0.5
TITLE_DEF_Y = 0.80


def _wrap(text, max_chars):
    """Greedy word-wrap into lines no longer than max_chars (a word longer than
    the limit gets its own line rather than being split)."""
    lines, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > max_chars:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}" if cur else word
    if cur:
        lines.append(cur)
    return lines


def title_filter(perf_titles, t_in, t_out, work_dir, gscale=1.0):
    """Build a drawtext filterchain for the titles overlapping this performance.

    Times in markers are global concert seconds; the rendered clip restarts at
    0, so each title's window is shifted by -t_in. Long text is word-wrapped and
    each line is drawn as its own drawtext (so every line is centred about the
    title's x). The whole block is centred on the title's normalized (x, y) —
    defaulting to a centred lower-third. Font size = base * gscale (global) * the
    title's own `scale`, with the wrap width scaled inversely so a line still
    fills ~the same frame width. Text is written to sidecar files and pulled in
    with textfile= so quotes/colons/commas need no escaping. '' when nothing
    overlaps.
    """
    style = (f"fontfile='{TITLE_FONT}':fontcolor=white:borderw=3:"
             f"bordercolor=black@0.9:shadowcolor=black@0.55:shadowx=2:shadowy=2")
    parts = []
    for n, ttl in enumerate(perf_titles):
        a = max(0.0, float(ttl["in"]) - t_in)
        b = min(t_out, float(ttl["out"])) - t_in
        if b - a <= 0.05:
            continue
        fd = min(0.4, max(0.05, (b - a) / 2))
        # per-title font scale (x the global scale); default 1.0
        s = gscale * (float(ttl["scale"]) if ttl.get("scale") else 1.0)
        fs_main, fs_sub = round(H / 16 * s), round(H / 27 * s)
        lh_main, lh_sub = fs_main * 1.18, fs_sub * 1.25
        cx = TITLE_DEF_X if ttl.get("x") is None else float(ttl["x"])
        cy = TITLE_DEF_Y if ttl.get("y") is None else float(ttl["y"])
        # each line horizontally centred about cx; block vertically centred on cy
        x_expr = f"(w*{cx:.4f}-text_w/2)"
        tail = f"x={x_expr}:enable='between(t,{a:.3f},{b:.3f})':{_fade_alpha(a, b, fd)}"

        main_lines = _wrap((ttl.get("text") or "").strip(), max(6, round(TITLE_MAIN_MAX_CHARS / s)))
        sub_lines = _wrap((ttl.get("subtitle") or "").strip(), max(8, round(TITLE_SUB_MAX_CHARS / s)))
        gap = fs_main * 0.5 if (main_lines and sub_lines) else 0
        block_h = len(main_lines) * lh_main + gap + len(sub_lines) * lh_sub
        y0 = cy * H - block_h / 2        # centre the block vertically on cy

        for li, line in enumerate(main_lines):
            tf = os.path.join(work_dir, f"title_{n}_m{li}.txt")
            with open(tf, "w") as f:
                f.write(line)
            y = round(y0 + li * lh_main)
            parts.append(f"drawtext=textfile='{tf}':{style}:fontsize={fs_main}:y={y}:{tail}")
        sy0 = y0 + len(main_lines) * lh_main + gap
        for li, line in enumerate(sub_lines):
            tf = os.path.join(work_dir, f"title_{n}_s{li}.txt")
            with open(tf, "w") as f:
                f.write(line)
            y = round(sy0 + li * lh_sub)
            parts.append(f"drawtext=textfile='{tf}':{style}:fontsize={fs_sub}:y={y}:{tail}")
    return ",".join(parts)


def render_performance(perf, index, seed, audio_cam, encoder, dry, titles=(), title_scale=1.0,
                       live_clips=()):
    t_in, t_out = float(perf["in"]), float(perf["out"])
    title = perf.get("title", "Untitled")
    name = f"{index+1:02d}_{slugify(title)}"
    print(f"\n=== #{index+1}  {title} — {perf.get('composer','')}  "
          f"[{t_in:.2f} -> {t_out:.2f}, {t_out-t_in:.1f}s] ===")

    transitions, _beats = detect_transitions(src_path(audio_cam), t_in, t_out)
    segments, stats = build_segments(t_in, t_out, transitions, seed, index,
                                     live_clips=live_clips)
    print(f"  plan: {stats['segments']} segments  "
          f"({stats['audio_cuts']} audio-snapped, {stats['heuristic_cuts']} heuristic cuts, "
          f"{stats['live_segments']} live/5D2 covering {stats['live_seconds']:.1f}s)")

    # Titles whose window overlaps this performance, with times shifted to the
    # clip's local (0-based) timeline for the overlay/plan.
    perf_titles = [
        {"text": tt.get("text", ""), "subtitle": tt.get("subtitle", ""),
         "in": float(tt["in"]), "out": float(tt["out"]),
         "x": tt.get("x"), "y": tt.get("y"), "scale": tt.get("scale"),
         "local_in": round(max(0.0, float(tt["in"]) - t_in), 3),
         "local_out": round(min(t_out, float(tt["out"])) - t_in, 3)}
        for tt in titles
        if float(tt["out"]) > t_in and float(tt["in"]) < t_out
    ]
    if perf_titles:
        print(f"  titles: {len(perf_titles)} overlay(s) — "
              + ", ".join(f'"{t["text"]}"' for t in perf_titles))

    plan = {
        "performance": index + 1, "title": title, "composer": perf.get("composer", ""),
        "in": t_in, "out": t_out, "seed": seed, "audio_source": audio_cam,
        "stats": stats, "segments": segments, "titles": perf_titles,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, name + ".plan.json"), "w") as f:
        json.dump(plan, f, indent=2)

    if dry:
        for s in segments:
            src_note = f"  (5D2 src {s['start']-s['delta']:8.2f})" if "delta" in s else ""
            print(f"    {s['start']:8.2f} -> {s['end']:8.2f}  "
                  f"{s['camera']:10s} [{s['cut_type']}]{src_note}")
        for tt in perf_titles:
            print(f"    title {tt['local_in']:8.2f} -> {tt['local_out']:8.2f}  "
                  f'"{tt["text"]}"' + (f" / {tt['subtitle']}" if tt["subtitle"] else ""))
        return

    # 1) cut each segment from its camera's original file
    seg_dir = os.path.join(SEG_DIR, name)
    os.makedirs(seg_dir, exist_ok=True)
    listfile = os.path.join(seg_dir, "concat.txt")
    enc = encoder_args(encoder)
    hw = decode_hwaccel(encoder)
    with open(listfile, "w") as lf:
        for s in segments:
            out = os.path.join(seg_dir, f"seg_{s['index']:04d}.mp4")
            # the live camera lives on its own clock: src = concert_t - delta
            seek = s["start"] - s["delta"] if "delta" in s else s["start"]
            run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 *hw,
                 "-ss", f"{seek:.3f}", "-i", src_path(s["camera"]),
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
    audio_src = AUDIO_BED if os.path.isfile(AUDIO_BED) else src_path(audio_cam)
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-ss", f"{t_in:.3f}", "-i", audio_src, "-t", f"{t_out-t_in:.3f}",
         "-vn", "-c:a", "aac", "-b:a", "256k", "-ar", "48000", audio_only])

    out_path = os.path.join(OUT_DIR, name + ".mp4")
    title_vf = title_filter(perf_titles, t_in, t_out, seg_dir, title_scale) if perf_titles else ""
    if title_vf:
        # Burn the titles in -> the video must be re-encoded (can't stream-copy).
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", video_only, "-i", audio_only,
             "-map", "0:v:0", "-map", "1:a:0",
             "-vf", title_vf, *enc, "-r", str(FPS), "-c:a", "copy",
             "-dn", "-write_tmcd", "0", "-movflags", "+faststart", "-shortest", out_path])
    else:
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
    ap.add_argument("--encoder", choices=["auto", "videotoolbox", "nvenc", "x264"],
                    default="auto", help="auto = videotoolbox on macOS, nvenc if available, else x264")
    ap.add_argument("--audio", default=None, help="override audio source camera id")
    args = ap.parse_args()

    encoder = resolve_encoder(args.encoder)

    if not os.path.isfile(args.markers):
        sys.exit(f"No markers file at {args.markers}. Mark performances in the editor first.")
    with open(args.markers) as f:
        data = json.load(f)

    seed = int(data.get("seed", 42))
    audio_cam = args.audio or data.get("audio_source", "back")
    perfs = data.get("performances", [])
    titles = data.get("titles", [])
    title_scale = float(data.get("title_scale") or 1.0)
    if not perfs:
        sys.exit("No performances in markers.json.")

    live_clips = load_live_clips()

    only = {int(x) for x in args.only.split(",") if x.strip()} if args.only else None
    print(f"seed={seed}  audio={audio_cam}  encoder={encoder}  "
          f"performances={len(perfs)}  titles={len(titles)}  "
          f"live-cam clips={len(live_clips)}"
          + (f"  only={sorted(only)}" if only else ""))

    for i, perf in enumerate(perfs):
        if only and (i + 1) not in only:
            continue
        render_performance(perf, i, seed, audio_cam, encoder, args.dry_run, titles, title_scale,
                           live_clips=live_clips)

    print("\nDone." + ("  (dry run — no video encoded)" if args.dry_run else f"  Output in {OUT_DIR}/"))


if __name__ == "__main__":
    main()
