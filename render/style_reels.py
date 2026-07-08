#!/usr/bin/env python3
"""
Style: "Reel" — a portrait (1080x1920) 3-camera stack, cut as ONE compound clip.

Layout (top -> bottom, order from reels.json `layout`):
    livestream  (1080x640)
    piano       (1080x640)
    back        (1080x640)

The cut list comes from reels.json (edited in the reels production interface,
/reels.html): an ordered list of concert-time segments. Every segment cuts all
three cameras AND the audio bed at the same window, so trimming/splitting in
the interface ripples the whole stack. Per-camera `cams` framing (zoom/x/y,
in output pixels, same math as the interface preview) pans each 16:9 source
inside its 1080x640 pane. Per-camera color grades (markers.json
camera_grades) are applied exactly like the main render.

reels.json is a multi-project store; this renders the `active` project (the
one open in the interface) unless --project ID is given.

Run:  python3 render/style_reels.py [--dry-run] [--project ID]
Output: output/reel_<project-name>.mp4 (+ .plan.json)

Progress lines are "cut seg X/Y" / "✓ <file>" — the shared style-render
protocol the editor's export poller parses.
"""
import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render as R   # sources, grades, encoder, audio bed, run()
try:
    import title_image as TI   # Pillow renderer for emoji titles (optional)
except Exception:
    TI = None                  # no Pillow -> emoji titles fall back to drawtext

ROOT = R.ROOT
OUT_DIR = R.OUT_DIR
SEG_DIR = os.path.join(ROOT, "cache", "segments")

DEFAULT_LAYOUT = ["livestream", "piano", "back"]


def slug(s):
    if hasattr(R, "slugify"):
        return R.slugify(s)
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s or "reel"

_DIMS_CACHE = {}


def probe_dims(path):
    """(width, height) of a source file, cached."""
    if path not in _DIMS_CACHE:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
            text=True).strip().split(",")
        _DIMS_CACHE[path] = (int(out[0]), int(out[1]))
    return _DIMS_CACHE[path]


def title_filter(titles, work_dir, gscale, pw, ph):
    """drawtext chain for all titles, in OUTPUT (reel) time.

    Titles live on the reel clock — a title is 'show this text from reel-time
    in..out', independent of the clips beneath it. So this runs once over the
    final concatenated reel (not per segment). fontsize and vertical layout use
    the reel height `ph` (1920); x/y are normalized over the reel frame. '' when
    there are no titles."""
    style = (f"fontfile='{R.TITLE_FONT}':fontcolor=white:borderw=4:"
             f"bordercolor=black@0.9:shadowcolor=black@0.55:shadowx=2:shadowy=3")
    parts = []
    for n, ttl in enumerate(titles):
        if TI and TI.title_needs_image(ttl):
            continue                       # emoji title -> image overlay, not drawtext
        a = max(0.0, float(ttl["in"]))
        b = float(ttl["out"])
        if b - a <= 0.05:
            continue
        fd = min(0.4, max(0.05, (b - a) / 2))
        s = gscale * (float(ttl["scale"]) if ttl.get("scale") else 1.0)
        fs_main, fs_sub = round(ph / 16 * s), round(ph / 27 * s)
        lh_main, lh_sub = fs_main * 1.18, fs_sub * 1.25
        cx = 0.5 if ttl.get("x") is None else float(ttl["x"])
        cy = 0.80 if ttl.get("y") is None else float(ttl["y"])
        x_expr = f"(w*{cx:.4f}-text_w/2)"
        alpha = (f"alpha='if(lt(t,{a:.3f}),0,if(lt(t,{a + fd:.3f}),"
                 f"(t-{a:.3f})/{fd:.3f},if(lt(t,{b - fd:.3f}),1,"
                 f"if(lt(t,{b:.3f}),({b:.3f}-t)/{fd:.3f},0))))'")
        tail = f"x={x_expr}:enable='between(t,{a:.3f},{b:.3f})':{alpha}"
        wrap = ttl.get("wrap") is not False        # default on
        mains = _wrap(ttl.get("text") or "", max(6, round(40 / s)), wrap)
        subs = _wrap(ttl.get("subtitle") or "", max(8, round(56 / s)), wrap)
        gap = fs_main * 0.5 if (mains and subs) else 0
        block_h = len(mains) * lh_main + gap + len(subs) * lh_sub
        y0 = cy * ph - block_h / 2
        for li, line in enumerate(mains):
            tf = os.path.join(work_dir, f"ttl_{n}_m{li}.txt")
            with open(tf, "w") as f:
                f.write(line)
            parts.append(f"drawtext=textfile='{tf}':{style}:fontsize={fs_main}:"
                         f"y={round(y0 + li * lh_main)}:{tail}")
        sy0 = y0 + len(mains) * lh_main + gap
        for li, line in enumerate(subs):
            tf = os.path.join(work_dir, f"ttl_{n}_s{li}.txt")
            with open(tf, "w") as f:
                f.write(line)
            parts.append(f"drawtext=textfile='{tf}':{style}:fontsize={fs_sub}:"
                         f"y={round(sy0 + li * lh_sub)}:{tail}")
    return ",".join(parts)


