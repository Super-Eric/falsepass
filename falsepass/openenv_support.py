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

ANSWER_KEYS = ("answer", "answers", "correct_answer", "expected_answer", "expected", "target", "label", "solution", "gold",
               "gold_answer", "ground_truth", "reference", "truth")  # fmt: skip
PROMPT_KEY = re.compile(r"prompt|question|task|instruction|problem|query|input|text|message|description|context|document|ticket"
                        r"|case|scenario|email|passage|incident|snippet|history|conversation|post|claim|queue|code|source", re.I)  # fmt: skip
NOISE_KEY = re.compile(r"feedback|error|status|reward|done|metadata|episode|step|id$|_at$|timestamp|session", re.I)
COUNTER_KEY = re.compile(r"step|count|turn|time|elapsed|index|remaining|budget|episode|attempt|length|tokens", re.I)
TEXT_FIELD_PREFERENCE = ("answer", "response", "reply", "message", "text", "output", "content", "solution", "code", "rationale",
                         "reason", "reasoning", "explanation", "justification", "summary", "note", "notes", "comment", "analysis",
                         "command", "action")  # fmt: skip
SELF_REPORT = re.compile(r"confidence|certainty|probab|score|quality|rating|likelihood", re.I)
DECISION_NAME = re.compile(r"(^|_)(type|kind|label|category|decision|intent|choice|option|verdict|route|routing|severity|priority)$", re.I)
OPENENV_DEPENDENCIES = {"fastapi", "pydantic", "uvicorn", "starlette", "requests", "websockets", "httpx", "pyyaml", "typer", "rich",
                        "huggingface-hub", "openai", "gradio", "fastmcp", "jsonschema", "packaging", "typing-extensions"}  # fmt: skip
MAX_HARVEST = 200  # tasks stepped once at load to read a reference the environment reveals only afterwards
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


def served_step_note(env_cls: type) -> str:
    """A class that overrides `step()` below the class its `step_async()` comes from (an
    `MCPEnvironment` subclass that puts its reward in `step()` alone): openenv's server calls
    `step_async()` whenever it differs from the base class's, so that `step()` never runs when
    served, and falsepass drives it the same way. "" otherwise."""
    base, _ = _base_classes()
    mro = getattr(env_cls, "__mro__", ())

    def owner(name: str) -> type | None:
        return next((c for c in mro if name in vars(c)), None)

    stepper, async_stepper = owner("step"), owner("step_async")
    if stepper is None or async_stepper is None or async_stepper is base or stepper is async_stepper:
        return ""
    if mro.index(stepper) < mro.index(async_stepper):
        return (f"{env_cls.__name__} overrides step() but inherits step_async() from {async_stepper.__name__}: openenv's "
                "server calls step_async(), so this step() never runs when the environment is served (falsepass drives it "
                "the same way). Rewards or episode ends computed only in step() are not paid in training")
    return ""


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
    # `action_type: str`, `label: str`: a decision spelled as free text; a reply is not what goes there
    decision_text = [n for n in texts if DECISION_NAME.search(n)]
    free = [n for n in texts if n not in decision_text] or texts
    by_name = [n for word in TEXT_FIELD_PREFERENCE for n in free if word in n.lower()]  # `cause_explanation` matches `explanation`
    required_texts = [n for n in free if fields[n].is_required()]
    field = by_name[0] if by_name else required_texts[0] if required_texts else free[0] if free else None
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
    return {"field": field, "fills": fills, "choices": choices, "reports": reports, "blockers": blockers,
            "decision_text": [n for n in decision_text if n != field]}  # fmt: skip


def describe_plan(action_cls: type, plan: dict[str, Any]) -> str:
    """One line for the notes: where a plain reply goes and what the other fields get."""
    fields = getattr(action_cls, "model_fields", {})
    parts = [f"a plain reply goes into `{plan['field']}`" if plan["field"] else "no free-text field: replies must be JSON actions"]
    if plan["fills"]:
        parts.append("required fields filled with constants: " + ", ".join(f"{k}={v!r}" for k, v in plan["fills"].items()))
    defaults = {n: f.default for n, f in fields.items() if n != "metadata" and n != plan["field"] and n not in plan["fills"]
                and not f.is_required()}  # fmt: skip
    if defaults:
        parts.append("other fields keep their defaults: " + ", ".join(f"{k}={getattr(v, 'value', v)!r}" for k, v in list(defaults.items())[:8]))
    if plan["decision_text"]:
        parts.append(f"{', '.join(plan['decision_text'])} look like decisions spelled as text and are left at their defaults "
                     "(a JSON action in tasks.json or --answers sets them)")  # fmt: skip
    return "; ".join(parts)


