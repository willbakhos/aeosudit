"""Programmatic 1200x630 hero image for cold-email bodies, OG cards, and the
post-audit delivery email. Mirrors the report-hero design so the visual is
continuous from cold email → click → loading → report.

Layout:
  ┌──────────────────────────────────┬───────────────────────────┐
  │ AI VISIBILITY AUDIT  (pill)      │  ┌─────────────────────┐  │
  │                                  │  │ ● ● ●  https://...  │  │
  │ {Headline that adapts to         │  ├─────────────────────┤  │
  │  visibility — invisible /        │  │                     │  │
  │  barely showing / appears}       │  │  site screenshot    │  │
  │                                  │  │                     │  │
  │ Asked Google "best {category}"   │  │                     │  │
  │ Top answer named: A, B, C        │  └─────────────────────┘  │
  │                                  │                           │
  │ ┌───────────┬───────────┐        │                           │
  │ │ VIS  0%   │ CITED  0% │        │                           │
  │ └───────────┴───────────┘        │                           │
  │                                  │                           │
  │ monitoraeo.com                   │                           │
  └──────────────────────────────────┴───────────────────────────┘
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# OG-card dimensions — works for Twitter, LinkedIn, Slack, every email client.
W, H = 1200, 630

# Palette — matches the report hero CSS exactly.
INK = (15, 23, 42)              # slate-900
INK_2 = (30, 41, 59)             # slate-800
MUTED = (148, 163, 184)          # slate-400
SOFT = (226, 232, 240)           # slate-200
BLUE = (37, 99, 235)
PURPLE = (124, 58, 237)
CYAN = (6, 182, 212)
WARNING = (217, 119, 6)
DANGER = (220, 38, 38)
SUCCESS = (22, 163, 74)
WHITE = (255, 255, 255)
HERO_BLUE = (191, 219, 254)      # blue-200 — used for eyebrow + accent text

# Bundled Inter font files live under static/fonts/ in the repo so the
# rendering is identical across local dev and Railway's stripped-down
# container. Path is computed relative to this file (src/teaser_image.py).
_FONT_DIR = Path(__file__).resolve().parent.parent / "static" / "fonts"
_FONT_FILES = {
    "regular": _FONT_DIR / "Inter-Regular.ttf",
    "semibold": _FONT_DIR / "Inter-SemiBold.ttf",
    "bold": _FONT_DIR / "Inter-Bold.ttf",
    "black": _FONT_DIR / "Inter-Black.ttf",
}


def _font(size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
    """Load Inter at the requested size/weight. Falls back through the other
    weights, then DejaVu (Linux), then PIL default — but in practice the
    bundled files always exist so the fallback chain is just a safety net."""
    path = _FONT_FILES.get(weight)
    if path and path.exists():
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            pass
    # Try the other bundled weights as fallback
    for other in _FONT_FILES.values():
        if other.exists():
            try:
                return ImageFont.truetype(str(other), size)
            except OSError:
                continue
    # Last resort — system DejaVu (often present on Linux containers)
    for sys_path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]:
        if Path(sys_path).exists():
            try:
                return ImageFont.truetype(sys_path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _draw_gradient_rect(
    img: Image.Image,
    box: tuple[int, int, int, int],
    color_a: tuple[int, int, int],
    color_b: tuple[int, int, int],
    radius: int = 0,
) -> None:
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    grad = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(color_a[0] + (color_b[0] - color_a[0]) * t)
        g = int(color_a[1] + (color_b[1] - color_a[1]) * t)
        b = int(color_a[2] + (color_b[2] - color_a[2]) * t)
        grad.putpixel((0, y), (r, g, b))
    grad = grad.resize((w, h))
    if radius:
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w, h), radius=radius, fill=255)
        img.paste(grad, (x0, y0), mask)
    else:
        img.paste(grad, (x0, y0))


def _wrap_lines(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    draw: ImageDraw.ImageDraw,
    max_lines: int | None = None,
) -> list[str]:
    """Greedy word-wrap to fit max_width pixels."""
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = current + " " + word
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
            if max_lines and len(lines) >= max_lines:
                # Append an ellipsis to the last accepted line and stop
                last = lines[-1]
                while draw.textlength(last + "…", font=font) > max_width and last:
                    last = last[:-1]
                lines[-1] = last + "…"
                return lines
    lines.append(current)
    return lines


def _truncate(
    text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw
) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def _format_competitor_list(competitors: Iterable[str], max_n: int = 3) -> str:
    items = [c for c in competitors if c][:max_n]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _headline_for(brand: str, visibility_pct: float, has_competitors: bool) -> str:
    """Mirror the report hero's headline logic so the message is consistent
    from cold email teaser → free preview report."""
    if visibility_pct == 0:
        if has_competitors:
            return f"{brand} is invisible in Google AI Overviews."
        return f"{brand} did not appear in Google AI Overviews."
    if visibility_pct < 50:
        return f"{brand} is barely showing up in Google AI Overviews."
    return f"{brand} appears in Google AI — but who else is being cited?"


def _draw_browser_screenshot(
    img: Image.Image,
    site_screenshot: Path | None,
    domain: str,
    box: tuple[int, int, int, int],
) -> None:
    """Render a browser-frame mock with the site screenshot inside `box`."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0

    # Outer card with rounded corners
    card = Image.new("RGB", (w, h), (30, 41, 59))
    cd = ImageDraw.Draw(card)
    cd.rounded_rectangle((0, 0, w, h), radius=22, fill=(30, 41, 59))

    # Browser bar
    bar_h = 40
    cd.rectangle((0, 0, w, bar_h), fill=(15, 23, 42))
    for cx in [22, 44, 66]:
        cd.ellipse((cx - 5, bar_h // 2 - 5, cx + 5, bar_h // 2 + 5), fill=(75, 85, 99))
    # URL pill
    url_text = f"https://{domain}"
    url_font = _font(13, "semibold")
    pill_pad_x, pill_pad_y = 12, 5
    text_w = cd.textlength(url_text, font=url_font)
    pill_w = min(int(text_w + pill_pad_x * 2), w - 100)
    pill_x = 90
    pill_y = bar_h // 2 - 12
    cd.rounded_rectangle(
        (pill_x, pill_y, pill_x + pill_w, pill_y + 24), radius=12, fill=(30, 41, 59)
    )
    cd.text(
        (pill_x + pill_pad_x, pill_y + pill_pad_y - 1),
        _truncate(url_text, url_font, pill_w - pill_pad_x * 2, cd),
        font=url_font,
        fill=(203, 213, 225),
    )

    # Screenshot area
    shot_w, shot_h = w, h - bar_h
    if site_screenshot and site_screenshot.exists():
        try:
            shot = Image.open(site_screenshot).convert("RGB")
            ratio = max(shot_w / shot.width, shot_h / shot.height)
            new_size = (int(shot.width * ratio), int(shot.height * ratio))
            shot = shot.resize(new_size, Image.Resampling.LANCZOS)
            crop_x = (shot.width - shot_w) // 2
            shot = shot.crop((crop_x, 0, crop_x + shot_w, shot_h))
            card.paste(shot, (0, bar_h))
        except (OSError, ValueError):
            pass
    else:
        # Subtle placeholder gradient when no screenshot
        ph = Image.new("RGB", (shot_w, shot_h))
        for y in range(shot_h):
            t = y / shot_h
            r = int(30 + (15 - 30) * t)
            g = int(41 + (23 - 41) * t)
            b = int(59 + (42 - 59) * t)
            for x in range(shot_w):
                ph.putpixel((x, y), (r, g, b))
        card.paste(ph, (0, bar_h))

    # Round the outer corners
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w, h), radius=22, fill=255)
    out = Image.new("RGB", (w, h))
    out.paste(card, (0, 0), mask)

    # Soft drop shadow
    shadow_size = (w + 60, h + 60)
    shadow = Image.new("RGBA", shadow_size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle((30, 30, 30 + w, 30 + h), radius=22, fill=(0, 0, 0, 90))
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    img.paste(shadow, (x0 - 30, y0 - 24), shadow)
    img.paste(out, (x0, y0), mask)


def _draw_eyebrow(draw: ImageDraw.ImageDraw, x: int, y: int, text: str) -> int:
    """Pill-style eyebrow chip, returns its height."""
    font = _font(13, "bold")
    text_w = draw.textlength(text, font=font)
    pad_x, pad_y, h = 14, 8, 30
    draw.rounded_rectangle(
        (x, y, x + int(text_w) + pad_x * 2, y + h),
        radius=15,
        fill=None,
        outline=(255, 255, 255, 70),
        width=1,
    )
    # Render letter-spaced for a "tracking" feel
    spaced = " ".join(text)
    draw.text(
        (x + pad_x, y + pad_y - 2),
        text,
        font=font,
        fill=HERO_BLUE,
    )
    return h


def _draw_metric_tile(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    label: str,
    value: str,
    accent: tuple[int, int, int] = WHITE,
) -> None:
    """Translucent tile with small label + big value (matches report hero mini-stats)."""
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=14, fill=(255, 255, 255, 18), outline=(255, 255, 255, 35), width=1)
    label_font = _font(11, "bold")
    value_font = _font(34, "black")
    draw.text((x0 + 16, y0 + 14), label, font=label_font, fill=(191, 219, 254))
    draw.text((x0 + 16, y0 + 30), value, font=value_font, fill=accent)


