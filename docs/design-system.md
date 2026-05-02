# AEO Audit — Site Design System

The actual design system as implemented in [templates/_layout.html.j2](../templates/_layout.html.j2) and the page templates under [templates/pages/](../templates/pages/). This is what's shipping today, not aspirational. When the templates and this doc disagree, the templates win — update this doc to match.

---

## 1. Brand & voice

**Brand name in product:** AEO Audit.
**Tagline (working):** "See how AI engines describe your brand."

**Tone:**
- Direct, evidence-based, low-fluff. Numbers over adjectives.
- Speak to founders, CMOs, growth marketers, agencies — assume technical literacy.
- Name the gap, then name the fix. Avoid corporate softening ("we believe", "leverage", "synergies").
- Italics for consequence sentences ("Google pointed them to Prospa instead.").
- Reserve red for risk states; green for confirmation; blue/purple for action.

**What we don't do:**
- No "guaranteed AI rankings" claims — no one can guarantee that.
- No fake-data placeholders dressed as real findings (locked sections are visibly locked).
- No emoji as decoration. Use sparingly inside icon boxes only.

---

## 2. Color tokens

All color is exposed via CSS custom properties on `:root` in [_layout.html.j2](../templates/_layout.html.j2). Use the tokens, never the literals.

### Surfaces & text

| Token | Hex | Use |
|---|---:|---|
| `--ink` | `#0f172a` | Primary text, headlines |
| `--ink-2` | `#1e293b` | Body copy emphasis, list items |
| `--muted` | `#64748b` | Captions, helper text, supporting copy |
| `--soft-muted` | `#94a3b8` | Footnotes, low-priority metadata |
| `--line` | `rgba(15,23,42,.10)` | Borders, separators |
| `--bg` | `#f6f8fc` | Solid background fallback |
| `--panel` | `rgba(255,255,255,.78)` | Glass panels (over the gradient body) |
| `--panel-solid` | `#ffffff` | Cards, tables, answer blocks |

### Accents

| Token | Hex | Use |
|---|---:|---|
| `--blue` | `#2563eb` | Primary action, links, focus rings |
| `--purple` | `#7c3aed` | Premium / upgrade emphasis (paired with blue in gradient) |
| `--cyan` | `#06b6d4` | Secondary gradient stop, accent details |
| `--success` | `#16a34a` | Positive metrics, confirmation, "yes" markers |
| `--warning` | `#d97706` | Caution states, eyebrows on risk callouts |
| `--danger` | `#dc2626` | Negative metrics, error flashes, missed visibility |

### Page background

The `<body>` background is a layered radial-gradient + linear-gradient composition — never solid. Stays consistent across all pages.

```css
background:
  radial-gradient(circle at 8% 0%, rgba(37,99,235,.16), transparent 32rem),
  radial-gradient(circle at 85% 8%, rgba(124,58,237,.12), transparent 30rem),
  linear-gradient(180deg, #f8fbff 0%, #f8fafc 38%, #eef4ff 100%);
```

### Gradients

Reuse these — don't roll new ones.

| Name | CSS | Use |
|---|---|---|
| **Primary CTA** | `linear-gradient(135deg, var(--blue), var(--purple))` | Primary buttons, logo background, focus moments |
| **Featured pricing card** | `linear-gradient(160deg, #1d4ed8 0%, #4338ca 50%, #6d28d9 100%)` | The "Most popular" pricing tier card, premium upgrade panels |
| **Hero dark** | `linear-gradient(135deg, rgba(15,23,42,.97), rgba(30,41,59,.96) 48%, rgba(30,64,175,.94) 100%)` | Dark hero blocks (home page, free report card) |
| **Takeaway warm** | `linear-gradient(135deg, #fff7ed 0%, #fff 52%, #eff6ff 100%)` | The orange-tinted "takeaway" callout in audit reports |
| **Soft success** | `linear-gradient(135deg, #eff6ff, #ede9fe)` | Tertiary informational panels |

---

## 3. Typography

**Font stack** (single declaration in body):

```css
font-family: Inter, ui-sans-serif, system-ui, -apple-system,
             BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
```

`-webkit-font-smoothing: antialiased` is on globally.

### Scale

All headings use negative letter-spacing for tightness. `clamp()` is used for fluid sizing on landing/hero copy.

