"""Programmatic 1200x630 hero image for cold-email bodies, OG cards, and the
post-audit delivery email. Renders the preview-page's audit-hero card —
browser-frame screenshot on the left, "audit subject" panel with metrics
on the right — so the visual is continuous from cold email → click →
loading → report.

Layout (1200x630):
  ┌──────────────────────────────────────────────────────────────────────┐
  │  ┌────────────────────────┐    ┌───────────────────────────────────┐ │
  │  │ ● ● ●  https://decidr  │    │  [AUDIT SUBJECT]                  │ │
  │  │────────────────────────│    │  Decidr visibility check          │ │
  │  │                        │    │  Snapshot of how Google AI…       │ │
  │  │  [site screenshot]     │    │                                   │ │
  │  │                        │    │  ┌──────────┐  ┌──────────┐       │ │
  │  │   [Website audited]    │    │  │VISIBILITY│  │COMPETITORS│      │ │
  │  │           [decidr.ai↗] │    │  │   0%     │  │    6      │      │ │
  │  └────────────────────────┘    │  └──────────┘  └──────────┘       │ │
  │                                │  Full report: 5 engines, 40 qs…   │ │
  │      MONITORAEO.COM            └───────────────────────────────────┘ │
  └──────────────────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# OG-card dimensions — works for Twitter, LinkedIn, Slack, every email client.
W, H = 1200, 630

# Palette matched to the report hero CSS.
INK = (15, 23, 42)
INDIGO = (49, 46, 129)
BLUE_700 = (29, 78, 216)
BLUE_200 = (191, 219, 254)
SLATE_300 = (203, 213, 225)
SLATE_400 = (148, 163, 184)
WHITE = (255, 255, 255)
RED_300 = (252, 165, 165)
AMBER_300 = (252, 211, 77)
GREEN_300 = (134, 239, 172)
AMBER_FG = (146, 64, 14)         # amber-800 — text on cream chip
AMBER_BG = (255, 251, 235)       # amber-50 — chip background
CARD_FROST = (44, 49, 96)        # solid color that reads as frosted over the gradient
TILE_FROST = (54, 60, 110)       # one notch lighter for nested tiles
CARD_BORDER = (120, 135, 180)    # 1px definition border

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
    """RGB diagonal (135deg) gradient with multiple color stops (0..1)."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    img = Image.new("RGB", (w, h), stops[0][1])
    px = img.load()
    diag_len = max(1, w + h - 2)
    for y in range(h):
        for x in range(w):
            t = (x + y) / diag_len
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


