from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CandidateStatus(StrEnum):
    MONITORED = "monitored"
    PENDING = "pending"
    REJECTED = "rejected"


class SourceType(StrEnum):
    HTML = "html"
    PDF = "pdf"
    SITEMAP = "sitemap"
    FIXTURE = "fixture"


class ChangeImportance(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RunStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class Evidence(StrictModel):
    source_url: str
    excerpt: str = Field(min_length=1, max_length=500)
    observed_at: datetime

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://", "fixture://")):
            raise ValueError("source_url must use http, https, or fixture scheme")
        return value


class PriceTier(StrictModel):
    name: str = Field(min_length=1)
    amount: float | None = Field(default=None, ge=0)
    currency: str | None = None
    period: str | None = None
    unit: str | None = None
    qualifiers: list[str] = Field(default_factory=list)


class Candidate(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    homepage: str
    category: str
    score: float = Field(ge=0, le=1)
    reasons: list[str] = Field(min_length=1)
    status: CandidateStatus

    @field_validator("homepage")
    @classmethod
    def validate_homepage(cls, value: str) -> str:
        if not value.startswith(("http://", "https://", "fixture://")):
            raise ValueError("homepage must use http, https, or fixture scheme")
        return value.rstrip("/")


class SourceDocument(StrictModel):
    candidate_id: str
    url: str
    title: str
    fetched_at: datetime
    content_hash: str = Field(min_length=16)
    source_type: SourceType
    text: str


class ProductSnapshot(StrictModel):
    candidate_id: str
    observed_at: datetime
    summary: str
    features: list[str] = Field(default_factory=list)
    specifications: dict[str, Any] = Field(default_factory=dict)
    configurations: dict[str, Any] = Field(default_factory=dict)
    pricing: list[PriceTier] = Field(default_factory=list)
    availability: str | None = None
    confidence: float = Field(ge=0, le=1)
    evidence: list[Evidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_evidence_for_facts(self) -> ProductSnapshot:
        has_facts = bool(
            self.summary
            or self.features
            or self.specifications
            or self.configurations
            or self.pricing
            or self.availability
        )
        if has_facts and not self.evidence:
            raise ValueError("evidence is required for structured facts")
        return self


class ChangeEvent(StrictModel):
    candidate_id: str
    field_path: str
    before: Any = None
    after: Any = None
    importance: ChangeImportance
    evidence: list[Evidence] = Field(default_factory=list)
    confirmed: bool = True
    detected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DigestProduct(StrictModel):
    candidate_id: str
    name: str
    homepage: str
    summary: str = ""
    features: list[str] = Field(default_factory=list, max_length=5)
    pricing: list[PriceTier] = Field(default_factory=list, max_length=3)
    configurations: dict[str, Any] = Field(default_factory=dict, max_length=3)
    confidence: float = Field(default=0.0, ge=0, le=1)
    evidence_urls: list[str] = Field(default_factory=list, max_length=2)


class Digest(StrictModel):
    run_id: str
    kind: str
    title: str
    summary: str
    changes: list[ChangeEvent] = Field(default_factory=list)
    products: list[DigestProduct] = Field(default_factory=list, max_length=5)
    failed_sources: list[str] = Field(default_factory=list)
    report_path: str


class DeliveryReceipt(StrictModel):
    adapter: str
    delivered: bool
    status_code: int | None = None
    detail: str = ""


class RunResult(StrictModel):
    run_id: str
    status: RunStatus
    started_at: datetime
    finished_at: datetime
    candidate_count: int = 0
    snapshot_count: int = 0
    change_count: int = 0
    digest: Digest | None = None
    delivery: DeliveryReceipt | None = None
    errors: list[str] = Field(default_factory=list)

