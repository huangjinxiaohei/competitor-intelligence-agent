# Feishu Base Quality Projection Implementation Plan

## Goal

Improve evidence-backed competitor extraction, normalize one competitor across its official pages, project current intelligence into a Feishu Base, and keep webhook notifications change-driven.

## Global Constraints

- Preserve the user's uncommitted `config/project.yaml`, `.env`, runtime `state/`, `reports/`, and `.codex/` files.
- Credentials come only from environment variables and are never logged or committed.
- SQLite remains the local source of truth; Feishu Base is a rebuildable projection.
- Search snippets remain discovery-only; structured facts require collected official evidence.
- All new Pydantic contract fields have backward-compatible defaults and matching JSON Schemas.
- Numeric prices are never inferred. Missing numeric prices render as `联系销售` or `暂未识别`.
- A no-change run updates Base and the run log but does not call the webhook.
- Base failure never overwrites the last valid local snapshot; it records a partial run and a retryable outbox item.
- Use TDD for every production behavior and keep total coverage at or above 85%.
- Do not contact real model, Base, permission, or webhook endpoints in automated tests.

### Task 1: Stable Identity, Contracts, and State Migration

**Scope:** `models.py`, `config.py`, `discovery.py`, `storage.py`, public schemas, focused tests, and the offline registrable-domain dependency.

- Add backward-compatible official entry URLs to `Candidate` and field-path evidence to `ProductSnapshot`.
- Add `ProjectionReceipt` and optional Base links/receipt fields to `Digest` and `RunResult`.
- Extend config with `collection.max_pages_per_candidate`, `adapters.projection`, and strict `feishu_base` settings.
- Compute stable candidate identity from an offline registrable-domain resolver; keep the canonical homepage at the site origin while retaining discovered official entry URLs.
- Add SQLite schema/state for candidate aliases, projection resources, projection record mappings, and projection outbox.
- Implement an idempotent migration that rewrites existing candidate IDs across candidates, snapshots, changes, and missing counts while recording aliases and resolving same-domain collisions deterministically.
- Regenerate all affected JSON Schemas.
- RED/GREEN tests must cover old JSON compatibility, domain identity, duplicate official pages, migration preservation, repeat migration, record mappings, and outbox persistence.

### Task 2: Multi-page Collection and Hybrid Evidence-backed Extraction

**Scope:** `collector.py`, `analyzer.py`, anonymous HTML fixtures, focused tests.

- Collect the entry URLs first, then discover same-registrable-domain links matching pricing, plans, product, features, docs/help, or security keywords, capped by `max_pages_per_candidate`.
- Preserve throttling, retries, PDF support, Playwright fallback, URL canonicalization, and deterministic page ordering.
- Enhance deterministic extraction for plan cards and text containing amount, currency, billing period, billing unit, qualifiers, configurations, and clean feature statements.
- Add an OpenAI-compatible chat-completions adapter selected by `hybrid-openai`; use `MODEL_API_URL`, `MODEL_API_KEY`, and `MODEL_NAME`.
- Merge policy: deterministic numeric price facts win; the model may fill missing semantic fields only when every supplied evidence excerpt matches an official collected document; recompute confidence from evidence coverage.
- Retry one invalid model response, then fall back to deterministic output and surface a degradation diagnostic without discarding evidence-backed facts.
- RED/GREEN tests must cover three anonymous SaaS page shapes, off-domain rejection, page caps, deterministic ordering, model merge, invalid JSON, mismatched candidate, unmatched evidence, timeout/429 fallback, and no fabricated prices.

### Task 3: Feishu Base Client and Idempotent Setup

**Scope:** new Feishu Base client/setup module, focused mocked-HTTP tests.

- Obtain and cache a tenant access token from `FEISHU_APP_ID` and `FEISHU_APP_SECRET`, refreshing before expiry.
- Implement bounded retry for 429 and 5xx responses, honoring `Retry-After`, and redact URLs/tokens from failures.
- Implement `doctor` capability checks and idempotent setup for one Base, four tables, fields, relation fields, and named grid/gallery/kanban views.
- Tables and machine/human fields must match the approved design: competitors, pricing, confirmed changes, and runs. Human attention level, tags, and notes are never overwritten by sync.
- Grant the configured owner email edit permission and target chat view permission using `FEISHU_OWNER_EMAIL` and `FEISHU_VIEWER_CHAT_ID`.
- Persist Base/table/view identifiers in projection resources and export the configured manifest path atomically.
- Re-running setup validates and reuses matching resources; partial setup resumes from stored identifiers rather than creating duplicates.
- RED/GREEN tests must cover token caching, scope/permission diagnostics, setup replay, partial recovery, field type validation, relations, views, collaborator payloads, pagination, retry, redaction, and manifest UTF-8.

### Task 4: Base Projection, Backfill, and Recovery

**Scope:** new projection module, storage queries/mappings/outbox, focused tests.

- Define `ProjectionAdapter.sync(...) -> ProjectionReceipt` independently of webhook delivery.
- Upsert current competitors by candidate ID and current price tiers by deterministic price key; preserve unmapped human-owned fields by sending machine-field-only updates.
- Mark a price inactive only after the local diff layer has confirmed its deletion.
- Append confirmed changes by deterministic event ID and runs by run ID; update the `是否最新` run marker.
- Resolve Base relation values from stored competitor record IDs.
- Batch writes at at most 200 records and persist business-key-to-record-ID mappings.
- Add idempotent backfill/resync from SQLite: latest candidate/snapshot/pricing state plus existing confirmed changes and finished runs.
- Persist failed batches to the SQLite outbox, drain older items before the current sync, and reconstruct mappings by querying business keys when local mappings are missing.
- RED/GREEN tests must cover setup/backfill replay, no duplicate records, human-field preservation, confirmed deletion, 200-record batching, relation IDs, outbox replay, missing mapping recovery, and partial batch failure.

### Task 5: Pipeline, CLI, Feishu Message Links, Documentation, and End-to-End Tests

**Scope:** `pipeline.py`, `cli.py`, `delivery.py`, examples/docs, end-to-end tests.

- Add `base-doctor`, `base-setup`, and `base-resync` CLI commands with `--config`; `base-doctor` is read-only and setup/resync return structured receipts.
- On each normal run: finish local persistence/reporting, drain/sync Base, attach projection receipt and Base links, then conditionally publish the webhook.
- A successful no-change run updates Base and appends a run record while making zero webhook calls.
- A Base failure marks the run partial, queues retry work, and still sends an evidence-backed change notification when confirmed changes exist, without a stale deep link.
- Feishu cards link to the competitor overview and current-change view when projection succeeds; local report paths remain auxiliary.
- Update `.env.example`, example YAML, README, contract docs, and schemas without modifying the user's real config or secret env file.
- Add migration and three-round integration tests: baseline/backfill, price and configuration change, then no change. Mock every external service.
- Run full pytest with coverage, compileall, schema validation, fixture doctor/run, and diff checks.

## Completion

- Each task is committed separately and independently reviewed for spec compliance and code quality.
- After Task 5, run a whole-branch production-readiness review and one final full verification on the merged feature branch.
