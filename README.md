# Oracle EIM Data Quality

Standalone MCP server for governed EIM data-quality checks against On-Prem Oracle
and Oracle ATP. This is **not** the Oracle MCP chatbot.

The chatbot project (`oracle-mcp-chatbot`) answers natural-language questions.
This project loads **ACTIVE** rules from `EIM_APPS.EIM_DQ_RULES_LOOKUP`, runs
approved read-only SQL, and returns a director-ready Markdown report. An
optional isolated writer can atomically persist governed summaries and failed
record details without enabling arbitrary DML.

```bash
pip install --target .pydeps -r requirements-dev.txt
PYTHONPATH=.pydeps:src pytest
cp .env.example .env                          # add credentials
PYTHONPATH=.pydeps:src python -m oracle_mcp.server --profile both --check
PYTHONPATH=.pydeps:src python -m oracle_mcp.server --profile both
```

Rule workflow and report contract: [docs/data-quality-framework.md](docs/data-quality-framework.md).

## What it does

| Capability | How |
|---|---|
| Separate from the chatbot | Own repo, own MCP server name `oracle-eim-dq` |
| ACTIVE rules only | Catalog filter `RULE_STATUS = 'ACTIVE'` |
| Checkpoint context | `REFERENCE_CHECKPOINT` is narrative, never executed as SQL |
| Read-only source access | AST guard plus `SET TRANSACTION READ ONLY` |
| Metrics | Total, failed, pass %, failure %, severity |
| Persisted reconciliation | Fixed parameterized INSERTs through a separate writer |
| Trend | Like-for-like population signature vs persisted history |

## MCP tools

- `list_active_dq_rules`
- `execute_data_quality_rule`
- `start_dq_run`, `execute_and_persist_data_quality_rule`, and `get_dq_run_report`
  (registered only when persistence is enabled). One `run_id` identifies the
  full company report.

## Enable persistence

Persistence is disabled by default. Have a DBA review and run
`sql/04_dq_results_schema.sql`, then configure the separate `DQ_WRITE_*`
credentials in `.env` and set:

```bash
ORACLE_MCP_DQ_PERSISTENCE_ENABLED=true
```

The writer receives INSERT only on `EIM_APPS.EIM_DQ_RECON_SUMMARY` and
`EIM_APPS.EIM_DQ_FAILED_RECORDS`. It must not reuse the read-only account.
Retention/purge is DBA-owned; the MCP process has no DELETE privilege.

## Cursor MCP

Point Cursor at `mcp-clients/cursor-mcp.json` (replace absolute paths). The
workspace `.cursor/mcp.json` should register `oracle-eim-dq` with `cwd` set to
this repository, not `oracle-mcp-chatbot`.

## Test locally

Run these commands from this repository:

```bash
# 1. Unit and security tests (no database required)
PYTHONPATH=.pydeps:src .pydeps/bin/pytest -q

# 2. Verify both Oracle connections
PYTHONPATH=.pydeps:src python3 -m oracle_mcp.server --profile both --check

# 3. Verify the MCP tool surface and read the live ACTIVE-rule catalog
PYTHONPATH=.pydeps:src python3 scripts/verify_dq.py
```

Expected live verification:

```json
{
  "status": "OK",
  "server": "oracle-eim-dq",
  "databases": ["ATP", "ONPREM"],
  "dq_tools": ["execute_data_quality_rule", "list_active_dq_rules"]
}
```

If `active_rule_count` is `0`, connectivity and MCP are working but the governed
catalog has no ACTIVE rules yet. Populate and approve catalog rows outside this
read-only application, then rerun the verification.

After changing `.cursor/mcp.json`, reload Cursor's MCP servers. In chat, first
ask: `List the active EIM data-quality rules.` Do not attempt a rule execution
until that call returns an ACTIVE rule ID.
