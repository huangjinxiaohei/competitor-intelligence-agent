from datetime import UTC, datetime

from competitor_agent.diffing import diff_snapshots
from competitor_agent.models import Evidence, PriceTier, ProductSnapshot

NOW = datetime(2026, 1, 1, tzinfo=UTC)
EVIDENCE = [Evidence(source_url="https://acme.test", excerpt="evidence", observed_at=NOW)]


def snap(**overrides) -> ProductSnapshot:
    values = {
        "candidate_id": "candidate-1", "observed_at": NOW, "summary": "Product summary",
        "features": ["fast"], "specifications": {"memory": "8GB"},
        "configurations": {"region": "us"},
        "pricing": [PriceTier(name="Pro", amount=10, currency="usd", period="monthly")],
        "availability": "available", "confidence": 0.9, "evidence": EVIDENCE,
    }
    values.update(overrides)
    return ProductSnapshot(**values)


def test_diff_returns_no_events_for_baseline_or_editorial_only_changes() -> None:
    assert diff_snapshots(None, snap()) == []
    changes = diff_snapshots(snap(), snap(summary=" Product   summary ", features=["fast"]))
    assert changes == []


def test_diff_normalizes_dictionary_order_currency_case_and_period_synonyms() -> None:
    previous = snap(specifications={"b": 2, "a": 1})
    current = snap(
        specifications={"a": 1, "b": 2},
        pricing=[PriceTier(name="Pro", amount=10, currency="USD", period="per month")],
    )
    assert diff_snapshots(previous, current) == []


def test_diff_emits_high_price_and_availability_changes_with_evidence() -> None:
    changes = diff_snapshots(
        snap(),
        snap(pricing=[PriceTier(name="Pro", amount=20, currency="USD", period="month")], availability="unavailable"),
    )
    assert {(change.field_path, change.importance.value) for change in changes} == {
        ("pricing.Pro.amount", "high"), ("availability", "high"),
    }
    assert all(change.evidence == EVIDENCE for change in changes)


def test_diff_confirms_structured_field_deletion_on_second_missing_run() -> None:
    counts: dict[str, int] = {}
    prior = snap(specifications={"memory": "8GB"})
    absent = snap(specifications={})

    first = diff_snapshots(prior, absent, counts)
    second = diff_snapshots(prior, absent, counts)

    assert len(first) == 1
    assert first[0].confirmed is False
    assert counts["specifications.memory"] == 2
    assert len(second) == 1
    assert second[0].field_path == "specifications.memory"
    assert second[0].before == "8GB"
    assert second[0].after is None
    assert second[0].confirmed is True


def test_diff_defers_missing_availability_until_second_run_and_resets_on_recovery() -> None:
    counts: dict[str, int] = {}
    missing = snap(availability=None)

    first = diff_snapshots(snap(), missing, counts)
    second = diff_snapshots(snap(), missing, counts)
    recovered = diff_snapshots(missing, snap(availability="available"), counts)

    assert first[0].field_path == "availability"
    assert first[0].confirmed is False
    assert second[0].confirmed is True
    assert counts["availability"] == 0
    assert recovered[0].after == "available"


def test_diff_casefolds_pricing_tier_names_before_comparing() -> None:
    assert diff_snapshots(
        snap(pricing=[PriceTier(name="Pro", amount=10, currency="USD", period="month")]),
        snap(pricing=[PriceTier(name="pro", amount=10, currency="usd", period="monthly")]),
    ) == []