| Element | Size | Weight | Line height | Letter-spacing | Notes |
|---|---:|---:|---:|---:|---|
| Hero `h1` | `clamp(40px, 5.6vw, 64px)` | 900 | 0.98 | -0.06em | Used inside `.hero` only |
| Page title `h1.page-title` | `clamp(40px, 5.4vw, 60px)` | 800 | 1.02 | -0.06em | Standard top-of-page |
| Section `h2` | `clamp(24px, 3vw, 34px)` | 800 | 1.1 | -0.04em | Top margin 56px |
| Sub-section `h3` | 20px | 700 | 1.2 | -0.02em | Top margin 28px |
| Card label / eyebrow | 11–13px | 800 | 1.2 | 0.06em–0.08em **uppercase** | `<span class="eyebrow">` |
| Stat number | 38–46px | 900 | 1 | -0.05em | `.stat .num`, `.tier-price` |
| Body | 15–17px | 400–500 | 1.55–1.7 | normal | Body color is `--ink-2`, not `--ink` |
| Lede | 17–18px | 400 | 1.55 | normal | `p.page-lede`, `.lede`, color `--muted` |
| Caption / micro | 12–13px | 600 | 1.4 | normal | `--muted` or `--soft-muted` |

### Rules

- Use **one** `h1` per page (`.page-title` on standard pages, `.hero h1` on the home).
- Body text color is `--ink-2`, not `--ink` — slightly softer.
- Italics are reserved for consequence emphasis ("Google pointed them to Prospa instead.").
- Use `code` (default styled) for domain names, env vars, CLI flags. Never as decoration.

---

## 4. Layout

### Page shell

```html
<main class="shell">
  <header class="topbar">…</header>
  {% block content %}{% endblock %}
  <footer class="footer">…</footer>
</main>
```

`.shell` is `width: min(1180px, calc(100% - 40px)); margin: 0 auto;` — used on every page.

For long-form text pages (Privacy, Terms, Support form), wrap content in `.narrow` (`max-width: 760px`).

### Grid primitives

Drop a class, get an evenly-spaced grid. All collapse responsively.

| Class | Columns | Gap |
|---|---:|---:|
| `.grid-2` | 2 | 18px |
| `.grid-3` | 3 | 18px |
| `.grid-4` | 4 | 16px |

At ≤930px they collapse to 2 columns. At ≤620px they collapse to 1.

### Radius scale

| Token | Value | Use |
|---|---:|---|
| `--radius-sm` | 16px | Inline cards, bar rows, form fields |
| (default `border-radius` for buttons) | 999px (pills) | All buttons, chips, badges |
| `--radius` | 28px | Major panels, sections, hero cards |
| (one-off larger) | 30–34px | Hero block, large CTA blocks |

### Shadows

Two main tokens — pick by elevation:

| Token | Value | Use |
|---|---|---|
| `--shadow-soft` | `0 14px 35px rgba(15,23,42,.08)` | Cards, panels, default raised state |
| `--shadow` | `0 24px 70px rgba(15,23,42,.10)` | Hero blocks, CTA blocks, modals |

For colored CTA buttons, use a tinted shadow matching the button color, e.g. `0 18px 36px rgba(37,99,235,.28)` for blue/purple gradient buttons.

---

## 5. Components

### 5.1 Brand mark

```html
<a class="brand" href="/">
  <div class="logo"><svg>…checkmark icon…</svg></div>
  <span>AEO Audit</span>
</a>
```

- Logo box: 42×42, `border-radius: 14px`, primary CTA gradient, white SVG.
- Checkmark + arrow icon (the upward-tick line) — see [_layout.html.j2](../templates/_layout.html.j2) for the path.
- Wordmark: `font-weight: 800; letter-spacing: -0.03em`.
- Used in topbar (active link to `/`) and in the footer.

### 5.2 Top nav

```html
<nav class="nav">
  <a href="/what-is-aeo">What is AEO</a>
  <div class="has-sub">
    <a href="/product/audit">Product</a>
    <div class="submenu">
      <a href="/product/audit">Audit<span class="desc">…</span></a>
      <a href="/product/monitoring">Monitoring<span class="desc">…</span></a>
    </div>
  </div>
  <a href="/how-it-works">How it works</a>
  <a href="/pricing">Pricing</a>
  <a href="/#preview" class="cta">Free preview →</a>
</nav>
```

