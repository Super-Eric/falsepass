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

import asyncio
import itertools
import random
import re
from collections.abc import Callable
from typing import Any

from falsepass.adapter import Adapter
from falsepass.model import Example, Score
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
    # the tag the reasoning goes in: <think>, or <reasoning> / <thinking> when the prompt or the graders use that
    think_tag = "think" if wants_think else next((t for t in ("reasoning", "thinking") if f"<{t}>" in prompt_text), "")
    wants_think = bool(think_tag)
    tags = [t for t in dict.fromkeys(re.findall(r"<([A-Za-z_][\w-]*)>", prompt_text))
            if t != think_tag and not wraps_input(prompt_text, t)]
    answer_field = getattr(parser, "answer_field", None)
    if isinstance(answer_field, str) and answer_field not in tags:
        tags.insert(0, answer_field)

    def add(name: str, body: Format) -> None:
        if wants_think:
            prefix = "think" if think_tag == "think" else f"<{think_tag}>"  # "reasoning+" below is plain prose
            found[f"{prefix}+{name}"] = lambda a, body=body: f"<{think_tag}>\n{REASONING}\n</{think_tag}>\n{body(a)}"
        found[f"reasoning+{name}"] = lambda a, body=body: f"{REASONING}\n{body(a)}"
        found[name] = body

    for tag in tags[:4]:
        add(f"<{tag}>", lambda a, tag=tag: f"<{tag}>{a}</{tag}>")
    for tag in tags[:1]:  # the same on lines of its own, as some format rewards count "\n<answer>\n"
        add(f"<{tag}>lines", lambda a, tag=tag: f"<{tag}>\n{a}\n</{tag}>\n")
        if "boxed" in prompt_text:  # a format reward wants the tag, a math grader the box inside it
            add(f"<{tag}>boxed", lambda a, tag=tag: f"<{tag}>\\boxed{{{a}}}</{tag}>")
            add(f"<{tag}>boxed lines", lambda a, tag=tag: f"<{tag}>\n\\boxed{{{a}}}\n</{tag}>\n")
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
        self.settings = env  # the env.toml table, for framework-specific keys (OpenEnv's app, env_class, action_class)
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
        if self.framework == "openenv":
            return self._load_openenv(n, seed)
        if self.framework == "reward":
            return self._load_reward(n, seed)
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

    def _load_reward(self, n: int, seed: int) -> tuple[Scorer, list[Example]]:
        """Plain reward functions (GRPOTrainer's convention) over a dataset (reward_support.py)."""
        from falsepass import reward_support

        s = self.settings
        scorer, self.pool, found = reward_support.load(
            self.task_spec, MAX_ROWS, s.get("dataset", ""), functions=s.get("functions"), weights=s.get("weights"),
            split=s.get("split", ""), config=s.get("dataset_config", ""), answer=s.get("answer_column", ""),
            prompt=s.get("prompt_column", ""), mapper=s.get("map", ""), conversational=s.get("conversational"),
            pythonpath=s.get("pythonpath"),
        )  # fmt: skip
        self.load_notes += found["notes"]
        self.answer_free = not any(e.answer.strip() for e in self.pool)
        self._classify_pool()
        sampled = random.Random(seed).sample(self.pool, min(n, len(self.pool)))
        # The format rewards' own patterns say what a reply should look like (`<think>`, `<reasoning>`).
        self.hint = (sampled[0].prompt + "\n" + found["format_hints"], None)
        self.reward_answer_column = found["answer_column"]
        self.loader = {"kind": "reward", "task": self.task_spec, "conversational": found["conversational"],
                       "pythonpath": s.get("pythonpath") or [],
                       "functions": found["functions"], "weights": found["weights"], "prompt_column": found["prompt_column"],
                       "rows": {e.key: e.raw["row"] for e in sampled}}  # fmt: skip
        return scorer, sampled

    def _load_openenv(self, n: int, seed: int) -> tuple[Scorer, list[Example]]:
        """An OpenEnv environment: its own reset() and step() grade the reply (openenv_support.py)."""
        from falsepass import openenv_support

        audit_dir = self.settings.get("_audit_dir")
        scorer, self.pool, found = openenv_support.load(
            self.task_spec, MAX_ROWS, self.answers, app=self.settings.get("app", ""),
            env_class=self.settings.get("env_class", ""), action_class=self.settings.get("action_class", ""),
            factory=self.settings.get("factory", ""),
            tasks_file=f"{audit_dir}/{openenv_support.TASKS_FILE}" if audit_dir else None,
        )  # fmt: skip
        self.load_notes += found["notes"]
        self.answer_free = not any(e.answer.strip() for e in self.pool)
        self._classify_pool()
        self.json_references = not self.answer_free and sum(_json_object(e.answer) for e in self.pool) > len(self.pool) / 2
        self.answer_free_probes = [openenv_support.o1_fixed_choice, openenv_support.o2_self_report]  # need no reference
        self.per_example_probes = [*self.per_example_probes, *self.answer_free_probes]
        sampled = random.Random(seed).sample(self.pool, min(n, len(self.pool)))
        self.hint = (sampled[0].prompt, None)
        self.loader = {"kind": "openenv", "task": self.task_spec, "answers": self.answers,
                       **{k: found[k] for k in ("app", "alias", "env_class", "factory", "action_class", "field", "fills")},
                       "resets": {e.key: e.raw["reset"] for e in sampled}, "prompts": {e.key: e.prompt for e in sampled}}  # fmt: skip
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

        scripted = bool(tasks) and not tasks[0].data.prompt_text and bool(scripted_user_turns(tasks[0].data))
        episodic = episode_reasons(self.module, taskset, tasks[:1])
        if scripted:
            episodic.insert(0, "a scripted user over several turns and no prompt")
        if episodic:
            return self._load_v1_episodes(tasks, episodic)
        pool = [
            Example(f"task-{i}", t.data.prompt_text, str(getattr(t.data, "answer", "") or ""), t)
            for i, t in enumerate(tasks)
        ]
        self.loader = {"kind": "v1", "module": self.module, "load_args": self.load_args}
        system = getattr(tasks[0].data, "system_prompt", None) or ""
        return V1TaskScorer(tasks[0]), pool, (f"{system}\n{tasks[0].data.prompt_text}", None)

    def _load_v1_episodes(self, tasks: list[Any], reasons: list[str]) -> tuple[Scorer, list[Example], tuple[str, Any]]:
        """A tool or multi-turn v1 taskset: every completion is played as a whole episode
        through the taskset's own loop (falsepass.v1_episodes). One trial episode first, so a
        taskset that cannot run here stops now with one line, not with every probe erroring."""
        from falsepass.scorers import scripted_user_turns
        from falsepass.v1_episodes import V1EpisodeScorer

        from falsepass.v1_episodes import outside_hosts

        runtime, args = self.settings.get("runtime", "subprocess"), list(self.settings.get("vf_args", []))
        network = self.settings.get("network", False)
        hosts = outside_hosts(self.module)
        trial = V1EpisodeScorer(self.module, self.load_args, runtime, args, network)
        first = Example("task-0", tasks[0].data.prompt_text or "", "", tasks[0])

        async def try_once() -> Score:
            try:
                return await trial.score(first, "Done.")
            finally:
                await trial.close()

        tried = asyncio.run(try_once())
        blocked = (f" Its source names outside hosts ({', '.join(hosts[:8])}), which are blocked during episodes: if the "
                   "environment needs one, run it against a local stand-in (`vf_args = [\"--env.<option>\", \"http://127.0.0.1:...\"]` "
                   "in env.toml) or, knowing it may act there (register, post, publish), allow it with `network = [\"host\"]`."
                   if hosts and network is not True else "")  # fmt: skip
        if tried.reward is None:
            raise ContainerTask(  # not a container either, but nothing to fall back to v0 for
                f"{type(tasks[0]).__name__} is played as whole episodes ({'; '.join(reasons)}), and a trial episode did not "
                f"finish: {'; '.join(tried.errors)[:600]}.{blocked} Other options: `runtime = \"docker\"` in env.toml, extra "
                "vf-eval options as `vf_args = [...]` there, or write an adapter (falsepass init <env> --force --policies)"
            )
        self.tool_mode, self.multi_turn = True, True
        tools = tried.info.get("tools") or []
        self.load_notes.append(
            f"played as whole episodes through the taskset's own loop ({'; '.join(reasons)}; "
            f"{len(tools)} tools offered{': ' + ', '.join(tools[:12]) if tools else ''}{', ...' if len(tools) > 12 else ''}), "
            f"runtime {runtime}: generic tool policies only (T5, T6); for task-aware policies write an adapter "
            "(falsepass init <env> --force --policies)"
        )
        self.load_notes += trial.notes
        if hosts and network is not True:
            self.load_notes.append(f"outside hosts named in the source ({', '.join(hosts[:8])}) were blocked during episodes "
                                   f"{'except ' + ', '.join(network) + ' ' if network else ''}(HTTP(S) proxies set to a closed port)")
        pool = [Example(f"task-{i}", t.data.prompt_text or (scripted_user_turns(t.data) or [""])[0], str(getattr(t.data, "answer", "") or ""), t) for i, t in enumerate(tasks)]
        self.loader = {"kind": "v1-episode", "module": self.module, "load_args": self.load_args, "runtime": runtime, "args": args,
                       "network": network}
        system = getattr(tasks[0].data, "system_prompt", None) or ""
        return V1EpisodeScorer(self.module, self.load_args, runtime, args, network), pool, (f"{system}\n{tasks[0].data.prompt_text}", None)

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
            openenv = self.framework == "openenv"
            self.inferred = {
                "validity": "no control: the tasks carry no reference answer ("
                            + ("OpenEnv environments keep it inside step()" if openenv else "HUD keeps it inside the grader")
                            + "), so only answer-free probes ran (P3, P4, A1, P10, P11"
                            + (", O1, O2" if openenv else "") + ") and the maximum reward is assumed to be 1.0. "
                            "Give reference answers with `falsepass init ... --answers file.json`"
                            + (" (or an `answer` per task in tasks.json)" if openenv else "") + " to run every probe",
                "format": "plain reply", "reference_reward_by_format": {}, "examples_tried": 0, "answers": "not available",
                "turns": "single answer, graded by the environment's step()" if openenv else "single answer, graded by the task template",
                "note": "",
            }  # fmt: skip
            trial = examples[:k]
            empties = [(await scorer.score(ex, "")).reward or 0.0 for ex in trial]
            floor_note = self._floor_note(max(empties, default=0.0), 1.0)
            unrelated = [(await scorer.score(ex, "This reply answers nothing in particular.")).reward or 0.0 for ex in trial]
            flat = ""
            if len(trial) >= 2 and len({*empties, *unrelated}) == 1:
                flat = (f"every reply scored {empties[0]:g} on {len(trial)} tasks, an empty one and a sentence alike: the reply "
                        "did not reach the part of the action that is graded (a decision or id field kept its default). "
                        "Give answers as JSON actions (tasks.json or --answers) or write an adapter; the rows below only show "
                        "that constant")  # fmt: skip
            self.inferred["note"] = "; ".join(n for n in [*self.load_notes, floor_note, flat] if n)
            return
        if self.tool_mode:
            self.inferred = {
                "validity": "no control: the reference cannot be replayed without a policy that solves the task, so the "
                            "maximum reward is assumed to be 1.0 and only the tool-policy probes (T5, T6) were run",
                "format": "tool calls", "reference_reward_by_format": {}, "examples_tried": 0, "answers": "given through tool calls",
                "turns": "multi-turn with tools: generic policies call the environment's own tools",
                "note": "; ".join(self.load_notes),
            }  # fmt: skip
            return
        if self.framework == "reward":
            await self._drop_skipped_tasks(scorer, examples)
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
        if floor_note := self._floor_note(max(empty), table[best]):
            notes.append(floor_note)
        if validity == "ok" and (spread_note := await self._task_maxima(scorer, examples)):
            notes.append(spread_note)
        if validity == "ok" and self.framework == "reward" and len(getattr(scorer, "funcs", {})) > 1:
            formatted = [(await scorer.score(ex, self.format(self.wrong_answer(ex)))).reward for ex in trial]
            if formatted and None not in formatted and len(set(formatted)) == 1 and 0 < formatted[0] < self.max_reward - 1e-9:
                self.format_floor = formatted[0]
                notes.append(f"a wrong answer in the right format earns {formatted[0]:g} from the format terms on every task tried: "
                             "that is the lazy-policy reward's floor (P6 shows it); other known-bad rows count as failing only "
                             "when paid more than that")  # fmt: skip
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

    async def _task_maxima(self, scorer: Scorer, examples: list[Example]) -> str:
        """When the reference earns clearly different amounts on different tasks (a clamp at
        0.99 on one, the first of two steps on another), judge correct-answer variants against
        what the reference earns on their own task rather than the average."""
        earned = {ex.key: (await scorer.score(ex, self.format(ex.answer))).reward for ex in examples}
        values = [v for v in earned.values() if v is not None and v > 0]
        if len(values) < 2 or max(values) - min(values) <= 0.05 * max(values):
            return ""
        self.task_max = {k: v for k, v in earned.items() if v is not None and v > 0}
        self.max_reward = round(max(values), 6)  # what a full answer can earn; the average would sit below some known-bad rows
        return (f"the reference earns between {min(values):g} and {max(values):g} depending on the task, so the maximum is "
                f"{max(values):g}: correct-answer variants (P2, P14, T7) are judged against the reference's own reward on their "
                "task; P1 rows below the maximum are tasks where a full answer earns less")  # fmt: skip

    async def _drop_skipped_tasks(self, scorer: Scorer, examples: list[Example]) -> None:
        """Reward functions return None to have the trainer skip a task (TRL's accuracy_reward
        does when it cannot parse the gold solution). A task on which the reference itself gets
        None in every candidate format is one the trainer never scores: leave it out and say so,
        instead of counting every probe on it as an error."""
        formats = list(candidate_formats(*self.hint).values())
        kept, skipped = [], []
        for example in examples:
            for fmt in formats:
                if (await scorer.score(example, fmt(example.answer))).reward is not None:
                    kept.append(example)
                    break
            else:
                skipped.append(example.key)
        if skipped and kept:
            examples[:] = kept
            self.load_notes.append(
                f"{len(skipped)} of {len(skipped) + len(kept)} sampled tasks left out: every reward function returned None for the "
                f"reference itself, so the trainer skips them ({', '.join(skipped[:5])}{' ...' if len(skipped) > 5 else ''}); "
                "if that share is large, the reference column may not be what the functions parse"
            )  # fmt: skip

    def _floor_note(self, floor: float, maximum: float) -> str:
        """A grader that clamps its reward to (0.01, 0.99) pays the floor to an empty reply:
        that floor is this grader's zero, not a false pass. Known-bad rows are judged against
        it; the ceiling and the means still show the raw numbers."""
        if 0 < floor <= 0.05 * maximum:
            self.bad_tolerance = floor
            return f"an empty reply earns {floor:g}: that floor counts as nothing (bad_tolerance {floor:g})"
        return ""

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
        has_digit = any(c.isdigit() for c in answer)
        others = [o.answer for o in self.pool if o.answer.strip().lower() != answer.lower()]
        alike = [o for o in others if any(c.isdigit() for c in o) == has_digit]  # "Yes" is no wrong value for "$47$"
        if alike or others:
            return (alike or others)[0]
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
        if getattr(self, "json_references", False):  # a JSON action: only forms that are still that object
            forms = {name: form for name, form in forms.items() if _json_object(form)}
        return forms

    def with_reference(self, example: Example, reference: str) -> Example | None:
        if not is_number(example.answer):
            return None
        if self.framework == "inspect":
            from falsepass import inspect_support

            return inspect_support.with_reference(example, reference)
        if self.framework in ("hud", "openenv"):  # the expected answer lives inside the environment: nothing to swap
            return None
        if self.framework == "reward":  # the functions read the reference column; swap it when it holds the bare value
            column = self.reward_answer_column
            row = dict(example.raw["row"])
            if not column or str(row.get(column, "")).strip() != example.answer.strip():
                return None
            row[column] = reference if isinstance(row[column], str) else type(row[column])(reference) if is_number(reference) else reference
            return Example(example.key, example.prompt, reference, {"row": row})
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


