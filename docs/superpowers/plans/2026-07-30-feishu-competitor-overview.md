# Feishu Competitor Overview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put up to five structured competitor snapshots directly into each Feishu digest card instead of showing only counts and a local report path.

**Architecture:** Add a bounded `DigestProduct` transport model to the versioned digest contract, construct it deterministically from `Candidate` and `ProductSnapshot`, and render each item as one Feishu Markdown element. Keep complete snapshots in Markdown/JSON/CSV reports while the card applies deterministic field and item limits.

**Tech Stack:** Python 3.11+, Pydantic 2, JSON Schema, Typer pipeline, Feishu interactive-card JSON, pytest, pytest-cov.

## Global Constraints

- A digest contains at most 5 products.
- Each product contains at most 5 features, 3 pricing tiers, 3 configurations, and 2 evidence URLs.
- Missing extracted fields render as `暂未识别`; no values are inferred or fabricated.
- Existing change-event display remains limited to 5 confirmed changes.
- The serialized Feishu request body remains below 20 KB at configured maximums.
- Complete Markdown, JSON, and CSV reports retain untruncated snapshots.
- Core test coverage remains at least 85%.
- The user's local `config/project.yaml`, `.env`, runtime `state/`, `reports/`, and `.codex/` metadata are excluded from feature commits.

---

## File Map

- Modify `src/competitor_agent/models.py`: define `DigestProduct` and add bounded `Digest.products`.
- Modify `schemas/Digest.schema.json`: publish the updated cross-host digest contract.
- Modify `src/competitor_agent/reporting.py`: map candidates and snapshots into bounded digest products.
- Modify `src/competitor_agent/delivery.py`: format product overview Markdown and append it to the Feishu card.
- Modify `tests/test_models.py`: validate digest-product defaults and limits.
- Modify `tests/test_reporting.py`: verify deterministic candidate/snapshot mapping and truncation.
- Modify `tests/test_delivery.py`: verify substantive card content, missing-field labels, and payload size.
- Modify `tests/test_pipeline.py`: verify fixture baselines expose product overviews end-to-end.
- Modify `README.md`: document that Feishu cards contain product overviews and that the local report remains the complete artifact.

---

### Task 1: Extend the Digest Data Contract

**Files:**
- Modify: `src/competitor_agent/models.py:126-133`
- Modify: `schemas/Digest.schema.json`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `DigestProduct` with `candidate_id`, `name`, `homepage`, `summary`, `features`, `pricing`, `configurations`, `confidence`, and `evidence_urls`.
- Produces: `Digest.products: list[DigestProduct]` with a default empty list and maximum length 5.
- Consumes: existing `PriceTier` and `StrictModel` definitions.

- [ ] **Step 1: Write the failing model tests**

Add imports for `Digest` and `DigestProduct`, then add:

```python
def test_digest_product_is_bounded_and_digest_defaults_remain_compatible() -> None:
    product = DigestProduct(
        candidate_id="candidate-1",
        name="Acme",
        homepage="https://acme.test",
        summary="Agent workspace",
        features=["automation"],
        pricing=[PriceTier(name="Pro", amount=29, currency="USD", period="month")],
        configurations={"deployment": "cloud"},
        confidence=0.9,
        evidence_urls=["https://acme.test/pricing"],
    )
    digest = Digest(
        run_id="run-1",
        kind="baseline",
        title="Competitor baseline",
        summary="One product",
        report_path="reports/run-1.md",
    )

    assert product.pricing[0].amount == 29
    assert digest.products == []

    with pytest.raises(ValidationError):
        Digest(
            run_id="run-2",
            kind="baseline",
            title="Too many",
            summary="Six products",
            products=[product] * 6,
            report_path="reports/run-2.md",
        )
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
$pytestTemp = Join-Path $env:TEMP ("competitor-agent-pytest-" + [guid]::NewGuid().ToString("N"))
.\.venv\Scripts\python.exe -m pytest --basetemp "$pytestTemp" -p no:cacheprovider tests/test_models.py -v
```

Expected: collection or import failure because `DigestProduct` does not exist.

- [ ] **Step 3: Implement the bounded models**

Add before `Digest`:

```python
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
```

Add to `Digest`:

```python
products: list[DigestProduct] = Field(default_factory=list, max_length=5)
```

