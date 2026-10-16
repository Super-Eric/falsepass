"""`falsepass mcp` — the command line as MCP tools, for coding agents.

Every tool runs the same `falsepass` command a person would type, in `workdir` (where
the `audits/` folder lives), and returns its output. Nothing here publishes anything or
calls a paid service; `audit` installs packages into a throw-away virtualenv with uv.

Register it with an MCP client, e.g. Claude Code:

    claude mcp add falsepass -- uvx --from "falsepass[mcp]" falsepass mcp
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
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

An audit can take several minutes (it installs the environment first). falsepass_audit
waits up to `wait` seconds; if the audit is still running it returns
{"status": "running", ...} and the audit carries on in the background. Then call
falsepass_audit_status with the same env until it returns the results.

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


# Audits run as background jobs so that a client's tool-call timeout never kills one halfway.
# The job is recorded in audits/<env>/job.json and its output in audits/<env>/job.log, so a
# restarted server (or a person) can still see it.
_jobs: dict[str, subprocess.Popen] = {}


def _audit_dir(workdir: str, env: str) -> Path:
    audits = Path(workdir).expanduser().resolve() / "audits"
    alias = audits / env.lower().replace("_", "-")  # the name `init` gives inspect_evals/arc_easy is arc-easy
    return alias if not (audits / env).exists() and alias.exists() else audits / env


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _job_state(workdir: str, env: str) -> dict[str, Any] | None:
    """The last audit job of `env`, with `state` brought up to date."""
    path = _audit_dir(workdir, env) / "job.json"
    if not path.is_file():
        return None
    job = json.loads(path.read_text())
    if job["state"] != "running":
        return job
    proc = _jobs.get(str(path))
    if proc is not None:
        code = proc.poll()
        if code is None:
            return job
        job.update(state="finished" if code == 0 else "failed", exit_code=code)
    elif _alive(job["pid"]):
        return job
    else:  # started by an earlier server; it wrote probe-results.json if it got that far
        results = path.parent / "probe-results.json"
        ok = results.is_file() and results.stat().st_mtime >= job["started"]
        job.update(state="finished" if ok else "failed", exit_code=0 if ok else None)
    job["ended"] = time.time()
    path.write_text(json.dumps(job, indent=2))
    _jobs.pop(str(path), None)
    return job


def _log_tail(workdir: str, env: str, lines: int = 12) -> list[str]:
    log = _audit_dir(workdir, env) / "job.log"
    if not log.is_file():
        return []
    text = re.sub(r"[^\n]*\r", "", log.read_text(errors="replace"))  # progress bars redraw with \r
    return [line for line in text.splitlines() if line.strip()][-lines:]


def _start_audit(env: str, args: list[str], workdir: str) -> dict[str, Any]:
    folder = _audit_dir(workdir, env)
    if not (folder / "env.toml").is_file():
        return {"status": "error", "error": f"no audits/{env}/env.toml under {Path(workdir).expanduser().resolve()}",
                "next": "Call falsepass_init with the environment first (or pass the workdir that holds audits/)."}  # fmt: skip
    job = _job_state(workdir, env)
    if job and job["state"] == "running":
        return job
    log = open(folder / "job.log", "w")  # the child process inherits it
    proc = subprocess.Popen([sys.executable, "-m", "falsepass.cli", "audit", env, *args], cwd=Path(workdir).expanduser(),
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)  # fmt: skip
    log.close()
    job = {"env": env, "args": args, "pid": proc.pid, "started": time.time(), "state": "running"}
    (folder / "job.json").write_text(json.dumps(job, indent=2))
    _jobs[str(folder / "job.json")] = proc
    return job


def _await_audit(env: str, workdir: str, wait: float) -> dict[str, Any]:
    """Wait up to `wait` seconds, then report: the results digest, a failure, or progress."""
    deadline = time.time() + max(0.0, min(wait, 600))
    job = _job_state(workdir, env)
    while job and job["state"] == "running" and time.time() < deadline:
        time.sleep(1)
        job = _job_state(workdir, env)
    if job is None:
        raise RuntimeError(f"no audit of {env!r} has been started from this workdir; call falsepass_audit first")
    elapsed = round((job.get("ended") or time.time()) - job["started"])
    if job["state"] == "running":
        return {"status": "running", "env": env, "elapsed_s": elapsed, "log_tail": _log_tail(workdir, env),
                "next": f"The audit continues in the background. Call falsepass_audit_status(env={env!r}) to wait for it."}  # fmt: skip
    if job["state"] == "failed":
        tail = "\n".join(_log_tail(workdir, env, 30))
        raise RuntimeError(f"the audit of {env} failed after {elapsed} s (exit code {job.get('exit_code')}). End of "
                           f"{_audit_dir(workdir, env) / 'job.log'}:\n{tail}\n\nCommon causes: a missing dependency "
                           "(falsepass_init with `extra`), a dataset that needs a login, or an environment automatic "
                           "mode cannot script (falsepass_init with manual=true writes an adapter skeleton).")  # fmt: skip
    digest = _json(_run(["results", env, "--json"], workdir, timeout=120))
    digest.update(status="finished", elapsed_s=elapsed, summary_file=str(_audit_dir(workdir, env) / "summary.md"))
    return digest


def _safe(value: Any) -> Any:
    """Probe completions can hold lone surrogates (P10), which JSON-RPC cannot carry: escape them."""
    if isinstance(value, str):
        return value.encode("utf-8", "backslashreplace").decode("utf-8")
    if isinstance(value, dict):
        return {_safe(k): _safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe(v) for v in value]
    return value


def _ok(result: dict[str, Any]) -> dict[str, Any]:
    """A command's result, or a tool error that says what went wrong (clients mark it isError)."""
    if result["exit_code"] != 0:
        reason = (result["stderr"].strip() or result["stdout"].strip() or "no output")[-2000:]
        raise RuntimeError(f"exit code {result['exit_code']}: {reason}")
    return result


