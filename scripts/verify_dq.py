"""Verify the standalone DQ MCP server and its live Oracle connections."""

from __future__ import annotations

import asyncio
import json

from fastmcp import Client

from oracle_mcp.server import create_server
from oracle_mcp.settings import Settings

REQUIRED_DQ_TOOLS = {"list_active_dq_rules", "execute_data_quality_rule"}


async def verify() -> int:
    server = create_server(Settings(profile="both", audit_sink="none"))
    async with Client(server) as client:
        tools = sorted(tool.name for tool in await client.list_tools())
        missing = sorted(REQUIRED_DQ_TOOLS - set(tools))
        if missing:
            print(json.dumps({"status": "FAILED", "missing_tools": missing}))
            return 1

        databases = (await client.call_tool("list_databases", {})).data
        rules = (await client.call_tool("list_active_dq_rules", {})).data
        summary = {
            "status": "OK",
            "server": "oracle-eim-dq",
            "databases": [
                row["database_name"] for row in databases.get("databases", [])
            ],
            "dq_tools": sorted(REQUIRED_DQ_TOOLS),
            "active_rule_count": rules.get("active_rule_count", 0),
            "catalog_object": rules.get("catalog_object"),
        }
        print(json.dumps(summary, indent=2))
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(verify()))