def episode_reasons(module: str, taskset: Any, tasks: list[Any]) -> list[str]:
    """Why a v1 taskset's reward depends on a whole episode rather than one reply: its own
    `Env` subclass (a custom episode loop), tool servers on the taskset or its tasks. Empty
    when a reply can be scored on its own (`V1TaskScorer`)."""
    reasons = []
    try:
        from verifiers.v1.utils import loaders

        env_class = loaders.environment_class(module)
        if env_class.__name__ != "SingleAgentEnv" and env_class is not getattr(loaders, "SingleAgentEnv", None):
            reasons.append(f"its own episode loop ({env_class.__name__})")
    except Exception:  # noqa: BLE001 - an older v1 without these: judge by the tools alone
        pass
    toolsets, failed = [], []
    for owner, config in [(taskset, taskset.config), *((task, task.config) for task in tasks)]:
        try:
            toolsets += type(owner).toolsets(config)
        except Exception as exc:  # noqa: BLE001 - a tool server that cannot even be built: the trial episode shows why
            failed.append(f"{type(owner).__name__}.toolsets() raised {type(exc).__name__}")
    if toolsets or failed:
        reasons.append(f"tool servers ({', '.join([*sorted({type(t).__name__ for t in toolsets}), *failed])})")
    return reasons


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
