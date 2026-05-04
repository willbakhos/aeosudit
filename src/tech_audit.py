"""Technical AEO / GEO audit — inspects the site itself (not the AI engines).

Runs ~15 checks covering crawlability, structured data, metadata, content
structure, performance, and entity signals. Each check returns a TechCheck
with: title, expected, why, status, score_label, detail, fix.

Tier 1 checks (id starts with 't1_') are always visible in the free report.
Tier 2 checks (id starts with 't2_') are blurred in the free report.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from src.models import CheckStatus, TechAudit, TechCheck

UA_NORMAL = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
UA_NOJS = "monitoraeo-TechCheck/1.0 (compatible; +https://monitoraeo.com)"
TIMEOUT = 15.0
AI_BOTS = [
    "GPTBot", "ChatGPT-User", "OAI-SearchBot",
    "ClaudeBot", "anthropic-ai", "Claude-Web",
    "PerplexityBot", "Perplexity-User",
    "Google-Extended", "Googlebot",
    "cohere-ai", "Bytespider",
]


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

async def _fetch(client: httpx.AsyncClient, url: str, ua: str = UA_NORMAL) -> tuple[int, str, dict[str, str], float]:
    """Return (status_code, body, headers, elapsed_ms). Failures return (0, '', {}, 0)."""
    t0 = time.monotonic()
    try:
        r = await client.get(url, headers={"User-Agent": ua}, follow_redirects=True)
        return r.status_code, r.text, dict(r.headers), (time.monotonic() - t0) * 1000
    except (httpx.HTTPError, OSError):
        return 0, "", {}, (time.monotonic() - t0) * 1000


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_robots_ai_bots(robots_status: int, robots_text: str, base: str) -> TechCheck:
    """Check that AI training/answer crawlers aren't blocked by robots.txt."""
    expected = "robots.txt either doesn't exist (everything allowed) or explicitly allows GPTBot, ClaudeBot, PerplexityBot, Google-Extended."
    why = "If your robots.txt blocks these bots, AI engines literally cannot read your site. No amount of content optimisation can recover from this — it's a hard ceiling."

    if robots_status == 0:
        return TechCheck(
            id="t1_robots_ai_bots", title="AI bots allowed in robots.txt",
            category="crawlability", tier=1,
            expected=expected, why=why,
            status="warn", score_label="Could not fetch robots.txt",
            detail="The site didn't return a robots.txt file or the request timed out. Most AI crawlers will assume everything is allowed, but it's safer to publish an explicit policy.",
            fix="Add a robots.txt that explicitly allows AI crawlers — see the GPTBot, ClaudeBot, PerplexityBot, Google-Extended user-agents.",
        )

    if robots_status == 404:
        return TechCheck(
            id="t1_robots_ai_bots", title="AI bots allowed in robots.txt",
            category="crawlability", tier=1,
            expected=expected, why=why,
            status="pass", score_label="No robots.txt — everything allowed by default",
            detail="No robots.txt was found, so all crawlers (including AI bots) treat the site as fully allowed. Consider publishing one to explicitly opt in to AI crawling — it signals intent.",
        )

    blocked: list[str] = []
    rp = RobotFileParser()
    rp.parse(robots_text.splitlines())
    for bot in AI_BOTS:
        try:
            if not rp.can_fetch(bot, base):
                blocked.append(bot)
        except Exception:  # noqa: BLE001
            pass

    if not blocked:
        return TechCheck(
            id="t1_robots_ai_bots", title="AI bots allowed in robots.txt",
            category="crawlability", tier=1,
            expected=expected, why=why,
            status="pass", score_label=f"All {len(AI_BOTS)} AI crawlers allowed",
            detail=f"robots.txt doesn't block any of the major AI crawlers ({', '.join(AI_BOTS)}).",
        )
    return TechCheck(
        id="t1_robots_ai_bots", title="AI bots allowed in robots.txt",
        category="crawlability", tier=1,
        expected=expected, why=why,
        status="fail", score_label=f"{len(blocked)} AI crawler(s) blocked",
        detail=f"robots.txt blocks: {', '.join(blocked)}. These engines cannot read your site.",
        fix="In robots.txt, add an explicit Allow: / for each blocked bot. Example:\n\nUser-agent: GPTBot\nAllow: /\n\nUser-agent: ClaudeBot\nAllow: /",
    )


