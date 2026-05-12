"""Programmatic 1200x630 hero image for cold-email bodies, OG cards, and the
post-audit delivery email. Mirrors the report-hero design (gradient, big bold
headline, frosted browser screenshot, white pill chips, single visibility
tile) so the visual is continuous from cold email → click → loading → report.

Layout (1200x630):
  ┌────────────────────────────────────────────────────────────────────┐
  │  AI ANSWER VISIBILITY AUDIT                                        │
  │                                                                    │
  │  Decidr is invisible in            ┌─────────────────────────────┐ │
  │  Google AI Overviews.              │  ● ● ●   https://decidr.ai  │ │
  │                                    ├─────────────────────────────┤ │
  │  Asked 'best agentic AI platforms'.│                             │ │
  │  Top answer named: n8n, Gumloop.   │   [site screenshot]         │ │
  │                                    │                             │ │
  │  ┌─────────────┐                   │                             │ │
  │  │ VISIBILITY  │                   │                             │ │
  │  │ 0%          │                   └─────────────────────────────┘ │
  │  └─────────────┘                                                   │
  │                                                                    │
  │  [decidr.ai]  [Google AI Overviews]  [40 queries]                  │
  │                                                                    │
  │  MONITORAEO.COM · See the full audit →                             │
  └────────────────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# OG-card dimensions — works for Twitter, LinkedIn, Slack, every email client.
W, H = 1200, 630

# Palette matched to the report hero CSS.
INK = (15, 23, 42)
INK_2 = (30, 41, 59)
INDIGO = (49, 46, 129)
INDIGO_DEEP = (30, 27, 75)
BLUE_700 = (29, 78, 216)
BLUE_300 = (147, 197, 253)
BLUE_200 = (191, 219, 254)
CYAN_400 = (34, 211, 238)
PURPLE_500 = (139, 92, 246)
SLATE_300 = (203, 213, 225)
SLATE_400 = (148, 163, 184)
WHITE = (255, 255, 255)
RED_300 = (252, 165, 165)
AMBER_300 = (252, 211, 77)
GREEN_300 = (134, 239, 172)

# Bundled Inter font files live under static/fonts/ in the repo so rendering
# is identical across local dev and Railway's stripped-down container.
_FONT_DIR = Path(__file__).resolve().parent.parent / "static" / "fonts"
_FONT_FILES = {
    "regular": _FONT_DIR / "Inter-Regular.ttf",
    "semibold": _FONT_DIR / "Inter-SemiBold.ttf",
    "bold": _FONT_DIR / "Inter-Bold.ttf",
    "black": _FONT_DIR / "Inter-Black.ttf",
}


def _font(size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
    path = _FONT_FILES.get(weight)
    if path and path.exists():
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            pass
    for other in _FONT_FILES.values():
        if other.exists():
            try:
                return ImageFont.truetype(str(other), size)
            except OSError:
                continue
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


def _diag_gradient(box: tuple[int, int, int, int], stops: list[tuple[float, tuple[int, int, int]]]) -> Image.Image:
    """Build an RGB diagonal (135deg) gradient with multiple stops.
    Stops are (position 0..1, RGB)."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    img = Image.new("RGB", (w, h), stops[0][1])
    # Project each pixel onto the 135deg axis (top-left → bottom-right)
    # then interpolate between stops.
    px = img.load()
    diag_len = max(1, w + h - 2)
    for y in range(h):
        for x in range(w):
            t = (x + y) / diag_len
            # find segment
            for i in range(len(stops) - 1):
                s0, s1 = stops[i], stops[i + 1]
                if s0[0] <= t <= s1[0] or i == len(stops) - 2:
                    local = (t - s0[0]) / max(1e-6, s1[0] - s0[0])
                    local = max(0.0, min(1.0, local))
                    r = int(s0[1][0] + (s1[1][0] - s0[1][0]) * local)
                    g = int(s0[1][1] + (s1[1][1] - s0[1][1]) * local)
                    b = int(s0[1][2] + (s1[1][2] - s0[1][2]) * local)
                    px[x, y] = (r, g, b)
                    break
    return img


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
                last = lines[-1]
                while draw.textlength(last + "…", font=font) > max_width and last:
                    last = last[:-1]
                lines[-1] = last + "…"
                return lines
    lines.append(current)
    return lines