def _json(result: dict[str, Any]) -> Any:
    return json.loads(_ok(result)["stdout"])


def build():
    import inspect

    try:  # mcp 2.x
        from mcp.server.mcpserver import MCPServer as Server
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as Server
        from mcp.server.fastmcp.exceptions import ToolError

    server = Server("falsepass", instructions=INSTRUCTIONS)

    def tool(function):
        """Register `function`; its docstring, without the source indentation, is the description."""
        description = "\n\n".join(" ".join(part.split()) for part in inspect.cleandoc(function.__doc__).split("\n\n"))

        def wrapped(*args, **kwargs):
            try:
                return _safe(function(*args, **kwargs))
            except Exception as failure:  # the client shows the reason only for a ToolError
                raise ToolError(_safe(str(failure) if isinstance(failure, RuntimeError) else f"{type(failure).__name__}: {failure}")) from failure

        wrapped.__name__, wrapped.__doc__, wrapped.__signature__ = function.__name__, function.__doc__, inspect.signature(function)
        wrapped.__annotations__ = function.__annotations__
        return server.tool(name=function.__name__, description=description)(wrapped)

    @tool
    def falsepass_scan(path: str, workdir: str = ".") -> Any:
        """Static pre-scan of reward source code (a .py file or a folder). Nothing is
        installed or run. Returns hints: patterns worth a look, each to be confirmed by an audit."""
        return {"hints": _json(_run(["scan", path, "--json"], workdir, timeout=300)),
                "note": "Hints are reasons to look; falsepass_audit confirms or rejects them."}  # fmt: skip

    @tool
    def falsepass_init(
        target: str, name: str | None = None, version: str | None = None, framework: str | None = None,
        task: str | None = None, answers: str | None = None, extra: list[str] | None = None,
        manual: bool = False, force: bool = False, workdir: str = ".",
    ) -> Any:  # fmt: skip
        """Set up audits/<name>/ for an environment and return the name to audit it with.

        `target`: a Hub slug (owner/environment), a local folder with a pyproject.toml
        (verifiers or Inspect project), an Inspect task file, an Inspect registry name
        (inspect_evals/gsm8k), or a HUD environment folder. Options: `name` (folder name,
        default from the environment), `version` (Hub version), `framework` ("verifiers",
        "inspect" or "hud" when detection is wrong), `task` (Inspect: which @task), `answers`
        (HUD: path to a JSON file of reference answers, an object from task slug to answer or
        a list in task order; without it only answer-free probes run), `extra` (more
        requirements to install, e.g. ["inspect-evals[math]"]), `manual` (also write an
        adapter.py skeleton for environments automatic mode cannot script), `force` (rewrite
        an existing audit's env.toml, e.g. to add answers)."""
        args = ["init", target, "--json"]
        for flag, value in (("--name", name), ("--version", version), ("--framework", framework), ("--task", task), ("--answers", answers)):
            if value:
                args += [flag, value]
        for requirement in extra or []:
            args += ["--with", requirement]
        args += ["--manual"] * manual + ["--force"] * force
        return _json(_run(args, workdir, timeout=600))

    @tool
    def falsepass_audit(env: str, n: int = 25, search: int = 0, wait: int = 45, workdir: str = ".") -> Any:
        """Probe one environment set up with falsepass_init: installs it in a throw-away
        virtualenv and scores known-good and known-bad completions with its own reward code.

        Takes from under a minute to several minutes, so it runs in the background: this
        call waits up to `wait` seconds (max 600). If the audit is done, it returns the
        headline numbers, the failing probes with one example completion each, and static
        hints (status "finished"); otherwise {"status": "running", ...}: then call
        falsepass_audit_status. Calling it again while an audit of `env` runs does not start
        a second one. `n` is the number of tasks sampled; `search` > 0 adds mutation search
        (scorer calls per direction and example; 120 is a good value)."""
        started = _start_audit(env, ["-n", str(n), *(["--search", str(search)] if search else [])], workdir)
        if started.get("status") == "error":
            raise RuntimeError(f"{started['error']}. {started['next']}")
        return _await_audit(env, workdir, wait)

    @tool
    def falsepass_audit_status(env: str, wait: int = 45, workdir: str = ".") -> Any:
        """Wait up to `wait` seconds (max 600) for the audit started by falsepass_audit, then
        return its results (status "finished") or progress (status "running": call again).
        A failed audit is returned as an error with the end of its log."""
        return _await_audit(env, workdir, wait)

    @tool
    def falsepass_results(env: str, workdir: str = ".") -> Any:
        """The last completed probe run of an environment (headline, failing probes, static
        hints). While an audit is still running, its progress instead."""
        job = _job_state(workdir, env)
        if job and job["state"] == "running":
            return _await_audit(env, workdir, 0)
        return _json(_run(["results", env, "--json"], workdir, timeout=120))

    @tool
    def falsepass_completion(env: str, completion_id: str, workdir: str = ".") -> Any:
        """The full text of one completion from the last run (a `completion_id` from the
        results): the digest shows only its start and end."""
        return {"completion_id": completion_id, "text": _ok(_run(["results", env, "--completion", completion_id], workdir, timeout=60))["stdout"]}

    @tool
    def falsepass_repro(env: str, probe: list[str] | None = None, workdir: str = ".") -> Any:
        """Write audits/<env>/repro_auto.py: a script that depends only on the environment's
        own packages and re-scores the exact failing completions. `probe` limits it to some
        probe codes, e.g. ["P5"]. Run it twice from a clean virtualenv (the command is in its
        docstring); identical output is what makes a lead reproducible."""
        args = ["repro", env]
        for code in probe or []:
            args += ["--probe", code]
        return {"message": _ok(_run(args, workdir, timeout=120))["stdout"].strip()}

    @tool
    def falsepass_discover(days: int = 90, limit: int = 40, workdir: str = ".") -> Any:
        """Recently maintained Hub environments and what each needs (sandbox, tools, LLM
        judge, API key, reference answers). Needs the `prime` CLI."""
        return {"table": _ok(_run(["discover", "--days", str(days), "--limit", str(limit)], workdir, timeout=900))["stdout"]}

    return server


def _relaunch_with_mcp() -> None:
    """`uvx falsepass mcp` (how MCP directories start a PyPI server) has no `mcp` package:
    start the same release again with the `mcp` extra, through uvx. Returns only if that
    is not possible."""
    import shutil

    from falsepass import __version__

    uvx = shutil.which("uvx")
    if uvx is None or os.environ.get("FALSEPASS_MCP_RELAUNCHED"):
        return
    os.environ["FALSEPASS_MCP_RELAUNCHED"] = "1"
    os.execv(uvx, [uvx, "--quiet", "--from", f"falsepass[mcp]=={__version__}", "falsepass", "mcp"])


def main() -> int:
    try:
        server = build()
    except ImportError as missing:
        _relaunch_with_mcp()
        print(f"falsepass: the MCP server needs the `mcp` package: pip install 'falsepass[mcp]' ({missing})", file=sys.stderr)
        return 1
    server.run()
    return 0