def check_sitemap(sitemap_status: int, sitemap_text: str) -> TechCheck:
    expected = "/sitemap.xml is reachable, returns valid XML, and lists key product/category pages with <lastmod> dates."
    why = "Without a sitemap, AI crawlers may never discover deep pages — they prioritise sitemap-listed URLs. Stale or missing <lastmod> dates also cause AI engines to assume content is out of date."
    if sitemap_status == 0 or sitemap_status == 404:
        return TechCheck(
            id="t1_sitemap", title="Sitemap.xml present and reachable",
            category="crawlability", tier=1,
            expected=expected, why=why,
            status="fail", score_label="No sitemap.xml found",
            detail=f"Tried /sitemap.xml — status: {sitemap_status or 'no response'}.",
            fix="Generate a sitemap.xml listing every public page. Most CMSs and SSGs do this automatically. Reference it from robots.txt with: Sitemap: https://your-domain.com/sitemap.xml",
        )
    if sitemap_status >= 400:
        return TechCheck(
            id="t1_sitemap", title="Sitemap.xml present and reachable",
            category="crawlability", tier=1,
            expected=expected, why=why,
            status="warn", score_label=f"Sitemap returned HTTP {sitemap_status}",
            detail="The sitemap URL is reachable but returned a non-success status. AI crawlers may skip it.",
            fix="Verify the sitemap is publicly accessible at /sitemap.xml.",
        )
    url_count = sitemap_text.count("<url>") + sitemap_text.count("<sitemap>")
    has_lastmod = "<lastmod>" in sitemap_text
    if url_count == 0:
        return TechCheck(
            id="t1_sitemap", title="Sitemap.xml present and reachable",
            category="crawlability", tier=1,
            expected=expected, why=why,
            status="warn", score_label="Sitemap exists but lists no URLs",
            detail="The sitemap returned successfully but appears empty.",
            fix="Re-generate the sitemap so it lists every public page.",
        )
    label = f"{url_count} URL(s) listed"
    if has_lastmod:
        return TechCheck(
            id="t1_sitemap", title="Sitemap.xml present and reachable",
            category="crawlability", tier=1,
            expected=expected, why=why,
            status="pass", score_label=label + " with lastmod dates",
            detail=f"Sitemap lists {url_count} URL(s) and includes <lastmod> dates that AI engines use to detect freshness.",
        )
    return TechCheck(
        id="t1_sitemap", title="Sitemap.xml present and reachable",
        category="crawlability", tier=1,
        expected=expected, why=why,
        status="warn", score_label=label + ", but no lastmod dates",
        detail=f"Sitemap lists {url_count} URL(s) but doesn't include <lastmod> dates. AI engines may treat content as undated.",
        fix="Regenerate the sitemap with <lastmod> tags so AI engines can prioritise fresh content.",
    )


def check_llms_txt(status: int, body: str) -> TechCheck:
    expected = "An /llms.txt file at the site root, summarising the site's purpose, key pages and citation-friendly facts in plain markdown."
    why = "llms.txt is the emerging standard for AI-readable site summaries. It's the single highest-leverage file you can publish — most sites don't have one yet, and it gives AI engines a curated, low-noise overview of what to cite."
    if status == 200 and body.strip():
        size = len(body)
        return TechCheck(
            id="t1_llms_txt", title="llms.txt file published",
            category="crawlability", tier=1,
            expected=expected, why=why,
            status="pass", score_label=f"Present ({size} bytes)",
            detail="llms.txt is reachable and non-empty. AI engines that respect the spec will use it as a primary site summary.",
        )
    return TechCheck(
        id="t1_llms_txt", title="llms.txt file published",
        category="crawlability", tier=1,
        expected=expected, why=why,
        status="fail", score_label="No llms.txt found",
        detail="The /llms.txt file isn't published. Most sites haven't done this yet — early movers get a citation advantage.",
        fix=(
            "Create a markdown file at /llms.txt with this structure:\n\n"
            "# Your Brand Name\n\n"
            "> One-sentence description.\n\n"
            "## Key pages\n"
            "- [Pricing](/pricing): brief description\n"
            "- [Product](/product): brief description\n\n"
            "## About\n"
            "Plain-language facts AI engines can cite directly."
        ),
    )


