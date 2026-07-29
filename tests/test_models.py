from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from competitor_agent.models import (
    Candidate,
    CandidateStatus,
    ChangeEvent,
    ChangeImportance,
    Evidence,
    PriceTier,
    ProductSnapshot,
)


def test_candidate_score_must_be_between_zero_and_one() -> None:
    with pytest.raises(ValidationError):
        Candidate(
            id="candidate-1",
            name="Example",
            homepage="https://example.test",
            category="agent-platform",
            score=1.1,
            reasons=["功能重合"],
            status=CandidateStatus.MONITORED,
        )


def test_snapshot_requires_evidence_for_structured_facts() -> None:
    with pytest.raises(ValidationError, match="evidence"):
        ProductSnapshot(
            candidate_id="candidate-1",
            observed_at=datetime.now(UTC),
            summary="示例",
            features=["workflow"],
            specifications={"context": "large"},
            configurations={"deployment": "cloud"},
            pricing=[PriceTier(name="Pro", amount=10, currency="USD", period="month")],
            availability="public",
            confidence=0.9,
            evidence=[],
        )


def test_change_event_keeps_source_evidence() -> None:
    evidence = Evidence(
        source_url="https://example.test/pricing",
        excerpt="Pro plan: $20 per month",
        observed_at=datetime.now(UTC),
    )
    event = ChangeEvent(
        candidate_id="candidate-1",
        field_path="pricing.Pro.amount",
        before=10,
        after=20,
        importance=ChangeImportance.HIGH,
        evidence=[evidence],
    )

    assert event.confirmed is True
    assert event.evidence[0].source_url.endswith("/pricing")

