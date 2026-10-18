"""OpenEnv environments (https://github.com/huggingface/OpenEnv): score replies with the
environment's own `step()`.

An OpenEnv environment is a Python package with a FastAPI app (`server/app.py`, built with
`create_app(EnvironmentClass, ActionClass, ObservationClass)`) whose `step()` computes the
reward. falsepass drives the environment class directly, as a WebSocket session on the
server does (`env.reset(...)`, then `env.step(Action(...))`), in this process: no Docker, no
port. The HTTP `/reset` and `/step` routes of such a server build a fresh instance per
request, so a reset followed by a step cannot go through them.

Tasks: `audits/<env>/tasks.json` lists the keyword sets to call `reset()` with (most
environments pick a task through `task_id`, `task_name`, `difficulty` or a seed; each set may
also carry an `answer`), else the Task API when the environment implements it, else repeated
`reset()` calls, one task per distinct prompt. An environment that draws a random task per
reset is reset again until the recorded task comes up. The reference answer is usually not
exposed, as with HUD: without one only answer-free probes run. With `--answers` (a JSON
object by task key, or a list in task order), a `tasks.json` answer, or an answer-like
field in a Task API record, every probe runs, and the P1 control shows whether that is what
the grader compares against.

Actions: a plain reply goes into the action's free-text field; required fields of other
kinds (a decision enum, a flag, a list) are filled with a constant, and the O1 probe plays
every value of such a decision field in turn (a fixed strategy: its mean is the lazy-policy
reward). A reply that is a JSON object naming the action's fields is taken as the action
itself, which is how an adapter, or an answers file of JSON objects, fills several fields.
The O2 probe sets a self-reported field (`confidence`, `score`, ...) to its maximum and to
its minimum: a grader that trusts it pays differently.

The spec is the environment folder (with an openenv.yaml, or a server/app.py). openenv is
imported lazily, inside the throw-away virtualenv of an audit.
"""

from __future__ import annotations

import asyncio
import enum
import importlib
import inspect
import json
import os
import re
import sys
import types
import typing
from pathlib import Path
from typing import Any

from falsepass.model import Completion, Example, Expect, ProbeResult, Score
from falsepass.scorers import _guarded

ANSWER_KEYS = ("answer", "answers", "expected_answer", "expected", "target", "label", "solution", "gold", "ground_truth", "reference")
PROMPT_KEY = re.compile(r"prompt|question|task|instruction|problem|query|input|text|message|description|context|document|ticket"
                        r"|case|scenario|email|passage|incident|snippet|history|conversation|post|claim|queue|code|source", re.I)  # fmt: skip
NOISE_KEY = re.compile(r"feedback|error|status|reward|done|metadata|episode|step|id$|_at$|timestamp|session", re.I)
COUNTER_KEY = re.compile(r"step|count|turn|time|elapsed|index|remaining|budget|episode|attempt|length|tokens", re.I)
TEXT_FIELD_PREFERENCE = ("answer", "response", "reply", "message", "text", "output", "content", "solution", "code", "rationale",
                         "reason", "reasoning", "explanation", "justification", "summary", "note", "notes", "comment", "analysis",
                         "command", "action")  # fmt: skip
SELF_REPORT = re.compile(r"confidence|certainty|probab|score|quality|rating|likelihood", re.I)
FACTORY_NAME = re.compile(r"factory|create_|make_|build_|get_env", re.I)
SKIP_MODULES = re.compile(r"^(test|tests|inference|train|baseline|client|scripts?|examples?|eval|evaluate|run_|setup$|conftest|demo)", re.I)
DEFAULT_APPS = ("server.app:app", "app:app", "api:app", "main:app", "server.main:app")
DEFAULT_APP = DEFAULT_APPS[0]
SOURCE_VAR = "FALSEPASS_SOURCE"  # a patched copy of the folder (verify-fix, --source): read instead of env.toml's
TASKS_FILE = "tasks.json"
MAX_CHOICES = 12
MAX_RESET_WALK = 200  # distinct tasks collected by calling reset() repeatedly, when nothing lists them


