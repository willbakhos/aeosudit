"""Programmatic 1200x630 hero image for cold-email bodies, OG cards, and the
post-audit delivery email. Mirrors the visual feel of the report hero so the
recipient gets visual continuity when they click through.

Layout (left → right):
  ┌──────────────────────────────────┬───────────────────────────┐
  │ AI VISIBILITY AUDIT (eyebrow)    │  ┌─────────────────────┐  │
  │                                  │  │  ● ● ●              │  │
  │ {brand_name}                     │  │  https://{domain}   │  │
  │ visibility check                 │  ├─────────────────────┤  │
  │                                  │  │                     │  │
  │ {visibility%} visibility ·       │  │   site screenshot   │  │
  │ Competitors named: A, B, C       │  │                     │  │
  │                                  │  └─────────────────────┘  │
  │                              monitoraeo.com                  │
  └──────────────────────────────────┴───────────────────────────┘
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# OG image standard dimensions — works for Twitter, LinkedIn, Slack, email clients
W, H = 1200, 630

# Color palette matches the report hero
INK = (15, 23, 42)          # slate-900
INK_2 = (30, 41, 59)        # slate-800
MUTED = (148, 163, 184)     # slate-400
SOFT = (226, 232, 240)      # slate-200
BLUE = (37, 99, 235)
PURPLE = (124, 58, 237)
CYAN = (6, 182, 212)
WARNING = (217, 119, 6)
DANGER = (220, 38, 38)
SUCCESS = (22, 163, 74)
WHITE = (255, 255, 255)


def _font(size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
    """Find a system font that exists. Falls back to PIL's default if none.
    Tries Helvetica/Arial first (broadly available on macOS + most Linux distros)."""
    candidates = {
        "regular": [
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ],
        "bold": [
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ],
    }
    for path in candidates.get(weight, candidates["regular"]):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
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
    """Vertical gradient inside a rounded rect, drawn into `img` in place."""
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
    text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw
) -> list[str]:
    """Greedy word-wrap to fit max_width pixels. Returns list of lines."""
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
    lines.append(current)
    return lines


def _truncate(text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw) -> str:
    """Trim with ellipsis to fit max_width on one line."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def _draw_browser_screenshot(
    img: Image.Image, site_screenshot: Path | None, domain: str, box: tuple[int, int, int, int]
) -> None:
    """Render a browser-frame mock with the site screenshot inside, inside `box`."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0

    # Outer card with rounded corners
    card = Image.new("RGB", (w, h), (30, 41, 59))
    cd = ImageDraw.Draw(card)
    cd.rounded_rectangle((0, 0, w, h), radius=22, fill=(30, 41, 59))

    # Browser bar (38px tall)
    bar_h = 38
    cd.rectangle((0, 0, w, bar_h), fill=(15, 23, 42))
    # Three traffic-light dots
    for i, cx in enumerate([18, 38, 58]):
        cd.ellipse((cx - 5, bar_h // 2 - 5, cx + 5, bar_h // 2 + 5), fill=(255, 255, 255, 50) if False else (75, 85, 99))
    # URL pill
    url_text = f"https://{domain}"
    url_font = _font(13)
    pill_pad_x, pill_pad_y = 12, 5
    text_w = cd.textlength(url_text, font=url_font)
    pill_w = min(int(text_w + pill_pad_x * 2), w - 90)
    pill_x = 80
    pill_y = bar_h // 2 - 11
    cd.rounded_rectangle((pill_x, pill_y, pill_x + pill_w, pill_y + 22), radius=11, fill=(30, 41, 59))
    cd.text(
        (pill_x + pill_pad_x, pill_y + pill_pad_y - 1),
        _truncate(url_text, url_font, pill_w - pill_pad_x * 2, cd),
        font=url_font, fill=(203, 213, 225),
    )

    # Screenshot area (below browser bar)
    shot_box = (0, bar_h, w, h)
    shot_w, shot_h = w, h - bar_h

    if site_screenshot and site_screenshot.exists():
        try:
            shot = Image.open(site_screenshot).convert("RGB")
            # Fit to shot_box, cover (crop to fill)
            ratio = max(shot_w / shot.width, shot_h / shot.height)
            new_size = (int(shot.width * ratio), int(shot.height * ratio))
            shot = shot.resize(new_size, Image.Resampling.LANCZOS)
            crop_x = (shot.width - shot_w) // 2
            shot = shot.crop((crop_x, 0, crop_x + shot_w, shot_h))
            card.paste(shot, (0, bar_h))
        except (OSError, ValueError):
            pass
    else:
        # Placeholder: subtle gradient + "Website preview"
        ph = Image.new("RGB", (shot_w, shot_h))
        for y in range(shot_h):
            t = y / shot_h
            r = int(30 + (15 - 30) * t)
            g = int(41 + (23 - 41) * t)
            b = int(59 + (42 - 59) * t)
            for x in range(shot_w):
                ph.putpixel((x, y), (r, g, b))
        card.paste(ph, (0, bar_h))

    # Round outer corners again now that we've pasted the screenshot
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w, h), radius=22, fill=255)
    out = Image.new("RGB", (w, h))
    out.paste(card, (0, 0), mask)

    # Soft drop shadow first
    shadow_size = (w + 60, h + 60)
    shadow = Image.new("RGBA", shadow_size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle((30, 30, 30 + w, 30 + h), radius=22, fill=(0, 0, 0, 90))
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    img.paste(shadow, (x0 - 30, y0 - 24), shadow)
    img.paste(out, (x0, y0), mask)


def _format_competitor_list(competitors: Iterable[str], max_n: int = 3) -> str:
    items = [c for c in competitors if c][:max_n]
    if not items:
        return "no competitors detected"
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])} and {items[-1]}"


def generate(
    *,
    brand_name: str,
    domain: str,
    visibility_pct: float,
    competitors: list[str],
    site_screenshot: Path | None,
    output_path: Path,
) -> Path:
    """Compose the hero image and write it to output_path. Returns the path.

    visibility_pct = 0..100 (percentage of queries that named the brand).
    competitors = list of brand names (we display the top 3).
    """
    img = Image.new("RGB", (W, H), (248, 251, 255))
    draw = ImageDraw.Draw(img)

    # Background — radial-ish gradient via two overlaid blurred discs
    bg_overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bg_overlay)
    bd.ellipse((-200, -300, 700, 600), fill=(37, 99, 235, 38))
    bd.ellipse((700, -200, 1500, 700), fill=(124, 58, 237, 32))
    bg_overlay = bg_overlay.filter(ImageFilter.GaussianBlur(120))
    img.paste(bg_overlay, (0, 0), bg_overlay)

    # Main hero card
    hero_box = (40, 40, W - 40, H - 40)
    _draw_gradient_rect(
        img, hero_box,
        color_a=(15, 23, 42),
        color_b=(30, 64, 175),
        radius=34,
    )

    # Soft cyan glow in top-right of hero
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((W - 600, -100, W + 100, 500), fill=(6, 182, 212, 55))
    glow = glow.filter(ImageFilter.GaussianBlur(80))
    # Mask to inside the hero
    hero_mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(hero_mask).rounded_rectangle(hero_box, radius=34, fill=255)
    img.paste(glow, (0, 0), Image.eval(hero_mask, lambda v: min(v, 255)))

    # ---------- Left column (text) ----------
    pad_x = 70
    pad_y = 70
    left_x = pad_x
    left_w = (W - 80) // 2 - 30
    cur_y = pad_y

    # Eyebrow pill
    eyebrow_text = "AI VISIBILITY AUDIT"
    eyebrow_font = _font(14, "bold")
    e_w = draw.textlength(eyebrow_text, font=eyebrow_font)
    eb_box = (left_x, cur_y, left_x + int(e_w) + 26, cur_y + 32)
    draw.rounded_rectangle(eb_box, radius=16, fill=(255, 255, 255, 25), outline=(255, 255, 255, 60), width=1)
    draw.text((left_x + 13, cur_y + 8), eyebrow_text, font=eyebrow_font, fill=(191, 219, 254))
    cur_y += 56

    # Headline — brand name, big
    h_font = _font(72, "bold")
    headline_lines = _wrap_lines(brand_name, h_font, left_w, draw)
    if len(headline_lines) > 2:
        headline_lines = headline_lines[:1]  # Single line if brand is too long
        headline_lines[0] = _truncate(headline_lines[0], h_font, left_w, draw)
    for line in headline_lines:
        draw.text((left_x, cur_y), line, font=h_font, fill=WHITE)
        cur_y += 78
    sub_font = _font(40, "bold")
    draw.text((left_x, cur_y), "visibility check", font=sub_font, fill=(191, 219, 254))
    cur_y += 80

    # Visibility stat
    stat_label_font = _font(13, "bold")
    stat_value_font = _font(56, "bold")
    pct_text = f"{visibility_pct:.0f}%"
    pct_color = SUCCESS if visibility_pct >= 60 else (WARNING if visibility_pct >= 30 else (255, 100, 100))
    draw.text((left_x, cur_y), "VISIBILITY", font=stat_label_font, fill=(148, 163, 184))
    draw.text((left_x, cur_y + 18), pct_text, font=stat_value_font, fill=WHITE)

    # Competitors line under the stat
    comp_label_font = _font(13, "bold")
    comp_body_font = _font(20)
    comp_y = cur_y + 92
    draw.text((left_x, comp_y), "AI POINTED TO", font=comp_label_font, fill=(148, 163, 184))
    comp_text = _format_competitor_list(competitors, max_n=3)
    comp_text_truncated = _truncate(comp_text, comp_body_font, left_w, draw)
    draw.text((left_x, comp_y + 20), comp_text_truncated, font=comp_body_font, fill=(241, 245, 249))

    # ---------- Right column (browser-frame screenshot) ----------
    right_x = (W // 2) + 10
    right_w = W - right_x - pad_x
    shot_box = (right_x, pad_y + 30, right_x + right_w, H - pad_y - 70)
    _draw_browser_screenshot(img, site_screenshot, domain, shot_box)

    # ---------- Footer (bottom-left of hero) ----------
    footer_font = _font(13, "bold")
    foot_y = H - pad_y - 18
    draw.text((left_x, foot_y), "MONITORAEO.COM", font=footer_font, fill=(191, 219, 254))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, "PNG", optimize=True)
    return output_path


def generate_to_bytes(**kwargs) -> bytes:
    """Same as generate() but returns the PNG bytes without writing to disk.
    Useful for inline-embedding in email bodies."""
    output_path = kwargs.pop("output_path", None)
    buf = io.BytesIO()
    img = Image.new("RGB", (W, H))  # placeholder, will be replaced
    # Reuse generate() logic by writing to a temp path then reading — simpler than refactoring
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