- Links: 14px / 600 / `--muted`. Active (`.active`) and hover both → `--blue`.
- Set `{% set active = 'pricing' %}` in each page template — the layout auto-applies the `active` class.
- **Dropdown:** `.has-sub` uses `padding-bottom: 16px; margin-bottom: -16px;` so the cursor never crosses an unhoverable gap. Don't add `margin-top` to `.submenu` — it'll re-introduce the gap and the dropdown will collapse before you reach it.
- CTA pill: primary CTA gradient + white text + tinted blue shadow.

### 5.3 Footer

Four-column grid (`1.4fr 1fr 1fr 1fr`):
1. Brand block + one-line description
2. Product (Audit, Monitoring, Pricing, How it works)
3. Resources (What is AEO, Run a free preview)
4. Company (Support, Privacy, Terms)

Below the columns: a `.legal` strip with copyright + a "Get help →" link.

Collapses to 2 columns at ≤930px, 1 at ≤620px.

### 5.4 Buttons

| Class | Use | Style |
|---|---|---|
| `.btn-primary` | Default action — "Run preview", "See pricing" | Primary CTA gradient, white text, tinted shadow |
| `.btn-secondary` | Quiet alternative | White, `--ink` text, 1px line border |
| `.tier-cta` (inside pricing card) | Per-tier CTA | Black on light card; white-on-blue on featured card |
| `.nav .cta` | Always-visible primary action in nav | Primary CTA gradient pill, smaller padding |

All buttons:
- Pill shape (`border-radius: 999px`)
- Padding ≥ `12px 18px` for tap area
- `font-weight: 800`
- `text-decoration: none`

### 5.5 Eyebrow

```html
<span class="eyebrow">Pricing</span>
```

Tiny uppercase tag above a page title or section title. 11–12px / 800 / `--blue` on `#eff6ff` background, pill shape.

