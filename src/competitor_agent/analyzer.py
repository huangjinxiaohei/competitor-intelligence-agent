from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

import httpx
from pydantic import ValidationError

from .models import Candidate, Evidence, PriceTier, ProductSnapshot, SourceDocument


@runtime_checkable
class Analyzer(Protocol):
    def analyze(self, candidate: Candidate, documents: Sequence[SourceDocument]) -> ProductSnapshot: ...


_CURRENCY = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "CNY", "USD": "USD", "EUR": "EUR", "GBP": "GBP", "CNY": "CNY"}
_CURRENCY_PATTERN = r"\$|€|£|¥|USD|EUR|GBP|CNY"
_PERIODS = {"month": "month", "monthly": "month", "year": "year", "annual": "year", "annually": "year", "day": "day", "daily": "day"}


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _excerpt(text: str, start: int, end: int) -> str:
    return _normalise(text[max(0, start - 120): min(len(text), end + 180)])[:500]


def _evidence(document: SourceDocument, start: int, end: int) -> Evidence:
    return Evidence(source_url=document.url, excerpt=_excerpt(document.text, start, end), observed_at=document.fetched_at)


def _add_evidence(evidence: list[Evidence], document: SourceDocument, start: int, end: int) -> Evidence:
    item = _evidence(document, start, end)
    if item not in evidence:
        evidence.append(item)
    return item


def _record_evidence(
    evidence: list[Evidence], field_evidence: dict[str, list[Evidence]], path: str, document: SourceDocument, start: int, end: int
) -> None:
    item = _add_evidence(evidence, document, start, end)
    bucket = field_evidence.setdefault(path, [])
    if item not in bucket:
        bucket.append(item)


def _add_unique(items: list[str], value: str) -> bool:
    value = _normalise(value).strip(" .;:,-—–")
    if len(value) < 3 or value.casefold() in {item.casefold() for item in items}:
        return False
    if re.search(r"(?:\$|€|£|¥|\b(?:USD|EUR|GBP|CNY)\b|\d+\s*(?:per|/))", value, re.IGNORECASE):
        return False
    items.append(value)
    return True


def _plan_name(raw: str, candidate: Candidate) -> str:
    name = _normalise(raw).strip(" :-")
    name = re.sub(r"\bplan\s+(?:starting\s+at|from)\s*$", "", name, flags=re.IGNORECASE).strip()
    name = re.sub(r"\bplan\b\s*$", "", name, flags=re.IGNORECASE).strip()
    if name.casefold().startswith(candidate.name.casefold()):
        name = name[len(candidate.name):].strip(" :-")
    words = name.split()
    # A preceding product title can be swallowed by free-form page text.
    return words[-1] if words else "Plan"

def _period_and_unit(tail: str) -> tuple[str | None, str | None]:
    lower = tail.casefold()
    period: str | None = None
    for token, normalised in _PERIODS.items():
        if re.search(rf"(?:per\s+|/\s*|\b){token}\b", lower):
            period = normalised
            break
    unit_match = re.search(r"(?:per\s+|/\s*)(?:an?\s+)?(user|seat|editor|agent|workspace|team|project|member|organization)\b", lower)
    return period, unit_match.group(1) if unit_match else None


def _tier_key(name: str) -> str:
    return re.sub(r"\s+", "-", name.casefold().strip())


def _record_price_evidence(
    evidence: list[Evidence], field_evidence: dict[str, list[Evidence]], tier: PriceTier, document: SourceDocument, start: int, end: int
) -> None:
    prefix = f"pricing.{_tier_key(tier.name)}"
    _record_evidence(evidence, field_evidence, f"{prefix}.name", document, start, end)
    for field in ("amount", "currency", "period", "unit", "qualifiers"):
        if getattr(tier, field) not in (None, [], ""):
            _record_evidence(evidence, field_evidence, f"{prefix}.{field}", document, start, end)


def _price_qualifiers(intro: str, tail: str) -> list[str]:
    text = _normalise(f"{intro} {tail}").casefold()
    qualifiers: list[str] = []
    if "starting at" in text or text.startswith("from "):
        qualifiers.append("starting at")
    if "billed annually" in text or "annual billing" in text:
        qualifiers.append("billed annually")
    commitment = re.search(r"\bannual\s+(commitment|contract)(?:\s+(required|only))?", text)
    if commitment:
        qualifiers.append(f"annual {commitment.group(1)}" + (" required" if commitment.group(2) == "required" else ""))
    if re.search(r"\bfree\s+trial\b", text):
        qualifiers.append("free trial")
    return qualifiers


