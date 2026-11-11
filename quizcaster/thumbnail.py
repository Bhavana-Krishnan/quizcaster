"""Template thumbnails (1280x720 PNG) drawn with Pillow: theme colours, topic, mascot."""
from __future__ import annotations

import io
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

THEMES = {
    "light": {"bg1": (255, 216, 77), "bg2": (255, 185, 56), "fg": (27, 20, 70), "accent": (27, 20, 70),
              "card": (255, 255, 255), "mascot": (255, 122, 47)},
    "dark":  {"bg1": (22, 22, 38), "bg2": (52, 38, 92), "fg": (245, 243, 255), "accent": (255, 196, 0),
              "card": (40, 36, 66), "mascot": (255, 196, 0)},
    "neon":  {"bg1": (10, 4, 30), "bg2": (90, 0, 120), "fg": (255, 255, 255), "accent": (0, 255, 200),
              "card": (30, 10, 60), "mascot": (255, 64, 200)},
}

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/google-fonts/Poppins-ExtraBold.ttf",
    "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def _font(size: int) -> ImageFont.FreeTypeFont:
    for p in _FONT_CANDIDATES:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size=size)


def _gradient(w: int, h: int, c1, c2) -> Image.Image:
    img = Image.new("RGB", (w, h), c1)
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(c1, c2)))
    return img


def _mascot(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int, color, fg) -> None:
    """Quizzy: a round buddy with a question-mark antenna (original character)."""
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color, outline=(20, 16, 50), width=max(5, r // 18))
    eye = r // 6
    for dx in (-r // 3, r // 3):
        d.ellipse([cx + dx - eye, cy - r // 5 - eye, cx + dx + eye, cy - r // 5 + eye], fill=(255, 255, 255))
        d.ellipse([cx + dx - eye // 2, cy - r // 5 - eye // 2, cx + dx + eye // 2, cy - r // 5 + eye // 2], fill=(20, 20, 30))
    d.arc([cx - r // 2, cy - r // 6, cx + r // 2, cy + r // 2], 20, 160, fill=(20, 20, 30), width=max(4, r // 14))
    d.line([(cx, cy - r), (cx, cy - r - r // 3)], fill=fg, width=max(4, r // 14))
    f = _font(int(r * 0.55))
    d.text((cx, cy - r - r // 3 - r // 4), "?", font=f, fill=fg, anchor="mm")


def make_thumbnail(topic: str, theme: str = "dark", channel: str = "") -> bytes:
    t = THEMES.get(theme, THEMES["dark"])
    W, H = 1280, 720
    img = _gradient(W, H, t["bg1"], t["bg2"])
    d = ImageDraw.Draw(img)
    # LIVE pill
    d.rounded_rectangle([60, 56, 300, 126], radius=35, fill=(230, 33, 23))
    d.ellipse([84, 79, 108, 103], fill=(255, 255, 255))
    d.text((200, 91), "LIVE", font=_font(44), fill=(255, 255, 255), anchor="mm")
    d.text((60, 280), "QUIZ", font=_font(150), fill=t["accent"], anchor="ls")
    lines = textwrap.wrap(topic.upper(), width=16)[:3] or ["TRIVIA"]
    size = 88 if len(lines) == 1 else 72 if len(lines) == 2 else 58
    y = 380
    for line in lines:
        d.text((60, y), line, font=_font(size), fill=t["fg"], anchor="ls")
        y += int(size * 1.15)
    d.rounded_rectangle([60, 600, 700, 670], radius=20, fill=t["card"])
    d.text((90, 635), "Answer in chat • A B C D", font=_font(40), fill=t["fg"], anchor="lm")
    _mascot(d, 1010, 400, 190, t["mascot"], t["fg"])
    if channel:
        d.text((W - 40, H - 30), channel[:40], font=_font(30), fill=t["fg"], anchor="rs")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