Import `Any` from `typing` if it is not already imported.

- [ ] **Step 4: Regenerate the digest JSON Schema**

Run:

```powershell
.\.venv\Scripts\python.exe -c "import json; from pathlib import Path; from competitor_agent.models import Digest; Path('schemas/Digest.schema.json').write_text(json.dumps(Digest.model_json_schema(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')"
```

Verify the schema contains `$defs.DigestProduct`, `$defs.PriceTier`, and a `products` property with `maxItems: 5`.

- [ ] **Step 5: Run model and schema checks**

Run:

```powershell
$pytestTemp = Join-Path $env:TEMP ("competitor-agent-pytest-" + [guid]::NewGuid().ToString("N"))
.\.venv\Scripts\python.exe -m pytest --basetemp "$pytestTemp" -p no:cacheprovider tests/test_models.py -v
.\.venv\Scripts\python.exe -c "import json; from pathlib import Path; json.loads(Path('schemas/Digest.schema.json').read_text(encoding='utf-8')); print('schema: PASS')"
```

Expected: all model tests pass and schema parsing prints `schema: PASS`.

- [ ] **Step 6: Commit Task 1**

```powershell
git add src/competitor_agent/models.py schemas/Digest.schema.json tests/test_models.py
git commit -m "feat: add bounded digest product contract"
```

---

### Task 2: Build Product Overviews from Snapshots

**Files:**
- Modify: `src/competitor_agent/reporting.py:1-38`
- Test: `tests/test_reporting.py`

**Interfaces:**
- Consumes: `DigestProduct`, `Candidate`, `ProductSnapshot`, and `PriceTier`.
- Produces: `_digest_products(candidates: list[Candidate], snapshots: list[ProductSnapshot]) -> list[DigestProduct]`.
- Updates: `build_digest(...) -> Digest` so `Digest.products` is populated for baseline and update runs.

- [ ] **Step 1: Write failing reporting tests**

Update the existing snapshot fixture to include features, then extend `test_build_digest_labels_first_run_as_baseline_and_limits_message_changes`:

```python
assert len(digest.products) == 1
assert digest.products[0].name == "Acme"
assert digest.products[0].features == ["workflow automation", "dashboards"]
assert digest.products[0].pricing[0].name == "Pro"
assert digest.products[0].configurations == {"deployment": "cloud"}
assert digest.products[0].evidence_urls == ["https://acme.test/pricing"]
```

Add a URL-name and bounds test:

```python
def test_build_digest_normalizes_url_names_and_bounds_card_fields(tmp_path) -> None:
    item = candidate().model_copy(
        update={"name": "https://www.acme.test/pricing", "homepage": "https://www.acme.test/pricing"}
    )
    snap = snapshot().model_copy(
        update={
            "features": [f"feature-{index}" for index in range(8)],
            "configurations": {f"key-{index}": f"value-{index}" for index in range(5)},
            "evidence": [
                Evidence(source_url=f"https://acme.test/source-{index}", excerpt="fact", observed_at=NOW)
                for index in range(4)
            ],
        }
    )

    digest = build_digest("run-2", [item], [snap], [], [], tmp_path / "run-2.md", True)

    assert digest.products[0].name == "acme.test"
    assert len(digest.products[0].features) == 5
    assert len(digest.products[0].configurations) == 3
    assert len(digest.products[0].evidence_urls) == 2
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
$pytestTemp = Join-Path $env:TEMP ("competitor-agent-pytest-" + [guid]::NewGuid().ToString("N"))
.\.venv\Scripts\python.exe -m pytest --basetemp "$pytestTemp" -p no:cacheprovider tests/test_reporting.py -v
```

Expected: failure because `digest.products` is empty.

- [ ] **Step 3: Implement deterministic product construction**

Add imports for `re`, `urlsplit`, and `DigestProduct`. Add bounded helpers:

```python
def _clip(value: object, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


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
```

Pass `products=_digest_products(candidate_list, snapshot_list)` when constructing `Digest`.

- [ ] **Step 4: Run reporting tests and verify GREEN**

Run:

```powershell
$pytestTemp = Join-Path $env:TEMP ("competitor-agent-pytest-" + [guid]::NewGuid().ToString("N"))
.\.venv\Scripts\python.exe -m pytest --basetemp "$pytestTemp" -p no:cacheprovider tests/test_reporting.py -v
```