def _reply(completion: Completion) -> str:
    if isinstance(completion, str):
        return completion
    turns = [str(m.get("content") or "") for m in completion if m.get("role") == "assistant" and not m.get("tool_calls")]
    return turns[-1] if turns else ""


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _base_classes() -> tuple[type, type]:
    """openenv's `Environment` and `Action` (the older `openenv-core` wheel spelled the package `openenv_core`)."""
    for package in ("openenv.core.env_server", "openenv_core.env_server"):
        try:
            interfaces = importlib.import_module(f"{package}.interfaces")
            types_module = importlib.import_module(f"{package}.types")
            return interfaces.Environment, types_module.Action
        except ImportError:
            continue
    raise RuntimeError("openenv is not installed: add `openenv` (or the version the environment pins) to env.toml's `openenv`")


async def _call(env: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    """`env.reset(...)` / `env.step(...)`: the async override when the environment has one
    (as openenv's server decides), else the sync method; awaited either way if needed."""
    base, _ = _base_classes()
    asynchronous = getattr(type(env), f"{name}_async", None)
    if asynchronous is not None and asynchronous is not getattr(base, f"{name}_async", None):
        return await asynchronous(env, *args, **kwargs)
    result = getattr(env, name)(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def _accepted(method: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """The keyword arguments `method` declares (all of them with a `**kwargs`), as the server filters them."""
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return kwargs
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return kwargs
    return {k: v for k, v in kwargs.items() if k in parameters}


def _normal(text: str) -> str:
    return " ".join(text.split())


# --- the action's shape ---------------------------------------------------------------------


def _unwrap(annotation: Any) -> list[Any]:
    """The alternatives of an Optional / Union annotation, the annotation itself otherwise."""
    origin = typing.get_origin(annotation)
    if origin is typing.Union or (hasattr(types, "UnionType") and origin is types.UnionType):
        return [a for a in typing.get_args(annotation) if a is not type(None)]
    return [annotation]


def _is_text(annotation: Any) -> bool:
    return any(a is str for a in _unwrap(annotation))


def _is_number(annotation: Any) -> bool:
    return any(a in (int, float) for a in _unwrap(annotation))


def _choices(annotation: Any) -> list[Any]:
    """The values a field can take when they are finite: a Literal, an Enum, a bool."""
    for option in _unwrap(annotation):
        if typing.get_origin(option) is typing.Literal:
            return list(typing.get_args(option))
        if inspect.isclass(option) and issubclass(option, enum.Enum):
            return [e.value for e in option]
        if option is bool:
            return [False, True]
    return []


def _container(annotation: Any) -> Any:
    for option in _unwrap(annotation):
        origin = typing.get_origin(option) or option
        if origin in (list, tuple, set):
            return []
        if origin is dict:
            return {}
    return None


def _bounds(field: Any) -> tuple[float, float]:
    """A numeric field's declared range (pydantic `ge`/`le`, `gt`/`lt`), else 0..1."""
    low, high = 0.0, 1.0
    for constraint in getattr(field, "metadata", []):
        for name in ("ge", "gt"):
            if getattr(constraint, name, None) is not None:
                low = float(getattr(constraint, name))
        for name in ("le", "lt"):
            if getattr(constraint, name, None) is not None:
                high = float(getattr(constraint, name))
    return low, high


def action_plan(action_cls: type) -> dict[str, Any]:
    """How a reply becomes an action: `field` (the free-text field a plain reply goes into),
    `fills` (constants for the other required fields), `choices` (decision fields and their
    values, for O1), `reports` (self-reported numeric fields and their range, for O2) and
    `blockers` (required fields nothing can be made up for)."""
    fields = {n: f for n, f in getattr(action_cls, "model_fields", {}).items() if n != "metadata"}
    texts = [n for n, f in fields.items() if _is_text(f.annotation)]
    by_name = [n for n in TEXT_FIELD_PREFERENCE if n in texts]
    required_texts = [n for n in texts if fields[n].is_required()]
    field = by_name[0] if by_name else required_texts[0] if required_texts else texts[0] if texts else None
    fills: dict[str, Any] = {}
    blockers: list[str] = []
    for name, info in fields.items():
        if name == field or not info.is_required():
            continue
        annotation = info.annotation
        if _is_text(annotation):
            fills[name] = ""
        elif _choices(annotation):
            fills[name] = _choices(annotation)[0]
        elif _is_number(annotation):
            fills[name] = type(_bounds(info)[0])(_bounds(info)[0]) if _unwrap(annotation)[0] is float else int(_bounds(info)[0])
        elif _container(annotation) is not None:
            fills[name] = _container(annotation)
        else:
            blockers.append(name)
    choices = {n: _choices(f.annotation)[:MAX_CHOICES] for n, f in fields.items() if _choices(f.annotation) and n != field}
    reports = {n: _bounds(f) for n, f in fields.items() if _is_number(f.annotation) and SELF_REPORT.search(n)}
    return {"field": field, "fills": fills, "choices": choices, "reports": reports, "blockers": blockers}


# --- the scorer ----------------------------------------------------------------------------


class OpenEnvScorer:
    """Score a reply with the environment's own `step()`, after a `reset()` to the task."""

    concurrent_safe = False  # one environment instance, used in turn: state between episodes is part of what probes look at

    def __init__(self, make_env: Any, action_cls: type, plan: dict[str, Any]) -> None:
        self.make_env, self.action_cls, self.plan = make_env, action_cls, plan
        self.env: Any = None
        self.lock = asyncio.Lock()
        self.retries = 3  # resets to find the recorded task again when reset() draws tasks at random; set from the pool size

    @property
    def weights(self) -> dict[str, float]:
        return {}

    def environment(self) -> Any:
        if self.env is None:
            self.env = self.make_env()
        return self.env

    def action(self, reply: str) -> Any:
        """The action for a reply: a JSON object naming the action's own fields completes
        the constant fills (how an adapter fills several fields); any other text goes into
        the free-text field."""
        fields = getattr(self.action_cls, "model_fields", {})
        data = _json_object(reply)
        if data is not None and data and set(data) <= set(fields):
            return self.action_cls(**{**self.plan["fills"], **data})
        if self.plan["field"] is None:
            raise RuntimeError(f"{self.action_cls.__name__} has no text field for a plain reply: render() must return a JSON object")
        return self.action_cls(**{**self.plan["fills"], self.plan["field"]: reply})

    async def reset(self, example: Example) -> Any:
        """reset() to the example's task; when the environment draws tasks at random, again
        until the recorded prompt comes back (it may never: that is reported, not guessed)."""
        env = self.environment()
        kwargs = _accepted(env.reset, example.raw["reset"])
        wanted = _normal(example.prompt)
        last = ""
        for _ in range(max(1, self.retries)):
            observation = await _call(env, "reset", **kwargs)
            last = _normal(observation_text(observation))
            if not wanted or last == wanted:
                return observation
        raise RuntimeError(
            f"reset({', '.join(f'{k}={v!r}' for k, v in kwargs.items())}) did not bring back the recorded task in {self.retries} tries "
            f"(it starts '{last[:60]}', the task starts '{wanted[:60]}'): the environment draws a task at random or changes its text "
            "between resets. List the tasks with their reset keywords in tasks.json next to env.toml"
        )

    async def score(self, example: Example, completion: Completion) -> Score:
        from pydantic import ValidationError

        errors: list[str] = []
        result: dict[str, Any] = {}

        async def run() -> None:
            async with self.lock:
                await self.reset(example)
                try:
                    action = self.action(_reply(completion))
                except ValidationError as rejected:  # the server would answer 422: the environment refused the action
                    result["rejected"] = str(rejected).splitlines()[0]
                    return
                result["observation"] = await _call(self.environment(), "step", action)

        if not await _guarded(run(), errors):
            self.env = None  # a raised reset() or step() may leave the instance unusable: the next call builds a new one
            return Score(None, {}, errors)
        if "rejected" in result:
            return Score(0.0, {"rejected_by_action_schema": 1.0}, [])
        observation = result["observation"]
        reward = getattr(observation, "reward", None)
        done = bool(getattr(observation, "done", False))
        metrics = {}  # numbers the environment reports next to the reward are usually its components; counters are not
        for key, value in (getattr(observation, "metadata", None) or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and not COUNTER_KEY.search(str(key)):
                metrics[str(key)] = float(value)
        info = {"done": done}
        if reward is None:
            return Score(None, metrics, ["step() returned no reward" + ("" if done else
                         " and the episode is not over: this environment pays later in the episode, so one reply cannot be graded")], info)  # fmt: skip
        if isinstance(reward, dict):  # a hand-written environment: {"value": ..., "reason": ...}
            reward = reward.get("value", reward.get("reward"))
            if not isinstance(reward, (int, float)):
                return Score(None, metrics, ["step() returned a reward object without a numeric value"], info)
        return Score(float(reward), metrics, [], info)

    async def close(self) -> None:
        if self.env is not None and hasattr(self.env, "close"):
            try:
                result = self.env.close()
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
        self.env = None


# --- the OpenEnv probes ----------------------------------------------------------------------


def _constant_action(plan: dict[str, Any], **overrides: Any) -> str:
    action = dict(plan["fills"])
    if plan["field"]:
        action[plan["field"]] = ""
    for name, values in plan["choices"].items():
        action.setdefault(name, values[0])
    action.update(overrides)
    return json.dumps(action, ensure_ascii=False)


async def o1_fixed_choice(adapter: Any, scorer: Any, ex: Example) -> list[ProbeResult]:
    """Every value of a decision field, with nothing else said: a fixed strategy. Its mean
    reward is what a policy earns without reading the task (the lazy-policy reward)."""
    from falsepass.probes import check

    plan = getattr(scorer, "plan", None)
    if not plan or not plan["choices"]:
        return []
    results = []
    for name, values in plan["choices"].items():
        for value in values:
            done = _constant_action(plan, **{name: value})
            results.append(await check("O1", "fixed_choice", f"{name}={value}", Expect.LAZY, done, adapter, scorer, ex, answer_free=True))
    return results


async def o2_self_report(adapter: Any, scorer: Any, ex: Example) -> list[ProbeResult]:
    """A self-reported number (confidence, quality, score) at its maximum and at its minimum,
    everything else equal: a grader that trusts the field pays the two differently."""
    from falsepass.probes import preview

    plan = getattr(scorer, "plan", None)
    if not plan or not plan["reports"]:
        return []
    results = []
    for name, (low, high) in plan["reports"].items():
        top, bottom = _constant_action(plan, **{name: high}), _constant_action(plan, **{name: low})
        scored = [(await scorer.score(ex, done)).reward for done in (top, bottom)]
        same = scored[0] == scored[1]
        detail = ("" if same else f"{name}={high} scores {scored[0]}, {name}={low} scores {scored[1]}: the reward moves with a "
                  "self-reported field")  # fmt: skip
        results.append(ProbeResult("O2", "self_report", name, ex.key, Expect.STABLE, scored[0], same, detail, preview(top)))
    return results


# --- finding the classes --------------------------------------------------------------------


def _import_object(spec: str) -> Any:
    module, _, name = spec.partition(":")
    return getattr(importlib.import_module(module), name)


def alias_package(name: str, folder: Path) -> str:
    """Make `folder` importable as the package `name` (a Space laid out as its own package:
    `server/app.py` imports `<name>.models`), without copying or symlinking anything."""
    name = re.sub(r"\W", "_", name)
    if name not in sys.modules:
        package = types.ModuleType(name)
        package.__path__ = [str(folder)]  # type: ignore[attr-defined]
        package.__file__ = str(folder / "__init__.py")
        sys.modules[name] = package
    return name


def import_app(folder: Path, app: str, name: str) -> tuple[Any, str, str]:
    """The app module: the declared path, then the usual ones, each also tried under a
    package alias named after the environment. Returns (module, app path, alias or "")."""
    tried: list[str] = []
    for candidate in dict.fromkeys([app, *DEFAULT_APPS]):
        if not candidate:
            continue
        module_name = candidate.partition(":")[0]
        try:
            return importlib.import_module(module_name), candidate, ""
        except ImportError as exc:
            tried.append(f"{module_name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - imported but failed to load (data missing, a server started): say so, try the next
            tried.append(f"{module_name}: {type(exc).__name__}: {exc}")
            continue
        if name:
            alias = alias_package(name, folder)
            try:
                return importlib.import_module(f"{alias}.{module_name}"), candidate, alias
            except ImportError as exc:
                tried.append(f"{alias}.{module_name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                tried.append(f"{alias}.{module_name}: {type(exc).__name__}: {exc}")
    raise RuntimeError("no app module could be imported (" + "; ".join(tried[:6]) + '). Name it in env.toml as app = "module:attr"')


def _module_names(folder: Path) -> list[str]:
    """Importable modules of the folder, nearest first, without scripts that act on import."""
    names = []
    for file in sorted(folder.rglob("*.py")):
        parts = file.relative_to(folder).parts
        if len(parts) > 3 or any(p.startswith(".") or p in ("tests", "test", "__pycache__", "build", "dist") for p in parts):
            continue
        stem = parts[:-1] if file.name == "__init__.py" else (*parts[:-1], file.stem)
        if not stem or any(SKIP_MODULES.search(p) for p in stem):
            continue
        names.append(".".join(stem))
    return sorted(names, key=len)


def _subclasses(module: Any, base: type) -> list[type]:
    return [v for v in vars(module).values() if inspect.isclass(v) and issubclass(v, base) and v is not base and not inspect.isabstract(v)]


def _action_classes(module: Any, base: type) -> list[type]:
    found = _subclasses(module, base)
    if found:
        return found
    # create_app also serves plain pydantic models: take the ones named like an action
    return [v for v in vars(module).values() if inspect.isclass(v) and hasattr(v, "model_fields") and "Action" in v.__name__
            and v is not base and v.__module__ not in ("openenv.core.env_server.types", "openenv_core.env_server.types")]  # fmt: skip


def _factory(module: Any) -> Any:
    """A module-level function that builds the environment with no arguments (`create_textarena_environment`, `_env_factory`)."""
    for name, obj in vars(module).items():
        if not inspect.isfunction(obj) or name == "create_app" or not FACTORY_NAME.search(name):
            continue
        try:
            parameters = inspect.signature(obj).parameters.values()
        except (TypeError, ValueError):
            continue
        if all(p.default is not inspect.Parameter.empty or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in parameters):
            return obj
    return None


def find_classes(folder: Path, app: str, name: str, env_class: str = "", action_class: str = "",
                 factory: str = "") -> tuple[Any, type, list[str], str, str]:
    """What builds the environment (a factory function, else the class) and the action class:
    named in env.toml, else found in the app module (`create_app(Env, Action, Observation)`
    imports both), else in the folder's modules. Returns (make_env, action class, notes, app
    path, alias)."""
    environment_base, action_base = _base_classes()
    notes: list[str] = []
    module, app, alias = import_app(folder, app, name)
    make_env: Any = _import_object(factory) if factory else _import_object(env_class) if env_class else None
    actions = [_import_object(action_class)] if action_class else []
    if make_env is None:
        make_env = _factory(module)
        if make_env is not None:
            notes.append(f"the environment is built by {make_env.__name__}() from the app module")
    if make_env is None:
        environments = _subclasses(module, environment_base)
        if len(environments) > 1:
            notes.append(f"several Environment classes in the app module ({', '.join(c.__name__ for c in environments)}): "
                         f"{environments[0].__name__} used; env_class in env.toml picks another")  # fmt: skip
        make_env = environments[0] if environments else None
    actions = actions or _action_classes(module, action_base)
    if make_env is None or not actions:
        for module_name in _module_names(folder):
            try:
                other = importlib.import_module(f"{alias}.{module_name}" if alias else module_name)
            except Exception:  # noqa: BLE001 - unrelated modules of the folder may not import
                continue
            if make_env is None and _subclasses(other, environment_base):
                make_env = _subclasses(other, environment_base)[0]
            actions = actions or _action_classes(other, action_base)
            if make_env is not None and actions:
                break
    if make_env is None:
        raise RuntimeError(f"no subclass of openenv's Environment found through {app} or the modules of {folder}: "
                           'name it in env.toml as env_class = "module:Class" (or factory = "module:function")')  # fmt: skip
    if not actions:
        raise RuntimeError(f"no action class found through {app} or the modules of {folder}: "
                           'name it in env.toml as action_class = "module:Class"')  # fmt: skip
    if len(actions) > 1:
        with_text = [a for a in actions if action_plan(a)["field"]]
        actions = with_text or actions
        notes.append(f"several action classes ({', '.join(c.__name__ for c in actions)}): {actions[0].__name__} used; "
                     "action_class in env.toml picks another")  # fmt: skip
    if {"tool_name", "arguments"} <= set(getattr(actions[0], "model_fields", {})):
        raise RuntimeError(f"{actions[0].__name__} is an MCP tool call: this environment is played through tools, not a reply. "
                           "Write policies (falsepass init --force --policies)")  # fmt: skip
    return make_env, actions[0], notes, app, alias


# --- tasks and prompts ----------------------------------------------------------------------


def observation_text(observation: Any) -> str:
    """What the model would read: the observation's prompt-like text fields, else every text
    field that is not bookkeeping."""
    data = observation.model_dump() if hasattr(observation, "model_dump") else observation if isinstance(observation, dict) else {}
    prompt_like: list[str] = []
    other: list[str] = []

    def walk(value: Any, key: str) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, str(k))
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v, key)
        elif isinstance(value, str) and value.strip():
            if NOISE_KEY.search(key):
                return
            (prompt_like if PROMPT_KEY.search(key) else other).append(value)

    walk(data, "")
    return "\n\n".join(prompt_like or other)


def _split_name(split: Any) -> str:
    return split if isinstance(split, str) else str(split.get("name", split)) if isinstance(split, dict) else str(getattr(split, "name", split))


async def task_records(env: Any, max_rows: int) -> list[tuple[str, int, Any]]:
    """(split, index, record) through the Task API, evaluation splits first; [] without one."""
    if not all(callable(getattr(env, m, None)) for m in ("list_splits", "num_tasks", "get_task")):
        return []
    try:
        splits = [_split_name(s) for s in await _call(env, "list_splits")]
    except NotImplementedError:
        return []
    order = sorted(splits, key=lambda s: (s not in ("test", "eval", "evaluation", "validation", "dev"), splits.index(s)))
    rows: list[tuple[str, int, Any]] = []
    for split in order:
        try:
            count = int(await _call(env, "num_tasks", split))
        except NotImplementedError:
            continue
        for index in range(min(count, max_rows - len(rows))):
            rows.append((split, index, await _call(env, "get_task", split, index)))
        if len(rows) >= max_rows:
            break
    return rows


def _record_value(record: Any, key: str) -> Any:
    if isinstance(record, dict):
        return record.get(key)
    return getattr(record, key, None)


def reference_for(record: Any, references: Any, key: str, position: int) -> str:
    """The reference from the answers file (by task key, by the record's id, or by position),
    else an answer-like field of the task record; "" when there is none."""
    if isinstance(references, dict):
        for name in (key, str(_record_value(record, "id") or "")):
            if name and name in references:
                return _as_text(references[name])
    elif isinstance(references, list) and position < len(references):
        return _as_text(references[position])
    for name in ANSWER_KEYS:
        value = _record_value(record, name)
        if value not in (None, "", [], {}):
            return _as_text(value)
    return ""


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def read_tasks(path: Path | None) -> list[tuple[str, dict[str, Any]]]:
    """tasks.json: a list of reset keyword sets (`[{"task_id": 1, "answer": "approve"}, ...]`),
    or an object from task key to such a set. `key` and `answer` are kept apart from the
    keywords passed to reset()."""
    if path is None or not path.is_file():
        return []
    data = json.loads(path.read_text())
    items = list(data.items()) if isinstance(data, dict) else [(None, item) for item in data]
    tasks = []
    for position, (name, item) in enumerate(items):
        item = dict(item) if isinstance(item, dict) else {"task_id": item}
        key = str(item.pop("key", None) or name or f"task-{position}")
        tasks.append((key, item))
    return tasks


def reset_hints(env: Any) -> list[str]:
    """reset() parameters beyond the standard ones: how this environment picks a task."""
    try:
        parameters = inspect.signature(env.reset).parameters
    except (TypeError, ValueError):
        return []
    return [n for n, p in parameters.items() if n not in ("self", "seed", "episode_id", "kwargs") and p.kind is not p.VAR_KEYWORD]


async def _examples(scorer: OpenEnvScorer, max_rows: int, references: Any, tasks: list[tuple[str, dict[str, Any]]],
                    notes: list[str]) -> list[Example]:
    env = scorer.environment()
    pool: list[Example] = []
    if tasks:
        for position, (key, item) in enumerate(tasks[:max_rows]):
            answer = {k: item.pop(k) for k in list(item) if k in ANSWER_KEYS}
            observation = await _call(env, "reset", **_accepted(env.reset, item))
            reference = reference_for(answer, references, key, position) if answer or references else ""
            pool.append(Example(key, observation_text(observation), reference, {"reset": item, "task": item}))
        return pool
    records = await task_records(env, max_rows)
    if records:
        for position, (split, index, record) in enumerate(records):
            key = f"{split}-{index}"
            kwargs = {"split": split, "index": index}
            observation = await _call(env, "reset", **_accepted(env.reset, kwargs))
            pool.append(Example(key, observation_text(observation), reference_for(record, references, key, position), {"reset": kwargs, "task": record}))
        return pool
    hints = reset_hints(env)
    if hints:
        notes.append(f"reset() also takes {', '.join(hints)}: list the tasks to audit in {TASKS_FILE} next to env.toml "
                     "(a JSON list of reset keyword sets, each may carry an `answer`); without it, repeated reset() calls "
                     "decided which tasks were seen")  # fmt: skip
    seen: set[str] = set()
    repeats = 0
    max_rows = min(max_rows, MAX_RESET_WALK)
    for position in range(max_rows * 4):
        kwargs = _accepted(env.reset, {"seed": position})
        observation = await _call(env, "reset", **kwargs)
        prompt = observation_text(observation)
        if _normal(prompt) in seen:
            repeats += 1
            if repeats >= max(5, len(seen)):  # a cycle has come round, or random draws stopped finding new tasks
                break
            continue
        repeats = 0
        seen.add(_normal(prompt))
        key = f"reset-{position}"
        pool.append(Example(key, prompt, reference_for({}, references, key, len(pool)), {"reset": kwargs, "task": None}))
        if len(pool) >= max_rows:
            break
    return pool


def load(spec: str, max_rows: int, answers: str | None = None, app: str = "", env_class: str = "", action_class: str = "",
         factory: str = "", tasks_file: str | Path | None = None) -> tuple[OpenEnvScorer, list[Example], dict[str, Any]]:
    """The scorer and the tasks of the environment folder `spec` (or of `$FALSEPASS_SOURCE`, a
    patched copy). Works in the folder: data files are read relative to it."""
    folder = Path(os.environ.get(SOURCE_VAR) or spec).resolve()
    os.chdir(folder)
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    found_manifest = manifest(folder)
    app = app or found_manifest.get("app") or DEFAULT_APP
    make_env, action_cls, notes, app, alias = find_classes(folder, app, found_manifest.get("name") or folder.name, env_class, action_class, factory)
    plan = action_plan(action_cls)
    if plan["blockers"]:
        raise RuntimeError(
            f"{action_cls.__name__} has required fields nothing can be made up for ({', '.join(plan['blockers'])}): write an adapter "
            "whose render() returns a JSON object with the action's fields (falsepass init --force --manual)"
        )
    scorer = OpenEnvScorer(make_env, action_cls, plan)
    references: Any = json.loads(Path(answers).read_text()) if answers else {}
    tasks = read_tasks(Path(tasks_file) if tasks_file else None)
    pool = asyncio.run(_examples(scorer, max_rows, references, tasks, notes))
    if not pool:
        raise RuntimeError("reset() produced no task text to work with")
    # Tasks that reset() hands out in a cycle come back after one full cycle; random draws need
    # several times the pool size to be likely. Each try is one reset(), cheap in most environments.
    scorer.retries = min(2000, max(150, 8 * len(pool)))
    if plan["fills"]:
        notes.append("required action fields filled with constants for a plain reply: " + ", ".join(f"{k}={v!r}" for k, v in plan["fills"].items()))
    environment = scorer.environment()
    found = {
        "notes": notes, "app": app, "alias": alias, "field": plan["field"], "fills": plan["fills"],
        "env_class": f"{type(environment).__module__}:{type(environment).__qualname__}",
        "factory": f"{make_env.__module__}:{make_env.__name__}" if inspect.isfunction(make_env) else "",
        "action_class": f"{action_cls.__module__}:{action_cls.__qualname__}",
        "task_api": any(e.raw.get("task") is not None for e in pool) and not tasks, "tasks_file": bool(tasks),
        "resets": {e.key: e.raw["reset"] for e in pool},
    }  # fmt: skip
    return scorer, pool, found


# --- `falsepass init` (runs outside the audit virtualenv: no openenv import) -----------------


def manifest(folder: Path) -> dict[str, str]:
    """`name`, `version` and the app path from openenv.yaml, read without a YAML parser:
    community manifests spell the app as `app:`, `entry_points: server:` or `main:`."""
    path = folder / "openenv.yaml"
    if not path.is_file():
        return {}
    text = path.read_text(errors="replace")
    found: dict[str, str] = {}
    for key in ("name", "version"):
        match = re.search(rf"^\s*{key}:\s*[\"']?([^\"'\n#]+)", text, re.M)
        if match:
            found[key] = match.group(1).strip()
    app = (re.search(r"^app:\s*[\"']?([\w.]+:\w+)", text, re.M)
           or re.search(r"^\s+server:\s*[\"']?([\w.]+:\w+)", text, re.M)
           or re.search(r"^main:\s*[\"']?([\w.]+:\w+)", text, re.M))  # fmt: skip
    if app:
        found["app"] = app.group(1)
    return found


def uses_openenv(path: Path) -> bool:
    """An OpenEnv environment folder: an openenv.yaml, or a server/app.py that imports openenv."""
    folder = path if path.is_dir() else path.parent
    if (folder / "openenv.yaml").is_file():
        return True
    app = folder / "server" / "app.py"
    return app.is_file() and bool(re.search(r"^\s*(from|import)\s+openenv", app.read_text(errors="replace"), re.M))


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):  # a requirements.txt saved by a Windows editor
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def requirements(folder: Path) -> list[str]:
    """The folder's own dependencies: requirements.txt (root, else server/), else pyproject.toml."""
    for candidate in (folder / "requirements.txt", folder / "server" / "requirements.txt"):
        if candidate.is_file():
            lines = [line.split("#")[0].strip() for line in _read_text(candidate).splitlines()]
            return [line for line in lines if line and not line.startswith("-") and not line.startswith(".")]
    pyproject = folder / "pyproject.toml"
    if pyproject.is_file():
        import tomllib

        return [d.strip() for d in tomllib.loads(pyproject.read_text()).get("project", {}).get("dependencies", [])]
    return []


def describe(path: Path, answers: str | None = None) -> dict[str, Any]:
    """What `falsepass init` writes for an OpenEnv folder. The openenv release installed is
    the newest on PyPI (every Space surveyed, back to `openenv-core>=0.2.1` pins, imports and
    serves under it) unless the folder pins `openenv==X` itself."""
    from falsepass.hub import python_for, pypi_version

    folder = path.resolve()
    found = manifest(folder)
    declared = requirements(folder)
    own = [d for d in declared if re.match(r"openenv(-core)?\s*(\[|[<>=!~;@]|$)", d)]
    exact = next((re.search(r"==\s*([\w.]+)", d) for d in own if d.startswith("openenv") and not d.startswith("openenv-core") and "==" in d), None)
    try:
        latest = pypi_version("openenv")
    except Exception:  # noqa: BLE001 - offline
        latest = ""
    openenv_pin = f"openenv=={exact.group(1)}" if exact else f"openenv=={latest}" if latest else "openenv"
    extra = [d for d in declared if d not in own]
    requires_python = ""
    if (folder / "pyproject.toml").is_file():
        import tomllib

        requires_python = tomllib.loads((folder / "pyproject.toml").read_text()).get("project", {}).get("requires-python", "")
    name = (found.get("name") or folder.name).replace("_", "-").lower()
    info: dict[str, Any] = {
        "id": f"local/{name}", "framework": "openenv", "task": str(folder), "openenv": openenv_pin,
        "app": found.get("app", DEFAULT_APP), "package": "", "version": found.get("version", ""),
        "declared": ", ".join(own) or "openenv (not declared)", "python": python_for(requires_python),
        "extra": extra, "source": str(folder), "others": [],
    }  # fmt: skip
    if answers:
        info["answers"] = str(Path(answers).resolve())
    return info