def _extract_prices(candidate: Candidate, document: SourceDocument, pricing: list[PriceTier], evidence: list[Evidence], field_evidence: dict[str, list[Evidence]]) -> None:
    text = document.text
    price_pattern = re.compile(
        rf"(?P<name>[A-Z][A-Za-z0-9+ _-]{{0,60}}?)\s+(?:plan\s*[:\-\u2014\u2013]?\s*)?(?P<intro>(?:(?:starting\s+at|from)\s+)?)(?P<currency>{_CURRENCY_PATTERN})\s*(?P<amount>\d[\d,]*(?:\.\d{{1,2}})?)(?P<tail>(?:\s*(?:per|/|monthly|annual(?:ly)?|daily)[^.!?\n]*)?)",
        re.IGNORECASE,
    )
    for match in price_pattern.finditer(text):
        name = _plan_name(match.group("name"), candidate)
        if not name:
            continue
        currency_token = match.group("currency").upper()
        period, unit = _period_and_unit(match.group("tail"))
        tier = PriceTier(
            name=name,
            amount=float(match.group("amount").replace(",", "")),
            currency=_CURRENCY[currency_token],
            period=period,
            unit=unit,
            qualifiers=_price_qualifiers(match.group("intro"), match.group("tail")),
        )
        if tier not in pricing:
            pricing.append(tier)
            _record_price_evidence(evidence, field_evidence, tier, document, match.start(), match.end())

    contact_pattern = re.compile(r"(?P<name>[A-Z][A-Za-z0-9+ _-]{0,60}?)(?:\s+plan)?\s*[:\-\u2014\u2013]?\s*(?:contact|talk to)\s+sales", re.IGNORECASE)
    for match in contact_pattern.finditer(text):
        name = _plan_name(match.group("name"), candidate)
        tier = PriceTier(name=name, qualifiers=["contact sales"])
        if not any(item.name.casefold() == tier.name.casefold() for item in pricing):
            pricing.append(tier)
            _record_price_evidence(evidence, field_evidence, tier, document, match.start(), match.end())

def _confidence(field_evidence: dict[str, list[Evidence]], documents: Sequence[SourceDocument]) -> float:
    fields = len(field_evidence)
    if not fields:
        return 0.0
    linked = sum(bool(items) for items in field_evidence.values())
    coverage = linked / fields
    return min(1.0, round(0.30 + min(fields, 10) * 0.055 + min(len(documents), 4) * 0.04 + coverage * 0.08, 2))


