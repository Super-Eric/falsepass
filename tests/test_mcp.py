"""The MCP server, over stdio, with whichever mcp version is installed (CI runs 1.x and 2.x)."""

import asyncio
import json
import shutil
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


@pytest.mark.skipif(shutil.which("uv") is None, reason="audits need uv")
def test_audit_runs_in_the_background_and_status_returns_the_results(tmp_path):
    """A client with a short tool timeout: wait=0 hands back a running job, status finishes it."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    shutil.copytree(cli.demo_source(), tmp_path / "toy", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))

    async def call(session, tool, **arguments):
        result = await session.call_tool(tool, {"workdir": str(tmp_path), **arguments})
        return json.loads(result.content[0].text)

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "falsepass.cli", "mcp"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                missing = await call(session, "falsepass_audit", env="nothing-here", wait=0)
                await call(session, "falsepass_init", target=str(tmp_path / "toy"))
                first = await call(session, "falsepass_audit", env="falsepass-toy-env", n=5, wait=0)
                again = await call(session, "falsepass_audit", env="falsepass-toy-env", n=5, wait=0)
                done = await call(session, "falsepass_audit_status", env="falsepass-toy-env", wait=600)
                return missing, first, again, done

    missing, first, again, done = asyncio.run(go())
    assert missing["status"] == "error" and "falsepass_init" in missing["next"]
    assert first["status"] == "running" and again["status"] == "running"
    assert done["status"] == "finished", done
    assert done["headline"]["false_pass_ceiling"] == 1.0
    assert json.loads((tmp_path / "audits" / "falsepass-toy-env" / "job.json").read_text())["state"] == "finished"