def emoji_title_pngs(titles, work_dir, gscale, pw, ph):
    """Render each EMOJI title (color emoji can't go through drawtext) to a full
    -frame transparent PNG. Returns [{png, a, b, fd}] for the caller to overlay
    with the matching fade/enable timing. Empty if none / no Pillow."""
    if not TI:
        return []
    out = []
    for n, ttl in enumerate(titles):
        if not TI.title_needs_image(ttl):
            continue
        a = max(0.0, float(ttl["in"]))
        b = float(ttl["out"])
        if b - a <= 0.05:
            continue
        png = os.path.join(work_dir, f"ttl_emoji_{n}.png")
        TI.render_title_png(ttl, png, pw, ph, gscale)
        out.append({"png": png, "a": a, "b": b,
                    "fd": min(0.4, max(0.05, (b - a) / 2))})
    return out


def _wrap(text, max_chars, wrap=True):
    """Lay out text into lines. Explicit '\\n' breaks are always honored; when
    `wrap` is on, each such line is additionally word-wrapped at max_chars.
    Blank lines are dropped (nothing to draw)."""
    text = (text or "").replace("\r", "")
    if not text.strip():
        return []
    out = []
    for raw in text.split("\n"):
        if not raw.strip():
            continue
        if not wrap:
            out.append(raw.strip())
            continue
        cur = ""
        for word in raw.split():
            if cur and len(cur) + 1 + len(word) > max_chars:
                out.append(cur)
                cur = word
            else:
                cur = f"{cur} {word}" if cur else word
        if cur:
            out.append(cur)
    return out


