# EIM data-quality framework

## Runtime flow

1. Connect to the `oracle-eim-dq` MCP server (`--profile both`).
2. Call `list_active_dq_rules`. The server reads only rows whose
   `RULE_STATUS = 'ACTIVE'` from `EIM_APPS.EIM_DQ_RULES_LOOKUP`.
3. Use `DQ_RULE` and `REFERENCE_CHECKPOINT` as context to prepare two aggregate
   queries for a selected rule:
   - one integer aliased `TOTAL_RECORDS`
   - one integer aliased `FAILED_RECORDS`
4. Call `execute_data_quality_rule` for a read-only preview.
5. The server parses and independently validates both statements, restricts
   them to approved schemas, executes them in Oracle read-only transactions,
   calculates metrics, compares the previous like-for-like execution, and returns the
   required Markdown report.
6. For an approved persisted run, also prepare a failed-detail SELECT returning
   exactly `SYSTEM_SERIAL_NUMBER`, `SOURCE_RECORD_KEY`, `FAILURE_REASON`, and
   `DQ_ATTRIBUTES_JSON`, then call `execute_and_persist_data_quality_rule`.

Catalog text is never trusted as executable SQL. DML, DDL, PL/SQL, database
links, Oracle internal schemas, cartesian joins, and non-allowlisted objects are
rejected before execution.

## Metric contract

Example query shapes:

```sql
SELECT COUNT(*) AS TOTAL_RECORDS
FROM EIM.APPROVED_TABLE
```

```sql
SELECT COUNT(*) AS FAILED_RECORDS
FROM EIM.APPROVED_TABLE
WHERE REQUIRED_COLUMN IS NULL
```

Severity boundaries are deterministic:

- Critical: failure rate greater than 10%
- High: failure rate from 5% through 10%
- Medium: failure rate from 1% up to 5%
- Low: failure rate below 1%

At the shared 5% boundary, High takes precedence.

## Active-serial population

End Customer rules are scoped to active installed products. Use the same
population in the total, failed-count, and failed-detail statements:

```sql
SELECT DISTINCT SYSTEM_SERIAL_NUMBER
FROM EIM.EIM_PR_SYSTEM
WHERE INSTALLED_PRODUCT_STATUS = 'ACTIVE'
  AND SYSTEM_SERIAL_NUMBER IS NOT NULL
```

Join `EIM.EIM_PR_IB_LATEST` through `SYSTEM_SERIAL_NUMBER`; `ROLE_ID = 1`
identifies the End Customer relationship. Completeness rules must begin with
the active-system population and use `NOT EXISTS`, otherwise serials with no
relationship row are silently excluded.

Example failed-detail shape:

```sql
SELECT
    S.SYSTEM_SERIAL_NUMBER,
    S.SYSTEM_SERIAL_NUMBER AS SOURCE_RECORD_KEY,
    'Active serial has no End Customer Site' AS FAILURE_REASON,
    '{"installed_product_status":"ACTIVE"}' AS DQ_ATTRIBUTES_JSON
FROM (
    SELECT DISTINCT SYSTEM_SERIAL_NUMBER
    FROM EIM.EIM_PR_SYSTEM
    WHERE INSTALLED_PRODUCT_STATUS = 'ACTIVE'
) S
WHERE NOT EXISTS (
    SELECT 1
    FROM EIM.EIM_PR_IB_LATEST I
    WHERE I.SYSTEM_SERIAL_NUMBER = S.SYSTEM_SERIAL_NUMBER
      AND I.ROLE_ID = 1
      AND I.CMAT_SITE_ID IS NOT NULL
)
```

## Persisted results and trend history

The DBA migration `sql/04_dq_results_schema.sql` creates:

- `EIM_APPS.EIM_DQ_RECON_SUMMARY` for one metric/report row per rule in a
  company run. `RUN_ID` is the single company-run identifier (`batch_id` in
  the MCP API is an alias for the same value).
- `EIM_APPS.EIM_DQ_FAILED_RECORDS` for governed keys, failure reason, and
  approved DQ attributes; it does not store unrestricted source snapshots.

Call `start_dq_run` once, then pass that `run_id` to every
`execute_and_persist_data_quality_rule` in the report. Load the combined
summary with `get_dq_run_report` or:

```sql
SELECT *
FROM EIM_APPS.EIM_DQ_RECON_SUMMARY
WHERE RUN_ID = :run_id
ORDER BY RULE_ID;
```

Persistence uses a separate `EIM_DQ_WRITER` account with INSERT only on those
two tables. The source reader remains transactionally read-only, and all
failed-detail SQL passes the same AST guard before streaming. The summary and
all detail batches commit atomically; count mismatch, hard-limit breach, or an
Oracle error rolls back the complete run.

Trend comparison uses the latest persisted result for the same database, rule
ID, and population signature. This prevents an ACTIVE-only run from being
compared with an earlier all-record population. `logs/dq-history.jsonl` remains
an ignored local fallback while persisted history is unavailable.
An increased failure percentage is explicitly reported as deterioration.

## DBA deployment

1. Review and run `sql/04_dq_results_schema.sql` as `EIM_APPS` or a DBA.
2. Create `EIM_DQ_WRITER` with `CREATE SESSION`, zero tablespace quota, and no
   roles or system privileges beyond session access.
3. Apply only the object grants in the migration. Adjust `CHATBOT_RO` to the
   deployed read-account name if necessary.
4. Put `DQ_WRITE_USER` and `DQ_WRITE_PASSWORD` in `.env` or a secret manager.
5. Set `ORACLE_MCP_DQ_PERSISTENCE_ENABLED=true` and restart the MCP server.
6. Run a small approved rule and reconcile summary `FAILED_RECORDS` to
   `COUNT(*)` in the detail table for the same `RUN_ID` and `RULE_ID`.

DBA/governance reconciliation query:

```sql
SELECT
    S.RUN_ID,
    S.RULE_ID,
    S.FAILED_RECORDS AS SUMMARY_FAILED,
    COUNT(D.DETAIL_SEQUENCE) AS DETAIL_FAILED
FROM EIM_APPS.EIM_DQ_RECON_SUMMARY S
LEFT JOIN EIM_APPS.EIM_DQ_FAILED_RECORDS D
  ON D.RUN_ID = S.RUN_ID
 AND D.RULE_ID = S.RULE_ID
WHERE S.RUN_ID = :run_id
GROUP BY S.RUN_ID, S.RULE_ID, S.FAILED_RECORDS
```

Retention and purge remain DBA-owned because the MCP writer has no DELETE
privilege. The migration includes a 90-day example; governance must approve the
actual retention period.

## Governance

- Adding a source object to `config/policy/*.yaml` is an approval decision.
- Source database accounts should have only `CREATE SESSION` and approved
  `SELECT` grants. The isolated writer receives only the two documented INSERT
  grants.
- Keep credentials in `.env` or a secrets manager; never place them in MCP JSON.
- The ATP descriptor currently disables server-DN matching. Enable certificate
  hostname verification before production deployment.
- Rotate any password that has been shared in chat or another non-secret channel.

## Current catalog status

The live On-Prem `EIM_APPS.EIM_DQ_RULES_LOOKUP` catalog was reached through MCP
on 2026-08-23 and returned five ACTIVE End Customer rules.
