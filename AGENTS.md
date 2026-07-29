# Competitor intelligence agent

Read `agent/contract.md` before running tasks. Treat files under `schemas/` as the cross-host data contract.

Default offline validation:

```powershell
.\.venv\Scripts\python.exe -m competitor_agent.cli doctor --fixture
.\.venv\Scripts\python.exe -m competitor_agent.cli run --fixture
```

For live discovery, write host search results to `state/host_search_results.json` as documented in `README.md`; official source collection, evidence validation, snapshots, diffing, reports, and delivery remain deterministic Python steps.
