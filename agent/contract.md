# Portable agent contract

The host exchanges JSON instances of the versioned schemas in `schemas/`.

- Discovery: `Candidate.schema.json`
- Collection: `SourceDocument.schema.json`
- Extraction: `ProductSnapshot.schema.json`
- Comparison: `ChangeEvent.schema.json`
- Reporting: `Digest.schema.json`
- Orchestration: `RunResult.schema.json`

Tasks receive `run_id`, `fixture`, `dry_run`, and a configuration reference.
A task returns partial results and source failures instead of terminating the
whole run. The deterministic runner owns retry, locks, persistence, two-round
delete confirmation, reporting, and conditional delivery.

## Adapter boundaries

`DeliveryAdapter.publish(Digest) -> DeliveryReceipt` publishes chat reminders.
It must not write Base records or alter SQLite facts.

`ProjectionAdapter.sync(RunResult | None) -> ProjectionReceipt` projects the
already-persisted SQLite fact store to a rebuildable visual layer. SQLite is
the source of truth. A projection failure creates retryable outbox work and
returns a non-sensitive receipt; it never replaces a valid local snapshot.

`base-doctor` is read-only. `base-setup` creates/reuses projection resources and
backfills persisted facts. `base-resync` replays persisted facts only; none of
these commands collect websites. Credentials come exclusively from environment
variables and never appear in JSON, reports, or logs.

## Host handoffs

Search-capable hosts write `state/host_search_results.json` as `{title,url,snippet}`.
Snippets rank discovery only. Structured facts must have official collected
Evidence. Model-capable hosts may return snapshot JSON, but deterministic code
validates evidence excerpts against collected official text.