def analyze_documents(candidate: Candidate, documents: Sequence[SourceDocument]) -> ProductSnapshot:
    """Extract conservative, field-evidenced facts from collected official documents."""
    features: list[str] = []
    specifications: dict[str, object] = {}
    configurations: dict[str, object] = {}
    pricing: list[PriceTier] = []
    evidence: list[Evidence] = []
    field_evidence: dict[str, list[Evidence]] = {}
    availability: str | None = None

    for document in documents:
        text = document.text
        for match in re.finditer(r"(?:features?\s+(?:include|includes|are)|includes|supports|offers)\s+([^.!?]+)", text, re.IGNORECASE):
            for feature in re.split(r"\s*,\s*|\s+and\s+", match.group(1)):
                if _add_unique(features, feature):
                    _record_evidence(evidence, field_evidence, f"features.{len(features) - 1}", document, match.start(), match.end())
        for match in re.finditer(r"([\d][\d,]*)\s+requests?\s+(?:per\s+|/\s*)month", text, re.IGNORECASE):
            if "requests_per_month" not in specifications:
                specifications["requests_per_month"] = int(match.group(1).replace(",", ""))
                _record_evidence(evidence, field_evidence, "specifications.requests_per_month", document, match.start(), match.end())
        for match in re.finditer(r"(?:configure|configuration)\s+(model|region|deployment)\s*:\s*([A-Za-z0-9][A-Za-z0-9 _-]*?)(?=\s*(?:[.;\r\n]|$)|\s+(?:pricing|price|features?|configuration|configure|supports|includes|availability|available|plan)\s*:)", text, re.IGNORECASE):
            key, value = match.group(1).lower(), match.group(2).strip(" .")
            if key not in configurations:
                configurations[key] = value
                _record_evidence(evidence, field_evidence, f"configurations.{key}", document, match.start(), match.end())
        for match in re.finditer(r"(?:availability\s*:\s*|available\s+(?:in|worldwide|globally)\s*)([^.!?\n]+)?", text, re.IGNORECASE):
            value = _normalise(match.group(0)).strip(" .")
            if availability is None and value:
                availability = value
                _record_evidence(evidence, field_evidence, "availability", document, match.start(), match.end())
        _extract_prices(candidate, document, pricing, evidence, field_evidence)

    fact_count = len(features) + len(specifications) + len(configurations) + len(pricing) + int(availability is not None)
    summary = f"{candidate.name}: extracted {fact_count} product facts from {len(documents)} collected source(s)." if fact_count else ""
    if summary and evidence:
        field_evidence.setdefault("summary", list(evidence))
    return ProductSnapshot(candidate_id=candidate.id, observed_at=datetime.now(UTC), summary=summary, features=features, specifications=specifications, configurations=configurations, pricing=pricing, availability=availability, confidence=_confidence(field_evidence, documents), evidence=evidence, field_evidence=field_evidence)


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

    def _payload(self, candidate: Candidate, documents: Sequence[SourceDocument], validation_errors: str | None = None) -> dict[str, object]:
        payload: dict[str, object] = {"model": self.model_name, "candidate": candidate.model_dump(mode="json"), "documents": [document.model_dump(mode="json") for document in documents], "response_schema": ProductSnapshot.model_json_schema()}
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
            response = client.post(self.api_url, json=payload, headers={"Authorization": f"Bearer {self.api_key}"})
            response.raise_for_status()
            return response.content
        except httpx.HTTPError:
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


