#!/usr/bin/env python3
"""
Render a reel title (main + optional subtitle, multi-line) to a single
transparent PNG, matching the editor's on-screen look — white text, black
stroke + soft shadow — and drawing color EMOJI as images (ffmpeg's drawtext
can't render color-bitmap emoji, so we composite the whole line here instead).

Used by render/style_reels.py: it produces one PNG per title and ffmpeg
`overlay`s it at the title's (x,y) with the fade/enable timing, so emoji burn
in exactly as previewed.

Geometry mirrors style_reels.title_filter:
  fs_main = round(H/16 * s),  fs_sub = round(H/27 * s)   (s = global*title scale)
  line heights 1.18 / 1.25, sub gap = fs_main*0.5
  block centered on the title's (x*W, y*H)
The PNG is the full block; the caller overlays its top-left so the block's
center lands on (x*W, y*H).
"""
import os
import re

from PIL import Image, ImageFont, ImageDraw

# Text font: bold sans to match the editor (Arial Bold / DejaVu Bold).
_TEXT_CANDIDATES = [
    os.environ.get("TITLE_TEXT_FONT", ""),
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]
TEXT_FONT = next((p for p in _TEXT_CANDIDATES if p and os.path.isfile(p)), None)

# Color emoji font (Apple on mac; Noto Color Emoji on Linux). Bitmap strikes
# only exist at fixed sizes — we render at the nearest strike and scale.
_EMOJI_CANDIDATES = [
    os.environ.get("TITLE_EMOJI_FONT", ""),
    "/System/Library/Fonts/Apple Color Emoji.ttc",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/NotoColorEmoji.ttf",
]
EMOJI_FONT = next((p for p in _EMOJI_CANDIDATES if p and os.path.isfile(p)), None)
_APPLE_EMOJI_STRIKE = 160    # Apple Color Emoji built-in strike size

# Match drawtext: borderw≈4 (scaled with font), shadow offset 2/3.
STROKE_RATIO = 0.055         # stroke width as a fraction of font size
SHADOW = (0.028, 0.042)      # shadow dx,dy as fractions of font size

# Codepoint ranges that are actual color emoji (NOT general punctuation like
# … or — which the text font handles). Includes emoji blocks, dingbats,
# regional indicators, skin-tone modifiers, ZWJ (200D) and VS16 (FE0F) joiners.
_EMOJI_RE = re.compile(
    "((?:[\U0001F000-\U0001FAFF\U00002600-\U000026FF\U00002700-\U000027BF"
    "\U0001F1E6-\U0001F1FF\U00002B00-\U00002BFF\U0001F3FB-\U0001F3FF"
    "\U0000FE0F\U0000200D\U00002190-\U000021FF"
    "\U00002139\U00002194-\U000021AA\U0000231A-\U0000231B"
    "\U000023E9-\U000023FA\U000025AA-\U000025FE\U00002934-\U00002935"
    "\U00002B05-\U00002B07❤‼⁉™⭐⭕]+))")


def _has_emoji(s):
    return bool(_EMOJI_RE.search(s or "")) and EMOJI_FONT is not None


def title_needs_image(title):
    """True if any of the title's text needs the Pillow path (has emoji)."""
    return _has_emoji(title.get("text")) or _has_emoji(title.get("subtitle"))


def _segments(s):
    """Split a string into (text, is_emoji) runs, preserving order."""
    out, last = [], 0
    for m in _EMOJI_RE.finditer(s or ""):
        if m.start() > last:
            out.append((s[last:m.start()], False))
        out.append((m.group(0), True))
        last = m.end()
    if last < len(s or ""):
        out.append((s[last:], False))
    return out or [("", False)]


