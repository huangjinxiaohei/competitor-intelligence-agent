# Portable agent contract

The host invokes tasks by name and exchanges only JSON instances of the versioned schemas in `schemas/`.

- Discovery input/output: `schemas/Candidate.schema.json`
- Collection output: `schemas/SourceDocument.schema.json`
- Extraction output: `schemas/ProductSnapshot.schema.json`
- Comparison output: `schemas/ChangeEvent.schema.json`
- Reporting output: `schemas/Digest.schema.json`
- Orchestration output: `schemas/RunResult.schema.json`

Tasks receive a `run_id`, `fixture` boolean, `dry_run` boolean, and a configuration reference. A task must return partial results and source failures rather than terminate the run. Delivery uses the `DeliveryAdapter.publish(Digest) -> DeliveryReceipt` boundary; host code selects mock or webhook transport.


## Host handoffs

- Search-capable hosts write `state/host_search_results.json` using `{title,url,snippet}` objects. Snippets are ranking input only.
- Model-capable hosts return `ProductSnapshot` JSON with official `Evidence`. The deterministic runner validates every response.
- Standalone model mode uses `adapters.analyzer: http-json` and the `MODEL_*` environment variables.
- The runner owns retries, locks, persistence, two-round deletion confirmation, report export, and delivery.
