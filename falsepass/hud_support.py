"""HUD environments (https://docs.hud.ai): score answers with a task template's own grader.

A HUD task template is an async generator: it yields a prompt, receives the agent's
answer and yields a reward. falsepass drives it the way HUD's own server does
(`hud.environment.server.TaskRunner`: start, then grade with `{"answer": ...}`), after the
environment's initialize hooks have run. hud is imported lazily, inside the throw-away
virtualenv of an audit.

HUD tasks carry no reference answer: the expected value lives inside the template. Without
one, only answer-free probes run (an empty reply, the prompt echoed back, a reply that
lists every small integer, ...). With a reference file (`answers` in env.toml: a JSON object
from task slug to answer, or a list in task order) every probe runs.

The spec is a tasks file or a folder holding the environment and its tasks.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from falsepass.model import Completion, Example, Score
from falsepass.scorers import _guarded


def _reply(completion: Completion) -> str:
    if isinstance(completion, str):
        return completion
    turns = [str(m.get("content") or "") for m in completion if m.get("role") == "assistant" and not m.get("tool_calls")]
    return turns[-1] if turns else ""


class HudScorer:
    """Score a reply with the task template's grader, as HUD's server would."""

    def __init__(self, environments: dict[str, Any]) -> None:
        self.environments = environments
        self.started: list[Any] = []

    @property
    def weights(self) -> dict[str, float]:
        return {}

    async def _environment(self, name: str) -> Any:
        env = self.environments[name]
        if env not in self.started:
            await env.start()
            self.started.append(env)
        return env

    async def score(self, example: Example, completion: Completion) -> Score:
        from hud.environment.server import TaskRunner

        task = example.raw
        errors: list[str] = []
        result: dict[str, Any] = {}
        runner: Any = None

        async def run() -> None:
            nonlocal runner
            env = await self._environment(task.env)
            runner = TaskRunner(env.tasks[task.id], task.args)
            await runner.start()
            result.update(await runner.grade({"answer": _reply(completion)}))

        ok = await _guarded(run(), errors)
        if not ok:
            if runner is not None:
                await runner.cancel()
            return Score(None, {}, errors)
        metrics = {}
        for sub in result.get("subscores") or []:
            value = sub.get("value", sub.get("score")) if isinstance(sub, dict) else None
            if isinstance(value, (int, float)):
                metrics[str(sub.get("name", f"subscore{len(metrics)}"))] = float(value)
        return Score(float(result["score"]), metrics, errors)

    async def close(self) -> None:
        for env in reversed(self.started):
            await env.stop()
        self.started.clear()


def _environments(tasks: list[Any], source: Path) -> dict[str, Any]:
    from hud.environment import load_environment

    return {name: load_environment(source, name=name) for name in sorted({t.env for t in tasks})}


async def _prompts(scorer: HudScorer, examples: list[Example]) -> None:
    from hud.environment.server import TaskRunner

    try:
        for example in examples:
            env = await scorer._environment(example.raw.env)
            runner = TaskRunner(env.tasks[example.raw.id], example.raw.args)
            frame = await runner.start()
            await runner.cancel()
            prompt = frame.get("prompt", "")
            example.prompt = prompt if isinstance(prompt, str) else json.dumps(prompt)
    finally:
        await scorer.close()


def load(spec: str, max_rows: int, answers: str | None = None) -> tuple[HudScorer, list[Example]]:
    """Tasks and environments from `spec`; works in the environment's folder (its hooks use relative paths)."""
    from hud import Taskset

    path = Path(spec).resolve()
    folder = path if path.is_dir() else path.parent
    os.chdir(folder)
    tasks = list(Taskset.from_file(path).tasks.values())[:max_rows]
    if not tasks:
        raise RuntimeError(f"no HUD tasks found in {path}")
    references: Any = json.loads(Path(answers).read_text()) if answers else {}
    pool = []
    for i, task in enumerate(tasks):
        answer = references.get(task.slug, "") if isinstance(references, dict) else (references[i] if i < len(references) else "")
        pool.append(Example(task.slug or f"task-{i}", "", str(answer), task))
    scorer = HudScorer(_environments(tasks, folder))
    asyncio.run(_prompts(scorer, pool))
    return scorer, pool


# --- `falsepass init` (runs outside the audit virtualenv: no hud import) ----------------------


def uses_hud(path: Path) -> bool:
    """A HUD environment: Python source that imports hud and defines `@env.template` tasks."""
    files = [path] if path.is_file() else [f for f in path.glob("*.py")]
    text = "\n".join(f.read_text(errors="replace") for f in files)
    return bool(re.search(r"^\s*(from|import)\s+hud\b", text, re.M)) and (".template(" in text or "Taskset" in text or path.is_file())


def describe(path: Path, answers: str | None = None) -> dict[str, Any]:
    import tomllib

    from falsepass.hub import pypi_version

    path = path.resolve()
    folder = path if path.is_dir() else path.parent
    spec = folder / "tasks.py" if path.is_dir() and (folder / "tasks.py").is_file() else path
    extra: list[str] = []
    pyproject = folder / "pyproject.toml"
    if pyproject.is_file():  # the environment's own dependencies, without installing the folder as a package
        extra = [d for d in tomllib.loads(pyproject.read_text()).get("project", {}).get("dependencies", [])
                 if not re.match(r"hud(\[|\s|[<>=!~;]|$)", d.strip())]  # fmt: skip
    info = {"id": f"local/{folder.name}", "framework": "hud", "task": str(spec), "hud": pypi_version("hud"),
            "package": "", "version": "", "declared": "", "extra": extra, "others": []}  # fmt: skip
    if answers:
        info["answers"] = str(Path(answers).resolve())
    return info
