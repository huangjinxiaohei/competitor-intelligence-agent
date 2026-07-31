# Task 1 Report ? Stable Identity, Contracts, and State Migration

## Scope delivered

- Added backward-compatible `official_entry_urls` on `Candidate` and `field_evidence` on `ProductSnapshot`.
- Added `ProjectionReceipt`, plus optional `base_links` and projection receipts on `Digest`/`RunResult`.
- Added `collection.max_pages_per_candidate`, `adapters.projection`, and strict optional `feishu_base` configuration.
- Added offline registrable-domain identity (with `tldextract` declared in `pyproject.toml` and a no-network resolver configuration), origin homepages, and per-domain discovery merging.
- Added idempotent candidate-identity migration, aliases, collision handling, projection resources, record mappings, and retryable outbox persistence.
- Regenerated the six affected public schemas, including the new ProjectionReceipt schema.

## TDD record

1. **RED:** `tests/test_identity_contracts.py` initially failed collection because `ProjectionReceipt` did not exist.
   **GREEN:** Added contract fields/models, config fields, identity discovery logic, storage tables/API, and generated schemas; the focused suite passed.
2. **RED:** Same-domain migration collision test raised `sqlite3.IntegrityError` for duplicate `missing_counts` keys.
   **GREEN:** Migration now merges conflicting missing-count state deterministically using the larger count while preserving child records and aliases.
3. **RED:** Reopening a persisted legacy database did not migrate its candidate alias automatically.
   **GREEN:** Initialization runs the idempotent migration once per connection; subsequent state operations do not rerun it.

## Verification

- Focused: `24 passed` ? `tests/test_models.py tests/test_config.py tests/test_discovery.py tests/test_storage.py tests/test_identity_contracts.py` with isolated `--basetemp` and `-p no:cacheprovider`.
- Schema validation: generated JSON exactly matches `model_json_schema()` for Candidate, ProductSnapshot, Digest, RunResult, ProjectConfig, and ProjectionReceipt.
- `git diff --check` passed.

## Self-review

- Legacy JSON remains valid because every new Pydantic field has a default.
- Candidate IDs derive only from registrable domains; official page URLs remain available separately and duplicate pages merge per domain.
- Migration rewrites candidate, snapshot JSON, change JSON, and missing-count state in one transaction; aliases make legacy IDs resolvable and reruns are no-ops.
- Projection persistence is local SQLite state and does not contact Feishu or webhooks.

## Risks / follow-up

- The shared virtual environment could not be changed during this task because installation of `tldextract` was declined by the environment approval policy. The dependency is declared for normal installation, and the implementation includes a deterministic offline fallback so local validation remains runnable. The fallback intentionally covers common multi-label public suffixes; installed `tldextract` supplies the complete packaged public-suffix list.
