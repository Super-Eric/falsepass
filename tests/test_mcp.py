"""The MCP server, over stdio, with whichever mcp version is installed (CI runs 1.x and 2.x)."""

import asyncio
import json
import sys

import pytest

from falsepass import cli

mcp = pytest.importorskip("mcp")


def test_tools_are_listed_and_scan_answers():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "falsepass.cli", "mcp"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = [t.name for t in (await session.list_tools()).tools]
                result = await session.call_tool("falsepass_scan", {"path": str(cli.demo_source())})
                return names, json.loads(result.content[0].text)

    names, scanned = asyncio.run(go())
    assert {"falsepass_scan", "falsepass_init", "falsepass_audit", "falsepass_results"} <= set(names)
    assert [h["rule"] for h in scanned["hints"]] == ["reference_substring_match"]
