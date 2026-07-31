from __future__ import annotations

import csv
import json
import re
from urllib.parse import urlsplit
from pathlib import Path
from typing import Iterable

from .models import Candidate, ChangeEvent, Digest, DigestProduct, ProductSnapshot, RunResult



def _clip(value: object, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def _display_name(candidate: Candidate) -> str:
    if candidate.name.startswith(("http://", "https://")):
        hostname = urlsplit(candidate.homepage).hostname or candidate.name
        return hostname.removeprefix("www.")
    return _clip(candidate.name, 80)


def _digest_products(
    candidates: list[Candidate], snapshots: list[ProductSnapshot]
) -> list[DigestProduct]:
    snapshots_by_id = {item.candidate_id: item for item in snapshots}
    products: list[DigestProduct] = []
    for candidate in candidates:
        snapshot = snapshots_by_id.get(candidate.id)
        if snapshot is None:
            continue
        configurations = {
            _clip(key, 60): _clip(value, 120)
            for key, value in list(snapshot.configurations.items())[:3]
        }
        evidence_urls = list(
            dict.fromkeys(item.source_url for item in snapshot.evidence)
        )[:2]
        products.append(
            DigestProduct(
                candidate_id=candidate.id,
                name=_display_name(candidate),
                homepage=candidate.homepage,
                summary=_clip(snapshot.summary, 240),
                features=[_clip(item, 120) for item in snapshot.features[:5]],
                pricing=snapshot.pricing[:3],
                configurations=configurations,
                confidence=snapshot.confidence,
                evidence_urls=evidence_urls,
            )
        )
        if len(products) == 5:
            break
    return products


def build_digest(
    run_id: str,
    candidates: Iterable[Candidate],
    snapshots: Iterable[ProductSnapshot],
    changes: Iterable[ChangeEvent],
    errors: Iterable[str],
    report_path: str | Path,
    first_run: bool,
) -> Digest:
    candidate_list = list(candidates)
    snapshot_list = list(snapshots)
    change_list = list(changes)
    error_list = list(errors)
    kind = "baseline" if first_run else "update"
    title = "\u7ade\u54c1\u60c5\u62a5\u57fa\u7ebf" if first_run else "\u7ade\u54c1\u60c5\u62a5\u66f4\u65b0"
    summary = (
        f"\u5019\u9009 {len(candidate_list)} \u4e2a\uff0c\u5feb\u7167 {len(snapshot_list)} \u4efd\uff0c"
        f"\u6709\u6548\u53d8\u5316 {len(change_list)} \u6761\uff0c\u6765\u6e90\u5931\u8d25 {len(error_list)} \u4e2a\u3002"
    )
    return Digest(
        run_id=run_id,
        kind=kind,
        title=title,
        summary=summary,
        changes=change_list[:5],
        products=_digest_products(candidate_list, snapshot_list),
        failed_sources=error_list,
        report_path=str(report_path),
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _markdown(
    digest: Digest,
    candidates: list[Candidate],
    snapshots: list[ProductSnapshot],
    changes: list[ChangeEvent],
    errors: list[str],
    run_result: RunResult | None = None,
) -> str:
    lines = [f"# {digest.title}", "", digest.summary, "", "## \u53d8\u5316", ""]
    if changes:
        lines.extend(
            f"- **{event.importance.value.upper()}** `{event.candidate_id}` `{event.field_path}`: "
            f"{_json(event.before)} \u2192 {_json(event.after)}"
            for event in changes
        )
    else:
        lines.append("\u672a\u53d1\u73b0\u6709\u6548\u53d8\u5316\u3002")

    lines.extend(["", "## \u5f53\u524d\u5feb\u7167", ""])
    for item in snapshots:
        lines.extend(
            [
                f"### `{item.candidate_id}`",
                f"- \u89c2\u6d4b\u65f6\u95f4: {item.observed_at.isoformat()}",
                f"- \u6458\u8981: {item.summary or '-'}",
                f"- \u529f\u80fd: {_json(item.features)}",
                f"- \u53c2\u6570: {_json(item.specifications)}",
                f"- \u914d\u7f6e: {_json(item.configurations)}",
                f"- \u4ef7\u683c: {_json([tier.model_dump(mode='json') for tier in item.pricing])}",
                f"- \u53ef\u7528\u6027: {item.availability or '-'}",
                f"- \u7f6e\u4fe1\u5ea6: {item.confidence:.2f}",
                f"- \u8bc1\u636e: {_json(sorted({evidence.source_url for evidence in item.evidence}))}",
                "",
            ]
        )

    lines.extend(["## \u5019\u9009\u7ade\u54c1", ""])
    lines.extend(
        f"- {item.name} (`{item.id}`) - {item.homepage} - {item.score:.2f}"
        for item in candidates
    )
    if errors:
        lines.extend(["", "## \u91c7\u96c6\u5931\u8d25", ""] + [f"- {error}" for error in errors])
    if run_result is not None:
        projection = run_result.projection
        delivery = run_result.delivery
        lines.extend(["", "## \u6700\u7ec8\u8fd0\u884c\u56de\u6267", ""])
        lines.append(f"- \u72b6\u6001: {run_result.status.value}")
        lines.append(f"- Base \u540c\u6b65: {'synced' if projection and projection.synced else 'pending'}")
        if projection is not None:
            lines.append(f"- Base outbox: {projection.outbox_pending}")
        lines.append(f"- \u6d88\u606f\u53d1\u9001: {'sent' if delivery and delivery.delivered else 'not_sent'}")
        if digest.base_links:
            lines.append("- Base \u94fe\u63a5: " + _json(digest.base_links))
    return "\n".join(lines) + "\n"


def write_reports(
    digest: Digest,
    candidates: Iterable[Candidate],
    snapshots: Iterable[ProductSnapshot],
    changes: Iterable[ChangeEvent],
    errors: Iterable[str],
    reports_dir: str | Path,
    run_result: RunResult | None = None,
) -> dict[str, Path]:
    """Write complete Markdown, JSON, and tabular snapshot/change exports."""
    directory = Path(reports_dir)
    directory.mkdir(parents=True, exist_ok=True)
    candidate_list, snapshot_list = list(candidates), list(snapshots)
    change_list, error_list = list(changes), list(errors)
    markdown_path = directory / f"{digest.run_id}.md"
    json_path = directory / f"{digest.run_id}.json"
    csv_path = directory / f"{digest.run_id}.csv"

    markdown_path.write_text(
        _markdown(digest, candidate_list, snapshot_list, change_list, error_list, run_result),
        encoding="utf-8",
    )
    payload = {
        "digest": digest.model_dump(mode="json"),
        "candidates": [item.model_dump(mode="json") for item in candidate_list],
        "snapshots": [item.model_dump(mode="json") for item in snapshot_list],
        "changes": [item.model_dump(mode="json") for item in change_list],
        "errors": error_list,
        **({"run_result": run_result.model_dump(mode="json")} if run_result is not None else {}),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    fields = [
        "record_type", "candidate_id", "observed_at", "summary", "features",
        "specifications", "configurations", "pricing", "availability", "confidence",
        "source_urls", "field_path", "before", "after", "importance", "confirmed",
        "detected_at",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in snapshot_list:
            writer.writerow(
                {
                    "record_type": "snapshot",
                    "candidate_id": item.candidate_id,
                    "observed_at": item.observed_at.isoformat(),
                    "summary": item.summary,
                    "features": _json(item.features),
                    "specifications": _json(item.specifications),
                    "configurations": _json(item.configurations),
                    "pricing": _json([tier.model_dump(mode="json") for tier in item.pricing]),
                    "availability": item.availability,
                    "confidence": item.confidence,
                    "source_urls": _json(sorted({e.source_url for e in item.evidence})),
                }
            )
        for event in change_list:
            writer.writerow(
                {
                    "record_type": "change",
                    "candidate_id": event.candidate_id,
                    "field_path": event.field_path,
                    "before": _json(event.before),
                    "after": _json(event.after),
                    "importance": event.importance.value,
                    "confirmed": event.confirmed,
                    "detected_at": event.detected_at.isoformat(),
                    "source_urls": _json(sorted({e.source_url for e in event.evidence})),
                }
            )
    return {"markdown": markdown_path, "json": json_path, "csv": csv_path}
