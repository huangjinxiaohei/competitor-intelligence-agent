from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    import tldextract
except ImportError:  # pragma: no cover - exercised only before dependency installation
    tldextract = None

from .adapters.search import SearchProvider, SearchResult
from .config import ProjectConfig
from .models import Candidate, CandidateStatus

_TRACKING_KEYS = {"gclid", "fbclid", "dclid", "msclkid", "ref", "referrer"}


def canonicalize_url(url: str) -> str:
    """Return a stable URL identity while retaining meaningful query parameters."""
    parsed = urlsplit(url.strip())
    if parsed.scheme == "fixture":
        return urlunsplit(("fixture", parsed.netloc, parsed.path.rstrip("/"), "", ""))
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        host = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    params = [
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_KEYS and not key.lower().startswith("utm_")
    ]
    return urlunsplit((scheme, host, path, urlencode(sorted(params)), ""))


def _signal_value(value: object) -> float:
    if isinstance(value, bool):
        return float(value)
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def registrable_domain(url: str) -> str:
    """Resolve a registrable domain without making a network request."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if not host:
        return ""
    if tldextract is not None:
        extracted = tldextract.TLDExtract(suffix_list_urls=())(host)
        return extracted.top_domain_under_public_suffix or host
    # The packaged dependency is preferred. This small fallback keeps existing
    # offline installations operational until dependencies are refreshed.
    labels = host.split(".")
    if len(labels) < 3:
        return host
    two_part_suffixes = {"co.uk", "org.uk", "ac.uk", "com.au", "net.au", "co.jp"}
    suffix = ".".join(labels[-2:])
    return ".".join(labels[-3:]) if suffix in two_part_suffixes else suffix


def site_origin(url: str) -> str:
    """Return the canonical origin for a discovered official URL."""
    parsed = urlsplit(canonicalize_url(url))
    if parsed.scheme == "fixture":
        return urlunsplit(("fixture", parsed.netloc, "", "", ""))
    domain = registrable_domain(url)
    if not domain:
        return canonicalize_url(url)
    return urlunsplit((parsed.scheme, domain, "", "", ""))


def stable_candidate_id(url: str) -> str:
    domain = registrable_domain(url) or canonicalize_url(url)
    digest = hashlib.sha256(domain.encode("utf-8")).hexdigest()[:12]
    return f"candidate-{digest}"

def score_candidate(
    name: str,
    homepage: str,
    category: str,
    signals: Mapping[str, object],
    config: ProjectConfig,
) -> Candidate:
    official_entry_url = canonicalize_url(homepage)
    canonical_homepage = site_origin(official_entry_url)
    weighted = sum(
        _signal_value(signals.get(signal)) * weight
        for signal, weight in config.discovery.weights.items()
    )
    score = round(max(0.0, min(1.0, weighted)), 6)
    if score >= config.discovery.auto_monitor_threshold:
        status = CandidateStatus.MONITORED
    elif score >= config.discovery.pending_threshold:
        status = CandidateStatus.PENDING
    else:
        status = CandidateStatus.REJECTED
    reasons = [
        f"{signal}={_signal_value(signals.get(signal)):.2f} (weight {weight:.2f})"
        for signal, weight in config.discovery.weights.items()
        if _signal_value(signals.get(signal)) > 0
    ] or ["No configured discovery signals matched"]
    return Candidate(
        id=stable_candidate_id(canonical_homepage),
        name=name or canonical_homepage,
        homepage=canonical_homepage,
        category=category,
        score=score,
        reasons=reasons,
        status=status,
        official_entry_urls=[official_entry_url],
    )


def _signals_for_result(result: SearchResult, config: ProjectConfig) -> dict[str, float]:
    text = f"{result.title} {result.snippet}".lower()
    keywords = [keyword.lower() for keyword in config.project.keywords if keyword.strip()]
    matched = sum(1 for keyword in keywords if keyword in text)
    keyword_match = matched / len(keywords) if keywords else 0.0
    topic_terms = [part for part in re.findall(r"[\w-]+", config.project.topic.lower()) if len(part) > 2]
    feature_overlap = max(keyword_match, float(any(term in text for term in topic_terms)))
    business_model_overlap = float(any(term in text for term in ("pricing", "price", "free trial", "subscription", "$", "currency")))
    return {
        "feature_overlap": feature_overlap,
        "audience_overlap": float(matched > 0),
        "business_model_overlap": business_model_overlap,
        "keyword_match": keyword_match,
    }


def discover_candidates(config: ProjectConfig, provider: SearchProvider) -> list[Candidate]:
    """Discover eligible candidates; search snippets are used only for ranking."""
    raw_results: list[SearchResult] = []
    for query in dict.fromkeys([*config.project.keywords, config.project.topic]):
        raw_results.extend(provider.search(query))
    for seed_url in config.project.seed_urls:
        raw_results.append(SearchResult(title=seed_url, url=seed_url, snippet=""))

    by_url: dict[str, Candidate] = {}
    for result in raw_results:
        if not result.url:
            continue
        entry_url = canonicalize_url(result.url)
        if not entry_url.startswith(("http://", "https://", "fixture://")):
            continue
        candidate = score_candidate(
            result.title.strip() or entry_url,
            entry_url,
            config.project.topic,
            _signals_for_result(result, config),
            config,
        )
        previous = by_url.get(candidate.id)
        if previous is None:
            by_url[candidate.id] = candidate
        else:
            entries = list(dict.fromkeys([*previous.official_entry_urls, *candidate.official_entry_urls]))
            winner = candidate if candidate.score > previous.score else previous
            by_url[candidate.id] = winner.model_copy(update={"official_entry_urls": entries})

    ranked = sorted(by_url.values(), key=lambda item: (-item.score, item.homepage))
    eligible = [item for item in ranked if item.status is not CandidateStatus.REJECTED]
    selected: list[Candidate] = []
    monitored = 0
    for candidate in eligible:
        if len(selected) >= config.discovery.max_candidates:
            break
        if candidate.status is CandidateStatus.MONITORED:
            if monitored < config.discovery.max_monitored:
                monitored += 1
            else:
                candidate = candidate.model_copy(update={"status": CandidateStatus.PENDING, "reasons": [*candidate.reasons, "Monitoring capacity reached"]})
        selected.append(candidate)
    return selected