# --- the scorer ----------------------------------------------------------------------------


class OpenEnvScorer:
    """Score a reply with the environment's own `step()`, after a `reset()` to the task."""

    concurrent_safe = False  # one environment instance, used in turn: state between episodes is part of what probes look at

    def __init__(self, make_env: Any, action_cls: type, plan: dict[str, Any]) -> None:
        self.make_env, self.action_cls, self.plan = make_env, action_cls, plan
        self.env: Any = None
        self.lock = asyncio.Lock()
        self.retries = 3  # resets to find the recorded task again when reset() draws tasks at random; set from the pool size
        self.steps = {"replies": 0, "not_done": 0}  # one reply per score: an episode still open after it takes several steps

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
        self.steps["replies"] += 1
        self.steps["not_done"] += not done
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


# --- whole episodes, played by a policy ------------------------------------------------------

TOOL_FIELDS = {"tool_name", "arguments"}
EPISODE_TURN_CAP = 200  # policy turns before an episode is declared unbounded; the environment's own step limit comes first


def is_tool_action(action_cls: type) -> bool:
    """An MCP tool call (`CallToolAction` or a subclass): the environment is played through tools."""
    return TOOL_FIELDS <= set(getattr(action_cls, "model_fields", {}))


def tool_fills(action_cls: type, purpose: str) -> dict[str, Any]:
    """Values for a tool action's other required fields (`action_type: Literal["ListToolsAction",
    "ToolCallAction"]` in some Spaces): a choice that names the purpose ("call" or "list") when
    there is one, else the plan's constant."""
    fills = dict(action_plan(action_cls)["fills"])
    for name, info in getattr(action_cls, "model_fields", {}).items():
        if name in TOOL_FIELDS or name == "metadata" or not info.is_required():
            continue
        choices = _choices(info.annotation)
        named = [c for c in choices if isinstance(c, str) and purpose in c.lower()]
        if named:
            fills[name] = named[0]
    return {k: v for k, v in fills.items() if k not in TOOL_FIELDS}


def lists_through_action(action_cls: type) -> bool:
    """A tool action class with a choice that names listing (its own `ListToolsAction` value)."""
    return any(isinstance(c, str) and "list" in c.lower() for info in getattr(action_cls, "model_fields", {}).values()
               for c in _choices(info.annotation))


def _mcp_types() -> Any:
    for package in ("openenv.core.env_server.mcp_types", "openenv_core.env_server.mcp_types"):
        try:
            return importlib.import_module(package)
        except ImportError:
            continue
    return None


def _reward_value(reward: Any) -> float | None:
    if isinstance(reward, dict):  # a hand-written environment: {"value": ..., "reason": ...}
        reward = reward.get("value", reward.get("reward"))
    return float(reward) if isinstance(reward, (int, float)) and not isinstance(reward, bool) else None


ID_KEY = re.compile(r"(^|_)id$|episode|timestamp|_at$|session", re.I)


def episode_text(observation: Any) -> str:
    """An observation in the middle of an episode, as the model would read it: every text
    field (feedback included) but identifiers; with none, the other fields as JSON."""
    data = observation.model_dump() if hasattr(observation, "model_dump") else observation if isinstance(observation, dict) else {}
    texts: list[str] = []

    def walk(value: Any, key: str) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, str(k))
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v, key)
        elif isinstance(value, str) and value.strip() and not ID_KEY.search(key):
            texts.append(value)

    walk(data, "")
    if texts:
        return "\n\n".join(texts)
    rest = {k: v for k, v in data.items() if k not in ("done", "reward") and v not in (None, "", [], {})} if isinstance(data, dict) else {}
    return json.dumps(rest, ensure_ascii=False, default=str) if rest else ""


def tool_failed(observation: Any) -> bool:
    """openenv's `error`, or a Space's own `success=False` / `error_message`."""
    return getattr(observation, "error", None) is not None or getattr(observation, "success", True) is False \
        or bool(getattr(observation, "error_message", None))


