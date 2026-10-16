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
        return {"is_error": True, "text": result.content[0].text} if getattr(result, "is_error", None) or getattr(result, "isError", False) else json.loads(result.content[0].text)

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "falsepass.cli", "mcp"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                missing = await call(session, "falsepass_audit", env="nothing-here", wait=0)
                init = await call(session, "falsepass_init", target=str(tmp_path / "toy"))
                first = await call(session, "falsepass_audit", env=init["name"], n=5, wait=0)
                during = await call(session, "falsepass_results", env="falsepass_toy_env")  # underscores find it too
                again = await call(session, "falsepass_audit", env=init["name"], n=5, wait=0)
                done = await call(session, "falsepass_audit_status", env=init["name"], wait=600)
                full = await call(session, "falsepass_completion", env=init["name"], completion_id=done["failing"][0]["completion_id"])
                return missing, init, first, during, again, done, full

    missing, init, first, during, again, done, full = asyncio.run(go())
    assert missing["is_error"] and "falsepass_init" in missing["text"]
    assert init["name"] == "falsepass-toy-env" and init["next"] == "Call falsepass_audit(env='falsepass-toy-env')"
    assert first["status"] == during["status"] == again["status"] == "running"
    assert done["status"] == "finished", done
    assert done["headline"]["false_pass_ceiling"] == 1.0
    assert full["text"].strip() and len(full["text"]) >= len(done["failing"][0]["completion"].split(" … ")[0])
    assert json.loads((tmp_path / "audits" / "falsepass-toy-env" / "job.json").read_text())["state"] == "finished"


def test_long_output_reaches_the_client_whole(tmp_path):
    """Command output is not cut: a long completion (or a long JSON digest) arrives intact."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    audit = tmp_path / "audits" / "big"
    audit.mkdir(parents=True)
    (audit / "env.toml").write_text("[env]\n")
    (audit / "probe-results.json").write_text(json.dumps({"meta": {"environment": "big"}, "results": [], "static": []}))
    text = "word " * 12_000 + "THE END"
    (audit / "completions.json").write_text(json.dumps({"abc": text}))

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "falsepass.cli", "mcp"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("falsepass_completion", {"env": "big", "completion_id": "abc", "workdir": str(tmp_path)})
                return json.loads(result.content[0].text)

    assert asyncio.run(go())["text"].strip() == text.strip()