def _drop_shadow(img: Image.Image, box: tuple[int, int, int, int], radius: int, *, pad: int = 18, opacity: int = 110, blur: int = 12) -> None:
    """Paint a soft drop shadow under a rounded rectangle at `box`."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    shadow = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle(
        (pad, pad + 6, pad + w, pad + h + 6),
        radius=radius,
        fill=(0, 0, 0, opacity),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    img.paste(shadow, (x0 - pad, y0 - pad), shadow)


def _vis_accent(visibility_pct: float) -> tuple[int, int, int]:
    if visibility_pct >= 60:
        return GREEN_300
    if visibility_pct >= 30:
        return AMBER_300
    return RED_300


def _draw_dark_pill(
    cd: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    *,
    align: str = "left",
    bg: tuple[int, int, int] = (15, 23, 42),
    fg: tuple[int, int, int] = WHITE,
    font_size: int = 13,
    height: int = 36,
) -> int:
    """Dark overlay pill used on top of the screenshot (e.g. 'Website audited').
    `align` is 'left' (x is the left edge) or 'right' (x is the right edge)."""
    font = _font(font_size, "bold")
    text_w = int(cd.textlength(text, font=font))
    pad_x = 16
    pill_w = text_w + pad_x * 2
    if align == "right":
        x_left = x - pill_w
    else:
        x_left = x
    cd.rounded_rectangle(
        (x_left, y, x_left + pill_w, y + height),
        radius=height // 2,
        fill=bg,
    )
    bbox = font.getbbox(text)
    ty = y + (height - (bbox[3] - bbox[1])) // 2 - bbox[1] - 1
    cd.text((x_left + pad_x, ty), text, font=font, fill=fg)
    return x_left + pill_w


def _draw_browser_frame_card(
    img: Image.Image,
    site_screenshot: Path | None,
    domain: str,
    box: tuple[int, int, int, int],
) -> None:
    """Browser-window mock with site screenshot + two overlay pills at the bottom."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    radius = 24

    card = Image.new("RGB", (w, h), CARD_FROST)
    cd = ImageDraw.Draw(card)

    # URL bar
    bar_h = 52
    cd.rectangle((0, 0, w, bar_h), fill=(28, 31, 65))
    cd.line((0, bar_h - 1, w, bar_h - 1), fill=(60, 65, 110), width=1)

    # Traffic-light dots
    dots_y = bar_h // 2
    for cx in [24, 50, 76]:
        cd.ellipse((cx - 7, dots_y - 7, cx + 7, dots_y + 7), fill=(75, 85, 105))

    # URL pill
    url_text = f"https://{domain}"
    url_font = _font(14, "semibold")
    addr_x0, addr_x1 = 108, w - 26
    addr_h = 28
    addr_y = dots_y - addr_h // 2
    cd.rounded_rectangle(
        (addr_x0, addr_y, addr_x1, addr_y + addr_h),
        radius=14,
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

    # Screenshot area — fit to frame width (no horizontal clipping), crop overflow
    # off the bottom. Shows the full website width with the top of the page.
    shot_w, shot_h = w, h - bar_h
    if site_screenshot and site_screenshot.exists():
        try:
            shot = Image.open(site_screenshot).convert("RGB")
            ratio = shot_w / shot.width
            new_size = (shot_w, max(1, int(shot.height * ratio)))
            shot = shot.resize(new_size, Image.Resampling.LANCZOS)
            if shot.height >= shot_h:
                shot = shot.crop((0, 0, shot_w, shot_h))
                card.paste(shot, (0, bar_h))
            else:
                pad_top = (shot_h - shot.height) // 2
                card.paste(shot, (0, bar_h + pad_top))
        except (OSError, ValueError):
            pass
    else:
        ph = Image.new("RGB", (shot_w, shot_h), (60, 65, 115))
        card.paste(ph, (0, bar_h))

    # Overlay pills at the bottom of the screenshot area, sitting on top of the image
    overlay_pad = 18
    pill_h = 36
    pill_y = h - overlay_pad - pill_h
    _draw_dark_pill(cd, overlay_pad, pill_y, "Website audited", bg=(15, 23, 42), fg=WHITE, height=pill_h)
    _draw_dark_pill(cd, w - overlay_pad, pill_y, f"{domain} ↗", align="right", bg=(15, 23, 42), fg=WHITE, height=pill_h)

    # Drop shadow then composite the rounded card
    _drop_shadow(img, (x0, y0, x1, y1), radius=radius, pad=18, opacity=120, blur=12)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w, h), radius=radius, fill=255)
    img.paste(card, (x0, y0), mask)

    # 1px border for definition
    ImageDraw.Draw(img).rounded_rectangle(
        (x0, y0, x0 + w - 1, y0 + h - 1), radius=radius, outline=CARD_BORDER, width=1
    )


def _draw_panel_tile(
    cd: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    label: str,
    value: str,
    accent: tuple[int, int, int],
) -> None:
    """Frosted metric tile nested inside the audit panel card."""
    x0, y0, x1, y1 = box
    cd.rounded_rectangle(
        (x0, y0, x1 - 1, y1 - 1),
        radius=16,
        fill=TILE_FROST,
        outline=(140, 155, 200),
        width=1,
    )
    label_font = _font(12, "bold")
    value_font = _font(44, "black")
    cd.text((x0 + 18, y0 + 16), label, font=label_font, fill=BLUE_200)
    cd.text((x0 + 18, y0 + 38), value, font=value_font, fill=accent)


