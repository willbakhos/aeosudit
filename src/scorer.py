"""Deterministic scoring: regex on text, URL parse on citations."""
from __future__ import annotations

import re

from src.models import EngineResponse, ScoringResult, SiteConfig


def _word_pattern(term: str) -> re.Pattern[str]:
    """Case-insensitive match. Use word boundaries when the term is alphanumeric;
    otherwise (e.g. 'capify.com.au') use a literal substring match."""
    if re.fullmatch(r"[A-Za-z0-9 ]+", term):
        escaped = re.escape(term.strip())
        return re.compile(rf"\b{escaped}\b", re.IGNORECASE)
    return re.compile(re.escape(term), re.IGNORECASE)


def _domain_matches(target: str, candidate: str) -> bool:
    target = target.lower().lstrip(".")
    candidate = candidate.lower()
    if candidate.startswith("www."):
        candidate = candidate[4:]
    return candidate == target or candidate.endswith("." + target)


def score_response(response: EngineResponse, config: SiteConfig) -> ScoringResult:
    if response.error:
        return ScoringResult()

    text = response.response_text or ""
    brand_terms = [config.brand.name, *config.brand.aliases]

    # mention + position
    earliest: int | None = None
    for term in brand_terms:
        m = _word_pattern(term).search(text)
        if m and (earliest is None or m.start() < earliest):
            earliest = m.start()
    mentioned = earliest is not None

    # citations
    cited_as_source = False
    citation_rank: int | None = None
    for c in response.citations:
        if _domain_matches(config.brand.domain, c.domain):
            cited_as_source = True
            if citation_rank is None or c.position < citation_rank:
                citation_rank = c.position

    # competitors in text
    competitors_mentioned: list[str] = []
    for comp in config.competitors:
        if _word_pattern(comp).search(text):
            competitors_mentioned.append(comp)

    # competitor domains in citations — match on token contained in any cited domain
    competitors_cited: list[str] = []
    cited_domains_lower = [c.domain.lower() for c in response.citations]
    for comp in config.competitors:
        token = re.sub(r"[^a-z0-9]", "", comp.lower())
        if not token:
            continue
        for d in cited_domains_lower:
            stripped = d[4:] if d.startswith("www.") else d
            d_token = re.sub(r"[^a-z0-9]", "", stripped.split(".")[0])
            if token == d_token or token in stripped:
                competitors_cited.append(comp)
                break

    return ScoringResult(
        mentioned=mentioned,
        mention_position=earliest,
        cited_as_source=cited_as_source,
        citation_rank=citation_rank,
        competitors_mentioned=competitors_mentioned,
        competitors_cited=competitors_cited,
        all_cited_domains=[c.domain for c in response.citations],
    )
