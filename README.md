# Oracle EIM Data Quality

Standalone MCP server for governed EIM data-quality checks against On-Prem Oracle
and Oracle ATP. This is **not** the Oracle MCP chatbot.

The chatbot project (`oracle-mcp-chatbot`) answers natural-language questions.
This project only loads **ACTIVE** rules from `EIM.EIM_DQ_RULES_LOOKUP`, runs
approved read-only aggregate SQL, and returns a director-ready Markdown report.

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
| Read-only | AST guard plus `SET TRANSACTION READ ONLY` |
| Metrics | Total, failed, pass %, failure %, severity |
| Trend | Local JSONL history vs previous run |

## MCP tools

- `list_active_dq_rules`
- `execute_data_quality_rule`
- plus the same read-only discovery/validate/execute tools used to confirm objects

## Cursor MCP

Point Cursor at `mcp-clients/cursor-mcp.json` (replace absolute paths). The
workspace `.cursor/mcp.json` should register `oracle-eim-dq` with `cwd` set to
this repository, not `oracle-mcp-chatbot`.