Expected: all reporting tests pass.

- [ ] **Step 5: Commit Task 2**

```powershell
git add src/competitor_agent/reporting.py tests/test_reporting.py
git commit -m "feat: build competitor overview digests"
```

---

### Task 3: Render Substantive Feishu Product Cards

**Files:**
- Modify: `src/competitor_agent/delivery.py:1-58`
- Test: `tests/test_delivery.py`

**Interfaces:**
- Consumes: `Digest.products` and existing `Digest.changes`.
- Produces: `_product_block(product: DigestProduct) -> str`.
- Preserves: `render_payload(digest: Digest) -> dict` with `msg_type == "interactive"`.

- [ ] **Step 1: Write failing card-content tests**

Add a digest product to `make_digest` and assert the rendered Markdown contains substantive fields:

```python
product = DigestProduct(
    candidate_id="acme-1",
    name="Acme",
    homepage="https://acme.test",
    summary="Agent workspace",
    features=["workflow automation", "dashboards"],
    pricing=[PriceTier(name="Pro", amount=29, currency="USD", period="month", unit="user")],
    configurations={"deployment": "cloud"},
    confidence=0.9,
    evidence_urls=["https://acme.test/pricing"],
)
```

Then assert:

```python
card_text = "\n".join(
    element.get("content", "") for element in payload["card"]["elements"]
)
assert "Acme" in card_text
assert "workflow automation" in card_text
assert "Pro: USD 29/month/user" in card_text
assert "deployment=cloud" in card_text
assert "90%" in card_text
assert "https://acme.test/pricing" in card_text
```

Add missing-field behavior:

```python
def test_render_payload_labels_missing_product_fields() -> None:
    digest = make_digest(0).model_copy(
        update={
            "products": [
                DigestProduct(
                    candidate_id="empty-1",
                    name="Empty",
                    homepage="https://empty.test",
                    confidence=0.0,
                )
            ]
        }
    )

    text = str(render_payload(digest))

    assert text.count("暂未识别") >= 3
```

Add a maximum-size test that creates five bounded `DigestProduct` instances with maximum field counts and asserts:

```python
serialized = json.dumps(render_payload(digest), ensure_ascii=False).encode("utf-8")
assert len(serialized) < 20_000
```

- [ ] **Step 2: Run delivery tests and verify RED**

Run:

```powershell
$pytestTemp = Join-Path $env:TEMP ("competitor-agent-pytest-" + [guid]::NewGuid().ToString("N"))
.\.venv\Scripts\python.exe -m pytest --basetemp "$pytestTemp" -p no:cacheprovider tests/test_delivery.py -v
```

Expected: substantive-field assertions fail because products are not rendered.

- [ ] **Step 3: Implement card formatters**

Import `DigestProduct` and add:

```python
def _price_text(product: DigestProduct) -> str:
    if not product.pricing:
        return "暂未识别"
    values: list[str] = []
    for tier in product.pricing:
        amount = f"{tier.amount:g}" if tier.amount is not None else "联系销售"
        value = f"{tier.name}: {tier.currency or ''} {amount}".replace("  ", " ").strip()
        if tier.period:
            value += f"/{tier.period}"
        if tier.unit:
            value += f"/{tier.unit}"
        values.append(value)
    return "；".join(values)


def _product_block(product: DigestProduct) -> str:
    features = "；".join(product.features) or "暂未识别"
    configurations = (
        "；".join(f"{key}={value}" for key, value in product.configurations.items())
        or "暂未识别"
    )
    evidence = (
        " ".join(
            f"[来源{index}]({url})"
            for index, url in enumerate(product.evidence_urls, start=1)
        )
        or "暂未识别"
    )
    return "\n".join(
        [
            f"### [{product.name}]({product.homepage})",
            f"**核心功能**：{features}",
            f"**套餐价格**：{_price_text(product)}",
            f"**关键配置**：{configurations}",
            f"**置信度**：{product.confidence:.0%}",
            f"**官方证据**：{evidence}",
        ]
    )
```

Insert one `{"tag": "markdown", "content": _product_block(product)}` element per `digest.products` immediately after the run summary. Add a `**重点变化**` Markdown label only when changes exist. Change the footer copy to `本地完整报告：{digest.report_path}` without inline-code backticks.

