from competitor_agent.adapters.search import SearchResult, StaticSearchProvider
from competitor_agent.config import ProjectConfig
from competitor_agent.discovery import canonicalize_url, discover_candidates, score_candidate
from competitor_agent.models import CandidateStatus


def make_config() -> ProjectConfig:
    return ProjectConfig.model_validate({
        "project": {"id": "demo", "topic": "agent platform", "keywords": ["agent", "automation"]},
    })


def test_canonicalize_url_removes_tracking_fragment_and_normalizes_host():
    assert canonicalize_url("HTTPS://Example.COM/path/?b=2&utm_source=x&a=1#section") == "https://example.com/path?a=1&b=2"


def test_score_candidate_uses_configured_weights_and_thresholds():
    candidate = score_candidate(
        "Acme", "https://acme.test/", "agent platform",
        {"feature_overlap": 1, "audience_overlap": 1, "business_model_overlap": 0, "keyword_match": 1},
        make_config(),
    )
    assert candidate.score == 0.8
    assert candidate.status is CandidateStatus.MONITORED
    assert candidate.homepage == "https://acme.test"


def test_discovery_deduplicates_urls_and_caps_monitored_candidates():
    provider = StaticSearchProvider({
        "agent": [
            SearchResult("Alpha", "https://alpha.test/?utm_source=a", "agent platform automation pricing"),
            SearchResult("Alpha duplicate", "https://alpha.test/#top", "agent platform"),
        ],
        "automation": [SearchResult("Beta", "https://beta.test", "agent automation pricing")],
    })
    candidates = discover_candidates(make_config(), provider)
    assert [item.homepage for item in candidates] == ["https://alpha.test", "https://beta.test"]
    assert all(item.status is not CandidateStatus.REJECTED for item in candidates)


def test_discovery_threshold_boundaries_are_inclusive() -> None:
    config = make_config()
    monitored = score_candidate("Monitor", "https://monitor.test", "agent", {"feature_overlap": 1, "audience_overlap": 1, "business_model_overlap": .5, "keyword_match": 0}, config)
    pending = score_candidate("Pending", "https://pending.test", "agent", {"feature_overlap": 1, "audience_overlap": 0, "business_model_overlap": .25, "keyword_match": 0}, config)
    assert monitored.score == config.discovery.auto_monitor_threshold
    assert monitored.status is CandidateStatus.MONITORED
    assert pending.score == config.discovery.pending_threshold
    assert pending.status is CandidateStatus.PENDING


def test_discovery_filters_rejected_and_applies_candidate_and_monitor_limits() -> None:
    config = ProjectConfig.model_validate({
        "project": {"id": "demo", "topic": "agent", "keywords": ["agent"]},
        "discovery": {"max_candidates": 3, "max_monitored": 1, "weights": {"feature_overlap": .4, "audience_overlap": .25, "business_model_overlap": .2, "keyword_match": .15}},
    })
    provider = StaticSearchProvider({"agent": [
        SearchResult(f"Candidate {index}", f"https://{index}.test", "agent pricing")
        for index in range(4)
    ] + [SearchResult("Ignored", "https://ignored.test", "unrelated")]})
    candidates = discover_candidates(config, provider)
    assert len(candidates) == 3
    assert sum(item.status is CandidateStatus.MONITORED for item in candidates) == 1
    assert sum(item.status is CandidateStatus.PENDING for item in candidates) == 2
    assert all(item.homepage != "https://ignored.test" for item in candidates)
