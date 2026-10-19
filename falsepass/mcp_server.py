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
Intellect Environments Hub, OpenEnv / Hugging Face Spaces, Inspect AI, HUD, and plain reward
functions written for TRL's GRPOTrainer; published or in a local folder): it scores
completions whose correctness is known
(the reference, an empty reply, a wrong answer in perfect format, an answer that is then
denied, ...) with the environment's own grader and reports every disagreement.

Typical flow: falsepass_init (Hub slug or local folder) -> falsepass_audit -> read
`failing` and `headline` -> falsepass_repro for anything worth confirming -> read the
reward code -> falsepass_draft for a report skeleton -> falsepass_upstream and
falsepass_verify_fix when a fix is going upstream. Start with falsepass_scan when you
only have source code.

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
    # stdout is kept whole: it is often JSON, and a cut-off document cannot be parsed
    return {"exit_code": done.returncode, "stdout": done.stdout, "stderr": done.stderr[-4000:]}


# Audits run as background jobs so that a client's tool-call timeout never kills one halfway.
# The job is recorded in audits/<env>/job.json and its output in audits/<env>/job.log, so a
# restarted server (or a person) can still see it.
_jobs: dict[str, subprocess.Popen] = {}
RESULT_FILE = {"audit": "probe-results.json", "reach": "reach/latest.json", "repro": "repro-run.json",
               "verify-fix": "verify-fix.txt"}


def _audit_dir(workdir: str, env: str) -> Path:
    audits = Path(workdir).expanduser().resolve() / "audits"
    alias = audits / env.lower().replace("_", "-")  # the name `init` gives inspect_evals/arc_easy is arc-easy
    return alias if not (audits / env).exists() and alias.exists() else audits / env


