"""Inspect AI tasks (https://inspect.aisi.org.uk): load a task and score completions with it.

A completion is scored the way an Inspect eval would score it: the task's own solver
chain runs with a scripted stand-in for the model (so prompt templates and answer
parsing, e.g. `multiple_choice()`, are the task's real code), then every scorer of the
task runs on the resulting state. inspect_ai is imported lazily, inside the throw-away
virtualenv of an audit.

Task specs: `module:function`, `path/to/file.py@function` (or just the file, when it
defines one task), or a registry name such as `inspect_evals/gsm8k`.
"""

from __future__ import annotations

import importlib
import importlib.util
import itertools
import re
from pathlib import Path
from typing import Any

from falsepass.model import Completion, Example, Score
from falsepass.scorers import _guarded

SCRIPTED_MODEL = "falsepass/scripted"


def _tasks_in(module: Any) -> list[tuple[str, Any]]:
    from inspect_ai._util.registry import is_registry_object, registry_info

    found = []
    for name, obj in vars(module).items():
        if callable(obj) and is_registry_object(obj) and registry_info(obj).type == "task":
            found.append((name, obj))
    return found


TASK_MODULE: dict[str, str] = {}  # spec -> the module its task function lives in (installed tasks only)


def load_task(spec: str, args: dict[str, Any] | None = None) -> Any:
    """The `inspect_ai.Task` named by `spec`, created with `args`."""
    args = args or {}
    target, _, function = spec.partition("@")
    if target.endswith(".py"):
        path = Path(target).resolve()
        loaded = importlib.util.spec_from_file_location(f"falsepass_inspect_{path.stem}", path)
        assert loaded and loaded.loader, f"cannot import {path}"
        module = importlib.util.module_from_spec(loaded)
        loaded.loader.exec_module(module)
        tasks = dict(_tasks_in(module))
        if not function:
            if len(tasks) != 1:
                raise RuntimeError(f"{path} defines {len(tasks)} tasks ({sorted(tasks)}): name one as {target}@<task>")
            function = next(iter(tasks))
        return getattr(module, function)(**args)
    if ":" in spec:
        module_name, function = spec.split(":", 1)
        TASK_MODULE[spec] = module_name
        return getattr(importlib.import_module(module_name), function)(**args)
    package, _, name = spec.partition("/")
    try:  # inspect_evals/gsm8k -> inspect_evals.gsm8k:gsm8k
        made = getattr(importlib.import_module(f"{package}.{name}"), name)(**args)
        TASK_MODULE[spec] = f"{package}.{name}"
        return made
    except (ImportError, AttributeError):
        pass
    from inspect_ai._util.registry import ensure_entry_points, registry_lookup

    importlib.import_module(package)
    ensure_entry_points()
    found = registry_lookup("task", spec)
    if found is None:
        raise RuntimeError(f"no Inspect task named {spec!r}")
    TASK_MODULE[spec] = getattr(found, "__module__", "") or ""
    return found(**args)


def source_files(spec: str, scorers: list[Any]) -> dict[str, Path]:
    """What the static scan should read for an installed task, as {path under src/: file}:
    the folder of the task's module (its own package files), and the module of each scorer
    it uses together with the sibling modules that module imports (an Inspect built-in such
    as `match()` lives in `inspect_ai/scorer/_match.py` and calls `_common.py`)."""
    import inspect as pyinspect
    import sys

    def module_file(name: str) -> Path | None:
        module = sys.modules.get(name)
        file = getattr(module, "__file__", None)
        return Path(file) if file else None

    def under_src(name: str, file: Path) -> str:
        return "/".join(name.split(".")[:-1] + [file.name]) if file.name != "__init__.py" else name.replace(".", "/") + "/__init__.py"

    found: dict[str, Path] = {}
    task_module = TASK_MODULE.get(spec, "")
    file = module_file(task_module) if task_module else None
    if file is not None:
        package = task_module if file.name == "__init__.py" else task_module.rpartition(".")[0]
        if package and "." in package:  # the task's own folder (inspect_evals/gsm8k/), not the whole distribution
            for path in sorted(file.parent.glob("*.py")):
                found[package.replace(".", "/") + "/" + path.name] = path
        else:
            found[under_src(task_module, file)] = file
    for scorer in scorers:
        name = getattr(pyinspect.unwrap(scorer), "__module__", "") or ""
        file = module_file(name)
        if file is None or not name:
            continue
        found.setdefault(under_src(name, file), file)
        package = name.rpartition(".")[0]
        for imported in re.findall(r"^from \.(\w+) import", file.read_text(errors="replace"), re.M):
            sibling = file.parent / f"{imported}.py"
            if sibling.is_file():
                found.setdefault(package.replace(".", "/") + "/" + sibling.name, sibling)
    return found


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(getattr(part, "text", "") or "" for part in content or [])