Inside dark hero blocks, use the inverted variant (the local `.eyebrow` redefined in landing's `extra_css` — translucent white on dark).

### 5.6 Chip

Same shape as eyebrow but more general — used for metadata tags, status indicators.

| Variant | Color |
|---|---|
| `.chip` (default) | `--muted` text on translucent white |
| `.chip.warning` | `#92400e` on warm background — used for "Free preview" |
| `.chip.blue` | `#1d4ed8` on light blue — used for hero metadata |
| `.chip.dark` | `#334155` on light gray — used for neutral tags |

### 5.7 Card / panel

```html
<div class="panel">
  <h3>Title</h3>
  <p>Body…</p>
</div>
```

- Default: `--panel-solid` background, 1px line border, `--shadow-soft`, 28px padding, 28px radius.
- Variants used in pages: `.feature` (with `.icon-box`), `.stat` (with `.num` + `.lab`), `.preview-card`, `.resource`, `.faq-item`, `.step-card`, `.era`.
- All use the same primitives — pick the existing class instead of writing new CSS.

### 5.8 Pricing card

Three-card grid (`.pricing-grid`). Used identically on home page ([landing.html.j2](../templates/landing.html.j2)) and pricing page ([pricing.html.j2](../templates/pages/pricing.html.j2)).

```html
<div class="price-card featured">
  <span class="badge">Most popular</span>
  <div class="tier-name">Full Audit</div>
  <div class="tier-price">$149<small>once</small></div>
  <p class="tagline">…</p>
  <ul>
    <li>…</li>
  </ul>
  <a href="/#preview" class="tier-cta">Start with a free preview →</a>
</div>
```

- Default card: white, 1px line, soft shadow.
- `.featured`: applies the **Featured pricing card** gradient + white text + lifted by `translateY(-4px)` + heavier shadow.
- Tier price: 44px / 900 / -0.05em letter-spacing. The `<small>` after price (e.g. "once") is 14px and muted.
- Bullets use a green checkmark `::before` (`content: "✓"`).
- "Most popular" badge: orange gradient pill, absolute positioned at top center of card.

### 5.9 Form

```html
<div class="field">
  <label for="x">Field name</label>
  <input id="x" name="x" type="text" required placeholder="…">
</div>
```

- Labels: 12px uppercase, `--muted`, 800 weight.
- Inputs: 12px padding, 12px radius, 1px `--line` border, 15px font.
- Focus state: `--blue` border + `0 0 0 3px rgba(37,99,235,.18)` ring. **Always use this** — no other focus style.
- Submit buttons take primary CTA gradient + 14px padding + tinted shadow.

### 5.10 Bullets list

```html
<ul class="bullets">
  <li>Point one</li>
</ul>
```

Custom green checkmark instead of disc. 8px vertical padding per item.

### 5.11 Mock SERP

Used on `/what-is-aeo` to illustrate old vs new search behavior. Realistic-looking but stylized — not screenshots.

```html
<div class="mock-serp">
  <div class="browser">
    <span class="dot"></span><span class="dot"></span><span class="dot"></span>
    <div class="url">google.com.au/search?q=…</div>
  </div>
  <div class="ai-block">
    <div class="label">AI answer · web search</div>
    <p>…</p>
    <div class="sources">
      <span class="src competitor">prospa.com</span>
      <span class="src you">capify.com.au</span>
    </div>
  </div>
  <div class="organic">
    <strong>Page title — site name</strong>
    Snippet text…
  </div>
</div>
```

- `.src.you` = green, `.src.competitor` = red, `.src` (default) = neutral.
- The browser bar (3 dots + URL pill) is the visual cue this is a SERP, not real content.

### 5.12 Locked section (paid-tier teaser)

Used in the free report to preview paid sections. Always pair a blurred mock with an overlay CTA — never use fake numerical data alone.

```html
<div class="locked">
  <div class="blur">…blurred mock content…</div>
  <div class="overlay">
    <div class="overlay-card">
      <div class="lock-icon">🔒</div>
      <h4>Headline framed as the value</h4>
      <p>One sentence why it matters.</p>
      <a href="#upgrade" class="btn blue">See pricing →</a>
    </div>
  </div>
</div>
```

- The `.blur` element gets `filter: blur(5px); opacity: .52; pointer-events: none;`.
- The overlay sits absolutely positioned on top with a white-fade gradient so blurred content reads as "obscured" not "broken".

### 5.13 Takeaway block (audit report only)

Orange-tinted callout for the headline finding. Two columns: signal icon + content.

- Background: **Takeaway warm** gradient.
- Border: `1px solid rgba(251,146,60,.28)`.
- Headline: `clamp(25px, 3.2vw, 40px)`, 850–900 weight, -0.05em letter-spacing.
- Italic competitor sentence sits between headline and stat-line.

---

## 6. Page templates

| URL | Template | Purpose | Layout pattern |
|---|---|---|---|
| `/` | [landing.html.j2](../templates/landing.html.j2) | Free-preview form + product overview | Hero + form + stats + how + engine strip + pricing |
| `/pricing` | [pages/pricing.html.j2](../templates/pages/pricing.html.j2) | Detailed pricing | Pricing grid + comparison table + FAQ |
| `/what-is-aeo` | [pages/what_is_aeo.html.j2](../templates/pages/what_is_aeo.html.j2) | Education | Timeline + mock SERPs + signal explanation |
| `/product/audit` | [pages/product_audit.html.j2](../templates/pages/product_audit.html.j2) | Product page (Audit) | Feature grid (icon boxes) + coverage panel |
| `/product/monitoring` | [pages/product_monitoring.html.j2](../templates/pages/product_monitoring.html.j2) | Product page (Monitoring, coming soon) | Feature grid + planned pricing + waitlist form |
| `/how-it-works` | [pages/how_it_works.html.j2](../templates/pages/how_it_works.html.j2) | Detailed walkthrough | 7 numbered step cards |
| `/privacy` | [pages/privacy.html.j2](../templates/pages/privacy.html.j2) | Legal | `.narrow` long-form |
| `/terms` | [pages/terms.html.j2](../templates/pages/terms.html.j2) | Legal | `.narrow` long-form |
| `/support` | [pages/support.html.j2](../templates/pages/support.html.j2) | Ticket form | Two-column grid: form + resources |
| `/report/{run_id}` | [report.html.j2](../templates/report.html.j2) / [report_free.html.j2](../templates/report_free.html.j2) | Audit reports | Self-contained — does NOT extend `_layout` |

### Page template skeleton

```jinja
{% extends "_layout.html.j2" %}
{% set active = 'pricing' %}
{% block title %}Pricing — AEO Audit{% endblock %}

{% block extra_css %}
  /* Page-scoped styles only. Use shared tokens. */
{% endblock %}

{% block content %}
<span class="eyebrow">Pricing</span>
<h1 class="page-title">…</h1>
<p class="page-lede">…</p>

<h2>…</h2>
…
{% endblock %}
```

`active` controls which nav link is highlighted. Valid values: `home`, `aeo`, `audit`, `monitoring`, `how`, `pricing`. Add new ones as new pages ship.

---

## 7. Responsive

Two breakpoints, applied consistently:

| Breakpoint | Behavior |
|---|---|
| **≤ 930px** | 4-col → 2-col, 3-col → 1-col (varies), nav gaps shrink to 14px, hero collapses to 1 column, pricing grid → 1 column, featured tier-card un-lifts (`transform: none`) |
| **≤ 620px** | All grids collapse to 1 column, nav `.desktop-only` items hide (Pricing + free-preview CTA stay), `h1` shrinks to 36–38px, `.takeaway` becomes 1-column |

When adding a new component:
- Default to `grid-template-columns: 1fr` and use `repeat(N, 1fr)` only at desktop sizes if needed.
- Form fields and CTAs should never wrap awkwardly on mobile — set inputs to `flex: 1 1 240px` if inline.

---

## 8. Voice & copy patterns

### Page openings

Always: `<span class="eyebrow">{section}</span>` → `<h1 class="page-title">{question or claim}</h1>` → `<p class="page-lede">{explainer}</p>`.

The H1 should ideally be a question or a sharp claim, not a noun phrase. Examples used in production:
- "How does AI describe your business?"
- "Three audit tiers. No subscriptions."
- "When buyers ask AI a question, are you in the answer?"
- "From a domain to an action plan in minutes."

### Numbers

- Always show actual numbers. "Up to 30%" beats "high percentage". "200 AI answers" beats "many answers".
- Hedge time claims softly: "in minutes" / "usually under 10". Don't promise specific minute counts you can't always hit.
- Currency formatted as `$149`, with `<small>once</small>` after.
- Percentages without decimal unless < 10 (e.g. "32%" not "32.0%"; but "8.5%" yes).

### Competitor naming

When a competitor surfaces in an audit, mention them by name in the takeaway — not just "your competitors". Italicize the consequence sentence:

> *"Google pointed them to **Prospa** instead."*

### Words to use

| Use | Don't |
|---|---|
| AEO / AI visibility | AI SEO |
| AI answer engines | Chatbots |
| Visibility / named in answers | Mentions, hits |
| Citations | Links, backlinks |
| Hallucinations / unsupported claims | Made-up answers, lies |
| Competitors surfaced | Rival brands |

---

## 9. Accessibility

- All interactive elements have visible `:focus` states. The standard ring is `box-shadow: 0 0 0 3px rgba(37,99,235,.18)` + `--blue` border.
- Contrast: body text on the gradient background passes WCAG AA. The dark hero passes AAA for white text.
- Don't communicate state via color alone — pair with text labels or icons (the `.feature-list` uses both ✓ and green; the locked sections use both 🔒 and the blurred preview).
- Keyboard navigation: the dropdown supports `:focus-within` so tab + arrow keys reach submenu items.
- Forms always have explicit `<label for="…">` and `required` where applicable.

---

## 10. Token starter (copy-paste)

```css
:root {
  --ink: #0f172a;
  --ink-2: #1e293b;
  --muted: #64748b;
  --soft-muted: #94a3b8;
  --line: rgba(15,23,42,.10);
  --bg: #f6f8fc;
  --panel: rgba(255,255,255,.78);
  --panel-solid: #ffffff;
  --blue: #2563eb;
  --purple: #7c3aed;
  --cyan: #06b6d4;
  --warning: #d97706;
  --danger: #dc2626;
  --success: #16a34a;
  --radius: 28px;
  --radius-sm: 16px;
  --shadow: 0 24px 70px rgba(15,23,42,.10);
  --shadow-soft: 0 14px 35px rgba(15,23,42,.08);
}
```

---

## 11. Quality checklist

Before adding a new page or component:

- [ ] Extends `_layout.html.j2` (unless it's a self-contained audit report)
- [ ] Sets `{% set active = '…' %}` for nav highlighting
- [ ] Uses `<span class="eyebrow">` + `<h1 class="page-title">` + `<p class="page-lede">` opening
- [ ] No hardcoded hex values — only `var(--token)`
- [ ] No new shadow scales — uses `--shadow` or `--shadow-soft`
- [ ] No new corner radii — uses 16px / 22px / 28px or 999px (pill)
- [ ] Buttons use existing classes (`.btn-primary`, `.btn-secondary`, `.tier-cta`)
- [ ] All grids collapse to 1 column ≤620px
- [ ] All forms have visible labels + the standard focus ring
- [ ] Time claims are hedged ("in minutes", "usually under 10") — never specific minute counts
- [ ] No "guaranteed" or "secret" language in any CTA
- [ ] If displaying audit data, locked sections are visibly blurred and not fake-numbered
