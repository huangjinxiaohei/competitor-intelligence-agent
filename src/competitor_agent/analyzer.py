from __future__ import annotations

import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx
from pydantic import ValidationError
from typing import Protocol, runtime_checkable

from .models import Candidate, Evidence, PriceTier, ProductSnapshot, SourceDocument


@runtime_checkable
class Analyzer(Protocol):
    def analyze(self, candidate: Candidate, documents: Sequence[SourceDocument]) -> ProductSnapshot: ...


def _excerpt(text: str, start: int, end: int) -> str:
    return re.sub(r"\s+", " ", text[max(0, start - 120): min(len(text), end + 180)]).strip()[:500]


def _evidence(document: SourceDocument, start: int, end: int) -> Evidence:
    return Evidence(source_url=document.url, excerpt=_excerpt(document.text, start, end), observed_at=document.fetched_at)


def _add_evidence(evidence: list[Evidence], document: SourceDocument, start: int, end: int) -> None:
    item = _evidence(document, start, end)
    if item not in evidence:
        evidence.append(item)


def _add_unique(items: list[str], value: str) -> None:
    value = value.strip(" .;:,-")
    if value and value.casefold() not in {item.casefold() for item in items}:
        items.append(value)


def analyze_documents(candidate: Candidate, documents: Sequence[SourceDocument]) -> ProductSnapshot:
    """Extract conservative product facts from collected documents and attach evidence."""
    features: list[str] = []
    specifications: dict[str, object] = {}
    configurations: dict[str, object] = {}
    pricing: list[PriceTier] = []
    evidence: list[Evidence] = []

    for document in documents:
        text = document.text
        for match in re.finditer(r"(?:features?\s+(?:include|includes|are)|includes|supports|offers)\s+([^.!?]+)", text, re.IGNORECASE):
            for feature in re.split(r"\s*,\s*|\s+and\s+", match.group(1)):
                before = len(features)
                _add_unique(features, feature)
                if len(features) > before:
                    _add_evidence(evidence, document, match.start(), match.end())
        for match in re.finditer(r"([\d][\d,]*)\s+requests?\s+(?:per\s+|/\s*)month", text, re.IGNORECASE):
            specifications.setdefault("requests_per_month", int(match.group(1).replace(",", "")))
            _add_evidence(evidence, document, match.start(), match.end())
        for match in re.finditer(r"(?:configure|configuration)\s+(model|region|deployment)\s*:\s*([A-Za-z0-9][A-Za-z0-9 _-]*?)(?=\s*(?:[.;\r\n]|$)|\s+(?:pricing|price|features?|configuration|configure|supports|includes|availability|available|plan)\s*:)", text, re.IGNORECASE):
            configurations.setdefault(match.group(1).lower(), match.group(2).strip(" ."))
            _add_evidence(evidence, document, match.start(), match.end())
        for match in re.finditer(r"([A-Za-z][A-Za-z0-9_-]{0,30})\s+plan\s*[:\-]?\s*([\$€£¥])\s*([\d,]+(?:\.\d{1,2})?)\s*(?:per|/)\s*(month|year|day)\b(?:\s+per\s+([A-Za-z]+))?", text, re.IGNORECASE):
            symbol, amount, period, unit = match.group(2), match.group(3), match.group(4).lower(), match.group(5)
            currency = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "CNY"}[symbol]
            tier = PriceTier(name=match.group(1).strip(), amount=float(amount.replace(",", "")), currency=currency, period=period, unit=unit.lower() if unit else None)
            if tier not in pricing:
                pricing.append(tier)
                _add_evidence(evidence, document, match.start(), match.end())

    fact_count = len(features) + len(specifications) + len(configurations) + len(pricing)
    summary = ""
    if fact_count:
        summary = f"{candidate.name}: extracted {fact_count} product facts from {len(documents)} collected source(s)."
    confidence = min(1.0, round(0.35 + min(fact_count, 8) * 0.08 + min(len(documents), 3) * 0.05, 2)) if fact_count else 0.0
    return ProductSnapshot(
        candidate_id=candidate.id,
        observed_at=datetime.now(UTC),
        summary=summary,
        features=features,
        specifications=specifications,
        configurations=configurations,
        pricing=pricing,
        confidence=confidence,
        evidence=evidence,
    )


class HeuristicAnalyzer:
    def analyze(self, candidate: Candidate, documents: Sequence[SourceDocument]) -> ProductSnapshot:
        return analyze_documents(candidate, documents)


class _ResponseValidationError(ValueError):
    """An API response was received but is not an acceptable snapshot."""


class HttpJsonAnalyzer:
    """Generic HTTP adapter for a model endpoint that returns a ProductSnapshot JSON object."""

    def __init__(self, timeout_seconds: float = 30.0) -> None:
        self.api_url = os.environ.get("MODEL_API_URL", "").strip()
        self.api_key = os.environ.get("MODEL_API_KEY", "").strip()
        self.model_name = os.environ.get("MODEL_NAME", "").strip()
        if not (self.api_url and self.api_key and self.model_name):
            raise RuntimeError("MODEL_API_URL, MODEL_API_KEY, and MODEL_NAME must be configured")
        self.timeout_seconds = timeout_seconds

    def _payload(
        self,
        candidate: Candidate,
        documents: Sequence[SourceDocument],
        validation_errors: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self.model_name,
            "candidate": candidate.model_dump(mode="json"),
            # These are collected first-party documents. Search snippets are deliberately absent.
            "documents": [document.model_dump(mode="json") for document in documents],
            "response_schema": ProductSnapshot.model_json_schema(),
        }
        if validation_errors is not None:
            payload["validation_errors"] = validation_errors
        return payload

    @staticmethod
    def _validate_response(raw: bytes, candidate_id: str) -> ProductSnapshot:
        try:
            snapshot = ProductSnapshot.model_validate_json(raw)
        except (ValidationError, ValueError) as exc:
            raise _ResponseValidationError(str(exc)) from None
        if snapshot.candidate_id != candidate_id:
            raise _ResponseValidationError("candidate_id must match the requested candidate")
        return snapshot

    def _post(self, client: httpx.Client, payload: dict[str, object]) -> bytes:
        try:
            response = client.post(
                self.api_url,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
            return response.content
        except httpx.HTTPError:
            # Deliberately omit exception details: they may contain an authenticated URL or key material.
            raise RuntimeError("model analyzer request failed") from None

    def analyze(self, candidate: Candidate, documents: Sequence[SourceDocument]) -> ProductSnapshot:
        validation_errors: str | None = None
        with httpx.Client(timeout=self.timeout_seconds, trust_env=False) as client:
            for attempt in range(2):
                raw = self._post(client, self._payload(candidate, documents, validation_errors))
                try:
                    return self._validate_response(raw, candidate.id)
                except _ResponseValidationError as exc:
                    if attempt == 1:
                        raise RuntimeError("model analyzer returned an invalid ProductSnapshot after retry") from None
                    validation_errors = str(exc)
        raise AssertionError("unreachable")
