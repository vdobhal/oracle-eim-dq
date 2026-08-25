"""Email one company DQ recon summary. Failed-record details are never included."""

from __future__ import annotations

import argparse
import json
import sys

from oracle_mcp.server import build_service
from oracle_mcp.settings import get_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Email the EIM DQ recon summary (metrics only, no failed rows)."
    )
    parser.add_argument("--run-id", required=True, help="Company RUN_ID from start_dq_run")
    parser.add_argument(
        "--to",
        default="",
        help="Recipient. Defaults to ORACLE_MCP_DQ_MAIL_TO (vdobhal@netapp.com).",
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    service = build_service(settings)
    result = service.email_dq_run_summary(
        args.run_id,
        to_address=args.to or None,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("status") == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