- [ ] **Step 4: Run delivery tests and verify GREEN**

Run:

```powershell
$pytestTemp = Join-Path $env:TEMP ("competitor-agent-pytest-" + [guid]::NewGuid().ToString("N"))
.\.venv\Scripts\python.exe -m pytest --basetemp "$pytestTemp" -p no:cacheprovider tests/test_delivery.py -v
```

Expected: all delivery tests pass, including the request-size assertion.

- [ ] **Step 5: Commit Task 3**

```powershell
git add src/competitor_agent/delivery.py tests/test_delivery.py
git commit -m "feat: render competitor overviews in Feishu cards"
```

---

### Task 4: End-to-End Regression, Documentation, and Delivery Verification

**Files:**
- Modify: `tests/test_pipeline.py`
- Modify: `README.md`
- Verify: all files changed in Tasks 1-3

**Interfaces:**
- Consumes: complete pipeline `run_pipeline(...) -> RunResult`.
- Verifies: `RunResult.digest.products` survives fixture baseline creation and Feishu payload rendering.

- [ ] **Step 1: Write the failing pipeline assertion**

In `test_fixture_pipeline_baseline_change_and_no_change_rounds`, add:

```python
assert baseline.digest is not None
assert len(baseline.digest.products) == 2
assert {product.name for product in baseline.digest.products} == {"NovaBoard", "OrbitNote"}
assert all(product.evidence_urls for product in baseline.digest.products)
```

- [ ] **Step 2: Run the pipeline test**

Run:

```powershell
$pytestTemp = Join-Path $env:TEMP ("competitor-agent-pytest-" + [guid]::NewGuid().ToString("N"))
.\.venv\Scripts\python.exe -m pytest --basetemp "$pytestTemp" -p no:cacheprovider "tests/test_pipeline.py::test_fixture_pipeline_baseline_change_and_no_change_rounds" -v
```

Expected after Tasks 1-3: PASS. A failure indicates an integration gap that must be corrected in the owning module before continuing.

- [ ] **Step 3: Update README delivery behavior**

Document that Feishu cards show up to five competitor overviews, each with up to five functions, three prices, three configurations, confidence, and two evidence links. State that missing extracted fields display `暂未识别` and full reports remain local unless a hosting adapter is added.

- [ ] **Step 4: Run complete verification**

Run:

```powershell
$pytestTemp = Join-Path $env:TEMP ("competitor-agent-pytest-" + [guid]::NewGuid().ToString("N"))
.\.venv\Scripts\python.exe -m pytest --basetemp "$pytestTemp" -p no:cacheprovider --cov=competitor_agent --cov-report=term-missing
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m competitor_agent.cli doctor --fixture
git diff --check -- src tests schemas README.md
```

Expected: all tests pass, coverage is at least 85%, compile check is silent, doctor reports fixture readiness, and diff check is clean.

- [ ] **Step 5: Inspect a real payload without sending it**

Run:

```powershell
.\.venv\Scripts\python.exe -c "import json; from competitor_agent.delivery import render_payload; from competitor_agent.pipeline import run_pipeline; r=run_pipeline(config_path='config/project.yaml', dry_run=True); print(json.dumps({'candidate_count': r.candidate_count, 'snapshot_count': r.snapshot_count}, ensure_ascii=False))"
```

Then construct the final live verification only after tests and commits are complete; the user executes the existing `force_publish=True` command so no unsolicited Feishu message is sent during automated verification.

- [ ] **Step 6: Commit Task 4**

```powershell
git add tests/test_pipeline.py README.md
git commit -m "docs: document Feishu overview delivery"
```

- [ ] **Step 7: Push the completed branch**

```powershell
git push origin codex/competitive-intel-agent
```

- [ ] **Step 8: User acceptance command**

Ask the user to run:

```powershell
.\.venv\Scripts\python.exe -c "from competitor_agent.pipeline import run_pipeline; r=run_pipeline(config_path='config/project.yaml', delivery_adapter='webhook', force_publish=True); print(r.delivery.model_dump_json(indent=2) if r.delivery else 'NO_DELIVERY')"
```

Expected Feishu result: one blue card containing three competitor sections for the current test configuration, substantive extracted features, explicit missing-price/configuration labels, confidence values, evidence links, and the local full-report path at the bottom.