def _emoji_img(ch, px):
    """Rasterize one emoji cluster to an RGBA image ~px tall."""
    strike = _APPLE_EMOJI_STRIKE if "Apple" in EMOJI_FONT else px
    try:
        font = ImageFont.truetype(EMOJI_FONT, strike)
    except OSError:
        font = ImageFont.truetype(EMOJI_FONT, px)
    canvas = Image.new("RGBA", (strike * 2, int(strike * 1.4)), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    try:
        d.text((0, 0), ch, font=font, embedded_color=True)
    except Exception:
        return None
    bbox = canvas.getbbox()
    if not bbox:
        return None
    glyph = canvas.crop(bbox)
    scale = px / glyph.height
    return glyph.resize((max(1, round(glyph.width * scale)), px), Image.LANCZOS)


def _measure_line(segs, text_font, px):
    """(width, ascent-ish height) of a mixed text/emoji line."""
    w = 0
    dummy = Image.new("RGBA", (4, 4))
    dd = ImageDraw.Draw(dummy)
    for txt, is_emoji in segs:
        if is_emoji:
            for ch in _emoji_clusters(txt):
                img = _emoji_img(ch, px)
                if img:
                    w += img.width + round(px * 0.04)
        elif txt:
            w += dd.textlength(txt, font=text_font)
    return w


def _emoji_clusters(s):
    """Split an emoji run into individual clusters (keep ZWJ/VS sequences)."""
    # simple: split on whitespace-free grapheme-ish boundaries; good enough for
    # typical single emoji. Keep combined sequences (ZWJ 200D, VS16 FE0F).
    out, cur = [], ""
    for ch in s:
        if cur and ch not in "‍️" and not (0x1F3FB <= ord(ch) <= 0x1F3FF) \
           and not cur.endswith("‍"):
            out.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def _draw_line(base, segs, cx, y, text_font, px, stroke):
    """Draw one centered line (text + emoji) onto `base` at vertical y."""
    lw = _measure_line(segs, text_font, px)
    x = cx - lw / 2
    d = ImageDraw.Draw(base)
    sdx, sdy = round(px * SHADOW[0]), round(px * SHADOW[1])
    for txt, is_emoji in segs:
        if is_emoji:
            for ch in _emoji_clusters(txt):
                img = _emoji_img(ch, px)
                if img:
                    base.alpha_composite(img, (round(x), round(y)))
                    x += img.width + round(px * 0.04)
        elif txt:
            # shadow
            d.text((x + sdx, y + sdy), txt, font=text_font, fill=(0, 0, 0, 140))
            # stroked white text
            d.text((x, y), txt, font=text_font, fill=(255, 255, 255, 255),
                   stroke_width=stroke, stroke_fill=(0, 0, 0, 230))
            x += d.textlength(txt, font=text_font)


def render_title_png(title, out_path, W, H, gscale):
    """Render `title` (main+sub, wrapped per its `wrap` flag) to a transparent
    PNG the size of the whole reel frame (WxH), with the text block centered on
    (x*W, y*H). Returns (out_path, W, H) so the caller overlays at (0,0)."""
    s = gscale * (float(title.get("scale") or 1.0))
    fs_main, fs_sub = round(H / 16 * s), round(H / 27 * s)
    lh_main, lh_sub = fs_main * 1.18, fs_sub * 1.25
    cx = 0.5 if title.get("x") is None else float(title["x"])
    cy = 0.80 if title.get("y") is None else float(title["y"])
    wrap = title.get("wrap") is not False
    main_font = ImageFont.truetype(TEXT_FONT, fs_main)
    sub_font = ImageFont.truetype(TEXT_FONT, fs_sub)

    mains = _wrap(title.get("text") or "", max(6, round(40 / s)), wrap)
    subs = _wrap(title.get("subtitle") or "", max(8, round(56 / s)), wrap)
    gap = fs_main * 0.5 if (mains and subs) else 0
    block_h = len(mains) * lh_main + gap + len(subs) * lh_sub
    y0 = cy * H - block_h / 2

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    stroke_main = max(1, round(fs_main * STROKE_RATIO))
    stroke_sub = max(1, round(fs_sub * STROKE_RATIO))
    xc = cx * W
    for i, line in enumerate(mains):
        _draw_line(img, _segments(line), xc, y0 + i * lh_main, main_font, fs_main, stroke_main)
    sy0 = y0 + len(mains) * lh_main + gap
    for i, line in enumerate(subs):
        _draw_line(img, _segments(line), xc, sy0 + i * lh_sub, sub_font, fs_sub, stroke_sub)

    img.save(out_path)
    return out_path


def _wrap(text, max_chars, wrap=True):
    """Same wrapping as style_reels._wrap (newline-aware + optional word-wrap).
    Emoji count as 2 chars for width budgeting so lines don't overpack."""
    text = (text or "").replace("\r", "")
    if not text.strip():
        return []
    out = []
    for raw in text.split("\n"):
        if not raw.strip():
            continue
        if not wrap:
            out.append(raw)
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


if __name__ == "__main__":
    # quick visual smoke test
    render_title_png(
        {"text": "Audience thinks its\njust Fur Elise again… 😴",
         "subtitle": "", "x": 0.5, "y": 0.5, "scale": 1.1, "wrap": False},
        "/tmp/title_test.png", 1080, 1920, 1.0)
    print("wrote /tmp/title_test.png")