def _write_job(path: Path, job: dict[str, Any]) -> None:
    """Replace job.json in one step: another server process may be reading it."""
    tmp = path.with_name(f".job.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(job, indent=2))
    os.replace(tmp, path)


class _JobLock:
    """One job start per environment at a time, across server processes (a client may start
    a server per call): without it two starts both saw no running job and the second
    overwrote the first's job.json."""

    def __init__(self, folder: Path) -> None:
        self.path = folder / ".job.lock"

    def __enter__(self) -> None:
        self.handle = open(self.path, "w")
        try:
            import fcntl

            fcntl.flock(self.handle, fcntl.LOCK_EX)
        except ImportError:  # Windows: no flock; atomic writes still keep job.json readable
            pass

    def __exit__(self, *exc: Any) -> None:
        self.handle.close()  # releases the lock


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
    else:  # started by an earlier server; it wrote its results file if it got that far
        results = path.parent / RESULT_FILE[job.get("command", "audit")]
        ok = results.is_file() and results.stat().st_mtime >= job["started"]
        job.update(state="finished" if ok else "failed", exit_code=0 if ok else None)
    job["ended"] = time.time()
    _write_job(path, job)
    _jobs.pop(str(path), None)
    return job


def _log_tail(workdir: str, env: str, lines: int = 12) -> list[str]:
    log = _audit_dir(workdir, env) / "job.log"
    if not log.is_file():
        return []
    text = re.sub(r"[^\n]*\r", "", log.read_text(errors="replace"))  # progress bars redraw with \r
    bar = re.compile(r"^[\w ./-]*:\s*\d+%\|")  # a finished progress bar ("Map: 100%|████| 72/72 ...")
    return [line for line in text.splitlines() if line.strip() and not bar.match(line)][-lines:]


def _start_audit(env: str, args: list[str], workdir: str, command: str = "audit") -> dict[str, Any]:
    folder = _audit_dir(workdir, env)
    if not (folder / "env.toml").is_file():
        return {"status": "error", "error": f"no audits/{env}/env.toml under {Path(workdir).expanduser().resolve()}",
                "next": "Call falsepass_init with the environment first (or pass the workdir that holds audits/)."}  # fmt: skip
    with _JobLock(folder):
        job = _job_state(workdir, env)
        if job and job["state"] == "running":
            return job
        log = open(folder / "job.log", "w")  # the child process inherits it
        proc = subprocess.Popen([sys.executable, "-m", "falsepass.cli", command, env, *args], cwd=Path(workdir).expanduser(),
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)  # fmt: skip
        log.close()
        job = {"env": env, "command": command, "args": args, "pid": proc.pid, "started": time.time(), "state": "running"}
        _write_job(folder / "job.json", job)
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
                "next": f"The {job.get('command', 'audit')} job continues in the background. Call falsepass_audit_status(env={env!r}) to wait for it."}  # fmt: skip
    ran = _audit_dir(workdir, env) / "repro-run.json"
    if job.get("command") == "repro" and ran.is_file() and ran.stat().st_mtime >= job["started"]:
        digest = json.loads(ran.read_text())  # runs that differ exit 1 but are a result, not a failure
        digest.update(status="finished", elapsed_s=elapsed)
        return digest
    if job["state"] == "failed":
        tail = "\n".join(_log_tail(workdir, env, 30))
        command = job.get("command", "audit")
        hint = ("\n\nCommon causes: a missing dependency (falsepass_init with `extra`), a dataset that needs a login, or "
                "an environment automatic mode cannot script (falsepass_init with manual=true writes an adapter skeleton)."
                if command == "audit" else "")  # fmt: skip
        raise RuntimeError(f"the {command} job of {env} failed after {elapsed} s (exit code {job.get('exit_code')}). End of "
                           f"{_audit_dir(workdir, env) / 'job.log'}:\n{tail}{hint}")  # fmt: skip
    if job.get("command") == "verify-fix":
        return {"status": "finished", "elapsed_s": elapsed, "report": (_audit_dir(workdir, env) / "verify-fix.txt").read_text()}
    if job.get("command") == "reach":
        digest = json.loads((_audit_dir(workdir, env) / "reach" / "latest.json").read_text())
        for row in digest["rows"]:  # the file keeps every task; the reply stays short
            if len(row["failed_tasks"]) > 50:
                row["failed_tasks"] = row["failed_tasks"][:50] + [f"... {len(row['failed_tasks']) - 50} more in {digest['file']}"]
        digest.update(status="finished", elapsed_s=elapsed)
        return digest
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

    from falsepass import __version__

    if "version" in inspect.signature(Server.__init__).parameters:  # mcp 2.x
        server = Server("falsepass", instructions=INSTRUCTIONS, version=__version__)
    else:  # mcp 1.x: set on the low-level server, which reports it as serverInfo.version
        server = Server("falsepass", instructions=INSTRUCTIONS)
        server._mcp_server.version = __version__

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
        without: list[str] | None = None, env_class: str | None = None, action_class: str | None = None,
        dataset: str | None = None, functions: list[str] | None = None, weights: list[float] | None = None,
        split: str | None = None, answer_column: str | None = None, prompt_column: str | None = None,
        dataset_config: str | None = None, map: str | None = None, path: list[str] | None = None,
        manual: bool = False, policies: bool = False, force: bool = False, workdir: str = ".",
    ) -> Any:  # fmt: skip
        """Set up audits/<name>/ for an environment and return the name to audit it with.

        `target`: a Prime Hub slug (owner/environment), a Python file of reward functions
        written for TRL's GRPOTrainer (`def accuracy_reward(completions, solution, **kwargs)`;
        or a module name such as `trl.rewards` with framework "reward") together with
        `dataset` (a .jsonl/.json/.csv/.parquet file, `hf:owner/name`, or `call:function` in
        the file; optional `functions`, `weights`, `split`, `dataset_config`, `answer_column`,
        `prompt_column`, `map` (a row mapper in the file), `path` (folders put on sys.path for a
        package that is imported rather than installed, e.g. a clone's `src`)),
        an OpenEnv Space (`hf:owner/space`,
        cloned next to the audit) or folder (openenv.yaml), a local folder with a
        pyproject.toml (verifiers or Inspect project), an Inspect task file, an Inspect
        registry name (inspect_evals/gsm8k), or a HUD environment folder. Options: `name`
        (folder name, default from the environment), `version` (Hub version), `framework`
        ("verifiers", "openenv", "reward", "inspect" or "hud" when detection is wrong), `task` (Inspect:
        which @task), `answers` (HUD and OpenEnv: path to a JSON file of reference answers, an
        object from task key to answer or a list in task order; without it only answer-free
        probes run, unless an OpenEnv task record carries an answer field), `extra` (more
        requirements to install, e.g. ["inspect-evals[math]"]; added to the environment's own),
        `without` (requirement names to drop from the environment's own, e.g. a pin that does
        not resolve), `env_class` / `action_class` (OpenEnv, "module:Class": the environment
        class or factory and the action class when detection picks the wrong one), `manual` (also write an
        adapter.py skeleton for a Q&A environment automatic mode cannot script), `policies`
        (the same for games, simulations and tool tasks: a skeleton built around policies;
        use it when an audit comes back "inconclusive", or for a verifiers v1 tool taskset
        once the automatic run of generic tool policies is done, to add a solver as the control
        and task-aware exploits; v1 episodes run through the taskset's own loop), `force` (rewrite an existing
        audit's env.toml, e.g. to add answers; adapter.py is kept)."""
        args = ["init", target, "--json"]
        for flag, value in (("--name", name), ("--version", version), ("--framework", framework), ("--task", task), ("--answers", answers),
                            ("--env-class", env_class), ("--action-class", action_class), ("--dataset", dataset), ("--split", split),
                            ("--answer-column", answer_column), ("--prompt-column", prompt_column),
                            ("--dataset-config", dataset_config), ("--map", map)):  # fmt: skip
            if value:
                args += [flag, value]
        for requirement in extra or []:
            args += ["--with", requirement]
        for requirement in without or []:
            args += ["--without", requirement]
        for function in functions or []:
            args += ["--func", function]
        for folder in path or []:
            args += ["--path", folder]
        for weight in weights or []:
            args += ["--weight", str(weight)]
        args += ["--manual"] * manual + ["--policies"] * policies + ["--force"] * force
        manual = manual or policies
        result = _json(_run(args, workdir, timeout=600))
        if manual:
            result["next"] = f"Fill in {result['folder']}/adapter.py, then call falsepass_audit(env={result['name']!r})"
        elif result.get("automatic_likely") is False:
            result["next"] = (f"Automatic mode will probably stop ({result.get('automatic_unlikely_because', 'see the source')}). "
                              f"Call falsepass_init again with force=true and manual=true (a Q&A task whose reply you can "
                              f"write) or policies=true (a game, simulation or tool task), fill in adapter.py, then "
                              f"falsepass_audit(env={result['name']!r}). Or try falsepass_audit first: it says why it stops.")  # fmt: skip
        else:
            result["next"] = f"Call falsepass_audit(env={result['name']!r})"
        return result

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
        if started.get("command", "audit") != "audit":
            raise RuntimeError(f"a {started.get('command', 'audit')} job of {env} is still running; wait for it with falsepass_audit_status first")
        return _await_audit(env, workdir, wait)

    @tool
    def falsepass_audit_status(env: str, wait: int = 45, workdir: str = ".") -> Any:
        """Wait up to `wait` seconds (max 600) for the job started by falsepass_audit or
        falsepass_reach, then return its results (status "finished") or progress (status
        "running": call again). A failed job is returned as an error with the end of its log."""
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
    def falsepass_repro(env: str, probe: list[str] | None = None, run: bool = False, script: str = "",
                        wait: int = 45, workdir: str = ".") -> Any:  # fmt: skip
        """Write audits/<env>/repro_auto.py: a script that depends only on the environment's
        own packages and re-scores the exact failing completions. `probe` limits it to some
        probe codes, e.g. ["P5"]. Identical output from two clean runs is what makes a lead
        reproducible: with `run`=true the script is also run twice, each in a fresh
        virtualenv, in the background like an audit (status "running": call
        falsepass_audit_status). The result says whether the runs were identical, where the
        first is stored (headed by its command), and how many cases still fail. `script`
        runs an existing script in the audit folder instead (e.g. a hand-written repro.py)."""
        args = [a for code in probe or [] for a in ("--probe", code)] + (["--script", script] if script else [])
        if not run:
            return {"message": _ok(_run(["repro", env, *args], workdir, timeout=120))["stdout"].strip()}
        started = _start_audit(env, ["--run", *args], workdir, command="repro")
        if started.get("status") == "error":
            raise RuntimeError(f"{started['error']}. {started['next']}")
        if started.get("command", "audit") != "repro":
            raise RuntimeError(f"a {started.get('command', 'audit')} job of {env} is still running; wait for it with falsepass_audit_status first")
        return _await_audit(env, workdir, wait)

    @tool
    def falsepass_reach(env: str, probe: list[str] | None = None, templates: list[str] | None = None,
                        expect: str = "zero", tasks: int = 0, wait: int = 45, workdir: str = ".") -> Any:
        """After an audit: how many tasks a failing probe reaches. Scores only the rows asked
        for, e.g. probe=["P15/denied"] (a code, or code/variant as in the results), plus the
        reference (P1) as a control, on every task (`tasks`=0) or a sample of `tasks`.
        `templates` counts completions of your own instead of writing a script (rows U1/t1,
        t2, ...): "{answer}", "{wrong}", "{answer_in_format}" and "{wrong_in_format}" are
        filled per task, other braces stay, e.g. "\\boxed{{answer} \\text{ is wrong}}";
        `expect` "zero" counts where they are paid, "full" where they are not paid in full.
        Each template row also returns `first_completion`, the text it became on the first task.
        Returns per row the tasks counted, how many failed (expected 0: paid; expected full:
        not paid in full), full-reward and error counts, the failing task keys and up to
        three failing completions. Per-example probes only (P1-P9, P13-P17, A1, X1).
        Runs in the background like falsepass_audit: status "running" means call
        falsepass_audit_status. Whole datasets whose reward starts a process per call
        (uv scripts) take minutes to an hour."""
        if not probe and not templates:
            raise RuntimeError('nothing to count: give probe=["P15/denied"] or templates=["..."]')
        if expect not in ("zero", "full"):
            raise RuntimeError('expect is "zero" or "full"')
        args = [a for code in probe or [] for a in ("--probe", code)] + [a for text in templates or [] for a in ("--template", text)]
        args += ["--expect", expect, "--tasks", str(tasks) if tasks > 0 else "all"]
        started = _start_audit(env, args, workdir, command="reach")
        if started.get("status") == "error":
            raise RuntimeError(f"{started['error']}. {started['next']}")
        if started.get("command", "audit") != "reach":
            raise RuntimeError(f"a {started.get('command', 'audit')} job of {env} is still running; wait for it with falsepass_audit_status first")
        return _await_audit(env, workdir, wait)

    @tool
    def falsepass_verify_fix(env: str, source: str, known_good: int = -1, wait: int = 45, workdir: str = ".") -> Any:
        """Check a patch: rerun the stored repro (repro.py, else repro_auto.py) with the
        environment taken from `source` and list the lines that change (no stored output:
        the unpatched script runs first and is stored as the baseline). `source` is a
        patched package folder, or a patched copy of the audit's src/ without pyproject.toml,
        which is laid over the release. `known_good` >= 0 also scores every correct-answer
        row (P1, P2, P14) with and without the patch, on every task (0) or a sample, and
        lists the tasks whose correct answer the patch stops paying. Runs in the background
        like an audit (status "running": call falsepass_audit_status)."""
        path = Path(source).expanduser()
        path = path if path.is_absolute() else Path(workdir).expanduser() / path
        args = ["--source", str(path.resolve())] + (["--known-good", str(known_good) if known_good > 0 else "all"] if known_good >= 0 else [])
        started = _start_audit(env, args, workdir, command="verify-fix")
        if started.get("status") == "error":
            raise RuntimeError(f"{started['error']}. {started['next']}")
        if started.get("command", "audit") != "verify-fix":
            raise RuntimeError(f"a {started.get('command', 'audit')} job of {env} is still running; wait for it with falsepass_audit_status first")
        return _await_audit(env, workdir, wait)

    @tool
    def falsepass_draft(env: str, workdir: str = ".") -> Any:
        """Write audits/<env>/REPORT.draft.md from the last run: each failing probe group as a
        candidate finding with the confirmed static hints attached, and the places marked
        where a person has to judge (defect or design choice, severity). Returns the file
        path and its text. A draft is not a report: confirm each candidate with
        falsepass_repro and the reward code first."""
        _ok(_run(["draft", env], workdir, timeout=300))
        path = _audit_dir(workdir, env) / "REPORT.draft.md"
        return {"file": str(path), "text": path.read_text()}

    @tool
    def falsepass_upstream(env: str, repo: str = "", limit: int = 5, workdir: str = ".") -> Any:
        """Find the environment's source repository on GitHub (code search for the package
        name, then for its most distinctive class and function names), compare every audited
        file with the repository's default branch, and report what a contributor needs:
        default branch, licence, last push, open pull requests, how many recent merges came
        from outside. `repo` (owner/name) checks one repository instead of searching. Needs
        the `gh` CLI, logged in."""
        args = ["upstream", env] + (["--repo", repo] if repo else []) + ["--limit", str(limit)]
        return {"report": _ok(_run(args, workdir, timeout=600))["stdout"]}

    @tool
    def falsepass_discover(days: int = 90, limit: int = 40, hub: str = "prime", workdir: str = ".") -> Any:
        """Recently maintained environments: `hub` "prime" (the Prime Intellect Hub, with what
        each needs: sandbox, tools, LLM judge, API key, reference answers; needs the `prime`
        CLI) or "hf" (OpenEnv Spaces on the Hugging Face Hub, most liked first, each with
        the `hf:owner/space` target for falsepass_init; the public listing only)."""
        args = ["discover", "--days", str(days), "--limit", str(limit), "--hub", hub]
        return {"table": _ok(_run(args, workdir, timeout=900))["stdout"]}

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