def generate(
    *,
    brand_name: str,
    domain: str,
    visibility_pct: float,
    competitors: list[str],
    site_screenshot: Path | None,
    output_path: Path,
    citation_pct: float | None = None,
    category: str | None = None,
) -> Path:
    """Compose the hero image and write it to output_path. Returns the path."""
    img = Image.new("RGBA", (W, H), (248, 251, 255, 255))
    draw = ImageDraw.Draw(img)

    # Page background — radial glows behind the hero card
    bg_overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bg_overlay)
    bd.ellipse((-200, -300, 700, 600), fill=(37, 99, 235, 38))
    bd.ellipse((700, -200, 1500, 700), fill=(124, 58, 237, 32))
    bg_overlay = bg_overlay.filter(ImageFilter.GaussianBlur(120))
    img.alpha_composite(bg_overlay)

    # Main hero card
    hero_box = (40, 40, W - 40, H - 40)
    _draw_gradient_rect(
        img,
        hero_box,
        color_a=(15, 23, 42),
        color_b=(30, 64, 175),
        radius=34,
    )

    # Cyan glow in the top-right of the hero
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((W - 600, -100, W + 100, 500), fill=(6, 182, 212, 60))
    glow = glow.filter(ImageFilter.GaussianBlur(80))
    # Mask the glow to inside the hero rectangle so it doesn't bleed outside
    hero_mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(hero_mask).rounded_rectangle(hero_box, radius=34, fill=255)
    img.paste(glow, (0, 0), Image.eval(hero_mask, lambda v: min(v, 255)))

    # ============================================================
    # LEFT COLUMN — eyebrow, headline, lede, metric tiles, footer
    # ============================================================
    pad_x = 56
    pad_y = 56
    left_x = pad_x
    left_w = (W - 80) // 2 - 24
    cur_y = pad_y

    # Eyebrow pill
    eyebrow_text = "AI VISIBILITY AUDIT"
    eb_h = _draw_eyebrow(draw, left_x, cur_y, eyebrow_text)
    cur_y += eb_h + 22

    # Headline — big, wraps to 3 lines max
    headline_font = _font(44, "black")
    headline = _headline_for(brand_name, visibility_pct, bool(competitors))
    headline_lines = _wrap_lines(headline, headline_font, left_w, draw, max_lines=3)
    for line in headline_lines:
        draw.text((left_x, cur_y), line, font=headline_font, fill=WHITE)
        cur_y += 50
    cur_y += 8

    # Lede paragraph — what was asked + top competitors
    lede_font = _font(16, "regular")
    if category:
        lede_intro = f"Asked Google 'best {category}'."
    else:
        lede_intro = f"Asked Google about {brand_name}."
    draw.text((left_x, cur_y), lede_intro, font=lede_font, fill=(203, 213, 225))
    cur_y += 26
    if competitors:
        comp_label_font = _font(11, "bold")
        comp_value_font = _font(16, "semibold")
        comp_text = _format_competitor_list(competitors, max_n=3)
        # Truncate to one line; brands stay punchy
        comp_text = _truncate(comp_text, comp_value_font, left_w - 145, draw)
        draw.text((left_x, cur_y + 4), "TOP ANSWER NAMED:", font=comp_label_font, fill=HERO_BLUE)
        draw.text((left_x + 145, cur_y), comp_text, font=comp_value_font, fill=WHITE)
        cur_y += 28
    cur_y += 18

    # Metric tiles — visibility + citation rate
    tile_h = 76
    tile_gap = 12
    tiles_y = cur_y
    tile_w = (left_w - tile_gap) // 2
    vis_color = SUCCESS if visibility_pct >= 60 else (WARNING if visibility_pct >= 30 else (255, 100, 100))
    _draw_metric_tile(
        draw,
        (left_x, tiles_y, left_x + tile_w, tiles_y + tile_h),
        label="VISIBILITY",
        value=f"{visibility_pct:.0f}%",
        accent=vis_color,
    )
    if citation_pct is not None:
        cite_color = SUCCESS if citation_pct >= 30 else (WARNING if citation_pct >= 10 else (255, 160, 160))
        _draw_metric_tile(
            draw,
            (left_x + tile_w + tile_gap, tiles_y, left_x + 2 * tile_w + tile_gap, tiles_y + tile_h),
            label="CITED",
            value=f"{citation_pct:.0f}%",
            accent=cite_color,
        )
    else:
        # Second tile shows competitor count instead
        n = min(8, len(competitors))
        _draw_metric_tile(
            draw,
            (left_x + tile_w + tile_gap, tiles_y, left_x + 2 * tile_w + tile_gap, tiles_y + tile_h),
            label="COMPETITORS NAMED",
            value=str(n),
            accent=WHITE,
        )

    # Footer brand
    footer_font = _font(12, "bold")
    foot_y = H - pad_y - 10
    draw.text((left_x, foot_y), "MONITORAEO.COM", font=footer_font, fill=HERO_BLUE)

    # ============================================================
    # RIGHT COLUMN — browser-frame screenshot
    # ============================================================
    right_x = (W // 2) + 8
    right_w = W - right_x - pad_x
    shot_box = (right_x, pad_y + 30, right_x + right_w, H - pad_y - 50)
    _draw_browser_screenshot(img, site_screenshot, domain, shot_box)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(output_path, "PNG", optimize=True)
    return output_path


def generate_to_bytes(**kwargs) -> bytes:
    """Same as generate() but returns PNG bytes without writing to disk."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        generate(output_path=tmp_path, **kwargs)
        return tmp_path.read_bytes()
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