def _target_text(target: Any) -> str:
    return str(target[0] if isinstance(target, list) and target else target or "")


class InspectScorer:
    """Score a completion with an Inspect task's solver chain and scorers."""

    def __init__(self, task: Any) -> None:
        from inspect_ai.model import GenerateConfig, ModelOutput, get_model
        from inspect_ai.model._model import init_active_model

        self.task = task
        self.scorers = list(task.scorer or [])
        if not self.scorers:
            raise RuntimeError(f"task {task.name} has no scorer")
        self.model_calls = 0

        def outputs():  # a solver or scorer that calls a model itself gets this, and is counted
            while True:
                self.model_calls += 1
                yield ModelOutput.from_content("mockllm/model", "")

        init_active_model(get_model("mockllm/model", custom_outputs=outputs()), GenerateConfig())

    @property
    def weights(self) -> dict[str, float]:
        return {self._name(s): 1.0 for s in self.scorers}

    @staticmethod
    def _name(scorer: Any) -> str:
        from inspect_ai._util.registry import is_registry_object, registry_unqualified_name

        return registry_unqualified_name(scorer) if is_registry_object(scorer) else getattr(scorer, "__name__", "scorer")

    def _state(self, example: Example) -> Any:
        from inspect_ai.model import ChatMessageUser
        from inspect_ai.scorer import Target
        from inspect_ai.solver import TaskState

        sample = example.raw
        messages = [ChatMessageUser(content=sample.input)] if isinstance(sample.input, str) else list(sample.input)
        return TaskState(
            model=SCRIPTED_MODEL, sample_id=sample.id if sample.id is not None else example.key, epoch=1,
            input=sample.input, messages=messages, target=Target(sample.target), choices=sample.choices,
            metadata=dict(sample.metadata or {}),
        )  # fmt: skip

    async def _solve(self, example: Example, replies: list[str]) -> tuple[Any, list[str]]:
        """Run the solver chain; each call to generate() takes the next scripted reply."""
        from inspect_ai.model import ModelOutput

        script = iter(replies)
        prompts: list[str] = []

        async def generate(state: Any, *args: Any, **kwargs: Any) -> Any:
            prompts.append(_text(state.messages[-1].content) if state.messages else "")
            reply = next(script, None)
            if reply is None:
                raise RuntimeError("the solver asked for more model turns than the completion has")
            state.output = ModelOutput.from_content(SCRIPTED_MODEL, reply)
            state.messages.append(state.output.message)
            return state

        state = self._state(example)
        state = await self.task.solver(state, generate)
        return state, prompts

    def rendered_prompt(self, example: Example) -> str:
        """The last message the model would see before answering."""
        import asyncio

        prompts: list[str] = []

        async def run() -> None:
            try:
                prompts.extend((await self._solve(example, [""]))[1])
            except Exception:  # an empty reply may upset a later solver; the prompt is already captured
                pass

        asyncio.run(run())
        return prompts[0] if prompts else _text(example.raw.input if isinstance(example.raw.input, str) else example.raw.input[-1].content)

    async def score(self, example: Example, completion: Completion) -> Score:
        from inspect_ai.scorer import Target, value_to_float

        # One reply per call to generate(). Tool calls and their results happen inside a single
        # generate() in Inspect, so only assistant messages that end a turn count as replies.
        replies = [completion] if isinstance(completion, str) else [
            str(m.get("content") or "") for m in completion if m.get("role") == "assistant" and not m.get("tool_calls")
        ] or [""]  # fmt: skip
        errors: list[str] = []
        calls_before = self.model_calls
        result: dict[str, Any] = {}

        async def run() -> None:
            state, _ = await self._solve(example, replies)
            to_float = value_to_float()
            for scorer in self.scorers:
                value = (await scorer(state, Target(example.raw.target))).value
                if isinstance(value, dict):  # several values from one scorer: report each, reward the mean
                    for key, part in value.items():
                        result[f"{self._name(scorer)}.{key}"] = to_float(part)
                else:
                    result[self._name(scorer)] = to_float(value)

        ok = await _guarded(run(), errors)
        if self.model_calls > calls_before:
            errors.append("the solver or a scorer called a model itself (a judge or self-critique): this score used an "
                          "empty stand-in reply and is not meaningful")  # fmt: skip
        if not ok or not result:
            return Score(None, result, errors)
        return Score(sum(result.values()) / len(result), result, errors)

    async def close(self) -> None:
        return None


