"""Seed content for the 12 DB-backed glossary entries at /glossary/{slug}.

Idempotent: `seed_glossary_pages()` only inserts entries whose slug isn't
already in the DB. Safe to call on every startup. Updating an existing
entry's content here does NOT overwrite the DB row — that's intentional so
ad-hoc API edits aren't reverted by a redeploy. To force a content update
either bump the entry's data here and DELETE the row first, or PATCH via
the /api/definitional-pages/{slug} endpoint.

Each entry mirrors the DefinitionalPage SQLModel shape exactly.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any


SEED_PAGES: list[dict[str, Any]] = [
    # ─────────────────────────────────────────────────────────────────────
    # Metrics — 4 entries
    # ─────────────────────────────────────────────────────────────────────
    {
        "slug": "visibility-metric",
        "name": "Visibility (AI search metric)",
        "parent_section": "Metrics",
        "target_kw": "ai visibility metric",
        "alternate_names": ["AI visibility", "answer visibility", "brand visibility in AI"],
        "short_definition": "Percentage of AI answers that name your brand in the response prose. Measures who the AI recommends to the buyer.",
        "meta_description": "AI visibility is the percentage of AI answers that name your brand in the prose. The primary AEO metric — measures who the AI recommends, not just who it cites.",
        "lede": "AI visibility is the percentage of AI answers that name your brand in the response prose. It measures who the AI recommends to the buyer — distinct from citation rate, which measures who the AI cites as a source.",
        "sections": [
            {"heading": "How it's calculated",
             "body_html": "<p>For a given query set: <code>visibility = answers_naming_brand / total_answers</code>, expressed as a percentage. A brand with 50% visibility appears by name in half of the AI answers across the audited query set.</p><p>The denominator is the full audited query set, not just the answers where AI mentioned <em>any</em> brand. This is important: queries where the AI gives a generic answer with no brand mentions still count against your visibility.</p>"},
            {"heading": "Visibility vs citation rate",
             "body_html": "<p>Two related but distinct metrics. <strong>Visibility</strong> = the brand is mentioned by name in the answer prose (the AI is recommending you). <strong>Citation rate</strong> = the brand's domain appears in the AI's source list (the AI trusts you as evidence). A brand can be cited without being named (often happens with comparison pages that list you among options) or named without being cited (well-known brands the AI references from memory).</p><p>Most teams should optimise for both — citation rate moves first (within 7–14 days of schema fixes), visibility follows 2–3 weeks behind once the engine has seen enough fresh evidence.</p>"},
            {"heading": "What 'good' visibility looks like",
             "body_html": "<p>Depends entirely on category competitiveness. In low-competition categories (specialised B2B SaaS verticals, niche regional services) brands routinely hit 60–80% visibility. In commodity categories (CRM, hosting, project management) a 30–40% visibility puts you firmly in the recommended set. Below 10% means AI engines have effectively no idea you exist for these queries.</p><p>Benchmark against the top brand in your category, not against an abstract target. Our public <a href='/ai-visibility'>industry rankings</a> show the visibility distribution per category.</p>"},
            {"heading": "How to improve visibility",
             "body_html": "<p>Three moves in order of leverage. (1) Earn mentions on the sites AI engines already trust in your category — typically G2, Capterra, Reddit, Wikipedia, major trade publications. Each earned mention compounds into AI citations within 2–4 weeks. (2) Publish FAQ-shape content directly answering the queries you're losing — AI engines preferentially cite pages that match the query intent structurally. (3) Build entity signals (Organization + sameAs schema, Wikipedia presence if eligible, consistent NAP across listings) so AI engines disambiguate your brand reliably.</p><p>What doesn't help: keyword density, backlinks from low-authority sites, marketing-fluff content. AI engines actively de-weight promotional prose.</p>"},
        ],
        "faqs": [
            {"q": "How is visibility different from citation rate?",
             "a": "Visibility = brand named in the answer prose (who the AI recommends). Citation rate = brand's domain in the source list (who the AI trusts as evidence). A brand can be cited without named, or named without cited. Most teams optimise for both."},
            {"q": "What's a 'good' AI visibility score?",
             "a": "Depends on category competitiveness. 60–80% is common in low-competition niches; 30–40% is strong in commodity SaaS categories. Below 10% means the AI doesn't know you exist for these queries. Benchmark against the top brand in your category, not an abstract target."},
            {"q": "How fast can visibility move?",
             "a": "Citation rate moves first, typically within 7–14 days of shipping schema fixes + new content. Visibility follows 2–3 weeks behind because AI engines need to see enough fresh evidence to confidently name a new brand in answers."},
            {"q": "Do I need 100% visibility?",
             "a": "No — and chasing it is usually unproductive. Even category leaders rarely exceed 80%. Some queries don't lend themselves to brand mentions (purely educational queries, broad how-tos). Aim to be in the recommended set on the queries your buyers actually ask."},
            {"q": "Why is my visibility 0% when I have great content?",
             "a": "Almost always one of two things: AI crawlers can't read your site (robots.txt blocks them, JS-only rendering, missing schema) — fix the GEO foundations first. Or your brand name as you write it doesn't match how AI engines naturally write it (e.g. 'JB Hi-Fi' vs 'jbhifi'). Both are diagnosable with an audit."},
        ],
        "related_slugs": ["citation-rate-meaning", "share-of-voice-in-ai-answers", "/what-is-aeo", "/answer-engine-optimization-checklist"],
    },
    {
        "slug": "citation-rate-meaning",
        "name": "Citation rate (AI search metric)",
        "parent_section": "Metrics",
        "target_kw": "citation rate ai",
        "alternate_names": ["AI citation rate", "AI source citation rate"],
        "short_definition": "Percentage of AI answers that include your domain in the source citation list. Measures who the AI trusts as evidence.",
        "meta_description": "AI citation rate is the percentage of AI answers that include your domain in the source list. The trust-and-evidence metric — different from visibility, which measures who AI recommends.",
        "lede": "AI citation rate is the percentage of AI answers that include your domain in the source list. It measures who the AI trusts as evidence — distinct from visibility, which measures who the AI recommends by name in the prose.",
        "sections": [
            {"heading": "How it's calculated",
             "body_html": "<p>For a given query set: <code>citation_rate = answers_citing_your_domain / total_answers</code>, expressed as a percentage. Citation matching is on the apex domain — a citation of <code>blog.yourbrand.com/post</code> counts the same as <code>yourbrand.com</code>.</p><p>One answer can cite your domain multiple times (e.g. citing several pages from your site for one buyer question); we count it as ONE citation for that answer. Citation rate is binary per answer — cited or not — to keep the metric easy to reason about.</p>"},
            {"heading": "Citation rate vs visibility",
             "body_html": "<p><strong>Citation rate</strong> = your domain appeared in the source list. The AI used your content as evidence for its answer. <strong>Visibility</strong> = your brand name appeared in the prose. The AI explicitly recommended you to the user.</p><p>Common patterns: well-known brands have high visibility but average citation (the AI references them from memory). Mid-market brands often have higher citation than visibility (their content gets cited but they're not yet defaults). Both signals matter — citation tells you the AI trusts your content; visibility tells you the AI is willing to put your name in front of buyers.</p>"},
            {"heading": "Why your citation rate may not match competitors'",
             "body_html": "<p>Three structural reasons. (1) Domain authority on the specific topic — AI engines weight category-specific authority more than overall site authority. A blog focused on your niche outranks a generalist site even if the latter has higher classical DR. (2) Content structure — pages with FAQPage schema and 2–3 sentence extractable answers get cited at materially higher rates than essay-form content. (3) Recency — <code>dateModified</code> matters more than total content volume for citation rate.</p>"},
            {"heading": "How to improve citation rate",
             "body_html": "<p>Citation rate is the more controllable of the two metrics — it responds to on-site changes faster than visibility does. The leverage moves: add FAQPage + Article schema everywhere, restructure long-form content into Q&amp;A blocks, update <code>dateModified</code> on stale pages with even minor revisions, ensure server-rendered HTML, allow AI crawlers explicitly in robots.txt.</p><p>Most brands see citation rate move 5–15 percentage points within 14 days of shipping these fixes. Visibility takes longer because it requires the AI to update its 'mental model' of which brands matter in a category, not just which sources it can extract from.</p>"},
        ],
        "faqs": [
            {"q": "Does citing one page count differently than citing multiple pages?",
             "a": "No. Citation rate is binary per answer — your domain appeared in the source list or it didn't. Counting individual citations would over-reward queries where the AI happens to cite multiple pages from one site."},
            {"q": "Do subdomains count?",
             "a": "Yes — citation rate matches on apex domain. Citations of blog.yourbrand.com, app.yourbrand.com, and docs.yourbrand.com all count toward yourbrand.com's citation rate."},
            {"q": "Can my citation rate exceed my visibility?",
             "a": "Yes, and often does. Comparison pages and listicles cite many brands but only name a few in the lead — being cited without being named is common for mid-tier brands."},
            {"q": "Which moves first when I optimise — citation rate or visibility?",
             "a": "Citation rate, by 2–3 weeks. The AI can update its source set within days of finding new content; updating which brands it 'recommends' takes longer because it depends on repeated reinforcement across many answers."},
            {"q": "What kills citation rate fastest?",
             "a": "JS-only rendering (AI crawlers see a blank page), robots.txt blocking AI bots, and stale dateModified on pages that AI engines have de-prioritised. All three are foundational issues — fix them before optimising anything else."},
        ],
        "related_slugs": ["visibility-metric", "share-of-voice-in-ai-answers", "/what-is-aeo", "/answer-engine-optimization-checklist"],
    },
    {
        "slug": "share-of-voice-in-ai-answers",
        "name": "Share of voice in AI answers",
        "parent_section": "Metrics",
        "target_kw": "ai share of voice",
        "alternate_names": ["AI SoV", "share of voice AEO", "AI answer SoV"],
        "short_definition": "Your brand visibility relative to the other brands the AI mentions in the same answer set. The most defensible AEO metric — normalises for query selection bias.",
        "meta_description": "AI share of voice is your brand visibility relative to competitors in the same answer set. Beats raw visibility because it normalises for which questions you choose to measure.",
        "lede": "Share of voice in AI answers is your brand's visibility relative to the other brands the AI mentions in the same answer set. The most defensible AEO metric because it normalises for query selection bias — what 'share' of the brand mentions did you win, regardless of which queries you asked?",
        "sections": [
            {"heading": "How it's calculated",
             "body_html": "<p>For a given query set: <code>your_sov = your_mentions / total_brand_mentions_across_all_brands</code>. Expressed as a percentage. Across all brands measured in the same set, share-of-voice sums to 100.</p><p>'Mentions' here means visibility-mentions: every time a brand is named in the prose of an AI answer counts as one mention. Two mentions of your brand in one answer count as one (binary per answer, like citation rate).</p>"},
            {"heading": "Why share of voice beats raw visibility",
             "body_html": "<p>Raw visibility moves based on which questions you ask. Add a softball brand-name question (<em>'Is HubSpot legitimate?'</em>) and visibility jumps. Add a hard category question (<em>'Best CRM for nonprofits with under 50 staff'</em>) and it drops. This makes raw visibility look better or worse than reality depending on query selection.</p><p>Share of voice normalises: across the same set you and your competitors were measured on, what fraction of the brand mentions did you win? It's robust against query selection and category competitiveness — a 25% SoV in CRM software means the same thing whether the query set was 8 questions or 40.</p>"},
            {"heading": "Tracking SoV over time",
             "body_html": "<p>The most actionable SoV view is a stacked area chart of your top 5–10 competitors over 6+ months. Patterns to watch: a competitor steadily eating share of voice (they're shipping AEO improvements faster than you); a sudden drop in your own SoV (a Google AI Mode update may have re-weighted sources); a new entrant appearing (PR or major content push you should investigate).</p><p>Monthly cadence is right for most brands. Weekly is too noisy unless you're actively experimenting. <a href='/ai-visibility'>Our industry rankings</a> publish category-level SoV updated monthly.</p>"},
            {"heading": "How to grow your share of voice",
             "body_html": "<p>You grow SoV either by gaining mentions (improve your own AEO) or by competitors losing them (rare, slow, mostly out of your control). The leverage is on your own side: focus on the queries where your top 2–3 competitors get named and you don't. Those are your highest-conversion AEO targets — same buyer intent, same answer slot, just need to displace whoever's currently there.</p><p>An audit shows you exactly which queries your competitors win on. Pitch the trusted sources those AI answers cite, publish content that directly addresses those query intents, ship FAQ schema for those buyer questions.</p>"},
        ],
        "faqs": [
            {"q": "How is share of voice different from visibility?",
             "a": "Visibility is absolute (% of answers naming your brand). SoV is relative (your share of all brand mentions across the same answer set). SoV is more defensible because it normalises for query selection bias."},
            {"q": "What's a good share of voice?",
             "a": "In a competitive category with 10+ established brands, 15–25% puts you in the 'recommended set'. Category leaders typically hit 30–45%. Below 5% means the AI rarely lists you alongside the competitive set."},
            {"q": "Does SoV sum to 100% across all brands?",
             "a": "Yes — across all brands measured in the same query set. If you audit 20 brands in a category, their SoVs add up to 100%."},
            {"q": "How is SoV different from market share?",
             "a": "Market share is real-world revenue or customer count. SoV is mindshare in AI answer engines. They correlate but aren't identical — strong AEO can give a smaller brand outsized SoV before it translates to market share, and incumbents with stale content can have SoV below their market share."},
            {"q": "How often should I measure share of voice?",
             "a": "Monthly for most brands. Weekly is too noisy. Quarterly misses month-to-month competitor shifts. Tie the cadence to your content shipping cadence — if you push major AEO changes monthly, measure monthly."},
        ],
        "related_slugs": ["visibility-metric", "citation-rate-meaning", "/ai-visibility", "/what-is-aeo"],
    },
    {
        "slug": "brand-hallucination-prevention",
        "name": "Brand hallucination prevention",
        "parent_section": "Metrics",
        "target_kw": "ai brand hallucination",
        "alternate_names": ["brand hallucination", "AI brand misinformation", "LLM brand fabrication"],
        "short_definition": "When AI engines fabricate false claims about your brand — invented features, wrong pricing, made-up partnerships. Caused by information vacuums where authoritative content is missing.",
        "meta_description": "Brand hallucination is when AI engines fabricate false claims about your brand. Why it happens, how to detect it, and the 5 most common claims AI gets wrong.",
        "lede": "Brand hallucination is when an AI engine fabricates false claims about your brand — invented features, wrong pricing, made-up partnerships, plagiarised reviews. It happens because the AI is filling in gaps where authoritative information is missing or contradictory.",
        "sections": [
            {"heading": "Why hallucinations happen",
             "body_html": "<p>Two structural causes. First, <strong>information vacuums</strong>: the AI is asked about a specific claim (your pricing, your security certifications, your integrations) where no authoritative source on the open web actually states the answer. The model fills the gap with plausible-but-wrong content. Second, <strong>source ambiguity</strong>: your brand name is similar to other brands or generic terms, and the AI conflates information across them.</p><p>Hallucination is not a bug in the model — it's a structural property of generative systems. You don't prevent it by complaining to OpenAI/Anthropic/Google. You prevent it by removing the information vacuums on your own side.</p>"},
            {"heading": "How to detect hallucinations in the wild",
             "body_html": "<p>Run periodic queries across the major AI engines asking factual questions about your brand: pricing, headcount, founding year, integrations, certifications, recent news. Capture the answers. Compare against your own ground truth. Any divergence is either a hallucination you need to defend against OR a real change you haven't published authoritative content for.</p><p>monitoraeo's paid audits include a hallucination-flag pass that runs a second-pass LLM against every AI response and flags claims that don't match the brand's published facts.</p>"},
            {"heading": "The 5 brand facts AI engines most commonly hallucinate",
             "body_html": "<ul><li><strong>Pricing</strong> — particularly when you've changed it recently and old prices are cached in third-party sources</li><li><strong>Integrations</strong> — AI engines confidently list integrations you don't have, often pulled from competitor pages</li><li><strong>Founding year + headcount</strong> — Crunchbase-style metadata that's often stale</li><li><strong>Security certifications</strong> (SOC 2, ISO 27001) — high-stakes facts where 'we have it' vs 'we're pursuing it' often gets garbled</li><li><strong>Funding details</strong> — round size, valuation, lead investor — fabricated when actual data is private</li></ul>"},
            {"heading": "How to prevent brand hallucinations",
             "body_html": "<p>Three moves. (1) Publish a single canonical source for every factual claim about your brand. Pricing on /pricing, security on /security, integrations on /integrations. Make these pages indexable, schema-marked (Product, Organization), and dated. (2) Update <code>dateModified</code> whenever a fact changes — AI engines weight recency. (3) Build entity signals (Organization JSON-LD with <code>sameAs</code> links to LinkedIn, Crunchbase, Wikipedia if eligible) so AI engines have a graph node to anchor facts to, not just scattered mentions.</p><p>You can't make AI engines stop generating — but you can give them factual sources to generate from. Fix the vacuum and the hallucination disappears.</p>"},
        ],
        "faqs": [
            {"q": "Can I get AI engines to stop hallucinating about my brand?",
             "a": "Indirectly. You can't tell OpenAI to suppress claims. But you can publish authoritative content for every fact AI engines get wrong — once they have an authoritative source, hallucination rates drop because there's no vacuum to fill."},
            {"q": "What's the most common hallucination?",
             "a": "Pricing. AI engines confidently quote old prices because third-party sources (review sites, blog posts) often outrank your own /pricing page in their retrieval ranking. Solution: make /pricing schema-rich (Product + Offer) and update dateModified whenever it changes."},
            {"q": "Are hallucinations more common in some engines than others?",
             "a": "Yes. Perplexity and ChatGPT-with-search are most grounded in live web content so they hallucinate less factually but can still misattribute. Claude and Gemini sometimes draw more from training data when search results are thin. Google AI Overview rarely hallucinates outright but often paraphrases inaccurately."},
            {"q": "Should I monitor brand hallucinations continuously?",
             "a": "If your brand is mid-to-large or in a high-stakes vertical (finance, healthcare, security), yes — monthly. For early-stage brands the hallucination volume is low and ad-hoc spot checks are enough. Audit tools (including ours) automate this."},
            {"q": "Can hallucinations harm my brand legally?",
             "a": "There have been cases where AI-generated false claims caused real reputational damage and several pending lawsuits against AI companies. Recourse is unclear. The defensive move is the same: publish authoritative content for every factual claim so there's no vacuum."},
        ],
        "related_slugs": ["visibility-metric", "citation-rate-meaning", "/what-is-aeo", "/answer-engine-optimization-checklist"],
    },
    # ─────────────────────────────────────────────────────────────────────
    # Engines — 5 entries
    # ─────────────────────────────────────────────────────────────────────
    {
        "slug": "how-google-ai-overviews-pick-sources",
        "name": "How Google AI Overviews pick sources",
        "parent_section": "Engines",
        "target_kw": "google ai overview sources",
        "alternate_names": ["AI Overview source selection", "AIO citations", "how AI Overview cites"],
        "short_definition": "Google AI Overview picks sources via a retrieval pipeline that overlaps with classical Google ranking but weights different signals — particularly structured data, freshness, and passage extractability.",
        "meta_description": "Google AI Overview chooses citations through a retrieval pipeline that overlaps with classical ranking but weights different signals — structured data, recency, passage extractability over raw PageRank.",
        "lede": "Google AI Overview chooses citations through a retrieval pipeline that overlaps with classical Google ranking but weights different signals. Particularly: structured data, content freshness, and passage extractability matter more than raw PageRank.",
        "sections": [
            {"heading": "The AI Overview retrieval pipeline",
             "body_html": "<p>When Google decides to render an AI Overview, it spawns a parallel retrieval pass alongside the classical SERP. The candidate set is broadly similar to what would appear in the top 50 organic results — but the re-ranking is different. The AI Overview retrieval system weights: structured data presence (FAQPage, Product, Article schemas), passage extractability (clear 2–3 sentence answers in the body), recency (dateModified is read directly), and topical authority on the specific query.</p><p>A page that ranks #5–10 organically can be cited #1 in the AI Overview if its structured data + passage quality beats the higher-ranked pages. AI Overview citations are NOT a re-rendering of PageRank — they're a parallel ranking system using overlapping but distinct signals.</p>"},
            {"heading": "Why AI Overview citations differ from organic rankings",
             "body_html": "<p>Three reasons. (1) Classical ranking optimises for click-through likelihood; AI Overview optimises for extraction quality. Pages that win the click don't necessarily provide the cleanest extractable answer. (2) Classical ranking weights backlinks heavily; AI Overview weights on-page signals (schema, structure, recency) more. (3) Classical ranking has multi-year inertia from established authority; AI Overview re-evaluates on shorter cycles, so newer content with strong schema can leapfrog established competitors.</p>"},
            {"heading": "What classical SEO signals carry over",
             "body_html": "<p>Most foundational SEO still helps: HTTPS, mobile usability, crawlability, canonical URLs, semantic HTML, page speed. These are necessary-but-not-sufficient — they get you into the candidate set. The differentiator at the re-ranking stage is AI-specific.</p><p>What doesn't carry: keyword density tactics, thin-content tricks, link schemes. AI Overview's re-ranking actively de-weights pages that look promotional or keyword-stuffed.</p>"},
            {"heading": "What new signals matter most",
             "body_html": "<p><strong>FAQPage schema</strong> — the single highest-leverage addition for AI Overview citation. Pages that wrap their FAQs in FAQPage JSON-LD get cited at materially higher rates. <strong>Article + dateModified</strong> — recency is a strong re-ranking signal; pages updated within 30 days outperform pages updated 6+ months ago for the same content quality. <strong>Lead-with-answer structure</strong> — sections that start with a 2–3 sentence direct answer to the heading get extracted. <strong>Entity signals</strong> — Organization JSON-LD with <code>sameAs</code> links helps Google's knowledge graph confidently associate your content with your brand.</p>"},
            {"heading": "How to win AI Overview citations",
             "body_html": "<p>Priority order: (1) Add FAQPage schema to every content page that has Q&amp;A-style sections. (2) Restructure long-form prose into 2–3 sentence answers at the top of each section. (3) Update <code>dateModified</code> on stale-but-accurate pages with even minor revisions. (4) Add Organization + sameAs schema sitewide. (5) Audit which competitor domains AI Overview cites for your category queries and pitch those publications.</p><p>Most teams see AI Overview citation rate move within 10–14 days of shipping FAQPage schema across their content library.</p>"},
        ],
        "faqs": [
            {"q": "Does ranking #1 on Google guarantee an AI Overview citation?",
             "a": "No. AI Overview's re-ranking pipeline weights different signals than classical PageRank. Pages that rank #5 organically can be cited #1 in AI Overview if their structured data and passage extractability are stronger."},
            {"q": "What schema type matters most for AI Overview?",
             "a": "FAQPage by a clear margin. Pages with FAQPage JSON-LD get cited at materially higher rates for informational queries — AI Overview preferentially extracts from FAQ-marked content."},
            {"q": "How often does Google update which sources AI Overview cites?",
             "a": "Per query. AI Overview re-generates on every fresh search — two searches for the same query minutes apart can return different cited sources. Track the pattern over time, not the snapshot."},
            {"q": "Are AI Overview citations a ranking signal for classical SEO?",
             "a": "Indirectly — Google doesn't treat AI Overview citations as a direct ranking factor, but the same on-page signals that help citation rate (schema, recency, semantic HTML) also help classical ranking. Optimising for one mostly benefits the other."},
            {"q": "Can I see which pages of my site are getting cited?",
             "a": "Yes, via audit tools that capture full AI Overview responses + their citation lists per query. Run the query, parse the response. AI Overview citations are the clickable links in the panel."},
        ],
        "related_slugs": ["/what-is-ai-overview", "/what-is-ai-mode", "how-chatgpt-chooses-citations", "/answer-engine-optimization-checklist"],
    },
    {
        "slug": "how-chatgpt-chooses-citations",
        "name": "How ChatGPT chooses citations",
        "parent_section": "Engines",
        "target_kw": "how chatgpt picks sources",
        "alternate_names": ["ChatGPT citations", "ChatGPT source ranking", "how ChatGPT cites"],
        "short_definition": "ChatGPT chooses citations by running a web search via its tool-use system, then re-ranking results through its own relevance model before generating the answer.",
        "meta_description": "ChatGPT picks citations by running a tool-use web search, re-ranking results on relevance + authority, then citing only the sources that materially fed the answer. Optimisation playbook inside.",
        "lede": "ChatGPT chooses citations by running a web search via its tool-use system, then re-ranking results through its own relevance model before generating the answer. The signals it weights — domain authority on the specific topic, content recency, structured data, semantic match — overlap with classical SEO but in different proportions.",
        "sections": [
            {"heading": "The ChatGPT citation pipeline",
             "body_html": "<p>When ChatGPT-with-web-search receives a query that benefits from current information, the model decides to invoke its <code>web_search</code> tool. The tool returns a search result set (provided by ChatGPT's search backend — historically Bing, more recently OpenAI's own). ChatGPT then re-ranks the returned URLs using its internal relevance model, fetches a subset, synthesises the answer, and cites only the sources that materially contributed to the response.</p><p>Important: ChatGPT doesn't cite every URL it fetched — only the ones the generated answer actually used. A page can be in the search result set, fetched, and still not cited if the answer didn't reference it.</p>"},
            {"heading": "What signals ChatGPT weights most",
             "body_html": "<p>Three signal classes dominate the re-ranking. <strong>Topical authority on the specific query</strong> — a focused page about CRM pricing outranks a generalist site's homepage even if the latter has higher overall authority. <strong>Recency</strong> — ChatGPT preferentially cites recently-updated content for queries with a temporal dimension (which is most buyer-intent queries). <strong>Semantic match between page content and query</strong> — ChatGPT's embedding-based matching favours pages that semantically address the buyer's question, not just keyword-matched ones.</p>"},
            {"heading": "Why some pages get cited and others don't",
             "body_html": "<p>Two common reasons a page in the search result set still gets skipped. (1) <strong>Content extractability</strong>: ChatGPT looks for clean, quotable passages. A page where the answer is buried 3 paragraphs in often loses to a page with the answer in the first sentence. (2) <strong>Source diversity</strong>: ChatGPT actively tries to cite a diverse set of sources rather than 5 pages from the same domain. If your competitor's page got cited first, your similar page from the same site is less likely to also be cited.</p>"},
            {"heading": "How to optimize for ChatGPT citations",
             "body_html": "<p>Priority order: (1) Make every page that answers a buyer question lead with the answer in 2–3 sentences. (2) Ensure your content is server-rendered (ChatGPT's web fetch doesn't reliably execute JavaScript). (3) Keep <code>dateModified</code> current — recency is heavily weighted. (4) Build topical clusters — multiple deep pages on one topic rank higher than one shallow page. (5) Pitch the sources ChatGPT already cites in your category; getting cited on those domains often cascades into ChatGPT citing you directly within weeks.</p>"},
        ],
        "faqs": [
            {"q": "Does ChatGPT use the same search engine as Google?",
             "a": "ChatGPT-with-web-search uses its own search backend (historically Bing-powered, increasingly OpenAI's own). Results overlap with Google but aren't identical. ChatGPT's re-ranking on top of these results is fully internal."},
            {"q": "Do my classical SEO efforts help with ChatGPT?",
             "a": "Mostly yes — strong on-page content, schema, and crawlability all help. The exception is link-building tactics specific to Google's PageRank. ChatGPT weights topical authority and recency more heavily than raw backlink count."},
            {"q": "Why does ChatGPT cite different sources for the same query on repeat asks?",
             "a": "ChatGPT's re-ranking has stochastic elements — same search result set, slightly different citations on repeat queries. Track the pattern over many queries, not individual results."},
            {"q": "How fresh does my content need to be for ChatGPT to cite it?",
             "a": "For most queries: within 12 months. For queries with explicit temporal dimensions ('best CRM 2026', 'latest features'): within 60 days for reliable citation. Bump dateModified whenever you meaningfully update a page."},
            {"q": "Can I get ChatGPT to remove a hallucinated claim about my brand?",
             "a": "Not directly. You can fill the information vacuum that caused the hallucination by publishing authoritative content (with schema) on your own site. ChatGPT updates the cited claim once it has a credible alternative source."},
        ],
        "related_slugs": ["how-claude-web-search-works", "how-perplexity-ranks-sources", "/answer-engine-optimization-checklist", "/what-is-aeo"],
    },
    {
        "slug": "how-claude-web-search-works",
        "name": "How Claude Web Search works",
        "parent_section": "Engines",
        "target_kw": "claude web search",
        "alternate_names": ["Claude citations", "Anthropic web search", "Claude tool-use search"],
        "short_definition": "Claude's web search is a tool-use capability where the model decides when to query the web, what to query for, and which results to use as evidence. Often runs multiple search hops per answer.",
        "meta_description": "Claude's web search is a tool-use capability where the model autonomously decides when and what to search. Often runs multi-hop searches, building chains of evidence per answer.",
        "lede": "Claude's web search is a tool-use capability where Claude itself decides when to query the web, what to query for, and which results to use as evidence. Unlike single-shot citation pipelines, Claude often runs multiple search hops per answer, building a chain of evidence from query to answer.",
        "sections": [
            {"heading": "The multi-hop search pattern",
             "body_html": "<p>Where other AI engines run one search per query, Claude often runs 2–4 sequential searches as it builds an answer. The first search returns broad results; Claude reads them, identifies gaps or follow-up questions, and runs more targeted searches. The final answer cites the union of sources it actually used across all hops.</p><p>This means Claude citations skew toward sources that answer specific sub-questions well rather than sources that broadly cover a topic. A focused page that answers 'what is the SOC 2 status of HubSpot' may get cited even though it's not the top-ranked page for 'HubSpot review'.</p>"},
            {"heading": "What signals Claude weights",
             "body_html": "<p>Claude's web search uses Anthropic's own ranking pipeline (not Bing or Google's). Signals that show up empirically: topical specificity (focused pages outrank generalists), recency (similar to ChatGPT — within 12 months for most queries, 60 days for temporal queries), authoritative entity associations (pages whose Organization schema clearly identifies them with the brand being asked about), and content extractability (clean answers in the first 2–3 sentences of relevant sections).</p>"},
            {"heading": "Differences from ChatGPT",
             "body_html": "<p>Three observable differences. (1) Claude tends to cite MORE sources per answer than ChatGPT — often 5–10 vs ChatGPT's 3–5. (2) Claude's multi-hop pattern means it digs into sub-claims more, so niche pages with specific answers get cited more often than generalist content. (3) Claude tends to attribute claims more carefully — if a fact came from one source, Claude is more likely to say so explicitly with an inline citation.</p>"},
            {"heading": "How to optimize for Claude",
             "body_html": "<p>The same fundamentals as ChatGPT (server-rendered HTML, schema, recency, lead-with-answer structure) plus two Claude-specific moves. (1) <strong>Publish for sub-questions, not just top-level questions</strong>. Claude's multi-hop searches favour sites with deep coverage. A blog with one 'CRM software guide' page loses to a site with 5 pages each answering one specific sub-question. (2) <strong>Strong author/entity signals matter more for Claude than for ChatGPT</strong>. Pages with Person schema + sameAs links to verifiable LinkedIn profiles get cited at higher rates — Anthropic's pipeline appears to weight author credibility explicitly.</p>"},
        ],
        "faqs": [
            {"q": "Does Claude use a different search engine than ChatGPT?",
             "a": "Yes — Claude's web search uses Anthropic's own ranking pipeline, not Bing or Google. Results overlap but aren't identical. The re-ranking is fully internal to Anthropic."},
            {"q": "How is Claude's multi-hop search different from regular search?",
             "a": "Most AI engines run one search per user query. Claude often runs 2–4 sequential searches, using the results of the first to inform the next. The final answer cites sources from across all hops."},
            {"q": "Do I need to optimize separately for Claude?",
             "a": "Mostly no — the same foundations (schema, recency, server-rendered HTML, lead-with-answer) help across all AI engines. Two Claude-specific moves: publish deep sub-question coverage and add Person schema with verifiable sameAs links."},
            {"q": "Why does Claude cite more sources than ChatGPT?",
             "a": "Claude's multi-hop pattern means it gathers evidence across multiple search passes, and Anthropic's models tend to attribute claims more explicitly. Typical answer: 5–10 cited sources vs ChatGPT's 3–5."},
            {"q": "Can I see Claude's search hops to debug my visibility?",
             "a": "Claude doesn't expose the intermediate searches by default. Audit tools that capture Claude's final responses give you the citation list. The multi-hop structure is inferred from the diversity of cited sources per answer."},
        ],
        "related_slugs": ["how-chatgpt-chooses-citations", "how-perplexity-ranks-sources", "/answer-engine-optimization-checklist", "/what-is-aeo"],
    },
    {
        "slug": "how-perplexity-ranks-sources",
        "name": "How Perplexity ranks sources",
        "parent_section": "Engines",
        "target_kw": "perplexity citations",
        "alternate_names": ["Perplexity ranking", "how Perplexity cites", "Perplexity source selection"],
        "short_definition": "Perplexity is a search-native AI — every answer is grounded in live web sources with inline numbered citations. It weights freshness, factual density, and primary-source signals more heavily than other AI engines.",
        "meta_description": "Perplexity ranks sources through a search-native pipeline that weights freshness, factual density, and primary-source signals. Every answer cites 5–15 sources inline.",
        "lede": "Perplexity is a search-native AI — every answer is grounded in live web sources, returned with inline numbered citations. Its ranking model weights freshness, factual density, and primary-source signals more heavily than ChatGPT or Claude, making it the strictest of the major AI engines on source quality.",
        "sections": [
            {"heading": "Perplexity's search-native architecture",
             "body_html": "<p>Where ChatGPT and Claude add web search as a tool the model invokes when needed, Perplexity is built search-first. Every answer query triggers a live retrieval pass — there's no 'closed-book' mode where Perplexity answers from training data only. The model is Sonar (Perplexity's own family of search-tuned LLMs), and the entire response is grounded in retrieved sources.</p><p>Consequence: Perplexity hallucinates less than other engines on factual queries but is also more sensitive to source quality. A query where the retrieved sources are weak produces a noticeably worse Perplexity answer than ChatGPT's.</p>"},
            {"heading": "What Perplexity favours in source ranking",
             "body_html": "<p>Three signals are particularly weighted. <strong>Recency</strong> — Perplexity favours fresh sources more aggressively than ChatGPT or Claude. A 2025 article often outranks a 2023 article on the same topic, even if the 2023 article has higher classical authority. <strong>Factual density</strong> — pages with specific numbers, dates, named entities, and concrete claims outrank pages with vague prose. <strong>Primary-source preference</strong> — Perplexity preferentially cites the source that originated a claim over aggregators that quote it. If your data was first published on your own site, Perplexity tends to cite you over the news outlet that picked it up.</p>"},
            {"heading": "The Pro Search difference",
             "body_html": "<p>Perplexity Pro Search runs a more elaborate retrieval pass: more queries, more sources, longer answers, with the Sonar model doing more reasoning per answer. Citation patterns shift — Pro Search tends to surface more obscure but high-relevance sources, while free Perplexity sticks closer to top-ranked results. If you're optimising for Perplexity visibility, audit both free and Pro Search separately — they can differ materially on the same query.</p>"},
            {"heading": "How to optimize for Perplexity",
             "body_html": "<p>Four high-leverage moves. (1) <strong>Lead with concrete facts</strong> — numbers, dates, specific claims. Perplexity rewards factual density. (2) <strong>Keep dateModified aggressively current</strong> — Perplexity weights recency more than other engines. Even minor revisions every 60–90 days help. (3) <strong>Publish primary data</strong> — original research, surveys, internal benchmarks. Perplexity's primary-source preference means original data tends to get cited even on hot topics where many secondary sources exist. (4) <strong>Add Article schema with explicit author + publisher entities</strong> — Perplexity's source-quality ranking takes named authorship seriously.</p>"},
        ],
        "faqs": [
            {"q": "Why does Perplexity always cite sources?",
             "a": "Perplexity is search-native — every answer is grounded in live retrieval. Unlike ChatGPT or Claude where citations are optional, Perplexity treats them as core product. The model can't answer without searching first."},
            {"q": "Does Perplexity Pro show different sources than free Perplexity?",
             "a": "Often yes — Pro Search runs more elaborate retrieval and tends to surface more obscure-but-relevant sources. Free Perplexity sticks closer to top-ranked results. Audit both if you're optimising specifically for Perplexity visibility."},
            {"q": "Is Perplexity better for primary sources?",
             "a": "Yes. Perplexity preferentially cites the source that originated a claim over aggregators that quote it. Original research and primary data published on your own site tend to get cited even when secondary sources also cover the topic."},
            {"q": "How often does Perplexity re-crawl?",
             "a": "Perplexity's search backend re-crawls continuously, but the speed at which a newly-published page becomes citable varies — typically 1–7 days. Recency is heavily weighted so fresh content moves the needle faster than on other engines."},
            {"q": "Does Perplexity hallucinate?",
             "a": "Less than other AI engines on factual queries because every answer is grounded in retrieved sources. But it can still misattribute — citing a source for a claim the source doesn't actually make. Worth monitoring on high-stakes facts."},
        ],
        "related_slugs": ["how-chatgpt-chooses-citations", "how-claude-web-search-works", "/answer-engine-optimization-checklist", "/what-is-aeo"],
    },
    {
        "slug": "how-gemini-grounding-works",
        "name": "How Gemini grounding works",
        "parent_section": "Engines",
        "target_kw": "gemini grounding",
        "alternate_names": ["Gemini citations", "Vertex AI grounding", "Google Gemini sources"],
        "short_definition": "Gemini grounding lets the model anchor answers in real web sources via Google's groundingMetadata. When enabled, Gemini returns the answer alongside a structured list of source URLs.",
        "meta_description": "Gemini grounding anchors answers in real web sources via Google's groundingMetadata. Available in Vertex AI and the Gemini app. How it works and how to be cited.",
        "lede": "Gemini grounding is the capability that lets Google's Gemini model anchor its answers in real web sources via the groundingMetadata API field. When grounding is enabled, Gemini returns its answer alongside a structured list of source URLs that fed each part of the response.",
        "sections": [
            {"heading": "How grounding works under the hood",
             "body_html": "<p>Without grounding, Gemini answers from training-data knowledge — no web sources. With grounding enabled (via Vertex AI's <code>tools=[{google_search: {}}]</code> or the Gemini app's web-search mode), the model gains access to Google Search's index. Per query, Gemini decides which retrieved results to actually use, generates the answer, and returns <code>groundingMetadata</code> with <code>groundingChunks</code> (source URLs) and <code>groundingSupports</code> (which spans of the generated text are supported by which sources).</p><p>The groundingMetadata is the closest any AI engine comes to telling you exactly which source fed each claim — far more granular than a flat citation list.</p>"},
            {"heading": "Where grounded Gemini shows up",
             "body_html": "<p>Three surfaces. (1) <strong>Gemini app</strong> (gemini.google.com) — defaults to grounding on for most queries. (2) <strong>Vertex AI API</strong> — opt-in via the <code>google_search</code> tool. Developer-facing. (3) <strong>Google AI Overview</strong> — uses Gemini-family models under the hood and has its own retrieval pipeline (covered in our <a href='/glossary/how-google-ai-overviews-pick-sources'>separate guide</a>). 'Gemini grounding' typically refers to the first two — the consumer Gemini app and the developer API.</p>"},
            {"heading": "What signals Gemini favors",
             "body_html": "<p>Because Gemini grounding uses Google Search as its retrieval backend, the signals that help classical Google ranking also help Gemini citation. Specifically: domain authority on the query topic, content freshness, structured data (Organization + Article + FAQPage), and clear semantic match between page content and query.</p><p>Where Gemini diverges from classical ranking: it weights extractability higher (clean 2–3 sentence answers in body), and it weights authority specifically on the query topic rather than overall site authority more heavily.</p>"},
            {"heading": "How to optimize for Gemini grounding",
             "body_html": "<p>The same foundations that help Google AI Overview help Gemini: FAQPage schema, Article + dateModified, Organization + sameAs entity signals, server-rendered HTML, lead-with-answer content structure. The optimisation playbook is essentially identical because both surfaces use Google Search as the retrieval backend and Gemini-family models for synthesis.</p><p>One Gemini-specific consideration: the Gemini app surfaces grounded responses in a particular UI that makes citations very prominent. Visibility in Gemini app = visibility to a fast-growing user base of consumer-facing Google users.</p>"},
        ],
        "faqs": [
            {"q": "What's the difference between Gemini grounding and Google AI Overview?",
             "a": "Both use Gemini-family models and Google Search as the retrieval backend. AI Overview is the panel that appears above standard Google search results. Gemini grounding is the capability in the Gemini app + Vertex AI API. Same engine room, different product surfaces."},
            {"q": "Do I need to optimize separately for Gemini?",
             "a": "Mostly no — the same SEO/AEO fundamentals that help Google AI Overview also help Gemini. The retrieval backend is the same. Practical priority: ship FAQPage schema, keep dateModified current, build strong entity signals."},
            {"q": "Can I see the exact source for each claim Gemini makes?",
             "a": "Yes via the API — the groundingMetadata field returns groundingSupports that map text spans to source URLs. The Gemini app shows citations as numbered inline links. More granular than most AI engines."},
            {"q": "Is Gemini grounding always on?",
             "a": "In the Gemini app, defaults to on for most queries. In Vertex AI, it's opt-in via the google_search tool. Without grounding, Gemini answers from training data only — no live web sources."},
            {"q": "How does Gemini's citation pattern differ from ChatGPT?",
             "a": "Gemini tends to cite fewer sources per answer (3–6 typical) and weights Google Search ranking signals more directly. ChatGPT's re-ranking is more independent of any single search engine's results."},
        ],
        "related_slugs": ["how-google-ai-overviews-pick-sources", "how-chatgpt-chooses-citations", "how-claude-web-search-works", "/answer-engine-optimization-checklist"],
    },
    # ─────────────────────────────────────────────────────────────────────
    # Concepts — 1 entry
    # ─────────────────────────────────────────────────────────────────────
    {
        "slug": "ai-search-vs-seo",
        "name": "AI search vs SEO",
        "parent_section": "Concepts",
        "target_kw": "ai search vs seo",
        "alternate_names": ["AI search optimization", "AI search vs classical SEO"],
        "short_definition": "Where SEO measures ranking in a list of blue links, AI search measures whether your brand is named and cited in the synthesised answer. Different user behaviour, different metrics.",
        "meta_description": "AI search vs SEO — the user behaviour shift (~30% of buyer research now starts in AI), the new metrics, and why optimising for both is the only viable position through 2026.",
        "lede": "AI search has redirected a meaningful share of buyer research from clicking blue links to reading AI-generated answers. Where SEO measures ranking in a list of blue links, AI search has no rank — there's just 'the answer,' and brands are either named in it or they're not.",
        "sections": [
            {"heading": "The behavioural shift",
             "body_html": "<p>Multiple industry reports through 2025–2026 put AI-search-led buyer research at roughly 20–30% of all buyer journeys, with the share growing 1–2 percentage points per quarter. The shift isn't uniform — informational and comparison queries shifted fastest; navigational and transactional queries are still mostly classical SEO. But across category-research queries (the top of most buyer funnels), AI has already become the dominant entry point in many verticals.</p><p>Consequence: classical SEO traffic is flat-to-declining for most B2B brands, while AI answer mentions correlate increasingly with new-customer acquisition.</p>"},
            {"heading": "What people search for in AI vs classical search",
             "body_html": "<p>Three observable differences. (1) <strong>AI queries are longer and more conversational</strong>. Classical search: 'best CRM'. AI search: 'what's the best CRM for a 20-person B2B SaaS startup that needs Salesforce integration but doesn't want enterprise pricing'. (2) <strong>AI queries skew comparison and recommendation-seeking</strong>. Users explicitly ask AI engines to recommend, not just to retrieve. (3) <strong>AI queries replace multi-step research</strong>. What previously was 'search → click 3 results → compare → decide' is now 'ask AI → get synthesised comparison → decide'.</p>"},
            {"heading": "Implications for marketers",
             "body_html": "<p>Three. (1) <strong>Top-of-funnel traffic is shifting from visiting your site to being mentioned in someone else's answer</strong>. You can be 'visited' by an AI Mode user without ever getting a page view — they read the answer that named you and went straight to demo signup. (2) <strong>The content investments that compound are different</strong>. SEO favoured long-tail keyword pages; AEO favours structurally-extractable factual content with strong entity signals. (3) <strong>Measurement requires a new tool stack</strong>. Classical analytics (Google Search Console, Ahrefs) doesn't capture AI mentions. You need either a dedicated AEO tool or manual auditing to know if AI engines name your brand.</p>"},
            {"heading": "What to measure in the AI search era",
             "body_html": "<p>Four metrics replace the SEO triumvirate of rank + traffic + backlinks. <strong>Visibility</strong> (% of AI answers naming your brand). <strong>Citation rate</strong> (% citing your domain). <strong>Share of voice</strong> (your visibility relative to competitors in the same answer set). <strong>Brand-mention sentiment + hallucination</strong> (whether the AI is accurate when it mentions you). These four together tell you the AI's 'mental model' of your brand and where it diverges from yours.</p>"},
        ],
        "faqs": [
            {"q": "Is AI search replacing classical SEO?",
             "a": "Eating the top of the funnel, not replacing the whole. Navigational queries and transactional intents still go to classical search. But category-research queries — the discovery part of the funnel — are increasingly AI-first. Optimise for both through 2026; AI-first will probably dominate by 2027–2028."},
            {"q": "How much buyer research happens in AI now?",
             "a": "Estimates vary by source and vertical. Reasonable consensus through mid-2026: ~20–30% of buyer journeys touch AI search somewhere, growing 1–2 percentage points per quarter. Higher in tech/B2B SaaS, lower in retail and local services."},
            {"q": "Can I track AI search visibility in Google Search Console?",
             "a": "Only partially — AI Overview impressions are reported (loosely) but citation-level data is not. ChatGPT, Claude, Perplexity, Gemini don't report to anyone. You need either a dedicated AEO tool or manual auditing to measure AI visibility comprehensively."},
            {"q": "Do my SEO efforts help my AI search visibility?",
             "a": "Mostly yes. Schema, server-rendered HTML, semantic structure, recency, content quality — all carry over. The exceptions: keyword-density tactics, low-value backlinks, and thin-content strategies that classical SEO sometimes rewards actively hurt AI search visibility."},
            {"q": "What's the fastest way to start measuring AI search visibility?",
             "a": "Run an audit against the 5 major AI engines (ChatGPT, Claude, Perplexity, Gemini, Google AI Overviews) with 8–40 buyer-intent queries for your category. Tools (including ours) automate this; doing it manually is feasible for 5–10 queries but breaks down at scale."},
        ],
        "related_slugs": ["/what-is-aeo", "/aeo-vs-seo", "/what-is-ai-mode", "/answer-engine-optimization-checklist"],
    },
    # ─────────────────────────────────────────────────────────────────────
    # Tactics — 2 entries
    # ─────────────────────────────────────────────────────────────────────
    {
        "slug": "structured-data-for-ai-engines",
        "name": "Structured data for AI engines",
        "parent_section": "Tactics",
        "target_kw": "schema for ai",
        "alternate_names": ["JSON-LD for AI", "schema for AEO", "structured data AI search"],
        "short_definition": "Structured data (JSON-LD) tells AI engines exactly what your page is about in a format they can parse unambiguously. The schemas that move the needle for AI citations are different from the schemas that win SEO rich results.",
        "meta_description": "Which JSON-LD schemas actually help AI engine citations vs which are wasted effort. The 6 schemas that matter most for AEO, ranked by leverage.",
        "lede": "Structured data (JSON-LD) tells AI engines exactly what your page is about, in a format they can parse without ambiguity. The schemas that move the needle for AI citations are different from the schemas that win classical SEO rich results — and getting the wrong ones wastes implementation effort.",
        "sections": [
            {"heading": "The 6 schema types that matter most for AI",
             "body_html": "<ol><li><strong>FAQPage</strong> — highest leverage. AI engines (especially Google AI Overview and ChatGPT) preferentially extract from FAQPage-marked content. Wrap every Q&amp;A block on the site.</li><li><strong>Organization</strong> — the canonical entity signal. Include name, url, logo, description, and <code>sameAs</code> links to verifiable profiles (LinkedIn, Crunchbase, Wikipedia, X). Publish sitewide via the layout.</li><li><strong>Article</strong> — for any editorial content. Include <code>headline</code>, <code>datePublished</code>, <code>dateModified</code>, <code>author</code> (with Person schema), and <code>publisher</code> reference. AI engines weight recency heavily — dateModified directly affects citation rate.</li><li><strong>Product / Service</strong> — for commercial pages. Include <code>name</code>, <code>description</code>, <code>brand</code> reference, and <code>Offer</code> with price + availability. Helps AI engines extract pricing accurately (a top hallucination vector when missing).</li><li><strong>Person</strong> — for author bylines on editorial content. Include <code>name</code>, <code>jobTitle</code>, and <code>sameAs</code> links to LinkedIn/X/GitHub. Anthropic's Claude weights named authorship explicitly.</li><li><strong>BreadcrumbList</strong> — for any page deeper than the homepage. Cheap, easy, helps AI engines build the hierarchy of your site.</li></ol>"},
            {"heading": "Schema types that DON'T help AI (and may waste effort)",
             "body_html": "<ul><li><strong>Recipe</strong>, <strong>HowTo</strong> (most cases) — unless your content is genuinely procedural, these don't help. Recipe schema on a non-recipe page is sometimes worse than no schema.</li><li><strong>VideoObject</strong> — only helps if you have actual video content. Marking a page with a single embedded YouTube as a Video page is overreach.</li><li><strong>Review / AggregateRating</strong> — these help classical SEO rich results but AI engines treat self-published ratings with suspicion. Third-party review schema (from G2, Capterra) helps more than your own.</li><li><strong>SoftwareApplication</strong> — useful in some cases, but AI engines often ignore software-specific schema in favour of Organization + Product. Lower leverage than the top 6.</li></ul>"},
            {"heading": "How to validate your schema",
             "body_html": "<p>Two tools, both free. <strong>Google's Rich Results Test</strong> (search.google.com/test/rich-results) — paste any URL, see what schemas Google parsed and which rich result types you're eligible for. <strong>Schema.org's validator</strong> (validator.schema.org) — pure spec compliance, catches structural errors. Run both on every new content page before considering it shipped.</p><p>Common mistake: schema validates structurally but the entity references (publisher @id, author @id) point to entities that aren't defined elsewhere. Use named entities (<code>@id</code>) consistently across pages so the entity graph resolves cleanly.</p>"},
            {"heading": "Common mistakes",
             "body_html": "<ul><li><strong>Putting schema in the rendered HTML but not in the initial server response</strong> — AI crawlers often don't execute JS reliably; schema injected client-side is invisible to many.</li><li><strong>Stale dateModified</strong> — bumping dateModified without actually modifying content is detectable and de-weighted. Bump only on real revisions.</li><li><strong>Duplicate Organization entries on every page with different @ids</strong> — pick one @id (e.g. <code>https://yoursite.com/#org</code>) and reference it sitewide.</li><li><strong>Marking a sales page as Article</strong> — Article schema implies editorial content. Promotional pages should be Product/Service/WebPage, not Article.</li><li><strong>FAQPage with too few Q&amp;As</strong> — under 3 Qs, FAQPage often gets ignored. Aim for 5+ substantive Q&amp;As per FAQ block.</li></ul>"},
        ],
        "faqs": [
            {"q": "Which schema type matters most for AI citations?",
             "a": "FAQPage by a clear margin. Pages with FAQPage JSON-LD get cited at materially higher rates by Google AI Overview and ChatGPT for informational queries. Make this the first schema you add."},
            {"q": "Do I need to add schema to every page?",
             "a": "Add Organization + BreadcrumbList sitewide via the layout. Article + FAQPage on every editorial page. Product/Service on commercial pages. Person on author bylines. Don't add schema types that don't apply — irrelevant schema is sometimes worse than no schema."},
            {"q": "What's the difference between JSON-LD and other schema formats?",
             "a": "JSON-LD is the recommended format — schema as a JSON block in <script type=application/ld+json>. Microdata and RDFa work but are harder to maintain. AI engines parse JSON-LD most reliably."},
            {"q": "Will adding schema improve my SEO ranking?",
             "a": "Sometimes — schema can unlock rich-result eligibility which affects click-through rate. But schema isn't a direct ranking factor in classical SEO. For AI engines, schema is a more direct citation signal."},
            {"q": "How do I know if my schema is working?",
             "a": "Use Google's Rich Results Test to verify it parses. Then monitor your AI visibility + citation rate before and after shipping new schema. Citation rate typically moves 7–14 days after shipping FAQPage on a content library."},
        ],
        "related_slugs": ["/what-is-geo", "/answer-engine-optimization-checklist", "/what-is-aeo", "llms-txt-examples"],
    },
    {
        "slug": "llms-txt-examples",
        "name": "llms.txt examples + templates",
        "parent_section": "Tactics",
        "target_kw": "llms.txt example",
        "alternate_names": ["llms.txt template", "llms.txt sample", "llms.txt examples"],
        "short_definition": "Real-world examples of llms.txt files from sites that publish them, plus a working template you can copy. Updated as the standard evolves.",
        "meta_description": "Working /llms.txt examples from monitoraeo, Anthropic and others, plus a minimal template you can copy. The spec in 60 seconds + common mistakes.",
        "lede": "Working examples of /llms.txt files from sites that publish them, plus a minimal template you can copy. The standard is young so conventions are still settling — these examples show what good looks like as of mid-2026.",
        "sections": [
            {"heading": "The /llms.txt spec in 60 seconds",
             "body_html": "<p>Four required sections in order: (1) an H1 with the site name, (2) a blockquote with a one-paragraph description of what the site is and who it's for, (3) one or more H2 sections grouping links by category (each link a markdown list item with URL + short description), (4) optional 'Optional' H2 for less-critical pages. Plain markdown, served at <code>yoursite.com/llms.txt</code> with content-type <code>text/markdown</code>. Full spec: <a href='https://llmstxt.org' target='_blank' rel='noopener'>llmstxt.org</a>. See also our <a href='/what-is-llms-txt'>What is llms.txt?</a> guide.</p>"},
            {"heading": "Example 1 — monitoraeo (our own)",
             "body_html": "<p>Our published <code>/llms.txt</code> covers the core concepts, product, pricing, engines, and metrics in ~50 lines:</p><pre># monitoraeo\n\n&gt; AI Answer Engine Optimisation (AEO) and Generative Engine Optimisation (GEO)\n&gt; audits. We measure how often Claude, ChatGPT, Perplexity, Gemini and Google\n&gt; AI Overviews name a brand, cite its domain, and recommend it in buyer-facing\n&gt; answers — then turn the gaps into a fix list.\n\n## Key concepts\n- [What is AEO?](https://www.monitoraeo.com/what-is-aeo): Answer Engine Optimisation — the practice of getting your brand named, cited and recommended...\n- [What is GEO?](https://www.monitoraeo.com/what-is-geo): Generative Engine Optimisation — the technical layer that makes a site retrievable...\n\n## Product\n- [How it works](https://www.monitoraeo.com/how-it-works): A monitoraeo audit takes a domain, runs 40 buyer-facing queries...\n- [Audit product](https://www.monitoraeo.com/product/audit): One-off diagnostic across all 5 AI engines.\n\n## Pricing\n- [Free preview](https://www.monitoraeo.com/#preview): 8 buyer-facing questions on Google AI Overviews.\n- [Two Engine Audit](https://www.monitoraeo.com/pricing): $29 one-off...</pre><p>Full file: <a href='/llms.txt'>monitoraeo.com/llms.txt</a></p>"},
            {"heading": "Example 2 — Anthropic",
             "body_html": "<p>Anthropic's <code>/llms.txt</code> is documentation-heavy, organising links by product (Claude, Console, API) and resource type (docs, examples, model cards). Notable for its disciplined use of the 'Optional' section for less-critical resources, keeping the main link list focused on what an AI agent would most commonly need.</p><p>Pattern to copy: clearly separate <em>essential</em> content (in the main H2 sections) from <em>supplementary</em> content (in 'Optional') so AI agents reading the file can prioritise.</p>"},
            {"heading": "Example 3 — minimal template you can fork",
             "body_html": "<p>Drop-in starter for any site. Replace bracketed values with your own:</p><pre># [Your Brand Name]\n\n&gt; [One-paragraph description of what your site is and who it's for. Keep\n&gt; this terse and factual — AI agents quote it. Aim for 2–3 sentences.]\n\n## About\n- [About us](https://yoursite.com/about): [One-line description]\n- [What we do](https://yoursite.com/services): [One-line description]\n\n## Product\n- [Pricing](https://yoursite.com/pricing): [One-line description]\n- [How it works](https://yoursite.com/how): [One-line description]\n- [Documentation](https://yoursite.com/docs): [One-line description]\n\n## Resources\n- [Blog](https://yoursite.com/blog): [One-line description]\n- [Guides](https://yoursite.com/guides): [One-line description]\n\n## Optional\n- [Privacy](https://yoursite.com/privacy)\n- [Terms](https://yoursite.com/terms)\n- [Contact](https://yoursite.com/contact)</pre><p>Save as <code>llms.txt</code>, serve at site root with content-type <code>text/markdown; charset=utf-8</code>, reference from robots.txt with a comment.</p>"},
            {"heading": "Common mistakes",
             "body_html": "<ul><li><strong>Including every URL on the site</strong> — defeats the purpose. llms.txt is a curated summary (10–30 links typical), not a sitemap.</li><li><strong>Skipping the blockquote</strong> — it's the most-quoted part. Write it carefully.</li><li><strong>Marketing-fluff descriptions</strong> — LLMs detect and de-weight promotional language. Stay factual.</li><li><strong>Serving as text/html</strong> — use <code>text/markdown</code> or <code>text/plain</code>. Some AI tools that sniff content-type skip HTML.</li><li><strong>Letting it go stale</strong> — broken links damage perceived authority. Review quarterly.</li></ul>"},
        ],
        "faqs": [
            {"q": "How long should my llms.txt be?",
             "a": "30–100 lines for most sites. Long enough to cover your authoritative pages with one-line descriptions, short enough to be a curated summary rather than a sitemap. If you're including more than 30 links, you're probably over-stuffing it."},
            {"q": "Do I need every link to have a description?",
             "a": "Strongly recommended for main-section links. AI agents often quote the link description as the canonical summary of the linked page. Links in the 'Optional' section can be barer."},
            {"q": "Can I include external links in llms.txt?",
             "a": "Possible but unusual. The standard is for your own content. If you reference external sources, mark them clearly so AI agents don't conflate them with your authoritative content."},
            {"q": "What content-type should I serve llms.txt with?",
             "a": "<code>text/markdown; charset=utf-8</code> preferred. <code>text/plain</code> also works. Avoid <code>text/html</code> — some AI tools that sniff content type will skip HTML files."},
            {"q": "How do I tell crawlers my llms.txt exists?",
             "a": "Add a comment line to robots.txt: <code># AI summary: https://yoursite.com/llms.txt</code>. AI crawlers that read robots.txt first will notice it. The standard hasn't formalised a dedicated robots.txt directive yet."},
        ],
        "related_slugs": ["/what-is-llms-txt", "/what-is-geo", "/answer-engine-optimization-checklist", "structured-data-for-ai-engines"],
    },
]


def seed_glossary_pages() -> None:
    """Insert any SEED_PAGES whose slug isn't already in the DB. Existing
    rows are LEFT ALONE — so API-edited content isn't reverted by deploys.
    Safe to call on every server start. No-op when DATABASE_URL isn't set."""
    import os
    if not os.environ.get("DATABASE_URL", "").strip():
        return
    try:
        from sqlmodel import select
        from src.db import DefinitionalPage, get_session
        with get_session() as s:
            existing_slugs = {row.slug for row in s.exec(select(DefinitionalPage))}
            now = datetime.utcnow()
            inserted = 0
            for entry in SEED_PAGES:
                if entry["slug"] in existing_slugs:
                    continue
                page = DefinitionalPage(
                    slug=entry["slug"],
                    name=entry["name"],
                    parent_section=entry.get("parent_section", "Concepts"),
                    target_kw=entry.get("target_kw", ""),
                    short_definition=entry.get("short_definition", ""),
                    meta_description=entry.get("meta_description", ""),
                    lede=entry.get("lede", ""),
                    sections=entry.get("sections", []),
                    faqs=entry.get("faqs", []),
                    related_slugs=entry.get("related_slugs", []),
                    alternate_names=entry.get("alternate_names", []),
                    published_at=now, updated_at=now,
                )
                s.add(page)
                inserted += 1
            if inserted:
                s.commit()
                print(f"[seed_glossary] inserted {inserted} new entries")
    except Exception as exc:  # noqa: BLE001
        print(f"[seed_glossary] skipped: {type(exc).__name__}: {exc}")