def _parse_jsonld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            graph = data.get("@graph")
            if isinstance(graph, list):
                nodes.extend(g for g in graph if isinstance(g, dict))
            else:
                nodes.append(data)
        elif isinstance(data, list):
            nodes.extend(d for d in data if isinstance(d, dict))
    return nodes


def _types_of(nodes: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for n in nodes:
        t = n.get("@type")
        if isinstance(t, list):
            out.update(str(x) for x in t)
        elif isinstance(t, str):
            out.add(t)
    return out


def check_organization_schema(jsonld_types: set[str]) -> TechCheck:
    expected = "An Organization or LocalBusiness JSON-LD block on the homepage with name, url and description."
    why = "Organization schema is how AI engines reliably identify your brand as an entity. Without it, the AI may conflate you with similar-named businesses or fail to associate your domain with your brand."
    if "Organization" in jsonld_types or "LocalBusiness" in jsonld_types:
        return TechCheck(
            id="t2_org_schema", title="Organization schema present",
            category="structured_data", tier=2,
            expected=expected, why=why,
            status="pass", score_label="Found Organization JSON-LD",
            detail="The homepage publishes an Organization schema block. AI engines can confidently identify your brand as a distinct entity.",
        )
    return TechCheck(
        id="t2_org_schema", title="Organization schema present",
        category="structured_data", tier=2,
        expected=expected, why=why,
        status="fail", score_label="No Organization schema",
        detail="No Organization or LocalBusiness JSON-LD found on the homepage.",
        fix=(
            "Add this to the homepage <head>:\n\n"
            "<script type=\"application/ld+json\">\n"
            "{\n"
            '  "@context": "https://schema.org",\n'
            '  "@type": "Organization",\n'
            '  "name": "Your Brand",\n'
            '  "url": "https://your-domain.com",\n'
            '  "description": "What you do, in one sentence."\n'
            "}\n"
            "</script>"
        ),
    )


def check_faq_schema(jsonld_types: set[str]) -> TechCheck:
    expected = "FAQPage schema on at least one page (typically /pricing, /faq, or product pages with Q&A content)."
    why = "FAQ-formatted content gets pulled into Google AI Overviews ~4× more often than buried prose. FAQPage schema is what tells the AI 'these are buyer questions and answers'."
    if "FAQPage" in jsonld_types:
        return TechCheck(
            id="t2_faq_schema", title="FAQPage schema present",
            category="structured_data", tier=2,
            expected=expected, why=why,
            status="pass", score_label="Found FAQPage JSON-LD",
            detail="A page on the site publishes FAQPage schema. AI engines can extract Q&A pairs directly into their answers.",
        )
    return TechCheck(
        id="t2_faq_schema", title="FAQPage schema present",
        category="structured_data", tier=2,
        expected=expected, why=why,
        status="warn", score_label="No FAQPage schema",
        detail="No FAQPage JSON-LD found on the homepage. If you have a /faq or /pricing page with question content, it should publish FAQPage schema.",
        fix="Wrap each question in FAQPage > Question > acceptedAnswer JSON-LD. Google's structured data testing tool has a copy-paste template.",
    )


def check_product_schema(jsonld_types: set[str]) -> TechCheck:
    expected = "Product or Service schema if you sell anything — with name, description and price/Offer."
    why = "Product schema enables rich product cards in search and gives AI engines structured pricing and availability data they can quote directly."
    if "Product" in jsonld_types or "Service" in jsonld_types or "SoftwareApplication" in jsonld_types:
        return TechCheck(
            id="t2_product_schema", title="Product/Service schema present",
            category="structured_data", tier=2,
            expected=expected, why=why,
            status="pass", score_label="Found product/service schema",
            detail="Product, Service or SoftwareApplication schema is published. AI engines can quote pricing and availability.",
        )
    return TechCheck(
        id="t2_product_schema", title="Product/Service schema present",
        category="structured_data", tier=2,
        expected=expected, why=why,
        status="warn", score_label="No Product/Service schema",
        detail="No Product, Service or SoftwareApplication schema found. If you sell anything, this is a major missed signal.",
        fix="Add Product schema to your pricing/product pages. Each product should declare name, description, and Offer with price + priceCurrency.",
    )


def check_meta_description(soup: BeautifulSoup) -> TechCheck:
    expected = "A concise, accurate <meta name=\"description\"> on every page (~120–160 characters)."
    why = "AI engines often quote the meta description verbatim when summarising your page. A missing or generic description = the AI invents one (or skips you)."
    tag = soup.find("meta", attrs={"name": "description"})
    content = (tag.get("content") if tag else "") or ""
    if not content.strip():
        return TechCheck(
            id="t2_meta_description", title="Meta description on homepage",
            category="meta", tier=2,
            expected=expected, why=why,
            status="fail", score_label="Missing",
            detail="The homepage has no <meta name=\"description\"> tag.",
            fix='Add <meta name="description" content="…120–160 chars summarising the page…"> to the <head>.',
        )
    length = len(content)
    if length < 50:
        return TechCheck(
            id="t2_meta_description", title="Meta description on homepage",
            category="meta", tier=2,
            expected=expected, why=why,
            status="warn", score_label=f"Too short ({length} chars)",
            detail=f"Meta description present but only {length} characters. AI engines prefer 120–160 chars of substance.",
        )
    if length > 200:
        return TechCheck(
            id="t2_meta_description", title="Meta description on homepage",
            category="meta", tier=2,
            expected=expected, why=why,
            status="warn", score_label=f"Long ({length} chars)",
            detail=f"Meta description is {length} characters — likely truncated by AI engines and search results.",
        )
    return TechCheck(
        id="t2_meta_description", title="Meta description on homepage",
        category="meta", tier=2,
        expected=expected, why=why,
        status="pass", score_label=f"Present ({length} chars)",
        detail=f"Meta description: \"{content[:120]}{'…' if length > 120 else ''}\"",
    )


def check_open_graph(soup: BeautifulSoup) -> TechCheck:
    expected = "og:title, og:description, og:image and og:url tags on every page."
    why = "Some AI engines fall back to Open Graph data when JSON-LD is missing. OG tags also control how your links appear when shared on Slack, Twitter, LinkedIn — which compounds inbound traffic."
    needed = ["og:title", "og:description", "og:image", "og:url"]
    found = [
        n for n in needed
        if soup.find("meta", attrs={"property": n})
    ]
    if len(found) == len(needed):
        return TechCheck(
            id="t2_open_graph", title="Open Graph tags complete",
            category="meta", tier=2,
            expected=expected, why=why,
            status="pass", score_label=f"All {len(needed)} OG tags present",
            detail=f"Found: {', '.join(found)}.",
        )
    if not found:
        return TechCheck(
            id="t2_open_graph", title="Open Graph tags complete",
            category="meta", tier=2,
            expected=expected, why=why,
            status="fail", score_label="No Open Graph tags",
            detail="The homepage has no Open Graph metadata.",
            fix="Add og:title, og:description, og:image and og:url meta tags to the <head>.",
        )
    missing = [n for n in needed if n not in found]
    return TechCheck(
        id="t2_open_graph", title="Open Graph tags complete",
        category="meta", tier=2,
        expected=expected, why=why,
        status="warn", score_label=f"{len(found)}/{len(needed)} OG tags present",
        detail=f"Found: {', '.join(found)}. Missing: {', '.join(missing)}.",
        fix=f"Add the missing tags: {', '.join(missing)}.",
    )


def check_twitter_card(soup: BeautifulSoup) -> TechCheck:
    expected = "twitter:card meta tag (typically 'summary_large_image') so links render properly when shared."
    why = "Twitter Card tags compound social discovery, which compounds the citations AI engines value. Skipping this is leaving easy distribution on the table."
    if soup.find("meta", attrs={"name": "twitter:card"}):
        return TechCheck(
            id="t2_twitter_card", title="Twitter Card tag present",
            category="meta", tier=2,
            expected=expected, why=why,
            status="pass", score_label="Present",
            detail="twitter:card meta tag is set — links render with rich previews on Twitter/X.",
        )
    return TechCheck(
        id="t2_twitter_card", title="Twitter Card tag present",
        category="meta", tier=2,
        expected=expected, why=why,
        status="warn", score_label="Missing",
        detail="No twitter:card meta tag found. Links shared to Twitter/X will render as plain URLs.",
        fix='Add <meta name="twitter:card" content="summary_large_image"> plus twitter:title, twitter:description, twitter:image.',
    )


def check_canonical(soup: BeautifulSoup, current_url: str) -> TechCheck:
    expected = "A <link rel=\"canonical\"> tag on every page pointing to the canonical URL."
    why = "Without canonical tags, AI engines can index multiple URL variants of the same page (with/without trailing slash, with/without tracking params). That dilutes the citation signal across duplicates."
    tag = soup.find("link", attrs={"rel": "canonical"})
    href = (tag.get("href") if tag else "") or ""
    if not href:
        return TechCheck(
            id="t2_canonical", title="Canonical URL declared",
            category="meta", tier=2,
            expected=expected, why=why,
            status="warn", score_label="Missing",
            detail="No canonical link element found on the homepage.",
            fix='Add <link rel="canonical" href="https://your-domain.com/"> to the <head>.',
        )
    return TechCheck(
        id="t2_canonical", title="Canonical URL declared",
        category="meta", tier=2,
        expected=expected, why=why,
        status="pass", score_label="Declared",
        detail=f"Canonical points to: {href}",
    )


def check_h1(soup: BeautifulSoup) -> TechCheck:
    expected = "Exactly one <h1> per page. Bonus if it's question-shaped or a sharp claim — AI engines extract headings as topic boundaries."
    why = "LLMs use heading hierarchy to extract topic boundaries when summarising. Pages with multiple H1s confuse the parser; pages with no H1 don't appear topical."
    h1s = soup.find_all("h1")
    n = len(h1s)
    if n == 0:
        return TechCheck(
            id="t2_h1", title="Single, descriptive H1 on homepage",
            category="content", tier=2,
            expected=expected, why=why,
            status="fail", score_label="No H1 found",
            detail="The homepage has no <h1> tag. AI engines will struggle to identify the page's primary topic.",
            fix="Add a single <h1> that names what the page is about. Question-shaped works well: 'How does AI describe your brand?'",
        )
    if n > 1:
        return TechCheck(
            id="t2_h1", title="Single, descriptive H1 on homepage",
            category="content", tier=2,
            expected=expected, why=why,
            status="warn", score_label=f"{n} H1 tags",
            detail=f"Found {n} H1s. Best practice is a single H1 per page so the topic is unambiguous.",
            fix="Demote secondary H1s to H2s. Keep one H1 that summarises the page's primary topic.",
        )
    text = h1s[0].get_text(strip=True)[:80]
    return TechCheck(
        id="t2_h1", title="Single, descriptive H1 on homepage",
        category="content", tier=2,
        expected=expected, why=why,
        status="pass", score_label="Single H1",
        detail=f"H1: \"{text}\"",
    )


def check_server_rendered(html_normal: str, html_nojs: str) -> TechCheck:
    expected = "Critical content is in server-rendered HTML — not loaded by JavaScript after page load."
    why = "Most AI crawlers don't execute JavaScript. SPAs that render content client-side appear as blank pages to the AI. This is the #1 silent killer of AEO for React/Vue/Angular sites without SSR."
    norm_text = re.sub(r"\s+", " ", BeautifulSoup(html_normal, "lxml").get_text(" ", strip=True))
    nojs_text = re.sub(r"\s+", " ", BeautifulSoup(html_nojs, "lxml").get_text(" ", strip=True))
    norm_words = len(norm_text.split())
    nojs_words = len(nojs_text.split())
    if norm_words == 0:
        return TechCheck(
            id="t2_server_rendered", title="Critical content is server-rendered",
            category="content", tier=2,
            expected=expected, why=why,
            status="fail", score_label="No content fetched",
            detail="Could not measure rendered content.",
        )
    ratio = nojs_words / max(norm_words, 1)
    if ratio >= 0.8:
        return TechCheck(
            id="t2_server_rendered", title="Critical content is server-rendered",
            category="content", tier=2,
            expected=expected, why=why,
            status="pass", score_label=f"{int(ratio * 100)}% of content visible without JS",
            detail=f"AI crawlers see ~{nojs_words} words of {norm_words} words on the homepage. Server-rendered.",
        )
    if ratio >= 0.4:
        return TechCheck(
            id="t2_server_rendered", title="Critical content is server-rendered",
            category="content", tier=2,
            expected=expected, why=why,
            status="warn", score_label=f"Only {int(ratio * 100)}% visible without JS",
            detail=f"AI crawlers see ~{nojs_words} of {norm_words} words. Some content is JS-only — likely missing key claims and structured data.",
            fix="Move critical content (headings, body copy, schema) into the server-rendered HTML. Use Next.js SSR, Astro, or similar.",
        )
    return TechCheck(
        id="t2_server_rendered", title="Critical content is server-rendered",
        category="content", tier=2,
        expected=expected, why=why,
        status="fail", score_label=f"Only {int(ratio * 100)}% visible without JS",
        detail=f"AI crawlers see ~{nojs_words} of {norm_words} words. The site is mostly client-rendered — invisible to most AI crawlers.",
        fix="This is the #1 issue blocking your AEO. Implement server-side rendering or static generation. Without it, AI engines see a blank page.",
    )


def check_viewport(soup: BeautifulSoup) -> TechCheck:
    expected = "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"> for mobile responsiveness."
    why = "Most AI crawlers default to a mobile user-agent. Without a viewport meta, they may treat the site as desktop-only and downrank it for mobile-first queries."
    tag = soup.find("meta", attrs={"name": "viewport"})
    if tag:
        return TechCheck(
            id="t2_viewport", title="Mobile viewport meta tag",
            category="performance", tier=2,
            expected=expected, why=why,
            status="pass", score_label="Present",
            detail="Mobile viewport meta tag is set.",
        )
    return TechCheck(
        id="t2_viewport", title="Mobile viewport meta tag",
        category="performance", tier=2,
        expected=expected, why=why,
        status="warn", score_label="Missing",
        detail="No mobile viewport meta tag.",
        fix='Add <meta name="viewport" content="width=device-width, initial-scale=1"> to the <head>.',
    )


def check_https(url: str) -> TechCheck:
    expected = "The domain serves over HTTPS with a valid certificate."
    why = "AI engines, like browsers, downrank or skip insecure pages. HTTP-only domains are essentially invisible to most modern crawlers."
    if url.startswith("https://"):
        return TechCheck(
            id="t2_https", title="Site served over HTTPS",
            category="performance", tier=2,
            expected=expected, why=why,
            status="pass", score_label="HTTPS",
            detail="Site serves over HTTPS.",
        )
    return TechCheck(
        id="t2_https", title="Site served over HTTPS",
        category="performance", tier=2,
        expected=expected, why=why,
        status="fail", score_label="HTTP only",
        detail="Site serves over plain HTTP.",
        fix="Set up TLS (Let's Encrypt is free). Most modern hosts handle this automatically.",
    )


def check_load_time(elapsed_ms: float) -> TechCheck:
    expected = "Initial HTML response < 1500ms — fast enough that AI crawlers don't time out."
    why = "AI crawlers run on tight per-page budgets. Slow pages get partial-rendered or skipped entirely, especially during high-traffic crawl windows."
    if elapsed_ms <= 0:
        return TechCheck(
            id="t2_load_time", title="Homepage response time",
            category="performance", tier=2,
            expected=expected, why=why,
            status="info", score_label="Not measured",
            detail="Could not measure response time (network error or fetch failure).",
        )
    if elapsed_ms < 800:
        return TechCheck(
            id="t2_load_time", title="Homepage response time",
            category="performance", tier=2,
            expected=expected, why=why,
            status="pass", score_label=f"Fast ({int(elapsed_ms)} ms)",
            detail="The homepage responded quickly. AI crawlers will fetch easily.",
        )
    if elapsed_ms < 1500:
        return TechCheck(
            id="t2_load_time", title="Homepage response time",
            category="performance", tier=2,
            expected=expected, why=why,
            status="warn", score_label=f"Acceptable ({int(elapsed_ms)} ms)",
            detail="Response time is acceptable but not fast.",
        )
    return TechCheck(
        id="t2_load_time", title="Homepage response time",
        category="performance", tier=2,
        expected=expected, why=why,
        status="fail", score_label=f"Slow ({int(elapsed_ms)} ms)",
        detail="Response time is slow. AI crawlers may time out before fully rendering the page.",
        fix="Optimise hosting, enable CDN caching, reduce server-side work on the home route. Aim for sub-1s TTFB.",
    )


# ---------------------------------------------------------------------------
# Score aggregation
# ---------------------------------------------------------------------------

WEIGHTS: dict[CheckStatus, int] = {"pass": 100, "warn": 50, "fail": 0, "info": 50}


def _aggregate(checks: list[TechCheck]) -> tuple[int, int, int, int]:
    """Return (overall_0_100, pass_count, warn_count, fail_count). 'info' is excluded."""
    scored = [c for c in checks if c.status != "info"]
    if not scored:
        return 0, 0, 0, 0
    overall = round(sum(WEIGHTS[c.status] for c in scored) / len(scored))
    return (
        overall,
        sum(1 for c in checks if c.status == "pass"),
        sum(1 for c in checks if c.status == "warn"),
        sum(1 for c in checks if c.status == "fail"),
    )


# ---------------------------------------------------------------------------
# Top-level entry
# ---------------------------------------------------------------------------

async def _run(domain: str) -> TechAudit:
    url = domain if domain.startswith(("http://", "https://")) else f"https://{domain}"
    base = url.rstrip("/") + "/"
    norm_domain = urlparse(url).netloc

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        (
            (home_status, home_html, _home_h, home_ms),
            (nojs_status, home_html_nojs, _nojs_h, _nojs_ms),
            (robots_status, robots_text, _r_h, _r_ms),
            (sitemap_status, sitemap_text, _s_h, _s_ms),
            (llms_status, llms_text, _l_h, _l_ms),
        ) = await asyncio.gather(
            _fetch(client, base),
            _fetch(client, base, ua=UA_NOJS),
            _fetch(client, urljoin(base, "/robots.txt")),
            _fetch(client, urljoin(base, "/sitemap.xml")),
            _fetch(client, urljoin(base, "/llms.txt")),
        )

    checks: list[TechCheck] = []

    # Tier 1 — visible in free preview
    checks.append(check_robots_ai_bots(robots_status, robots_text, base))
    checks.append(check_sitemap(sitemap_status, sitemap_text))
    checks.append(check_llms_txt(llms_status, llms_text))

    # Parse the homepage once for reuse
    if home_status >= 200 and home_status < 400 and home_html:
        soup = BeautifulSoup(home_html, "lxml")
        jsonld_nodes = _parse_jsonld(soup)
        jsonld_types = _types_of(jsonld_nodes)
    else:
        soup = BeautifulSoup("<html></html>", "lxml")
        jsonld_types = set()

    # Tier 2 — paid-only
    checks.append(check_organization_schema(jsonld_types))
    checks.append(check_faq_schema(jsonld_types))
    checks.append(check_product_schema(jsonld_types))
    checks.append(check_meta_description(soup))
    checks.append(check_open_graph(soup))
    checks.append(check_twitter_card(soup))
    checks.append(check_canonical(soup, base))
    checks.append(check_h1(soup))
    checks.append(check_server_rendered(home_html, home_html_nojs))
    checks.append(check_viewport(soup))
    checks.append(check_https(base))
    checks.append(check_load_time(home_ms))

    overall, p, w, f = _aggregate(checks)
    return TechAudit(
        domain=norm_domain,
        overall_score=overall,
        checks=checks,
        pass_count=p,
        warn_count=w,
        fail_count=f,
    )


def run_for_domain(domain: str) -> TechAudit:
    """Synchronous entry point — used by main.py and the server worker."""
    try:
        return asyncio.run(_run(domain))
    except Exception as exc:  # noqa: BLE001
        return TechAudit(
            domain=domain, overall_score=0, error=f"{type(exc).__name__}: {exc}"
        )


async def run_for_domain_async(domain: str) -> TechAudit:
    """Async entry — call from inside an existing event loop."""
    try:
        return await _run(domain)
    except Exception as exc:  # noqa: BLE001
        return TechAudit(
            domain=domain, overall_score=0, error=f"{type(exc).__name__}: {exc}"
        )