def cam_chain(cam, transform, pane_w, pane_h):
    """Per-camera grade + cover-fit scale/pan-crop into its pane.

    Mirrors the interface preview math exactly: cover-fit the source to the
    pane, multiply by the user zoom, then offset the centered crop window by
    (-x, -y) in output pixels.
    """
    src_w, src_h = probe_dims(R.src_path(cam))
    zoom = max(1.0, float(transform.get("scale", 1.0)))
    x = float(transform.get("x", 0.0))
    y = float(transform.get("y", 0.0))
    cover = max(pane_w / src_w, pane_h / src_h)
    s = cover * zoom
    sw = max(pane_w, int(round(src_w * s / 2)) * 2)
    sh = max(pane_h, int(round(src_h * s / 2)) * 2)
    cx = int(round((sw - pane_w) / 2 - x))
    cy = int(round((sh - pane_h) / 2 - y))
    cx = max(0, min(sw - pane_w, cx))
    cy = max(0, min(sh - pane_h, cy))
    eq = R.eq_filter(R.CAMERA_GRADES.get(cam))
    chain = (f"scale={sw}:{sh},crop={pane_w}:{pane_h}:{cx}:{cy},"
             f"fps={R.FPS},setpts=PTS-STARTPTS")
    return f"{eq},{chain}" if eq else chain


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reels", default=os.path.join(ROOT, "reels.json"))
    ap.add_argument("--markers", default=os.path.join(ROOT, "markers.json"))
    ap.add_argument("--project", default=None,
                    help="reel project id (default: the store's active project)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--encoder", default="auto")
    args = ap.parse_args()

    if not os.path.isfile(args.reels):
        sys.exit("No reels.json — open /reels.html and make at least one edit first.")
    with open(args.reels) as f:
        raw = json.load(f)
    if "projects" in raw:                       # multi-project store
        projs = raw.get("projects") or []
        pid = args.project or raw.get("active")
        doc = next((p for p in projs if p.get("id") == pid),
                   projs[0] if projs else None)
        if doc is None:
            sys.exit("reels.json has no projects — create one at /reels.html.")
    else:                                       # legacy single-doc file
        doc = raw
    pname = str(doc.get("name") or "3stack")
    name = "reel_" + slug(pname)
    if os.path.isfile(args.markers):
        with open(args.markers) as f:
            R.apply_camera_grades(json.load(f).get("camera_grades"))
    encoder = R.resolve_encoder(args.encoder)

    pw = int(doc.get("width", 1080))
    ph = int(doc.get("height", 1920))
    layout = [c for c in (doc.get("layout") or DEFAULT_LAYOUT)]
    cams_cfg = doc.get("cams", {})
    segs = doc.get("segments") or []
    if not segs:
        sys.exit("reels.json has no segments — nothing to render.")

    n = len(layout)
    pane_h = ph // n
    pane_hs = [pane_h] * n
    pane_hs[-1] = ph - pane_h * (n - 1)   # absorb any rounding remainder

    total = sum(float(s["out"]) - float(s["in"]) for s in segs)
    print(f"style=reels  encoder={encoder}  segments={len(segs)}  "
          f"portrait {pw}x{ph}  stack={'+'.join(layout)}  reel {total:.1f}s")

    os.makedirs(OUT_DIR, exist_ok=True)
    plan = {"style": "reels", "layout": layout, "cams": cams_cfg,
            "segments": [{"in": s["in"], "out": s["out"]} for s in segs],
            "duration": round(total, 3)}
    with open(os.path.join(OUT_DIR, name + ".plan.json"), "w") as f:
        json.dump(plan, f, indent=2)
    for k, s in enumerate(segs):
        print(f"  seg {k+1:3d}  {float(s['in']):8.2f} - {float(s['out']):8.2f}"
              f"  ({float(s['out']) - float(s['in']):6.2f}s)")
    if args.dry_run:
        print("(dry run — no video encoded)")
        return

    seg_dir = os.path.join(SEG_DIR, name)
    os.makedirs(seg_dir, exist_ok=True)
    enc = R.encoder_args(encoder)
    hw = R.decode_hwaccel(encoder)
    audio_src = R.AUDIO_BED if os.path.isfile(R.AUDIO_BED) else R.src_path("back")

    # Per-camera pane chains are cut-independent — build them once.
    chains = [cam_chain(cam, cams_cfg.get(cam, {}), pw, pane_hs[i])
              for i, cam in enumerate(layout)]
    titles = doc.get("titles") or []
    tscale = float(doc.get("title_scale") or 1.0)

    vlist = os.path.join(seg_dir, "concat_v.txt")
    alist = os.path.join(seg_dir, "concat_a.txt")
    with open(vlist, "w") as vf_, open(alist, "w") as af_:
        for k, s in enumerate(segs):
            t_in, t_out = float(s["in"]), float(s["out"])
            dur = t_out - t_in
            inputs = []
            for cam in layout:
                inputs += [*hw, "-ss", f"{t_in:.3f}", "-i", R.src_path(cam)]
            fc = ";".join(f"[{i}:v]{chains[i]}[v{i}]" for i in range(n))
            stack = "".join(f"[v{i}]" for i in range(n))
            fc += (f";{stack}vstack=inputs={n},format=yuv420p,"
                   f"setpts=PTS-STARTPTS[v]")
            vout = os.path.join(seg_dir, f"seg_{k:03d}.mp4")
            R.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                   *inputs, "-t", f"{dur:.3f}", "-filter_complex", fc,
                   "-map", "[v]", "-an", "-dn", *enc,
                   "-r", str(R.FPS), "-g", str(R.FPS), "-write_tmcd", "0",
                   "-video_track_timescale", "60000", vout])
            aout = os.path.join(seg_dir, f"seg_{k:03d}.wav")
            R.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                   "-ss", f"{t_in:.3f}", "-i", audio_src, "-t", f"{dur:.3f}",
                   "-vn", "-ac", "2", "-ar", "48000", aout])
            vf_.write(f"file '{os.path.abspath(vout)}'\n")
            af_.write(f"file '{os.path.abspath(aout)}'\n")
            print(f"    cut seg {k+1}/{len(segs)}   ")

    # Concat the graded segments. Titles burn in one pass over the whole reel
    # (OUTPUT time) — independent of the clips beneath. Plain titles go through
    # drawtext; titles containing color emoji are pre-rendered to PNGs (drawtext
    # can't draw color emoji) and overlaid with the same fade/enable timing.
    tvf = title_filter(titles, seg_dir, tscale, pw, ph)
    emojis = emoji_title_pngs(titles, seg_dir, tscale, pw, ph)
    video_only = os.path.join(seg_dir, "video.mp4")
    if tvf or emojis:
        reel_dur = sum(float(s["out"]) - float(s["in"]) for s in segs)
        inputs = ["-f", "concat", "-safe", "0", "-i", vlist]
        for e in emojis:
            # loop the static PNG into a stream bounded to the reel length so it
            # ends cleanly (an unbounded -loop never terminates)
            inputs += ["-loop", "1", "-t", f"{reel_dur:.3f}", "-i", e["png"]]
        # build filter_complex: [0:v] -> optional drawtext -> chain of overlays
        chain = f"[0:v]{tvf}[bg0]" if tvf else "[0:v]null[bg0]"
        parts = [chain]
        cur = "bg0"
        for i, e in enumerate(emojis):
            a, b, fd = e["a"], e["b"], e["fd"]
            # the looped image runs on the output clock, so fade its alpha in at
            # `a` and out at `b-fd` to match the text ramp; overlay `enable`
            # gates it to [a,b]. (No setpts — it desyncs the loop's clock.)
            fade = (f"[{i + 1}:v]format=yuva420p,"
                    f"fade=t=in:st={a:.3f}:d={fd:.3f}:alpha=1,"
                    f"fade=t=out:st={b - fd:.3f}:d={fd:.3f}:alpha=1[e{i}]")
            parts.append(fade)
            nxt = f"v{i}" if i < len(emojis) - 1 else "vout"
            parts.append(f"[{cur}][e{i}]overlay=0:0:enable='between(t,{a:.3f},{b:.3f})'[{nxt}]")
            cur = nxt
        fc = ";".join(parts)
        R.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
               "-filter_complex", fc, "-map", "[vout]" if emojis else "[bg0]",
               *enc, "-r", str(R.FPS), "-g", str(R.FPS), "-shortest",
               "-write_tmcd", "0", "-video_track_timescale", "60000",
               video_only])
    else:
        R.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "concat", "-safe", "0", "-i", vlist, "-c", "copy",
               video_only])
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
