"""`falsepass mcp` — the command line as MCP tools, for coding agents.

Every tool runs the same `falsepass` command a person would type, in `workdir` (where
the `audits/` folder lives), and returns its output. Nothing here publishes anything or
calls a paid service; `audit` installs packages into a throw-away virtualenv with uv.

Register it with an MCP client, e.g. Claude Code:

    claude mcp add falsepass -- uvx --from "falsepass[mcp]" falsepass mcp
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

INSTRUCTIONS = """\
falsepass checks the reward code of RL environments and eval tasks (verifiers / Prime
Intellect Environments Hub, Inspect AI, HUD; published or in a local folder): it scores
completions whose correctness is known
(the reference, an empty reply, a wrong answer in perfect format, an answer that is then
denied, ...) with the environment's own grader and reports every disagreement.

Typical flow: falsepass_init (Hub slug or local folder) -> falsepass_audit -> read
`failing` and `headline` -> falsepass_repro for anything worth confirming -> read the
reward code. Start with falsepass_scan when you only have source code.

How to read results: everything is a lead, not a verdict. A grader may be strict or
lenient on purpose (partial credit, letter case); say so instead of calling it a defect.
When telling a maintainer, give the exact completion, the score, the line of code that
causes it and a suggested fix. Never describe an environment as broken."""


def _run(args: list[str], workdir: str, timeout: int = 3600) -> dict[str, Any]:
    done = subprocess.run(
        [sys.executable, "-m", "falsepass.cli", *args], cwd=Path(workdir).expanduser(),
        capture_output=True, text=True, timeout=timeout, check=False,
    )  # fmt: skip
    return {"exit_code": done.returncode, "stdout": done.stdout[-20000:], "stderr": done.stderr[-4000:]}


def _json_or_raw(result: dict[str, Any]) -> Any:
    if result["exit_code"] == 0:
        try:
            return json.loads(result["stdout"])
        except json.JSONDecodeError:
            pass
    return result


def build():
    try:  # mcp 2.x
        from mcp.server.mcpserver import MCPServer as Server
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as Server

    server = Server("falsepass", instructions=INSTRUCTIONS)

    @server.tool()
    def falsepass_scan(path: str, workdir: str = ".") -> Any:
        """Static pre-scan of reward source code (a .py file or a folder). Nothing is
        installed or run. Returns hints: patterns worth a look, each to be confirmed by an audit."""
        found = _json_or_raw(_run(["scan", path, "--json"], workdir, timeout=300))
        return {"hints": found, "note": "Hints are reasons to look; falsepass_audit confirms or rejects them."} if isinstance(found, list) else found

    @server.tool()
    def falsepass_init(target: str, name: str | None = None, workdir: str = ".") -> dict[str, Any]:
        """Set up audits/<name>/ for an environment. `target` is a Hub slug (owner/environment),
        a local folder with a pyproject.toml (verifiers or Inspect project), an Inspect task file,
        an Inspect registry name (inspect_evals/gsm8k), or a HUD environment folder."""
        return _run(["init", target, *(["--name", name] if name else [])], workdir, timeout=300)

    @server.tool()
    def falsepass_audit(env: str, n: int = 25, search: int = 0, workdir: str = ".") -> Any:
        """Probe one environment set up with falsepass_init: installs it in a throw-away
        virtualenv and scores known-good and known-bad completions with its own reward code.
        Takes from under a minute to several minutes. `search` > 0 adds mutation search
        (scorer calls per direction and example; 120 is a good value). Returns the headline
        numbers, the failing probes with one example completion each, and static hints."""
        run = _run(["audit", env, "-n", str(n), *(["--search", str(search)] if search else [])], workdir)
        results = _run(["results", env, "--json"], workdir, timeout=120)
        if results["exit_code"] != 0:
            return {"audit": run, "results": results}
        digest = json.loads(results["stdout"])
        digest["summary_file"] = str(Path(workdir).expanduser() / "audits" / env / "summary.md")
        return digest

    @server.tool()
    def falsepass_results(env: str, workdir: str = ".") -> Any:
        """The last probe run of an environment as JSON (headline, failing probes, static hints)."""
        return _json_or_raw(_run(["results", env, "--json"], workdir, timeout=120))

    @server.tool()
    def falsepass_repro(env: str, probe: list[str] | None = None, workdir: str = ".") -> dict[str, Any]:
        """Write audits/<env>/repro_auto.py: a script that depends only on verifiers and the
        environment and re-scores the exact failing completions. Run it twice; identical
        output is what makes a lead reproducible."""
        args = ["repro", env]
        for code in probe or []:
            args += ["--probe", code]
        return _run(args, workdir, timeout=120)

    @server.tool()
    def falsepass_discover(days: int = 90, limit: int = 40, workdir: str = ".") -> dict[str, Any]:
        """Recently maintained Hub environments and what each needs (sandbox, tools, LLM
        judge, API key, reference answers). Needs the `prime` CLI."""
        return _run(["discover", "--days", str(days), "--limit", str(limit)], workdir, timeout=900)

    return server


def main() -> int:
    try:
        server = build()
    except ImportError as missing:
        print(f"falsepass: the MCP server needs the `mcp` package: pip install 'falsepass[mcp]' ({missing})", file=sys.stderr)
        return 1
    server.run()
    return 0
