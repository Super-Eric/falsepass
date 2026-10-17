"""Adapter inference: audit an environment without writing an adapter.

A hand-written adapter answers three questions. `AutoAdapter` answers them itself:

1. *How does this env want an answer written?*  Candidate formats are collected from the
   env's parser and prompt text, the reference answer is scored in each, and the format
   that the env's own reward code pays best is chosen. The verifier tells us its format.
2. *What is a wrong answer?*  Another label when answers form a small closed set, a
   shifted value when numeric, otherwise the reference of a different example.
3. *What are harmless variations?*  Generic ones only: surrounding whitespace, a trailing
   sentence, punctuation and padding inside the answer slot.

Works for single-turn environments that carry a reference answer (verifiers v0 rubric
envs and v1 tasksets). Multi-turn, tool-use and judge-only environments still need a
hand-written adapter; `load` says so instead of guessing.
"""

from __future__ import annotations

import itertools
import random
import re
from collections.abc import Callable
from typing import Any

from falsepass.adapter import Adapter
from falsepass.model import Example
from falsepass.scorers import Scorer, V0RolloutScorer, V0RubricScorer, V1TaskScorer

REASONING = (
    "Reading the problem carefully, the relevant facts are identified first and each "
    "condition is checked in turn. The intermediate steps are worked through one at a "
    "time, alternatives are compared against the constraints given, and the result is "
    "verified once more before the final answer is stated below."
)
MAX_ROWS = 2000
CLOSED_SET_MAX = 12
INPUT_WRAPPER_MIN = 200  # characters between <tag> and </tag>
Format = Callable[[str], str]


def candidate_formats(prompt_text: str, parser: Any = None) -> dict[str, Format]:
    """Ways to write an answer, most specific first. Insertion order is the tie-break."""
    found: dict[str, Format] = {}
    wants_think = "<think>" in prompt_text or "think" in (getattr(parser, "fields", None) or [])
    tags = [t for t in dict.fromkeys(re.findall(r"<([A-Za-z_][\w-]*)>", prompt_text))
            if t != "think" and not wraps_input(prompt_text, t)]
    answer_field = getattr(parser, "answer_field", None)
    if isinstance(answer_field, str) and answer_field not in tags:
        tags.insert(0, answer_field)

    def add(name: str, body: Format) -> None:
        if wants_think:
            found[f"think+{name}"] = lambda a, body=body: f"<think>\n{REASONING}\n</think>\n{body(a)}"
        found[f"reasoning+{name}"] = lambda a, body=body: f"{REASONING}\n{body(a)}"
        found[name] = body

    for tag in tags[:4]:
        add(f"<{tag}>", lambda a, tag=tag: f"<{tag}>{a}</{tag}>")
    if "boxed" in prompt_text:
        add("boxed", lambda a: f"\\boxed{{{a}}}")
    if "####" in prompt_text:
        add("####", lambda a: f"#### {a}")
    if re.search(r"\[[A-Za-z]\w*\]", prompt_text):  # games that take moves as [move]
        add("[brackets]", lambda a: f"[{a}]")
        for tag in tags[:2]:
            add(f"<{tag}>[brackets]", lambda a, tag=tag: f"<{tag}>[{a}]</{tag}>")
    if "```" in prompt_text:
        add("fenced", lambda a: f"```\n{a}\n```")
    if "ANSWER:" in prompt_text:  # Inspect's templates ask for "ANSWER: $ANSWER"
        add("ANSWER:", lambda a: f"ANSWER: {a}")
    # Nothing below was asked for by the prompt: the plain answer goes first, so that it wins a tie.
    found["bare"] = lambda a: a
    add("boxed", lambda a: f"\\boxed{{{a}}}")
    add("Answer:", lambda a: f"Answer: {a}")
    return found