class HybridOpenAIAnalyzer:
    """OpenAI chat-completions enrichment with deterministic extraction as the source of truth."""

    def __init__(self, timeout_seconds: float = 30.0) -> None:
        self.api_url = os.environ.get("MODEL_API_URL", "").strip()
        self.api_key = os.environ.get("MODEL_API_KEY", "").strip()
        self.model_name = os.environ.get("MODEL_NAME", "").strip()
        if not (self.api_url and self.api_key and self.model_name):
            raise RuntimeError("MODEL_API_URL, MODEL_API_KEY, and MODEL_NAME must be configured")
        self.timeout_seconds = timeout_seconds
        self.last_diagnostic = ""

    def _payload(self, candidate: Candidate, documents: Sequence[SourceDocument], retry: bool = False) -> dict[str, object]:
        instruction = "Return only a ProductSnapshot JSON object. Keep candidate_id unchanged. Every fact must cite an exact excerpt from the supplied official documents in evidence and field_evidence. Do not infer numeric prices."
        if retry:
            instruction += " The previous response was invalid; ensure all evidence excerpts literally occur in a source document."
        return {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": json.dumps({"candidate": candidate.model_dump(mode="json"), "documents": [item.model_dump(mode="json") for item in documents]}, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
        }

    @staticmethod
    def _matching_evidence(item: Evidence, documents: Sequence[SourceDocument]) -> Evidence:
        excerpt = _normalise(item.excerpt).casefold()
        for document in documents:
            if document.url == item.source_url and excerpt in _normalise(document.text).casefold():
                return Evidence(source_url=document.url, excerpt=item.excerpt, observed_at=document.fetched_at)
        raise _ResponseValidationError("model evidence does not match a collected official document")

    def _decode(self, response: httpx.Response, candidate: Candidate, documents: Sequence[SourceDocument]) -> ProductSnapshot:
        response.raise_for_status()
        try:
            raw = response.json()
            content = raw["choices"][0]["message"]["content"]
            if isinstance(content, str) and content.strip().startswith("```"):
                content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
            snapshot = ProductSnapshot.model_validate_json(content if isinstance(content, str) else json.dumps(content))
        except (KeyError, IndexError, TypeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
            raise _ResponseValidationError("invalid model JSON") from exc
        if snapshot.candidate_id != candidate.id:
            raise _ResponseValidationError("candidate_id must match the requested candidate")
        try:
            validated = [self._matching_evidence(item, documents) for item in snapshot.evidence]
            field_evidence = {path: [self._matching_evidence(item, documents) for item in items] for path, items in snapshot.field_evidence.items()}
        except _ResponseValidationError:
            raise
        return snapshot.model_copy(update={"evidence": validated, "field_evidence": field_evidence})

    @staticmethod
    def _extend_unique(target: list[Evidence], items: Sequence[Evidence]) -> None:
        for item in items:
            if item not in target:
                target.append(item)

    def _merge(self, rules: ProductSnapshot, model: ProductSnapshot, documents: Sequence[SourceDocument]) -> ProductSnapshot:
        payload = rules.model_dump(mode="python")
        field_evidence = {key: list(value) for key, value in rules.field_evidence.items()}
        evidence = list(rules.evidence)

        def model_evidence(path: str) -> list[Evidence]:
            # Generic evidence validates the response only; every merged field needs its own link.
            return list(model.field_evidence.get(path, []))

        def add_field(path: str, items: Sequence[Evidence]) -> None:
            if not items:
                return
            bucket = field_evidence.setdefault(path, [])
            self._extend_unique(bucket, items)
            self._extend_unique(evidence, items)

        summary_evidence = model_evidence("summary")
        if model.summary and summary_evidence:
            payload["summary"] = model.summary
            add_field("summary", summary_evidence)
        for index, feature in enumerate(model.features):
            source_evidence = model_evidence(f"features.{index}")
            if feature.casefold() not in {item.casefold() for item in payload["features"]} and source_evidence:
                payload["features"].append(feature)
                add_field(f"features.{len(payload['features']) - 1}", source_evidence)
        for section in ("specifications", "configurations"):
            for key, value in getattr(model, section).items():
                source_evidence = model_evidence(f"{section}.{key}")
                if key not in payload[section] and source_evidence:
                    payload[section][key] = value
                    add_field(f"{section}.{key}", source_evidence)
        availability_evidence = model_evidence("availability")
        if payload.get("availability") is None and model.availability and availability_evidence:
            payload["availability"] = model.availability
            add_field("availability", availability_evidence)
        by_name = {item["name"].casefold(): item for item in payload["pricing"]}
        for model_tier in model.pricing:
            key = model_tier.name.casefold()
            prefix = f"pricing.{_tier_key(model_tier.name)}"
            existing = by_name.get(key)
            if existing is None:
                name_evidence = model_evidence(f"{prefix}.name")
                if model_tier.amount is None and name_evidence:
                    existing = {"name": model_tier.name, "amount": None, "currency": None, "period": None, "unit": None, "qualifiers": []}
                    payload["pricing"].append(existing)
                    by_name[key] = existing
                    add_field(f"{prefix}.name", name_evidence)
                else:
                    continue
            # Amount is deliberately never model-authored. Rules own numeric prices.
            for field in ("currency", "period", "unit", "qualifiers"):
                value = getattr(model_tier, field)
                source_evidence = model_evidence(f"{prefix}.{field}")
                if existing.get(field) in (None, [], "") and value not in (None, [], "") and source_evidence:
                    existing[field] = value
                    add_field(f"{prefix}.{field}", source_evidence)
        payload["evidence"] = evidence
        payload["field_evidence"] = field_evidence
        payload["confidence"] = _confidence(field_evidence, documents)
        return ProductSnapshot.model_validate(payload)

    def analyze(self, candidate: Candidate, documents: Sequence[SourceDocument]) -> ProductSnapshot:
        rules = analyze_documents(candidate, documents)
        self.last_diagnostic = ""
        for attempt in range(2):
            try:
                with httpx.Client(timeout=self.timeout_seconds, trust_env=False) as client:
                    response = client.post(self.api_url, json=self._payload(candidate, documents, retry=attempt == 1), headers={"Authorization": f"Bearer {self.api_key}"})
                    model = self._decode(response, candidate, documents)
                return self._merge(rules, model, documents)
            except _ResponseValidationError:
                if attempt == 1:
                    self.last_diagnostic = "model_degraded: invalid model response"
                    return rules
            except httpx.HTTPError:
                self.last_diagnostic = "model_degraded: request failed"
                return rules
        self.last_diagnostic = "model_degraded: invalid model response"
        return rules


def build_analyzer(name: str) -> Analyzer:
    if name == "hybrid-openai":
        return HybridOpenAIAnalyzer()
    if name == "http-json":
        return HttpJsonAnalyzer()
    return HeuristicAnalyzer()
