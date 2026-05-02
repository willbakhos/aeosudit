"""Pydantic models for config and results."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------- config ----------

class BrandConfig(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    domain: str


class LocaleConfig(BaseModel):
    country: str = "US"
    language: str = "en"


class OpenRouterEngineConfig(BaseModel):
    model: str
    label: str


class ApifyEngineConfig(BaseModel):
    label: str


class EnginesConfig(BaseModel):
    openrouter: list[OpenRouterEngineConfig] = Field(default_factory=list)
    apify: list[ApifyEngineConfig] = Field(default_factory=list)


class SiteConfig(BaseModel):
    brand: BrandConfig
    competitors: list[str] = Field(default_factory=list)
    ground_truth: list[str] = Field(default_factory=list)
    locale: LocaleConfig = Field(default_factory=LocaleConfig)
    engines: EnginesConfig


class Query(BaseModel):
    query: str
    type: str
    free: bool = False
    spotlight: bool = False


# ---------- engine output ----------

class Citation(BaseModel):
    url: str
    title: str | None = None
    domain: str
    position: int


class EngineResponse(BaseModel):
    engine_label: str
    query: str
    query_type: str
    response_text: str = ""
    citations: list[Citation] = Field(default_factory=list)
    raw_response: dict[str, Any] | None = None
    error: str | None = None
    latency_ms: int = 0


# ---------- scoring output ----------

class ScoringResult(BaseModel):
    mentioned: bool = False
    mention_position: int | None = None
    cited_as_source: bool = False
    citation_rank: int | None = None
    competitors_mentioned: list[str] = Field(default_factory=list)
    competitors_cited: list[str] = Field(default_factory=list)
    all_cited_domains: list[str] = Field(default_factory=list)


SentimentLabel = Literal["positive", "neutral", "negative", "not_mentioned"]
AccuracyLabel = Literal["accurate", "partial", "contradicts_ground_truth", "not_applicable"]


class LLMScore(BaseModel):
    sentiment: SentimentLabel = "not_mentioned"
    accuracy: AccuracyLabel = "not_applicable"
    hallucination_flags: list[str] = Field(default_factory=list)
    confidence: float = 0.0


class ScoredRow(BaseModel):
    """A single (query × engine) row, fully scored."""
    response: EngineResponse
    deterministic: ScoringResult
    llm: LLMScore | None = None


# ---------- technical AEO/GEO audit ----------

CheckStatus = Literal["pass", "warn", "fail", "info"]


class TechCheck(BaseModel):
    id: str
    title: str
    category: Literal[
        "crawlability", "structured_data", "meta", "content",
        "performance", "entity",
    ]
    tier: int  # 1 = visible in free preview; 2 = paid only
    expected: str
    why: str
    status: CheckStatus
    score_label: str
    detail: str = ""
    fix: str | None = None


class TechAudit(BaseModel):
    domain: str
    overall_score: int  # 0-100
    checks: list[TechCheck] = Field(default_factory=list)
    pass_count: int = 0
    warn_count: int = 0
    fail_count: int = 0
    error: str | None = None
