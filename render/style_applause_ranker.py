#!/usr/bin/env python3
"""
Style: "Applause Ranker" — a portrait (1080x1920) YouTube Short.

For every performance that has an 'applause' region with a rank, show:
  1. a 3-5s showcase of the piece   (its 'highlight' region if marked,
                                     else the moment just before the applause)
  2. 3-5s of the applause itself    (the 'applause' region, capped)
with the piece title on top and a big "7/10"-style rank burned in. Segments
play in RANK-ASCENDING order so the video builds to the best reaction.

Data comes from markers.json (see editor: performances + regions). Camera
defaults to the back camera; a region's `cam` field overrides it. Per-camera
color grades (camera_grades) are applied exactly like the main render.

Run:  python3 render/style_applause_ranker.py [--dry-run] [--markers PATH]
Output: output/short_applause-ranker.mp4 (+ .plan.json)

Progress lines are "cut seg X/Y" / "✓ <file>" — the same shapes the editor's
export poller already parses, so any style script that prints them gets a
progress bar for free.
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render as R   # sources, grades, encoder, fonts, audio bed, run()

ROOT = R.ROOT
OUT_DIR = R.OUT_DIR
SEG_DIR = os.path.join(ROOT, "cache", "segments")

# Portrait geometry (9:16 short) — center-crop from the 16:9 sources.
PW, PH = 1080, 1920

SHOW_LEN = 4.0        # seconds of the piece to show (when no highlight marked)
SHOW_GAP = 1.0        # end the default showcase this long before the applause
APPLAUSE_MIN = 2.0
APPLAUSE_MAX = 5.0
DEFAULT_CAM = "back"


def portrait_vf(cam):
    """Per-camera grade + portrait scale/crop, mirroring vf_for()'s structure."""
    eq = R.eq_filter(R.CAMERA_GRADES.get(cam))
    chain = (f"scale=-2:{PH},crop={PW}:{PH},fps={R.FPS},"
             f"format=yuv420p,setpts=PTS-STARTPTS")
    return f"{eq},{chain}" if eq else chain


def drawtext_overlays(title_file, rank_text, dur):
    """Title top + big rank bottom, present for the whole clip."""
    style = (f"fontfile='{R.TITLE_FONT}':fontcolor=white:borderw=4:"
             f"bordercolor=black@0.9:shadowcolor=black@0.55:shadowx=2:shadowy=3")
    parts = [
        # piece title, centered near the top
        (f"drawtext={style}:textfile='{title_file}':fontsize={round(PH/24)}:"
         f"x=(w-text_w)/2:y={round(PH*0.09)}"),
        # big rank, lower third
        (f"drawtext={style}:text='{rank_text}':fontsize={round(PH/8)}:"
         f"x=(w-text_w)/2:y={round(PH*0.72)}"),
    ]
    return ",".join(parts)


def collect_entries(data):
    """[(perf_dict, applause_region, highlight_region|None)] for every ranked
    applause region, rank ASCENDING (build to the best). Unranked applause is
    skipped with a warning so a half-marked project still renders."""
    perfs = data.get("performances", [])
    regions = data.get("regions", [])
    entries, skipped = [], []
    for rg in regions:
        if rg.get("kind") != "applause":
            continue
        p = rg.get("perf")
        if p is None or not (0 <= int(p) < len(perfs)):
            skipped.append(f"applause @{rg['in']:.1f}s: no valid perf index")
            continue
        if rg.get("rank") is None:
            skipped.append(f"applause for #{int(p)+1}: no rank set")
            continue
        hl = next((h for h in regions
                   if h.get("kind") == "highlight" and h.get("perf") == p), None)
        entries.append((int(p), perfs[int(p)], rg, hl))
    for s in skipped:
        print(f"  ! skipped: {s}")
    entries.sort(key=lambda e: (float(e[2]["rank"]), e[2]["in"]))
    return entries