def tool_result_text(observation: Any) -> str:
    """What a tool call returned, as the model would read it: the error, the result's text
    parts, else its data, else the observation's own text."""
    error = getattr(observation, "error", None)
    if error is not None:
        kind = getattr(getattr(error, "error_type", None), "value", getattr(error, "error_type", "error"))
        return f"error ({kind}): {getattr(error, 'message', error)}"
    if getattr(observation, "error_message", None):
        return f"error: {observation.error_message}"
    result = getattr(observation, "result", None)
    if result is None:
        result = getattr(observation, "tool_result", None)  # a Space's own tool observation
    if result is not None:
        content = getattr(result, "content", None) if not isinstance(result, dict) else result.get("content")
        if isinstance(content, list):
            parts = [p.get("text", "") if isinstance(p, dict) else str(getattr(p, "text", "") or "") for p in content]
            if any(parts):
                return "".join(parts)
        data = getattr(result, "data", None) if not isinstance(result, dict) else result.get("data", result)
        value = data if data is not None else result
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return episode_text(observation)


class OpenEnvEpisodeScorer(OpenEnvScorer):
    """Play a whole episode with a policy: `reset()` to the task, then one `step()` per
    action until the environment says `done`, the policy stops, or the turn cap is hit.

    The policy sees the conversation as plain messages: the task as the first user message,
    a text environment's next observation as a user message, a tool's result as a `tool`
    message. A tool call becomes `CallToolAction(tool_name, arguments)` (the action class the
    server was given, when it is a tool call); text becomes the action as in `OpenEnvScorer`
    (the free-text field, or a JSON object naming the action's fields). In an environment
    played only through tools, a text reply ends the policy's turn, as it ends a model's.

    The reward is the episode's return (`reward = "sum"`, every step's reward added) or the
    last reward the environment gave (`"last"`); both are kept in the episode's info."""

    multi_turn = True

    def __init__(self, make_env: Any, action_cls: type, plan: dict[str, Any], reward: str = "sum") -> None:
        super().__init__(make_env, action_cls, plan)
        if reward not in ("sum", "last"):
            raise ValueError(f'reward must be "sum" or "last", not {reward!r}')
        self.reward_mode = reward
        self._tools: list[dict[str, Any]] | None = None
        self.counts = {"tool_calls": 0, "failed": 0, "first_failure": ""}  # over the whole run: every call failing is a set-up problem

    def tool_action_class(self) -> type | None:
        if is_tool_action(self.action_cls):
            return self.action_cls
        mcp = _mcp_types()
        environment = sys.modules.get("openenv.core.env_server.mcp_environment")
        if mcp is not None and environment is not None and isinstance(self.environment(), environment.MCPEnvironment):
            return mcp.CallToolAction  # an MCP environment served with its own action class: tools as well
        return None

    async def tools(self) -> list[dict[str, Any]]:
        """The tools on offer, listed once, after a reset: through the MCP server when the
        environment has one (as a client's `list_tools()` does over the MCP route: not a step,
        so no step counter moves), else with the action class's own listing value, or openenv's
        `ListToolsAction`, as a step."""
        if self._tools is not None:
            return self._tools
        found: list[Any] = []
        env = self.environment()
        handler = getattr(env, "_async_handle_list_tools", None)
        if handler is not None:
            found = list(getattr(await handler(), "tools", None) or [])
        elif lists_through_action(self.action_cls):
            listed = await _call(env, "step", self.action_cls(**tool_fills(self.action_cls, "list")))
            found = list(getattr(listed, "tools", None) or getattr(listed, "tools_list", None) or [])
        elif self.tool_action_class() is not None and _mcp_types() is not None:
            found = list(getattr(await _call(env, "step", _mcp_types().ListToolsAction()), "tools", None) or [])
        def field(tool: Any, *names: str) -> Any:
            for name in names:
                value = tool.get(name) if isinstance(tool, dict) else getattr(tool, name, None)
                if value:
                    return value
            return None

        self._tools = [{"name": field(t, "name") or "", "description": field(t, "description") or "",
                        "parameters": field(t, "input_schema", "inputSchema", "parameters") or {}} for t in found]  # fmt: skip
        return self._tools

    def _failed(self, text: str) -> None:
        self.counts["failed"] += 1
        self.counts["first_failure"] = self.counts["first_failure"] or (text.splitlines() or [""])[0][:300]

    async def score(self, example: Example, completion: Completion) -> Score:
        from pydantic import ValidationError

        from falsepass.v1_episodes import Player

        player = Player(completion, EPISODE_TURN_CAP)
        errors: list[str] = []
        rewards: list[float | None] = []
        messages: list[dict[str, Any]] = []
        state: dict[str, Any] = {"stop": "", "last": None}
        tool_cls = self.tool_action_class()
        tools_only = tool_cls is not None and is_tool_action(self.action_cls)

        def stepped(observation: Any) -> bool:
            rewards.append(_reward_value(getattr(observation, "reward", None)))
            state["last"] = observation
            return bool(getattr(observation, "done", False))

        async def run() -> None:
            async with self.lock:
                observation = await self.reset(example)
                tools = await self.tools() if tool_cls is not None else []
                messages.append({"role": "user", "content": observation_text(observation)})
                env = self.environment()
                while not state["stop"]:
                    reply = player.next(messages, tools)  # raises at the cap: "rollout did not stop after N model turns"
                    messages.append({"role": "assistant", **reply})
                    if reply.get("tool_calls"):
                        if tool_cls is None:
                            state["stop"] = "tool call in an environment without tools"
                            errors.append(f"the policy called a tool, but {self.action_cls.__name__} takes no tool calls")
                            break
                        for call in reply["tool_calls"]:
                            self.counts["tool_calls"] += 1
                            try:
                                action = tool_cls(**tool_fills(tool_cls, "call"), tool_name=call["name"], arguments=call.get("arguments") or {})
                            except ValidationError as rejected:
                                text = f"rejected: {str(rejected).splitlines()[0]}"
                                self._failed(text)
                                messages.append({"role": "tool", "name": call["name"], "content": text})
                                continue
                            observation = await _call(env, "step", action)
                            text = tool_result_text(observation)
                            if tool_failed(observation):
                                self._failed(text)
                            messages.append({"role": "tool", "name": call["name"], "content": text})
                            if stepped(observation):
                                state["stop"] = "done"
                                break
                        continue
                    if tools_only:  # a text reply ends the policy's turn; the episode ends with it
                        state["stop"] = "policy ended its turn without a tool call"
                        break
                    try:
                        action = self.action(reply.get("content") or "")
                    except ValidationError as rejected:  # the server would answer 422
                        state["stop"] = "action rejected by the action schema"
                        errors.append(f"rejected: {str(rejected).splitlines()[0]}")
                        break
                    observation = await _call(env, "step", action)
                    if stepped(observation):
                        state["stop"] = "done"
                        break
                    messages.append({"role": "user", "content": episode_text(observation)})

        ok = await _guarded(run(), errors)
        transcript = [{"role": m["role"], "content": " ".join(filter(None, [
            str(m.get("content") or ""), *(f"[tool call {c['name']}({json.dumps(c.get('arguments') or {}, ensure_ascii=False)})]"
                                            for c in m.get("tool_calls") or [])]))} for m in messages]  # fmt: skip
        if player.error:
            errors.append(player.error)
        if not state["stop"] and not ok and any("did not stop" in e for e in errors):
            state["stop"] = f"turn cap ({EPISODE_TURN_CAP})"
        info = {"turns": player.calls, "stop": state["stop"] or "error", "transcript": transcript, "steps": len(rewards),
                "script": [{"role": "assistant", **r} for r in player.replies], "done": state["stop"] == "done",
                "tools": [t["name"] for t in self._tools or []]}  # fmt: skip
        if not ok:
            self.env = None  # a raised reset() or step() may leave the instance unusable: the next call builds a new one
            return Score(None, {}, errors, info)
        paid = [r for r in rewards if r is not None]
        total, last = float(sum(paid)), (paid[-1] if paid else 0.0)
        info.update({"episode_return": total, "last_reward": last})
        metrics: dict[str, float] = {}  # numbers the environment reports next to its last reward: usually its components
        for key, value in (getattr(state["last"], "metadata", None) or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and not COUNTER_KEY.search(str(key)):
                metrics[str(key)] = float(value)
        return Score(total if self.reward_mode == "sum" else last, metrics, errors, info)


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


def alias_names(folder: Path, app: str, name: str) -> list[str]:
    """Package names the folder may be imported under: the ones its app module's own imports use
    (`from textarena_env.models import ...`), then the manifest's name; never the name of an
    installed module (aliasing over it would hide that library from the environment)."""
    import importlib.util

    names: list[str] = []
    module_file = folder / (app.partition(":")[0].replace(".", "/") + ".py")
    if module_file.is_file():
        text = module_file.read_text(errors="replace")
        names += re.findall(r"^\s*from\s+([A-Za-z_]\w*)\.(?:models|server|client|env|environment)\b", text, re.M)
    names.append(re.sub(r"\W", "_", name))
    out = []
    for candidate in dict.fromkeys(n for n in names if n):
        if candidate in sys.modules and getattr(sys.modules[candidate], "__path__", None) == [str(folder)]:
            out.append(candidate)  # aliased already
            continue
        try:
            installed = importlib.util.find_spec(candidate) is not None
        except (ImportError, ValueError):
            installed = False
        if not installed:
            out.append(candidate)
    return out


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
        for alias_name in alias_names(folder, candidate, name) if name else []:
            alias = alias_package(alias_name, folder)
            try:
                return importlib.import_module(f"{alias}.{module_name}"), candidate, alias
            except ImportError as exc:
                tried.append(f"{alias}.{module_name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                tried.append(f"{alias}.{module_name}: {type(exc).__name__}: {exc}")
    loaded = [t for t in tried if re.search(r": (?!ModuleNotFoundError|ImportError)\w+(Error|Exception): ", t)]
    if loaded:  # the module was found and raised while loading: a missing data file or setting, not a wrong name
        raise RuntimeError("the app module was found but raised while loading (" + "; ".join(loaded[:3]) + "). If it reads data "
                           "or settings from environment variables (a path such as /app/...), set them: falsepass init <target> "
                           "--force --var NAME=VALUE (MCP: vars={...}), or `vars = {\"NAME\" = \"value\"}` in env.toml")
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


def _server_from_app(app: Any) -> Any:
    """The HTTPEnvServer behind a FastAPI app built with `create_app`: its route handlers close
    over it, and it holds the environment factory and the action class exactly as served."""
    seen: set[int] = set()
    for route in getattr(app, "routes", []) or []:
        stack = [getattr(route, "endpoint", None)]
        while stack:
            function = stack.pop()
            if function is None or id(function) in seen:
                continue
            seen.add(id(function))
            owner = getattr(function, "__self__", None)
            if owner is not None and hasattr(owner, "_env_factory") and hasattr(owner, "action_cls"):
                return owner
            for cell in getattr(function, "__closure__", None) or ():
                try:
                    value = cell.cell_contents
                except ValueError:
                    continue
                if hasattr(value, "_env_factory") and hasattr(value, "action_cls"):
                    return value
                if callable(value):
                    stack.append(value)
            stack.append(getattr(function, "__wrapped__", None))
    return None


def _factory(module: Any, environment_base: type) -> Any:
    """A module-level function that builds the environment with no arguments
    (`create_textarena_environment`, `_env_factory`), checked by calling it once: a
    `_build_app()` that returns the FastAPI app is not one."""
    for name, obj in vars(module).items():
        if not inspect.isfunction(obj) or name == "create_app" or not FACTORY_NAME.search(name):
            continue
        try:
            parameters = inspect.signature(obj).parameters.values()
        except (TypeError, ValueError):
            continue
        if not all(p.default is not inspect.Parameter.empty or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in parameters):
            continue
        try:
            built = obj()
        except Exception:  # noqa: BLE001 - not a factory we can use
            continue
        if isinstance(built, environment_base):
            if hasattr(built, "close"):
                try:
                    built.close()
                except Exception:  # noqa: BLE001
                    pass
            return obj
    return None


def find_classes(folder: Path, app: str, name: str, env_class: str = "", action_class: str = "",
                 factory: str = "", episodes: bool = False) -> tuple[Any, type, list[str], str, str]:
    """What builds the environment (a factory function, else the class) and the action class:
    named in env.toml, else found in the app module (`create_app(Env, Action, Observation)`
    imports both), else in the folder's modules. Returns (make_env, action class, notes, app
    path, alias)."""
    environment_base, action_base = _base_classes()
    notes: list[str] = []
    module, app, alias = import_app(folder, app, name)
    make_env: Any = _import_object(factory) if factory else _import_object(env_class) if env_class else None
    actions = [_import_object(action_class)] if action_class else []
    server = _server_from_app(getattr(module, app.partition(":")[2] or "app", None))
    if server is not None:  # what create_app() was given: the surest source
        make_env = make_env or server._env_factory
        actions = actions or [server.action_cls]
    if make_env is None:
        make_env = _factory(module, environment_base)
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
    if is_tool_action(actions[0]) and not episodes:
        raise RuntimeError(f"{actions[0].__name__} is an MCP tool call: this environment is played through tools, not a reply. "
                           "Write policies that play whole episodes: falsepass init <target> --force --policies "
                           "(MCP: policies=true, force=true), then fill in solver() and exploits() in adapter.py")  # fmt: skip
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
    capped = False
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
            capped = True
            break
    if pool:
        again = _normal(observation_text(await _call(env, "reset", **pool[0].raw["reset"])))
        if again != _normal(pool[0].prompt):  # the same keywords brought another task: reset() does not follow them
            import hashlib

            for example in pool:  # keyed by their text, so a task keeps its key in another run (reach, repro)
                example.key = "task-" + hashlib.sha1(_normal(example.prompt).encode("utf-8", "surrogatepass")).hexdigest()[:10]
            notes.append("reset() does not pick its task from the keywords (it draws at random or hands tasks out in turn: "
                         "the same keywords brought another task back): tasks are keyed by "
                         "their text (task-<hash>), and each probe resets until its task comes back; for a fixed order "
                         f"list the tasks in {TASKS_FILE} if reset() can pick one")  # fmt: skip
    if capped:
        notes.append(f"reset() handed out at least {len(pool)} distinct tasks and the walk stopped there: there may be more "
                     f"(list them in {TASKS_FILE} to audit a known set)")
    elif len(pool) == 1 and position >= 4:
        notes.append(f"every reset() showed the same text ({position + 1} resets): if the hidden task changes per reset (a "
                     "secret word, a random board), one key stands for many tasks and a repro may not match; give "
                     f"{TASKS_FILE} a list of seeds when reset() takes one")
    return pool


def _answer_in(value: Any) -> str:
    """An answer-like field of an observation or state, top level or in its metadata."""
    data = value.model_dump() if hasattr(value, "model_dump") else value if isinstance(value, dict) else {}
    for holder in (data, data.get("metadata") or {}):
        if isinstance(holder, dict):
            for name in ANSWER_KEYS:
                if holder.get(name) not in (None, "", [], {}):
                    return _as_text(holder[name])
    return ""


async def _harvest(scorer: OpenEnvScorer, pool: list[Example], notes: list[str]) -> None:
    """References an environment reveals only after a step (`correct_answer` in the next
    observation, `ground_truth` in its state): one constant step per task, at load time."""
    env = scorer.environment()
    tried = pool[:MAX_HARVEST]
    found = 0
    for example in tried:
        try:
            await scorer.reset(example)
            observation = await _call(env, "step", scorer.action(""))
        except Exception:  # noqa: BLE001 - nothing revealed for this task
            continue
        value = _answer_in(observation) or _answer_in(getattr(env, "state", None))
        if value:
            example.answer = value
            found += 1
    if found:
        notes.append(f"reference answers read from what the environment reveals after a step ({found} of {len(tried)} tasks): "
                     "the P1 control shows whether that is what step() compares against")  # fmt: skip


def load(spec: str, max_rows: int, answers: str | None = None, app: str = "", env_class: str = "", action_class: str = "",
         factory: str = "", tasks_file: str | Path | None = None, episodes: bool = False,
         reward: str = "sum") -> tuple[OpenEnvScorer, list[Example], dict[str, Any]]:
    """The scorer and the tasks of the environment folder `spec` (or of `$FALSEPASS_SOURCE`, a
    patched copy). Works in the folder: data files are read relative to it. `episodes`: an
    `OpenEnvEpisodeScorer`, which plays whole episodes with policies (an adapter's)."""
    folder = Path(os.environ.get(SOURCE_VAR) or spec).resolve()
    os.chdir(folder)
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    found_manifest = manifest(folder)
    app = app or found_manifest.get("app") or DEFAULT_APP
    make_env, action_cls, notes, app, alias = find_classes(folder, app, found_manifest.get("name") or folder.name, env_class,
                                                           action_class, factory, episodes)  # fmt: skip
    plan = action_plan(action_cls)
    tools = is_tool_action(action_cls)
    if tools:
        plan = {**plan, "field": None, "fills": {}, "choices": {}, "reports": {}, "blockers": []}  # tool calls, not a filled-in reply
    if plan["blockers"] and not episodes:
        raise RuntimeError(
            f"{action_cls.__name__} has required fields nothing can be made up for ({', '.join(plan['blockers'])}): write an adapter "
            "whose render() returns a JSON object with the action's fields (falsepass init --force --manual)"
        )
    scorer = OpenEnvEpisodeScorer(make_env, action_cls, plan, reward) if episodes else OpenEnvScorer(make_env, action_cls, plan)
    references: Any = json.loads(Path(answers).read_text()) if answers else {}
    tasks = read_tasks(Path(tasks_file) if tasks_file else None)
    pool = asyncio.run(_examples(scorer, max_rows, references, tasks, notes))
    if not pool:
        raise RuntimeError("reset() produced no task text to work with")
    # Tasks that reset() hands out in a cycle come back after one full cycle; random draws need
    # several times the pool size to be likely. Each try is one reset(), cheap in most environments.
    scorer.retries = min(2000, max(150, 8 * len(pool)))
    notes.append("played through MCP tool calls" if tools else describe_plan(action_cls, plan))
    if not episodes and not any(e.answer.strip() for e in pool):
        asyncio.run(_harvest(scorer, pool, notes))
    environment = scorer.environment()
    if note := served_step_note(type(environment)):
        notes.insert(0, note)
    found = {
        "notes": notes, "app": app, "alias": alias, "field": plan["field"], "fills": plan["fills"], "retries": scorer.retries,
        "env_class": f"{type(environment).__module__}:{type(environment).__qualname__}",
        "factory": f"{make_env.__module__}:{make_env.__name__}" if inspect.isfunction(make_env) else "",
        "action_class": f"{action_cls.__module__}:{action_cls.__qualname__}",
        "task_api": any(e.raw.get("task") is not None for e in pool) and not tasks, "tasks_file": bool(tasks),
        "resets": {e.key: e.raw["reset"] for e in pool},
        "tool_fills": tool_fills(action_cls, "call") if tools else {},
    }  # fmt: skip
    return scorer, pool, found


def open_episodes(audit_dir: str | Path, n: int, seed: int, reward: str = "sum",
                  max_rows: int = 500) -> tuple[OpenEnvEpisodeScorer, list[Example], dict[str, Any]]:  # fmt: skip
    """For a policy adapter (`falsepass init <target> --policies`): the episode scorer and `n`
    tasks sampled with `seed`, from the audit folder's env.toml and tasks.json, and the
    `loader` that `falsepass repro` replays the stored episodes with."""
    import random
    import tomllib

    audit_dir = Path(audit_dir).resolve()
    env = tomllib.loads((audit_dir / "env.toml").read_text())["env"]
    scorer, pool, found = load(env["task"], max_rows, env.get("answers"), app=env.get("app", ""), env_class=env.get("env_class", ""),
                               action_class=env.get("action_class", ""), factory=env.get("factory", ""),
                               tasks_file=audit_dir / TASKS_FILE, episodes=True, reward=reward)  # fmt: skip
    sampled = random.Random(seed).sample(pool, min(n, len(pool)))
    if scorer.tool_action_class() is not None:  # list the tools once now: a failure here explains empty episodes later
        async def listing() -> list[dict[str, Any]]:
            await scorer.reset(sampled[0])
            return await scorer.tools()

        try:
            offered = asyncio.run(listing())
            if not offered:
                found["notes"].append("the environment listed no tools: policies get tools=[]")
        except Exception as exc:  # noqa: BLE001 - said in the notes, the episodes go on
            scorer._tools, scorer.env = [], None
            found["notes"].append(f"listing the tools failed ({type(exc).__name__}: {str(exc)[:200]}): policies get tools=[]")
    loader = {"kind": "openenv-episode", "task": env["task"], "reward": reward,
              **{k: found[k] for k in ("app", "alias", "env_class", "factory", "action_class", "field", "fills", "retries", "tool_fills")},
              "resets": {e.key: e.raw["reset"] for e in sampled}, "prompts": {e.key: e.prompt for e in sampled}}  # fmt: skip
    found["loader"] = loader
    return scorer, sampled, found


# --- `falsepass init` (runs outside the audit virtualenv: no openenv import) -----------------


TOOL_SOURCE = re.compile(r"\b(?:CallToolAction|MCPEnvironment|ListToolsAction)\b")


def declares_tools(folder: Path) -> bool:
    """Whether the Space is played through MCP tools: its openenv.yaml says so (`declared_tools:`,
    an `mcp` tag), or its own server code uses openenv's tool actions or `MCPEnvironment`."""
    path = folder / "openenv.yaml"
    text = path.read_text(errors="replace") if path.is_file() else ""
    if re.search(r"^\s*declared_tools:", text, re.M) or re.search(r"^\s*-\s*mcp\s*$", text, re.M):
        return True
    for file in sorted(folder.rglob("*.py"))[:400]:
        parts = file.relative_to(folder).parts
        if any(p.startswith(".") or p in ("tests", "test", "site-packages", "examples") for p in parts) or "openenv_core" in parts \
                or "/".join(parts).find("openenv/core/") >= 0:
            continue
        if TOOL_SOURCE.search(file.read_text(errors="replace")):
            return True
    return False


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


LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"


def lfs_pointers(folder: Path) -> list[str]:
    """Files under `folder` that are Git LFS pointers (the data was not fetched): paths relative to the folder, sorted."""
    found = []
    for path in folder.rglob("*"):
        if ".git" in path.parts or not path.is_file() or path.stat().st_size > 1024:
            continue
        try:
            with path.open("rb") as handle:
                head = handle.read(len(LFS_POINTER))
        except OSError:
            continue
        if head == LFS_POINTER:
            found.append(path.relative_to(folder).as_posix())
    return sorted(found)


def fetch_lfs_pointers(folder: Path, owner: str, repo: str, fetch: Any = None,
                       limit: int = 200 * 1024 * 1024) -> tuple[list[str], list[str]]:
    """Replace the Git LFS pointer files of a shallow Space clone with the objects they name, from
    the Space's `resolve/main` URL, each checked against the pointer's sha256. (fetched, failed)."""
    import hashlib
    import urllib.request

    def default_fetch(url: str) -> bytes:
        with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - a huggingface.co URL built here
            return response.read()

    fetch = fetch or default_fetch
    fetched, failed, total = [], [], 0
    for rel in lfs_pointers(folder):
        path = folder / rel
        head = path.read_text(errors="replace")
        oid = re.search(r"^oid sha256:([0-9a-f]{64})", head, re.M)
        size = re.search(r"^size (\d+)", head, re.M)
        if not oid or not size or total + int(size.group(1)) > limit:
            failed.append(rel)
            continue
        try:
            data = fetch(f"https://huggingface.co/spaces/{owner}/{repo}/resolve/main/{rel}")
        except Exception:  # noqa: BLE001 - the pointer stays, and the caller says how to fetch by hand
            failed.append(rel)
            continue
        if hashlib.sha256(data).hexdigest() != oid.group(1):
            failed.append(rel)
            continue
        path.write_bytes(data)
        fetched.append(rel)
        total += len(data)
    return fetched, failed


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
    extra, relaxed = [], []
    for requirement in (d for d in declared if d not in own):
        name = requirement_name(requirement)
        if name in OPENENV_DEPENDENCIES and re.search(r"==|<|~=|!=", requirement):  # a floor (`>=`) is left alone
            relaxed.append(requirement)  # a pin or ceiling on one of openenv's own dependencies: openenv's release decides
            requirement = re.split(r"[<>=!~;\s]", requirement, 1)[0]
        extra.append(requirement)
    requires_python = ""
    if (folder / "pyproject.toml").is_file():
        import tomllib

        requires_python = tomllib.loads((folder / "pyproject.toml").read_text()).get("project", {}).get("requires-python", "")
    name = (found.get("name") or folder.name).replace("_", "-").lower()
    info: dict[str, Any] = {
        "id": f"local/{name}", "framework": "openenv", "task": str(folder), "openenv": openenv_pin,
        "app": found.get("app", DEFAULT_APP), "package": "", "version": found.get("version", ""),
        "declared": (", ".join(own) or "openenv (not declared)")
                    + (f"; pins relaxed to openenv's own: {', '.join(relaxed)}" if relaxed else ""),
        "python": python_for(requires_python), "extra": extra, "source": str(folder), "others": [],
    }  # fmt: skip
    if answers:
        info["answers"] = str(Path(answers).resolve())
    return info


def requirement_name(requirement: str) -> str:
    """`fastapi==0.115.2` -> `fastapi`, `huggingface_hub[cli]>=0.20` -> `huggingface-hub`."""
    return re.split(r"[\[<>=!~;@\s]", requirement.strip(), 1)[0].lower().replace("_", "-")