def load(spec: str, args: dict[str, Any], max_rows: int) -> tuple[InspectScorer, list[Example]]:
    task = load_task(spec, args)
    if getattr(task, "sandbox", None):
        raise RuntimeError(f"task {task.name} runs in a sandbox ({task.sandbox}): not supported in automatic mode")
    pool = []
    for i, sample in enumerate(itertools.islice(iter(task.dataset), max_rows)):
        prompt = sample.input if isinstance(sample.input, str) else _text(sample.input[-1].content)
        pool.append(Example(f"sample-{sample.id if sample.id is not None else i}", prompt, _target_text(sample.target), sample))
    return InspectScorer(task), pool


def with_reference(example: Example, reference: str) -> Example:
    sample = example.raw.model_copy(update={"target": reference})
    return Example(example.key, example.prompt, reference, sample)


# --- `falsepass init` for Inspect tasks (runs outside the audit virtualenv: no inspect_ai import) ---


def task_functions(path: Path) -> list[str]:
    """Names of the functions in a Python file decorated with Inspect's `@task`."""
    import ast

    try:
        tree = ast.parse(path.read_text(errors="replace"))
    except SyntaxError:
        return []
    names = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            for deco in node.decorator_list:
                target = deco.func if isinstance(deco, ast.Call) else deco
                if (isinstance(target, ast.Name) and target.id == "task") or (isinstance(target, ast.Attribute) and target.attr == "task"):
                    names.append(node.name)
    return names


def uses_inspect(path: Path) -> bool:
    """A file that imports inspect_ai, or a project folder that depends on it (and not on verifiers)."""
    import re
    import tomllib

    if path.is_file():
        return bool(re.search(r"^\s*(from|import)\s+inspect_ai\b", path.read_text(errors="replace"), re.M))
    pyproject = path / "pyproject.toml"
    if not pyproject.is_file():
        return False
    deps = " ".join(tomllib.loads(pyproject.read_text()).get("project", {}).get("dependencies", []))
    return bool(re.search(r"\binspect[-_]ai\b", deps)) and not re.search(r"\bverifiers\b", deps)


def describe_file(path: Path, task: str | None = None) -> dict[str, Any]:
    from falsepass.hub import pypi_version

    path = path.resolve()
    tasks = task_functions(path)
    if not tasks:
        raise RuntimeError(f"no @task function in {path}")
    chosen = task or tasks[0]
    return {"id": f"local/{path.stem}", "framework": "inspect", "task": f"{path}@{chosen}", "package": "",
            "version": "", "inspect_ai": pypi_version("inspect-ai"), "declared": "", "others": [t for t in tasks if t != chosen]}  # fmt: skip


def describe_folder(path: Path, task: str | None = None) -> dict[str, Any]:
    """A local project that depends on inspect-ai: installed from the folder, task found by `@task`."""
    from falsepass.hub import describe_local

    info = describe_local(path)
    found = []
    for file in sorted(path.resolve().rglob("*.py")):
        parts = file.relative_to(path.resolve()).parts
        if any(p.startswith(".") or p in ("tests", "build", "dist") for p in parts):
            continue
        module = ".".join(p for p in (*parts[:-1], file.stem) if p != "src").removesuffix(".__init__")
        found += [f"{module}:{name}" for name in task_functions(file)]
    if not found:
        raise RuntimeError(f"no @task function under {path}")
    chosen = next((f for f in found if f.endswith(f":{task}")), None) if task else found[0]
    if chosen is None:
        raise RuntimeError(f"no task named {task!r}; found {found}")
    info.update(framework="inspect", task=chosen, inspect_ai="", verifiers="", others=[f for f in found if f != chosen])
    return info


def describe_registry(spec: str) -> dict[str, Any]:
    """A task from an installed package's registry, e.g. `inspect_evals/gsm8k`."""
    from falsepass.hub import pypi_version

    package = spec.split("/")[0].replace("_", "-")
    return {"id": f"inspect/{spec}", "framework": "inspect", "task": spec, "package": package,
            "version": pypi_version(package), "inspect_ai": pypi_version("inspect-ai"), "declared": "", "others": []}  # fmt: skip