def wraps_input(prompt_text: str, tag: str) -> bool:
    """A tag the prompt uses to enclose material it hands over (a document, a conversation),
    as opposed to one it shows as the place for the answer."""
    enclosed = re.search(rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>", prompt_text, re.S)
    return bool(enclosed) and len(enclosed.group(1)) > INPUT_WRAPPER_MIN


def own_markup(answer: str) -> bool:
    """The reference already carries an answer wrapper of its own."""
    return bool(re.search(r"<[A-Za-z_][\w-]*>|\\boxed|####", answer))


def is_number(text: str) -> bool:
    return bool(re.fullmatch(r"-?\$?\d[\d,]*(\.\d+)?", text.strip()))


class ContainerTask(TypeError):
    """A v1 task that needs a container. Raised so the caller does not fall back to v0 loading."""


class AutoAdapter(Adapter):
    def __init__(self, env: dict[str, Any]) -> None:
        self.name = env["id"]
        self.framework = env.get("framework", "verifiers")
        self.task_spec = env.get("task", "")
        self.answers = env.get("answers")
        self.answer_free = False  # no reference answers (HUD without an answers file): answer-free probes only
        self.module = env.get("module") or env.get("package") or self.task_spec
        self.load_args = env.get("load_args", {})
        self.multi_turn = False
        self.tool_mode = False  # a tool-use environment: played with policies, no answer format to infer
        self.open_ended = False
        self.loader: dict[str, Any] = {}
        self.per_example_probes = [other_task_reference]
        self.format_name, self.format = "bare", (lambda a: a)
        self.inferred: dict[str, Any] = {}
        self.load_notes: list[str] = []  # how the environment was found, when not the usual way

    # --- loading -------------------------------------------------------------------
    def load(self, n: int, seed: int) -> tuple[Scorer, list[Example]]:
        if self.framework == "inspect":
            return self._load_inspect(n, seed)
        if self.framework == "hud":
            return self._load_hud(n, seed)
        try:
            scorer, self.pool, self.hint = self._load_v1()
        except ContainerTask as needs_box:
            raise RuntimeError(str(needs_box)) from needs_box
        except Exception as v1_error:
            try:
                scorer, self.pool, self.hint = self._load_v0()
            except Exception as v0_error:
                raise RuntimeError(
                    f"could not load {self.module!r} as a v1 taskset ({v1_error}) or a v0 environment ({v0_error})"
                ) from v0_error
        self._classify_pool()
        return scorer, random.Random(seed).sample(self.pool, min(n, len(self.pool)))

    def _classify_pool(self) -> None:
        if not (self.tool_mode or self.answer_free):
            self.pool = [e for e in self.pool if e.answer.strip()]
        if not self.pool:
            raise RuntimeError(
                "no reference answers found: this environment needs a hand-written adapter. Run "
                f"`falsepass init {self.name} --force --manual` for a task with an answer to compare, or "
                "`--policies` for a game, simulation or tool task, then fill in adapter.py"
                + "".join(f". Note: {note}" for note in self.load_notes)
            )
        lengths = sorted(len(e.answer) for e in self.pool)
        # Open-ended tasks (write code, explain, document): the reference is a passage, not a value.
        self.open_ended = lengths[len(lengths) // 2] > 60 or sum("\n" in e.answer for e in self.pool) > len(self.pool) / 2
        self.labels = sorted({e.answer for e in self.pool})
        self.closed = len(self.labels) <= CLOSED_SET_MAX and len(self.pool) > 2 * len(self.labels)

    def _load_hud(self, n: int, seed: int) -> tuple[Scorer, list[Example]]:
        """A HUD task template: its own grader scores the reply (hud_support.py)."""
        from falsepass import hud_support

        scorer, self.pool = hud_support.load(self.task_spec, MAX_ROWS, self.answers)
        self.answer_free = not any(e.answer.strip() for e in self.pool)
        self._classify_pool()
        sampled = random.Random(seed).sample(self.pool, min(n, len(self.pool)))
        self.hint = (sampled[0].prompt, None)
        self.loader = {"kind": "hud", "task": self.task_spec, "answers": self.answers}
        return scorer, sampled

    def _load_inspect(self, n: int, seed: int) -> tuple[Scorer, list[Example]]:
        """An Inspect AI task: its solver chain and scorers grade the completion (inspect_support.py)."""
        from falsepass import inspect_support

        scorer, self.pool = inspect_support.load(self.task_spec, self.load_args, MAX_ROWS)
        self._classify_pool()
        sampled = random.Random(seed).sample(self.pool, min(n, len(self.pool)))
        for example in sampled:  # what the model would see: the task's templates applied
            example.prompt = scorer.rendered_prompt(example)
        self.hint = (sampled[0].prompt, None)
        self.loader = {"kind": "inspect", "task": self.task_spec, "load_args": self.load_args}
        return scorer, sampled

    def _load_v0(self) -> tuple[Scorer, list[Example], tuple[str, Any]]:
        import verifiers as vf

        env = vf.load_environment(self.module, **self.load_args)
        if not hasattr(env, "rubric"):
            raise TypeError("not a rubric environment")
        lineage = {cls.__name__ for cls in type(env).__mro__}
        needs_infrastructure = lineage & {"SandboxEnv", "PythonEnv", "CliAgentEnv", "MCPEnv", "BrowserEnv", "HarborEnv", "EnvGroup"}
        if needs_infrastructure:
            raise TypeError(f"{type(env).__name__} uses a sandbox or an outside service ({sorted(needs_infrastructure)}): write an adapter")
        self.multi_turn = "SingleTurnEnv" not in lineage and "MultiTurnEnv" in lineage
        # Tools that run in this process: the answer is given through tool calls, so there is
        # no text format to infer. Such an environment is played with policies (probe T6).
        self.tool_mode = bool(lineage & {"ToolEnv", "StatefulToolEnv"})
        rows = None
        for getter in ("get_eval_dataset", "get_dataset"):
            try:
                rows = getattr(env, getter)()
                if rows is not None and len(rows):
                    break
            except Exception:
                continue
        if rows is None:
            raise TypeError("environment exposes no dataset")
        rows = rows.select(range(min(len(rows), MAX_ROWS)))
        pool = [
            Example(f"row-{i}", self._last_user(row["prompt"]), str(row.get("answer") or ""), row)
            for i, row in enumerate(rows)
        ]
        system = getattr(env, "system_prompt", None) or ""
        first = " ".join(str(m.get("content") or "") for m in rows[0]["prompt"])
        scorer = V0RolloutScorer(env) if self.multi_turn else V0RubricScorer(env)
        self.loader = {"kind": "v0-rollout" if self.multi_turn else "v0", "module": self.module,
                       "load_args": self.load_args, "getter": getter}  # fmt: skip
        return scorer, pool, (f"{system}\n{first}", getattr(env, "parser", None))

    def _load_v1(self) -> tuple[Scorer, list[Example], tuple[str, Any]]:
        taskset = open_taskset(self.module, self.load_args, self.load_notes)
        tasks = list(itertools.islice(iter(taskset.load()), MAX_ROWS))
        if tasks and type(tasks[0]).NEEDS_CONTAINER:
            # Graded on what is left in a box, not on text: text probes would all score 0
            # and say nothing. See falsepass/container.py.
            raise ContainerTask(
                f"{type(tasks[0]).__name__} is graded on files left in a container: run `falsepass container "
                "<env>` (needs Docker), or write submissions with falsepass.container.score_submission"
            )
        from falsepass.scorers import scripted_user_turns

        if tasks and not tasks[0].data.prompt_text and scripted_user_turns(tasks[0].data):
            raise ContainerTask(  # not a container, but equally not something to fall back to v0 for
                f"{type(tasks[0]).__name__} has no prompt: its env plays a scripted user over several turns, and the "
                "answer for each turn cannot be inferred. Write an adapter whose render() returns the conversation; "
                "V1TaskScorer opens the trace with the first scripted turn (write an adapter.py with the scripted turns)"
            )
        pool = [
            Example(f"task-{i}", t.data.prompt_text, str(getattr(t.data, "answer", "") or ""), t)
            for i, t in enumerate(tasks)
        ]
        self.loader = {"kind": "v1", "module": self.module, "load_args": self.load_args}
        system = getattr(tasks[0].data, "system_prompt", None) or ""
        return V1TaskScorer(tasks[0]), pool, (f"{system}\n{tasks[0].data.prompt_text}", None)

    @staticmethod
    def _last_user(messages: Any) -> str:
        if isinstance(messages, str):
            return messages
        users = [m for m in messages if m.get("role") == "user"]
        return str((users or messages)[-1].get("content") or "")

    def hygiene_examples(self, limit: int) -> list[Example]:
        return self.pool[:limit]

    # --- format inference ------------------------------------------------------------
    async def prepare(self, scorer: Scorer, examples: list[Example], k: int = 5) -> None:
        """Score the reference in every candidate format and keep the best-paid one."""
        if self.answer_free:
            self.inferred = {
                "validity": "no control: the tasks carry no reference answer (HUD keeps it inside the grader), so only "
                            "answer-free probes ran (P3, P4, A1, P10, P11) and the maximum reward is assumed to be 1.0. "
                            "Give reference answers with `falsepass init ... --answers file.json` to run every probe",
                "format": "plain reply", "reference_reward_by_format": {}, "examples_tried": 0, "answers": "not available",
                "turns": "single answer, graded by the task template", "note": "",
            }  # fmt: skip
            return
        if self.tool_mode:
            self.inferred = {
                "validity": "no control: the reference cannot be replayed without a policy that solves the task, so the "
                            "maximum reward is assumed to be 1.0 and only the tool-policy probes (T5, T6) were run",
                "format": "tool calls", "reference_reward_by_format": {}, "examples_tried": 0, "answers": "given through tool calls",
                "turns": "multi-turn with tools: generic policies call the environment's own tools", "note": "",
            }  # fmt: skip
            return
        trial = examples[:k]
        table: dict[str, float] = {}
        for name, fmt in candidate_formats(*self.hint).items():
            rewards = [(await scorer.score(ex, fmt(ex.answer))).reward or 0.0 for ex in trial]
            table[name] = sum(rewards) / len(rewards)
        best = max(table, key=lambda name: table[name])  # max() keeps the first of equals
        self.format_name, self.format = best, candidate_formats(*self.hint)[best]
        if table[best] <= 0:
            raise RuntimeError(
                f"no candidate format earned any reward for the reference answer ({table}): "
                "write an adapter with `falsepass init --manual`"
            )
        self.max_reward = round(table[best], 6) if abs(table[best] - 1.0) > 1e-9 else 1.0
        # Control: an audit built on the reference is only meaningful if the reward tells
        # the reference apart from saying nothing.
        empty = [(await scorer.score(ex, "")).reward or 0.0 for ex in trial]
        blank = sum(empty) / len(empty)
        # Separated when saying nothing earns less than half of what the reference earns
        # and the gap is material. (An absolute threshold wrongly rejected environments
        # whose own reference answers score only 0.4.)
        separated = blank < 0.5 * table[best] and table[best] - blank >= 0.2
        if not separated:
            validity = (
                f"inconclusive: the reference scored {table[best]:.3f} and an empty completion "
                f"{blank:.3f}. Either this reward does not depend on the `answer` field "
                "(a game, a simulation, a judge), or it pays regardless of the answer. Automatic mode "
                "cannot tell which; the probe results below are not evidence either way"
            )
            if not self.multi_turn and sum(_json_object(ex.answer) for ex in trial) > len(trial) / 2:
                validity += (
                    ". The `answer` field holds a JSON object: if it is a grading record rather than the reply the "
                    "prompt asks for, write an adapter whose reference is that reply (`falsepass init <target> "
                    "--manual --force`)"
                )
        else:
            validity = "ok"
        notes = list(self.load_notes)
        if abs(table[best] - 1.0) > 1e-9:
            notes.append(f"the reference did not score exactly 1.0; the best format averaged {table[best]:.3f} and is used as the maximum")
        if sum(own_markup(ex.answer) for ex in trial) > len(trial) / 2:
            # Seen in curvebench: wrapping such a reference again, or a wrong value next to it,
            # leaves the grader reading the reference's own tags.
            notes.append("the reference answers carry answer markup of their own, so probes that place a wrong "
                         "answer in the format may not present a competing answer: check P5, P6 and P9 rows by hand")
        self.inferred = {
            "validity": validity,
            "format": best,
            "reference_reward_by_format": table,
            "examples_tried": len(trial),
            "answers": "closed set: " + ", ".join(self.labels) if self.closed
            else "open-ended passages" if self.open_ended else "open",
            "turns": "multi-turn: the answer is given at every turn through the env's own rollout loop"
            if self.multi_turn else "single-turn",
            "note": "; ".join(notes),
        }

    # --- the three adapter questions -----------------------------------------------
    def render(self, answer: str, example: Example) -> str:
        return self.format(answer)

    def other_reference(self, example: Example) -> str | None:
        """The reference answer of a different task, as far from this one's as the pool allows."""
        answer = example.answer.strip().lower()
        for other in self.pool:
            if other.key != example.key and other.answer.strip().lower() != answer:
                return other.answer
        return None

    def wrong_answer(self, example: Example) -> str:
        answer = example.answer.strip()
        if self.open_ended:
            # A passage that answers nothing. Another task's reference is probed separately
            # (X1): for open-ended tasks that measures task-independence, not wrongness.
            return "I am not sure how to approach this, so I will not give an answer."
        if self.closed:
            return self.labels[(self.labels.index(example.answer) + 1) % len(self.labels)]
        if is_number(answer):
            value = float(answer.replace(",", "").replace("$", ""))
            return f"{value + 7:g}"
        for other in self.pool:
            if other.answer.strip().lower() != answer.lower():
                return other.answer
        return "none of the above"

    def shotgun_answers(self, example: Example) -> list[str]:
        if self.closed:
            return list(self.labels)
        if is_number(example.answer):
            value = float(example.answer.replace(",", "").replace("$", ""))
            return [f"{value + d:g}" for d in (-3, -2, -1, 1, 2, 3)] + [example.answer]
        return [self.wrong_answer(example), example.answer]

    def equivalent_forms(self, example: Example) -> dict[str, str]:
        a, correct = example.answer, self.format(example.answer)
        forms = {
            "surrounding_whitespace": f"\n\n  {correct}  \n\n",
            "sentence_after": f"{correct}\nThat is my final answer.",
            "crlf_line_endings": correct.replace("\n", "\r\n"),
            "padding_inside_slot": self.format(f"  {a}  "),
            "trailing_period_inside_slot": self.format(f"{a}."),
        }
        if a.isalpha() and a.swapcase() != a:
            forms["answer_case_swapped"] = self.format(a.swapcase())
        return forms

    def with_reference(self, example: Example, reference: str) -> Example | None:
        if not is_number(example.answer):
            return None
        if self.framework == "inspect":
            from falsepass import inspect_support

            return inspect_support.with_reference(example, reference)
        raw = example.raw
        if hasattr(raw, "data"):  # v1 task
            data = raw.data.model_copy(update={"answer": reference})
            return Example(example.key, example.prompt, reference, type(raw)(data, raw.config))
        row = dict(raw)
        row["answer"] = reference
        return Example(example.key, example.prompt, reference, row)


async def other_task_reference(adapter: AutoAdapter, scorer: Scorer, ex: Example) -> list:
    """X1 — submit the reference answer of a different task. For value answers this is
    simply a wrong answer. For open-ended tasks it asks a different question: does the
    reward depend on which task was set, or only on what a good answer looks like?"""
    from falsepass.model import Expect
    from falsepass.probes import targeted

    other = adapter.other_reference(ex)
    if other is None:
        return []
    return await targeted("X1", "task_independence", {
        "other_task_reference": (Expect.ZERO, adapter.format(other)),
    }, adapter, scorer, ex, answer_free=("other_task_reference",))  # fmt: skip


def open_taskset(module: str, load_args: dict[str, Any] | None = None, notes: list[str] | None = None) -> Any:
    """A v1 taskset by its module, as verifiers loads it; failing that, the one Taskset
    subclass defined in a submodule (`pkg/taskset.py`), with a note in `notes`. For
    hand-written adapters too: `open_taskset("my_env").load()` gives the tasks."""
    import verifiers.v1 as vf
    from verifiers.v1.utils.loaders import taskset_class

    try:
        cls, config = taskset_class(module), vf.taskset_config_type(module)
    except (AttributeError, TypeError) as not_exported:
        found = submodule_taskset(module, vf.Taskset)
        if found is None:
            raise
        cls, config = found, taskset_config_of(found, vf)
        if notes is not None:
            notes.append(
                f"verifiers' own loader cannot load this taskset by its module ({not_exported}); falsepass found "
                f"{cls.__name__} in {cls.__module__} and audits that. A run addressed by the Hub id may not start: "
                "worth telling the maintainer"
            )
    return cls(config(id=module, **(load_args or {})))


def submodule_taskset(module: str, base: type) -> type | None:
    """The one Taskset subclass defined in a submodule of `module` (`pkg.taskset`), for a
    package whose top level does not export it. None when there is not exactly one."""
    import importlib
    import pkgutil

    package = importlib.import_module(module)
    found = []
    for info in pkgutil.walk_packages(getattr(package, "__path__", []), f"{module}."):
        try:
            sub = importlib.import_module(info.name)
        except Exception:  # noqa: BLE001 - a submodule that needs extras: not the taskset
            continue
        found += [obj for obj in vars(sub).values()
                  if isinstance(obj, type) and issubclass(obj, base) and obj is not base and obj.__module__ == sub.__name__]  # fmt: skip
    return found[0] if len(found) == 1 else None


def taskset_config_of(cls: type, vf: Any) -> type:
    """The config class a Taskset subclass is specialised with (`Taskset[Task, Config]`)."""
    try:
        from verifiers.v1.utils.loaders import concrete_type

        return concrete_type(cls, vf.TasksetConfig, origin=vf.Taskset) or vf.TasksetConfig
    except ImportError:
        return vf.TasksetConfig


def _json_object(text: str) -> bool:
    import json

    try:
        return isinstance(json.loads(text), dict)
    except (ValueError, TypeError):
        return False
