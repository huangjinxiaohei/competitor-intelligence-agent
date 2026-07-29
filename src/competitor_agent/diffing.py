from __future__ import annotations

import re
from collections.abc import MutableMapping
from typing import Any

from .models import ChangeEvent, ChangeImportance, ProductSnapshot


def _text(value: str) -> str:
    return " ".join(value.split())


def _normalise(value: Any, path: str = "") -> Any:
    if isinstance(value, str):
        result = _text(value)
        if path.endswith(".currency"):
            return result.upper()
        if path.endswith(".period"):
            compact = re.sub(r"[^a-z]", "", result.casefold())
            if compact in {"month", "monthly", "permonth"}:
                return "month"
            if compact in {"year", "yearly", "annual", "peryear"}:
                return "year"
        return result
    if isinstance(value, dict):
        return {str(key): _normalise(item, f"{path}.{key}") for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_normalise(item, path) for item in value]
    return value


def _mapping(value: dict[str, Any], root: str) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, item in value.items():
        path = f"{root}.{key}"
        if isinstance(item, dict):
            flattened.update(_mapping(item, path))
        else:
            flattened[path] = item
    return flattened


def _pricing(snapshot: ProductSnapshot) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for tier in snapshot.pricing:
        tier_name = _text(tier.name).casefold()
        for key, value in tier.model_dump().items():
            if key != "name":
                values[f"pricing.{tier_name}.{key}"] = value
    return values


def _pricing_display_names(snapshot: ProductSnapshot) -> dict[str, str]:
    return {_text(tier.name).casefold(): _text(tier.name) for tier in snapshot.pricing}


def _with_pricing_display(event: ChangeEvent, current: ProductSnapshot,
                          previous: ProductSnapshot) -> ChangeEvent:
    prefix, field = event.field_path.rsplit(".", maxsplit=1)
    tier_key = prefix.removeprefix("pricing.")
    names = _pricing_display_names(previous)
    names.update(_pricing_display_names(current))
    return event.model_copy(update={"field_path": f"pricing.{names.get(tier_key, tier_key)}.{field}"})


def _event(current: ProductSnapshot, field_path: str, before: Any, after: Any,
           importance: ChangeImportance, confirmed: bool = True) -> ChangeEvent:
    return ChangeEvent(
        candidate_id=current.candidate_id,
        field_path=field_path,
        before=before,
        after=after,
        importance=importance,
        evidence=current.evidence,
        confirmed=confirmed,
    )


def _compare_mapping(previous: ProductSnapshot, current: ProductSnapshot,
                     old: dict[str, Any], new: dict[str, Any],
                     counts: MutableMapping[str, int] | None,
                     importance: ChangeImportance) -> list[ChangeEvent]:
    events: list[ChangeEvent] = []
    for path in sorted(set(old) | set(new)):
        old_value, new_value = old.get(path), new.get(path)
        if path not in new:
            if counts is None:
                events.append(_event(current, path, old_value, None, importance))
            else:
                counts[path] = counts.get(path, 0) + 1
                events.append(_event(
                    current, path, old_value, None, importance,
                    confirmed=counts[path] >= 2,
                ))
            continue
        if counts is not None:
            counts[path] = 0
        if path not in old or _normalise(old_value, path) != _normalise(new_value, path):
            events.append(_event(current, path, old_value, new_value, importance))
    return events


def diff_snapshots(
    previous: ProductSnapshot | None,
    current: ProductSnapshot,
    missing_counts: MutableMapping[str, int] | None = None,
) -> list[ChangeEvent]:
    """Return material structured changes; textual summary and feature copy are excluded."""
    if previous is None:
        return []
    if previous.candidate_id != current.candidate_id:
        raise ValueError("snapshots must belong to the same candidate")

    events: list[ChangeEvent] = []
    if current.availability is None and previous.availability is not None and missing_counts is not None:
        missing_counts["availability"] = missing_counts.get("availability", 0) + 1
        events.append(_event(
            current, "availability", previous.availability, None, ChangeImportance.HIGH,
            confirmed=missing_counts["availability"] >= 2,
        ))
    elif current.availability is None and missing_counts is not None and missing_counts.get("availability", 0):
        missing_counts["availability"] += 1
        events.append(_event(
            current, "availability", None, None, ChangeImportance.HIGH,
            confirmed=missing_counts["availability"] >= 2,
        ))
    else:
        if missing_counts is not None:
            missing_counts["availability"] = 0
        if _normalise(previous.availability, "availability") != _normalise(current.availability, "availability"):
            events.append(_event(current, "availability", previous.availability, current.availability, ChangeImportance.HIGH))
    pricing_events = _compare_mapping(
        previous, current, _pricing(previous), _pricing(current), missing_counts, ChangeImportance.HIGH
    )
    events.extend(_with_pricing_display(event, current, previous) for event in pricing_events)
    events.extend(_compare_mapping(
        previous, current, _mapping(previous.specifications, "specifications"),
        _mapping(current.specifications, "specifications"), missing_counts, ChangeImportance.MEDIUM
    ))
    events.extend(_compare_mapping(
        previous, current, _mapping(previous.configurations, "configurations"),
        _mapping(current.configurations, "configurations"), missing_counts, ChangeImportance.MEDIUM
    ))
    return events