def _truncate(text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def _format_competitor_list(competitors: Iterable[str], max_n: int = 2) -> str:
    items = [c for c in competitors if c][:max_n]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{items[0]}, {items[1]}"


def _headline_for(brand: str, visibility_pct: float, has_competitors: bool) -> str:
    if visibility_pct == 0:
        if has_competitors:
            return f"{brand} is invisible in Google AI Overviews."
        return f"{brand} did not appear in Google AI Overviews."
    if visibility_pct < 50:
        return f"{brand} barely shows up in Google AI Overviews."
    return f"{brand} appears — but who else is being cited?"


def _draw_pill(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    label: str,
    *,
    bg: tuple[int, int, int] = WHITE,
    fg: tuple[int, int, int] = BLUE_700,
    font_size: int = 13,
    pad_x: int = 14,
    pad_y: int = 8,
    height: int = 34,
) -> int:
    """Solid pill chip. Returns the x-coordinate right after the pill (for chaining)."""
    font = _font(font_size, "bold")
    text_w = int(draw.textlength(label, font=font))
    pill_w = text_w + pad_x * 2
    draw.rounded_rectangle(
        (x, y, x + pill_w, y + height),
        radius=height // 2,
        fill=bg,
    )
    # Vertically center the text — Inter sits a bit high optically
    bbox = font.getbbox(label)
    text_h = bbox[3] - bbox[1]
    ty = y + (height - text_h) // 2 - bbox[1] - 1
    draw.text((x + pad_x, ty), label, font=font, fill=fg)
    return x + pill_w


def _draw_outlined_pill(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    label: str,
    *,
    fg: tuple[int, int, int] = BLUE_200,
    font_size: int = 12,
    height: int = 30,
) -> int:
    """Outlined eyebrow-style pill for the dark hero — used for the eyebrow only."""
    font = _font(font_size, "bold")
    # Render as letter-spaced uppercase
    spaced = label
    text_w = int(draw.textlength(spaced, font=font))
    pad_x = 14
    pill_w = text_w + pad_x * 2
    # Subtle frosted look — pick a color that reads as translucent over INK/INDIGO
    draw.rounded_rectangle(
        (x, y, x + pill_w, y + height),
        radius=height // 2,
        fill=(36, 47, 84),
        outline=(255, 255, 255),
        width=0,
    )
    bbox = font.getbbox(spaced)
    text_h = bbox[3] - bbox[1]
    ty = y + (height - text_h) // 2 - bbox[1] - 1
    draw.text((x + pad_x, ty), spaced, font=font, fill=fg)
    return x + pill_w


def _draw_browser_screenshot(
    img: Image.Image,
    site_screenshot: Path | None,
    domain: str,
    box: tuple[int, int, int, int],
) -> None:
    """Render a frosted browser-frame mock with the site screenshot inside `box`."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    radius = 24

    # Build the card on its own RGB canvas so we can round-mask cleanly.
    # Pre-mix the "frosted" look — solid colors that look translucent over INDIGO.
    card_bg = (44, 49, 96)  # mid-tone frosted
    card = Image.new("RGB", (w, h), card_bg)
    cd = ImageDraw.Draw(card)

    # Browser bar
    bar_h = 52
    cd.rectangle((0, 0, w, bar_h), fill=(28, 31, 65))
    # Subtle bottom border on bar
    cd.line((0, bar_h - 1, w, bar_h - 1), fill=(60, 65, 110), width=1)

    # Window dots
    dots_y = bar_h // 2
    for cx in [22, 46, 70]:
        cd.ellipse((cx - 6, dots_y - 6, cx + 6, dots_y + 6), fill=(75, 85, 105))

    # URL address bar (longer pill)
    url_text = f"https://{domain}"
    url_font = _font(14, "semibold")
    addr_x0, addr_x1 = 100, w - 24
    addr_h = 26
    addr_y = dots_y - addr_h // 2
    cd.rounded_rectangle(
        (addr_x0, addr_y, addr_x1, addr_y + addr_h),
        radius=13,
        fill=(36, 40, 78),
    )
    bbox = url_font.getbbox(url_text)
    ty = addr_y + (addr_h - (bbox[3] - bbox[1])) // 2 - bbox[1] - 1
    cd.text(
        (addr_x0 + 14, ty),
        _truncate(url_text, url_font, addr_x1 - addr_x0 - 28, cd),
        font=url_font,
        fill=(180, 195, 230),
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
        # Frosted placeholder
        ph = Image.new("RGB", (shot_w, shot_h), (60, 65, 115))
        card.paste(ph, (0, bar_h))

    # Mask to rounded corners
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w, h), radius=radius, fill=255)

    # Drop shadow — soft blur, placed under the card
    shadow_pad = 40
    shadow = Image.new("RGBA", (w + shadow_pad * 2, h + shadow_pad * 2), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle(
        (shadow_pad, shadow_pad + 8, shadow_pad + w, shadow_pad + h + 8),
        radius=radius,
        fill=(0, 0, 0, 120),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(22))
    img.paste(shadow, (x0 - shadow_pad, y0 - shadow_pad), shadow)

    # Composite the rounded card onto img
    img.paste(card, (x0, y0), mask)

    # 1px inner border for definition
    bd = ImageDraw.Draw(img)
    bd.rounded_rectangle(
        (x0, y0, x0 + w - 1, y0 + h - 1),
        radius=radius,
        outline=(120, 135, 180),
        width=1,
    )


def _draw_metric_tile(
    img: Image.Image,
    box: tuple[int, int, int, int],
    label: str,
    value: str,
    accent: tuple[int, int, int],
) -> None:
    """Frosted-glass metric tile — solid colors picked to read as translucent over the gradient."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0

    # Build tile on its own canvas so we can mask + add a subtle border crisply.
    tile_bg = (46, 50, 100)
    tile = Image.new("RGB", (w, h), tile_bg)
    td = ImageDraw.Draw(tile)
    # 1px inner border line for definition
    td.rounded_rectangle((0, 0, w - 1, h - 1), radius=18, outline=(140, 155, 200), width=1)

    label_font = _font(13, "bold")
    value_font = _font(48, "black")

    td.text((22, 18), label, font=label_font, fill=BLUE_200)
    # Value
    bbox = value_font.getbbox(value)
    td.text((22, 38), value, font=value_font, fill=accent)

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w, h), radius=18, fill=255)
    img.paste(tile, (x0, y0), mask)


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
    # Base canvas: diagonal gradient slate-900 → indigo-700.
    img = _diag_gradient(
        (0, 0, W, H),
        stops=[
            (0.0, INK),
            (0.55, (24, 30, 70)),
            (1.0, (49, 46, 129)),
        ],
    )

    # Radial glows — drawn as alpha overlays then composited onto the RGB base.
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    # Cyan glow top-right
    od.ellipse((W - 540, -260, W + 200, 460), fill=(6, 182, 212, 70))
    # Purple glow bottom-left
    od.ellipse((-260, H - 480, 540, H + 200), fill=(124, 58, 237, 75))
    overlay = overlay.filter(ImageFilter.GaussianBlur(90))
    img.paste(overlay, (0, 0), overlay)

    draw = ImageDraw.Draw(img)

    # ============================================================
    # LEFT COLUMN — eyebrow, headline, lede, single tile, footer
    # ============================================================
    pad_x = 56
    pad_y = 52
    left_x = pad_x
    left_w = 600  # slightly less than half — leaves room for the screenshot
    cur_y = pad_y

    # Eyebrow chip
    _draw_outlined_pill(draw, left_x, cur_y, "AI ANSWER VISIBILITY AUDIT", fg=BLUE_200, font_size=12, height=32)
    cur_y += 32 + 22

    # Headline — tight, big, max 3 lines
    headline_font = _font(54, "black")
    headline = _headline_for(brand_name, visibility_pct, bool(competitors))
    headline_lines = _wrap_lines(headline, headline_font, left_w, draw, max_lines=3)
    # If headline only takes 2 lines, give it a bit more breathing room before lede
    line_height = 58
    for line in headline_lines:
        draw.text((left_x, cur_y), line, font=headline_font, fill=WHITE)
        cur_y += line_height
    cur_y += 14

    # Lede — what was asked + top competitors (single line each, no wrap)
    lede_font = _font(17, "regular")
    if category:
        lede_intro = f"Asked Google 'best {category}'."
    else:
        lede_intro = f"Asked Google about {brand_name}."
    lede_intro = _truncate(lede_intro, lede_font, left_w, draw)
    draw.text((left_x, cur_y), lede_intro, font=lede_font, fill=SLATE_300)
    cur_y += 26

    if competitors:
        comp_label_font = _font(12, "bold")
        comp_value_font = _font(17, "semibold")
        comp_text = _format_competitor_list(competitors, max_n=2)
        label_w = int(draw.textlength("TOP ANSWER NAMED:  ", font=comp_label_font))
        comp_text = _truncate(comp_text, comp_value_font, left_w - label_w, draw)
        draw.text((left_x, cur_y + 3), "TOP ANSWER NAMED:", font=comp_label_font, fill=BLUE_200)
        draw.text((left_x + label_w, cur_y), comp_text, font=comp_value_font, fill=WHITE)
        cur_y += 26
    cur_y += 18

    # Single big metric tile — VISIBILITY %
    tile_w = 240
    tile_h = 108
    vis_accent = (
        GREEN_300 if visibility_pct >= 60
        else (AMBER_300 if visibility_pct >= 30 else RED_300)
    )
    _draw_metric_tile(
        img,
        (left_x, cur_y, left_x + tile_w, cur_y + tile_h),
        label="VISIBILITY",
        value=f"{visibility_pct:.0f}%",
        accent=vis_accent,
    )
    cur_y += tile_h + 22

    # Pill chips row — domain · engine · queries
    chips_y = cur_y
    chip_gap = 8
    chip_x = left_x
    chip_x = _draw_pill(draw, chip_x, chips_y, domain, bg=WHITE, fg=BLUE_700) + chip_gap
    chip_x = _draw_pill(draw, chip_x, chips_y, "GOOGLE AI OVERVIEWS", bg=WHITE, fg=BLUE_700) + chip_gap

    # Footer brand line — bottom-left
    footer_font = _font(13, "bold")
    foot_y = H - pad_y - 8
    draw.text((left_x, foot_y), "MONITORAEO.COM", font=footer_font, fill=BLUE_200)
    cta_font = _font(13, "semibold")
    cta_x = left_x + int(draw.textlength("MONITORAEO.COM", font=footer_font)) + 12
    draw.text((cta_x, foot_y), "·  See the full audit  →", font=cta_font, fill=SLATE_400)

    # ============================================================
    # RIGHT COLUMN — browser-frame screenshot
    # ============================================================
    shot_x0 = W - pad_x - 470
    shot_x1 = W - pad_x
    shot_y0 = pad_y + 26
    shot_y1 = H - pad_y - 60
    _draw_browser_screenshot(img, site_screenshot, domain, (shot_x0, shot_y0, shot_x1, shot_y1))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, "PNG", optimize=True)
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