def windows_for(perf, applause, highlight):
    """(show_in, show_out, ap_in, ap_out, cam) concert-time windows."""
    ap_in, ap_out = float(applause["in"]), float(applause["out"])
    ap_out = min(ap_out, ap_in + APPLAUSE_MAX)
    if ap_out - ap_in < APPLAUSE_MIN:
        ap_out = ap_in + APPLAUSE_MIN
    if highlight:
        s_in, s_out = float(highlight["in"]), float(highlight["out"])
    else:
        s_out = max(float(perf["in"]) + SHOW_LEN, ap_in - SHOW_GAP)
        s_in = max(float(perf["in"]), s_out - SHOW_LEN)
    cam = applause.get("cam") or (highlight.get("cam") if highlight else None) or DEFAULT_CAM
    return s_in, s_out, ap_in, ap_out, cam


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--markers", default=os.path.join(ROOT, "markers.json"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--encoder", default="auto")
    args = ap.parse_args()

    with open(args.markers) as f:
        data = json.load(f)
    R.apply_camera_grades(data.get("camera_grades"))
    encoder = R.resolve_encoder(args.encoder)

    entries = collect_entries(data)
    if not entries:
        sys.exit("No ranked applause regions. Mark applause (+ rank) in the editor first.")

    name = "short_applause-ranker"
    print(f"style=applause_ranker  encoder={encoder}  pieces={len(entries)}  "
          f"portrait {PW}x{PH}")

    # Build the flat cut list: 2 clips (showcase, applause) per entry.
    cuts = []   # {t_in, t_out, cam, title, rank_text}
    plan = []
    for (pi, perf, rg, hl) in entries:
        s_in, s_out, a_in, a_out, cam = windows_for(perf, rg, hl)
        rank_text = f"{rg['rank']:g}\\/10"        # escape '/' for drawtext
        title = perf.get("title") or f"Piece {pi+1}"
        cuts.append({"t_in": s_in, "t_out": s_out, "cam": cam,
                     "title": title, "rank": rank_text, "part": "showcase"})
        cuts.append({"t_in": a_in, "t_out": a_out, "cam": cam,
                     "title": title, "rank": rank_text, "part": "applause"})
        plan.append({"perf": pi + 1, "title": title, "rank": rg["rank"],
                     "showcase": [round(s_in, 3), round(s_out, 3)],
                     "applause": [round(a_in, 3), round(a_out, 3)], "cam": cam})
        print(f"  #{pi+1:2d} rank {rg['rank']:>4}  {title[:32]:32s} "
              f"show {s_in:7.1f}-{s_out:7.1f}  applause {a_in:7.1f}-{a_out:7.1f} [{cam}]")

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, name + ".plan.json"), "w") as f:
        json.dump({"style": "applause_ranker", "entries": plan}, f, indent=2)
    if args.dry_run:
        print("(dry run — no video encoded)")
        return

    seg_dir = os.path.join(SEG_DIR, name)
    os.makedirs(seg_dir, exist_ok=True)
    enc = R.encoder_args(encoder)
    hw = R.decode_hwaccel(encoder)
    audio_src = R.AUDIO_BED if os.path.isfile(R.AUDIO_BED) else R.src_path(
        data.get("audio_source", DEFAULT_CAM))

    vlist = os.path.join(seg_dir, "concat_v.txt")
    alist = os.path.join(seg_dir, "concat_a.txt")
    with open(vlist, "w") as vf_, open(alist, "w") as af_:
        for k, c in enumerate(cuts):
            dur = c["t_out"] - c["t_in"]
            title_file = os.path.join(seg_dir, f"title_{k:03d}.txt")
            with open(title_file, "w") as tf:
                tf.write(c["title"])
            vf = f"{portrait_vf(c['cam'])},{drawtext_overlays(title_file, c['rank'], dur)}"
            vout = os.path.join(seg_dir, f"seg_{k:03d}.mp4")
            R.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *hw,
                   "-ss", f"{c['t_in']:.3f}", "-i", R.src_path(c["cam"]),
                   "-t", f"{dur:.3f}", "-an", "-dn", "-vf", vf, *enc,
                   "-r", str(R.FPS), "-g", str(R.FPS), "-write_tmcd", "0",
                   "-video_track_timescale", "60000", vout])
            aout = os.path.join(seg_dir, f"seg_{k:03d}.wav")
            R.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                   "-ss", f"{c['t_in']:.3f}", "-i", audio_src, "-t", f"{dur:.3f}",
                   "-vn", "-ac", "2", "-ar", "48000", aout])
            vf_.write(f"file '{os.path.abspath(vout)}'\n")
            af_.write(f"file '{os.path.abspath(aout)}'\n")
            print(f"    cut seg {k+1}/{len(cuts)} [{c['part']}]   ")

    video_only = os.path.join(seg_dir, "video.mp4")
    R.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", vlist, "-c", "copy", video_only])
    audio_only = os.path.join(seg_dir, "audio.m4a")
    R.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", alist,
           "-c:a", "aac", "-b:a", "256k", "-ar", "48000", audio_only])

    out_path = os.path.join(OUT_DIR, name + ".mp4")
    R.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-i", video_only, "-i", audio_only,
           "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "copy",
           "-dn", "-write_tmcd", "0", "-movflags", "+faststart", "-shortest",
           out_path])
    print(f"  ✓ {out_path}")


if __name__ == "__main__":
    main()