def _draw_audit_panel(
    img: Image.Image,
    box: tuple[int, int, int, int],
    *,
    brand: str,
    visibility_pct: float,
    competitor_count: int,
    citation_pct: float | None,
    category: str | None,
) -> None:
    """Frosted audit-subject panel — eyebrow + title + description + two metric tiles + footer line."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    radius = 26

    card = Image.new("RGB", (w, h), CARD_FROST)
    cd = ImageDraw.Draw(card)

    pad = 32
    cy = pad

    # AUDIT SUBJECT eyebrow chip (cream/amber)
    eyebrow = "AUDIT SUBJECT"
    eb_font = _font(12, "bold")
    eb_text_w = int(cd.textlength(eyebrow, font=eb_font))
    eb_h = 32
    eb_pad_x = 16
    eb_w = eb_text_w + eb_pad_x * 2
    cd.rounded_rectangle(
        (pad, cy, pad + eb_w, cy + eb_h),
        radius=eb_h // 2,
        fill=AMBER_BG,
    )
    bbox = eb_font.getbbox(eyebrow)
    ty = cy + (eb_h - (bbox[3] - bbox[1])) // 2 - bbox[1] - 1
    cd.text((pad + eb_pad_x, ty), eyebrow, font=eb_font, fill=AMBER_FG)
    cy += eb_h + 18

    # Title — "{Brand} visibility check"
    title_font = _font(38, "black")
    title = f"{brand} visibility check"
    title_lines = _wrap_lines(title, title_font, w - pad * 2, cd, max_lines=2)
    for line in title_lines:
        cd.text((pad, cy), line, font=title_font, fill=WHITE)
        cy += 44
    cy += 10

    # Description
    desc_font = _font(15, "regular")
    if category:
        desc = (
            f"Snapshot of how Google AI Overviews surfaces, cites and compares "
            f"{brand} for buyer-facing queries about {category}."
        )
    else:
        desc = (
            f"Snapshot of how Google AI Overviews surfaces, cites and compares "
            f"{brand} in buyer-facing queries."
        )
    desc_lines = _wrap_lines(desc, desc_font, w - pad * 2, cd, max_lines=3)
    for line in desc_lines:
        cd.text((pad, cy), line, font=desc_font, fill=SLATE_300)
        cy += 22
    cy += 16

    # Two metric tiles — labelled as SAMPLE results so the recipient
    # understands they're based on the one teaser query, not the full
    # 8-query preview they'll see when they click through. The tiles in the
    # preview/report use proper averaged percentages; mixing both formats
    # under the same label ("VISIBILITY") made the email and the preview
    # disagree (e.g. teaser said 100%, preview said 50%).
    tile_gap = 14
    tile_w = (w - pad * 2 - tile_gap) // 2
    tile_h = 112
    found = visibility_pct >= 50  # single-query result is always 0 or 100
    _draw_panel_tile(
        cd,
        (pad, cy, pad + tile_w, cy + tile_h),
        "GOOGLE AI MENTION",
        "YES" if found else "NO",
        GREEN_300 if found else RED_300,
    )
    if citation_pct is not None:
        _draw_panel_tile(
            cd,
            (pad + tile_w + tile_gap, cy, pad + 2 * tile_w + tile_gap, cy + tile_h),
            "CITED IN SAMPLE",
            "YES" if citation_pct >= 50 else "NO",
            GREEN_300 if citation_pct >= 50 else RED_300,
        )
    else:
        _draw_panel_tile(
            cd,
            (pad + tile_w + tile_gap, cy, pad + 2 * tile_w + tile_gap, cy + tile_h),
            "COMPETITORS IN SAMPLE",
            str(competitor_count),
            WHITE,
        )
    cy += tile_h + 18

    # Footer line
    foot_font = _font(13, "regular")
    foot = "Full report expands to 5 engines, 40 queries and 200 AI answers."
    cd.text((pad, cy), _truncate(foot, foot_font, w - pad * 2, cd), font=foot_font, fill=SLATE_400)

    # Drop shadow + composite
    _drop_shadow(img, (x0, y0, x1, y1), radius=radius, pad=18, opacity=110, blur=12)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w, h), radius=radius, fill=255)
    img.paste(card, (x0, y0), mask)

    # 1px border
    ImageDraw.Draw(img).rounded_rectangle(
        (x0, y0, x0 + w - 1, y0 + h - 1), radius=radius, outline=CARD_BORDER, width=1
    )


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
    """Compose the audit-hero card and write it to output_path."""
    # Diagonal gradient background — slate-900 → indigo-900.
    img = _diag_gradient(
        (0, 0, W, H),
        stops=[(0.0, INK), (0.55, (24, 30, 70)), (1.0, INDIGO)],
    )

    # Radial glows for depth
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.ellipse((W - 540, -260, W + 200, 460), fill=(6, 182, 212, 70))
    od.ellipse((-260, H - 480, 540, H + 200), fill=(124, 58, 237, 75))
    overlay = overlay.filter(ImageFilter.GaussianBlur(90))
    img.paste(overlay, (0, 0), overlay)

    # ── Layout: two side-by-side cards ──────────────────────────────────
    pad_x = 48
    gap = 24
    # Left card: browser frame at native screenshot aspect (16:10-ish)
    left_w = 540
    left_h = 410   # URL bar 52 + screenshot 358 — matches 1440×900 scaled
    left_x0 = pad_x
    left_x1 = left_x0 + left_w
    left_y0 = 56
    left_y1 = left_y0 + left_h
    _draw_browser_frame_card(img, site_screenshot, domain, (left_x0, left_y0, left_x1, left_y1))

    # Brand line below the browser frame
    brand_font = _font(13, "bold")
    draw = ImageDraw.Draw(img)
    draw.text(
        (left_x0, left_y1 + 28),
        "MONITORAEO.COM",
        font=brand_font,
        fill=BLUE_200,
    )
    cta_font = _font(13, "semibold")
    cta_x = left_x0 + int(draw.textlength("MONITORAEO.COM", font=brand_font)) + 14
    draw.text(
        (cta_x, left_y1 + 28),
        "See the full audit  →",
        font=cta_font,
        fill=SLATE_300,
    )

    # Right card: audit subject panel
    right_x0 = left_x1 + gap
    right_x1 = W - pad_x
    right_y0 = 56
    right_y1 = H - 56
    _draw_audit_panel(
        img,
        (right_x0, right_y0, right_x1, right_y1),
        brand=brand_name,
        visibility_pct=visibility_pct,
        competitor_count=len(competitors),
        citation_pct=citation_pct,
        category=category,
    )

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
