"""`falsepass audit <env>` — run the probe kit against one audited environment.

Hub environments are built for different, mutually incompatible verifiers
versions, so each audit runs in its own throw-away virtualenv. The pins live in
`audits/<env>/env.toml`; this command turns them into one `uv run` invocation.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path


def install_args(env: dict, source: str | Path | None = None) -> list[str]:
    """The `uv run --with` arguments that install verifiers and the environment.

    `env.toml` may name local trees instead of releases: `source` for the environment
    package (a folder with a pyproject.toml) and `verifiers_source` for verifiers itself.
    `source` given here (a patched copy, an upstream checkout) overrides the file's."""
    source = source or env.get("source")
    if env.get("framework") == "reward":  # the file is imported from its path (FALSEPASS_SOURCE for a patched copy)
        args = []
    elif env.get("framework") == "openenv":  # the folder is imported from its path (FALSEPASS_SOURCE for a patched copy), not installed
        args = ["--with", env.get("openenv") or "openenv"]
    elif env.get("framework") == "hud":
        args = ["--with", f"hud=={env['hud']}" if env.get("hud") else "hud"]
        if source:
            args += ["--with", str(source)]
    elif env.get("framework") == "inspect":
        args = ["--with", f"inspect-ai=={env['inspect_ai']}" if env.get("inspect_ai") else "inspect-ai"]
        if source:
            args += ["--with", str(source)]
        elif env.get("package"):
            args += ["--with", f"{env['package']}=={env['version']}" if env.get("version") else env["package"]]
    else:
        args = ["--with", str(env["verifiers_source"]) if env.get("verifiers_source") else f"verifiers=={env['verifiers']}"]
        args += ["--with", str(source) if source else f"{env['package']}=={env['version']}"]
    if source and Path(source).is_dir() and str(source) in args:
        # uv caches the environment built from a folder and does not notice edits to its code
        # (not even with --reinstall-package): install it editable, so every run sees the folder as it is
        args[args.index(str(source)) - 1] = "--with-editable"
    for requirement in env.get("extra", []):
        args += ["--with", requirement]
    if env.get("index"):
        args += ["--extra-index-url", env["index"]]
    return args


def self_install_args() -> list[str]:
    """How the throw-away virtualenv gets this same copy of falsepass: the checkout it runs
    from, else the URL it was installed from (a git commit, a local wheel), else the
    release with this version number."""
    root = Path(__file__).resolve().parent.parent
    if (root / "pyproject.toml").is_file() and (root / "falsepass" / "__init__.py").is_file():
        return ["--with-editable", str(root)]
    from importlib import metadata

    try:
        dist = metadata.distribution("falsepass")
    except metadata.PackageNotFoundError:
        return ["--with", "falsepass"]
    direct = json.loads(dist.read_text("direct_url.json") or "{}")
    if direct.get("url"):
        vcs = direct.get("vcs_info")
        url = f"{vcs['vcs']}+{direct['url']}@{vcs['commit_id']}" if vcs else direct["url"]
        return ["--with", f"falsepass @ {url}"]
    return ["--with", f"falsepass=={dist.version}"]


def uv_command(audit_dir: Path, extra: list[str], module: str = "falsepass.runner", source: str | Path | None = None) -> list[str]:
    env = tomllib.loads((audit_dir / "env.toml").read_text())["env"]
    command = [
        "uv", "run", "--isolated", "--no-project", "--python", env.get("python", "3.12"),
        *self_install_args(), "--with", "rewardlint", *install_args(env, source),
    ]  # fmt: skip
    package = ["--package", env["package"]] if env.get("package") else []
    if env.get("framework") == "reward":  # the graders' own libraries decide the result (math-verify 0.5 and 0.9 parse differently)
        from falsepass.openenv_support import requirement_name

        package = [arg for requirement in env.get("extra", []) for arg in ("--package", requirement_name(requirement))]
    return [*command, "python", "-m", module, str(audit_dir), *package, *extra]


def source_command(audit_dir: Path, extra: list[str], module: str, source: Path | None) -> list[str]:
    """`uv_command`, with a folder of modules without packaging (a patched copy of the
    audit's src/) laid over the released package via PYTHONPATH, as verify-fix does."""
    from falsepass import verify

    env = tomllib.loads((audit_dir / "env.toml").read_text())["env"]
    if source is not None and env.get("framework") in ("openenv", "reward"):  # the patched folder or file is read in place of env.toml's
        return ["env", f"FALSEPASS_SOURCE={Path(source).resolve()}", *uv_command(audit_dir, extra, module, None)]
    if verify.overlay(source):
        return ["env", f"PYTHONPATH={verify.overlay_path(audit_dir, source)}", *uv_command(audit_dir, extra, module, None)]
    return uv_command(audit_dir, extra, module, source)


ADAPTER_TEMPLATE = '''"""falsepass adapter for {id}."""

import random

from falsepass import Adapter, Example
from falsepass.scorers import V0RubricScorer  # or V1TaskScorer / DevTasksetScorer


class Env(Adapter):
    name = "{id}"
    # How `falsepass repro` loads the same rows without this file; keys must end in the row index.
    loader = {{"kind": "v0", "module": "{package}", "load_args": {{}}, "getter": "get_dataset"}}

    def load(self, n, seed):
        import verifiers as vf

        self.env = vf.load_environment("{package}")
        self.rows = self.env.get_dataset()
        picks = random.Random(seed).sample(range(len(self.rows)), min(n, len(self.rows)))  # n may exceed it (reach)
        return V0RubricScorer(self.env), [self._example(i) for i in picks]

    def _example(self, i) -> Example:
        row = self.rows[i]
        return Example(f"row-{{i}}", row["prompt"][-1]["content"], row["answer"], row)

    def hygiene_examples(self, limit):
        return [self._example(i) for i in range(min(limit, len(self.rows)))]

    def render(self, answer, example):
        """Write `answer` exactly the way the environment's prompt asks for it."""
        raise NotImplementedError

    def wrong_answer(self, example):
        """Return an answer that is definitely wrong for this example."""
        raise NotImplementedError


ADAPTER = Env()
'''


V1_ADAPTER_TEMPLATE = '''"""falsepass adapter for {id} (a verifiers v1 taskset)."""

import random

from falsepass import Adapter, Example
from falsepass.scorers import V1TaskScorer

MODULE = "{package}"


class Env(Adapter):
    name = "{id}"
    # How `falsepass repro` loads the same tasks without this file; keys must end in the task index.
    loader = {{"kind": "v1", "module": MODULE, "load_args": {{}}}}

    def _tasks(self):
        from falsepass.auto import open_taskset  # also finds a Taskset in a submodule

        return list(open_taskset(MODULE).load())

    def _example(self, i, task) -> Example:
        # The reference: whichever field of task.data holds the correct reply, written the way
        # the prompt asks for it (an `answer`, a key such as `solutions[0]`, a JSON answer key).
        return Example(f"task-{{i}}", task.data.prompt_text, str(getattr(task.data, "answer", "") or ""), task)

    def load(self, n, seed):
        self.tasks = self._tasks()
        picks = random.Random(seed).sample(range(len(self.tasks)), min(n, len(self.tasks)))  # n may exceed it (reach)
        return V1TaskScorer(self.tasks[0]), [self._example(i, self.tasks[i]) for i in picks]

    def hygiene_examples(self, limit):
        return [self._example(i, t) for i, t in enumerate(self.tasks[:limit])]

    def render(self, answer, example):
        """Write `answer` exactly the way the environment's prompt asks for it."""
        raise NotImplementedError

    def wrong_answer(self, example):
        """Return an answer that is definitely wrong for this example."""
        raise NotImplementedError


ADAPTER = Env()
'''


POLICY_TEMPLATE = '''"""falsepass adapter for {id}: policies for a game, a simulation or a tool task.

Automatic mode cannot script these, because a fixed reply is not a fair test: the model
reacts to what the environment says. A policy does the same. It is a function from the
conversation so far (a list of {{"role", "content"}} dicts) to the next assistant message:
a string, or {{"content": ..., "tool_calls": [{{"name": ..., "arguments": {{...}}}}]}}
(`Policy(name, act, wants_tools=True)` also passes the tool schemas: `act(messages, tools)`).

`example` is a falsepass Example: `key` (the row id, ending in its index), `prompt` (the
task text the model sees), `answer` (the row's reference answer, possibly empty) and `raw`
(the environment's own row, e.g. a dict with "prompt", "answer" and "info").

Each policy runs through the environment's own rollout loop and rubric. Its episode
(what it said, and the whole conversation) is kept, so `falsepass results <env>
--completion <id>` shows it and `falsepass repro <env>` replays it turn by turn.
Run: falsepass audit <env>
"""

import random

from falsepass import Adapter, Example, policies
from falsepass.model import Expect, Policy
from falsepass.policies import heard, said  # what the environment / the policy said so far
from falsepass.probes import check
from falsepass.scorers import V0RolloutScorer


def solver(example):
    """Known-good: does the task properly (it may read the row's answer). Should earn full reward."""
    def act(messages):
        raise NotImplementedError("TODO: the next message of a policy that solves the task")
    return Policy("solver", act)


def gives_up(example):
    """Known-bad, honest: plays without solving (wrong moves, or none). Should earn nothing."""
    def act(messages):
        return "I don't know."
    return Policy("gives_up", act)


def exploits(example):
    """Known-bad policies that never use the answer. Each should earn nothing. The ready-made
    ones in falsepass.policies found real holes; adapt their messages to this environment.
    Other ideas: act twice in one message; act on something the task didn't ask about."""
    wrong = "TODO: a wrong move in this environment's format"
    return {{
        "after_the_end": policies.after_the_end(wrong),  # repeats what the environment gives away
        "keeps_talking": policies.keeps_talking(wrong),  # goes on after "game over"
        "never_acts": policies.never_acts(),
    }}


def special_number_scripts(example):
    """Fixed scripts: a sequence the environment accepts, with NaN / inf / -1 / 1e999 put
    in place of its numbers (a limit written as `amount > LIMIT` is false for NaN)."""
    accepted = ["TODO: e.g. ACTION: issue_refund #1234 25.00"]
    return policies.special_numbers(accepted)


async def policy_probes(adapter, scorer, ex):
    found = [await check("X1", "policy", "solver", Expect.FULL, solver(ex), adapter, scorer, ex)]
    found.append(await check("X1", "policy", "gives_up", Expect.ZERO, gives_up(ex), adapter, scorer, ex))
    for name, policy in exploits(ex).items():
        found.append(await check("X1", "policy", name, Expect.ZERO, policy, adapter, scorer, ex, answer_free=True))
    for name, script in special_number_scripts(ex).items():
        found.append(await check("X2", "special_number", name, Expect.ZERO, script, adapter, scorer, ex, answer_free=True))
    return found


class Env(Adapter):
    name = "{id}"
    max_reward = 1.0  # what the solver earns; set it to the real figure once the X1 solver rows show it
    per_example_probes = [policy_probes]
    # how `falsepass repro` reloads the same rows (keys must end in the row index)
    loader = {{"kind": "v0-rollout", "module": "{package}", "load_args": {{}}, "getter": "get_dataset"}}

    def load(self, n, seed):
        import verifiers as vf

        self.env = vf.load_environment("{package}")
        self.rows = self.env.get_dataset()
        picks = random.Random(seed).sample(range(len(self.rows)), min(n, len(self.rows)))
        return V0RolloutScorer(self.env), [self._example(i) for i in picks]

    def _example(self, i) -> Example:
        row = self.rows[i]
        return Example(f"row-{{i}}", row["prompt"][-1]["content"], row.get("answer", ""), row)

    def hygiene_examples(self, limit):
        return [self._example(i) for i in range(min(limit, len(self.rows)))]

    def render(self, answer, example):
        """The standard probes ask for "the right answer" and "a wrong one": map them to policies."""
        return solver(example) if answer == example.answer else gives_up(example)

    def wrong_answer(self, example):
        return "WRONG"


ADAPTER = Env()
'''


V1_POLICY_TEMPLATE = '''"""falsepass adapter for {id}: policies for a verifiers v1 tool or multi-turn taskset.

Each policy plays whole episodes through the taskset's own loop, as `vf-eval` runs them:
its harness, its tool servers, its scripted user, its stop conditions and its rewards
(falsepass.v1_episodes.V1EpisodeScorer). No model: a local endpoint on 127.0.0.1 answers
the harness with the policy's next message.

A policy is `act(messages, tools)`: the conversation so far (dicts with "role", "content",
and "tool_calls" / "tool_call_id" where present; tool results are role "tool") and the
offered tool schemas in; the next assistant message out: a string (ends the turn) or
`call("tool_name", arg=value, ...)` (one or more tool calls).

`example.raw` is the task: `example.raw.data` holds its fields (the prompt, and whatever
the grader compares against). The solver may read them; every other policy must not.

The generic tool policies (T5, T6) run as well. Each episode is kept, so `falsepass
results <env> --completion <id>` shows it and `falsepass repro <env>` replays it.
Run: falsepass audit <env>
"""

import random

from falsepass import Adapter, Example
from falsepass.auto import open_taskset
from falsepass.model import Expect, Policy
from falsepass.probes import check
from falsepass.v1_episodes import V1EpisodeScorer

MODULE = "{package}"
LOAD_ARGS = {{}}  # taskset options, as `--env.taskset.<option>` would set them, e.g. {{"split": "eval"}}
RUNTIME = "subprocess"  # or "docker": where the harness runs
NETWORK = False  # outside hosts are blocked during episodes; a list of hosts allows those (the env may act there)


def call(name, **arguments):
    return {{"content": "", "tool_calls": [{{"name": name, "arguments": arguments}}]}}


def turn(messages):
    """How many assistant messages came before: 0 on the first request."""
    return sum(1 for m in messages if m["role"] == "assistant")


def tool_results(messages):
    return [m["content"] for m in messages if m["role"] == "tool"]


def refusing_is_right(example):
    """True for a task where the correct episode moves nothing (a scam to refuse, a request
    to decline). Known-bad policies are not judged there: doing nothing is correct."""
    return False  # TODO: e.g. example.raw.data.family in ("pin_request", "fake_refund")


def solver(example):
    """Known-good: does the task properly from its own data. Should earn the maximum."""
    def act(messages, tools):
        raise NotImplementedError("TODO: e.g. `return call(...) if turn(messages) == 0 else 'Done.'`")
    return Policy("solver", act, wants_tools=True)


def gives_up(example):
    """Known-bad, honest: declines without trying. Should earn nothing (unless refusing is right)."""
    return Policy("gives_up", lambda messages, tools: "I'm sorry, I can't help with that.", wants_tools=True)


def exploits(example):
    """Known-bad policies that never read the task data. Each should earn nothing. Ideas: claim
    success without acting; act on everything the tools show; a number written another way
    (words, spaced digits); NaN, inf, -1 or 1e999 in an amount; a reply that lists every
    candidate answer."""
    return {{}}


async def policy_probes(adapter, scorer, ex):
    found = [await check("X1", "policy", "solver", Expect.FULL, solver(ex), adapter, scorer, ex)]
    bad = Expect.LAZY if refusing_is_right(ex) else Expect.ZERO
    found.append(await check("X1", "policy", "gives_up", bad, gives_up(ex), adapter, scorer, ex, answer_free=True))
    for name, policy in exploits(ex).items():
        found.append(await check("X1", "policy", name, bad, policy, adapter, scorer, ex, answer_free=True))
    return found


class Env(Adapter):
    name = "{id}"
    max_reward = 1.0  # what the solver earns; set it to the real figure once the X1 solver rows show it
    tool_mode = True  # played with policies: no text probes
    episode_probes = [policy_probes]
    # how `falsepass repro` replays the same tasks (keys must end in the task index)
    loader = {{"kind": "v1-episode", "module": MODULE, "load_args": LOAD_ARGS, "runtime": RUNTIME, "network": NETWORK}}

    def load(self, n, seed):
        self.tasks = list(open_taskset(MODULE, LOAD_ARGS).load())
        picks = random.Random(seed).sample(range(len(self.tasks)), min(n, len(self.tasks)))
        examples = [self._example(i) for i in picks]
        self.refusal_tasks = {{ex.key for ex in examples if refusing_is_right(ex)}}  # T5/T6 rows there are not counted
        return V1EpisodeScorer(MODULE, LOAD_ARGS, RUNTIME, network=NETWORK), examples

    def _example(self, i) -> Example:
        task = self.tasks[i]
        return Example(f"task-{{i}}", task.data.prompt_text or "", "", task)

    def render(self, answer, example):
        return solver(example) if answer == example.answer else gives_up(example)

    def wrong_answer(self, example):
        return "WRONG"


ADAPTER = Env()
'''


def find_audit(env: str, needs: str = "env.toml") -> Path | None:
    """The audit folder for `env`: a path to one, or a name under audits/ (`arc_easy` also
    finds `arc-easy`, the name `init` gives it)."""
    for candidate in (Path(env), Path("audits") / env, Path("audits") / env.lower().replace("_", "-")):
        if (candidate / needs).is_file():
            return candidate
    return None


def missing_audit(env: str, needs: str = "env.toml") -> int:
    """Explain why `env` can't be used, and what to run instead."""
    set_up = find_audit(env)
    if set_up is None:
        known = sorted(p.name for p in Path("audits").glob("*") if (p / "env.toml").is_file()) if Path("audits").is_dir() else []
        print(f"falsepass: no audit named {env!r} in {Path.cwd() / 'audits'} (set up here: {', '.join(known) or 'none'}). "
              f"Run `falsepass init <target>` first; it prints the name to use.", file=sys.stderr)  # fmt: skip
        return 1
    job = set_up / "job.json"
    if job.is_file() and json.loads(job.read_text()).get("state") == "running":
        print(f"falsepass: an audit of {set_up.name} is still running (started from the MCP server); "
              "its output is in job.log. Try again when it has finished.", file=sys.stderr)  # fmt: skip
        return 1
    print(f"falsepass: {set_up} has no completed audit yet. Run `falsepass audit {set_up.name}` first "
          "(if it stopped at the control, this environment needs a hand-written adapter: `falsepass init --manual`).",
          file=sys.stderr)  # fmt: skip
    return 1


def scaffold(args) -> int:
    import os
    import re

    from falsepass import hub, hud_support, inspect_support, openenv_support, reward_support

    local = Path(args.env_id)
    space = re.match(r"^(?:hf:|https?://huggingface\.co/spaces/)([\w.-]+)/([\w.-]+?)/?$", args.env_id)
    fresh_clone = False
    if space:  # an OpenEnv environment on the Hugging Face Hub: a shallow clone, kept next to the audit
        owner, repo = space.groups()
        clone = Path("audits") / (args.name or repo.lower().replace("_", "-")) / "space"
        fresh_clone = not clone.parent.exists()
        if not clone.is_dir():
            print(f"cloning https://huggingface.co/spaces/{owner}/{repo} into {clone}", file=sys.stderr)
            done = subprocess.run(["git", "clone", "--depth", "1", f"https://huggingface.co/spaces/{owner}/{repo}", str(clone)],
                                  env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, capture_output=True, text=True, check=False)  # fmt: skip
            if done.returncode != 0:
                print(f"falsepass: could not clone the Space: {done.stderr.strip()[-400:]}", file=sys.stderr)
                return 1
        local = clone
    if space or (local.exists() and (args.framework == "openenv" or (args.framework is None and openenv_support.uses_openenv(local)))):
        info = openenv_support.describe(local, args.answers)
        if space:
            info["id"] = f"hf/{owner}/{repo}"
    elif args.framework == "reward" or (args.framework is None and reward_support.uses_reward_functions(local)
                                         and not inspect_support.uses_inspect(local)):
        try:
            info = reward_support.describe(args.env_id, args.dataset or "", args.func or None, args.weight or None, args.split or "",
                                           args.dataset_config or "", args.answer_column or "", args.prompt_column or "", args.map or "",
                                           args.path or None)
        except (RuntimeError, SyntaxError) as stop:
            print(f"falsepass: {stop}", file=sys.stderr)
            return 1
        if not info.get("dataset"):
            print("falsepass: reward functions need a dataset: add --dataset (a .jsonl/.json/.csv/.parquet file, hf:owner/name, or "
                  "call:function for a function in the reward file that returns the rows)", file=sys.stderr)
            return 1
    elif local.exists() and (args.framework == "hud" or (args.framework is None and hud_support.uses_hud(local))):
        info = hud_support.describe(local, args.answers)
    elif local.is_file() and local.suffix == ".py" and inspect_support.uses_inspect(local):
        info = inspect_support.describe_file(local, args.task)
    elif (local / "pyproject.toml").is_file() and (args.framework == "inspect" or inspect_support.uses_inspect(local)):
        info = inspect_support.describe_folder(local, args.task)
    elif (local / "pyproject.toml").is_file():
        info = hub.describe_local(local)
    elif not local.exists() and (args.framework == "inspect" or args.env_id.startswith("inspect_evals/")):
        info = inspect_support.describe_registry(args.env_id)
    elif local.exists() or args.env_id.count("/") != 1:
        print(f"falsepass: {args.env_id} is neither a Hub slug (owner/environment), an Inspect task (a .py file, or "
              "a registry name with --framework inspect) nor a folder with a pyproject.toml", file=sys.stderr)
        return 1
    else:
        info = hub.describe(args.env_id, args.version)
    if args.verifiers:
        info["verifiers"] = args.verifiers
    if args.extra:
        info["extra"] = [*info.get("extra", []), *args.extra]
    if args.without:
        from falsepass.openenv_support import requirement_name

        dropped = {requirement_name(w) for w in args.without}
        info["extra"] = [d for d in info.get("extra", []) if requirement_name(d) not in dropped]
    for key in ("env_class", "action_class"):
        if getattr(args, key, None):
            info[key] = getattr(args, key)
    name = args.name or info["id"].split("/")[-1].lower().replace("_", "-")
    audit_dir = Path(args.dir or "audits") / name
    ref = name if not args.dir else str(audit_dir.resolve())  # what later commands are given: a name under ./audits, or the path
    existing = audit_dir / "env.toml"
    if existing.is_file() and not args.force and not (args.manual or args.policies):
        old = tomllib.loads(existing.read_text())["env"]
        if all(old.get(k) == info.get(k) for k in ("id", "version", "task", "source", "dataset", "functions", "weights")):  # the same target: nothing to do
            if args.json:
                print(json.dumps({"name": name, "folder": str(audit_dir.resolve()), "framework": old.get("framework", "verifiers"),
                                  "env": old, "manual": (audit_dir / "adapter.py").is_file(), "existing": True,
                                  "others": info.get("others", []), "next": f"falsepass audit {ref}"}, indent=1))  # fmt: skip
            else:
                print(f"{audit_dir} is already set up for {info['id']}; next: falsepass audit {ref}")
            return 0
    if audit_dir.exists() and not args.force and not fresh_clone:
        print(f"falsepass: {audit_dir} already exists. Pass --force to rewrite its env.toml (stored results are kept "
              "until the next audit), or --name to set up a second audit next to it.", file=sys.stderr)  # fmt: skip
        return 1
    if audit_dir.exists():
        import shutil

        shutil.rmtree(audit_dir / "src", ignore_errors=True)
    audit_dir.mkdir(parents=True, exist_ok=True)
    keys = ("id", "framework", "task", "package", "version", "module", "index", "verifiers", "python", "inspect_ai", "hud",
            "openenv", "app", "env_class", "action_class", "factory", "dataset", "split", "dataset_config", "functions", "weights",
            "answer_column", "prompt_column", "map", "pythonpath", "source", "answers", "extra")  # fmt: skip
    required = ("index",) if info.get("framework", "verifiers") == "verifiers" else ()
    lines = [f"{key} = {json.dumps(info[key])}" for key in keys if info.get(key) or key in required]
    declared = f"# declared by the package: {info['declared']}\n" if info.get("declared") else ""
    (audit_dir / "env.toml").write_text("[env]\n" + "\n".join(lines) + "\n" + declared)
    for source, content in info.get("sources", {}).items():  # the wheel's source, for the static pre-scan
        target = audit_dir / "src" / source
        if target.resolve().is_relative_to((audit_dir / "src").resolve()):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    kind = hub.classify(info["sources"]) if info.get("sources") else info.get("kind")  # read from the source text
    kept_adapter = (args.manual or args.policies) and (audit_dir / "adapter.py").exists()
    if (args.manual or args.policies) and not kept_adapter:
        v1 = bool(kind) and kind["generation"] == "v1"
        template = (V1_POLICY_TEMPLATE if v1 else POLICY_TEMPLATE) if args.policies else V1_ADAPTER_TEMPLATE if v1 else ADAPTER_TEMPLATE
        (audit_dir / "adapter.py").write_text(template.format(id=info["id"], package=info.get("module") or info.get("task", "")))
    if kept_adapter:
        print(f"falsepass: kept the existing {audit_dir}/adapter.py (delete it first for a fresh skeleton)", file=sys.stderr)
    args.manual = args.manual or args.policies
    why = ""
    if kind and not kind["automatic"]:
        why = ", ".join([*kind["needs"], *([] if kind["has_reference"] else ["no reference answer found in the source"]),
                         *([] if kind["turns"] != "unknown" else ["turn structure not recognised"])])  # fmt: skip
    if args.json:
        print(json.dumps({"name": name, "folder": str(audit_dir.resolve()), "framework": info.get("framework", "verifiers"),
                          "env": {k: info[k] for k in keys if info.get(k)}, "manual": bool(args.manual),
                          "automatic_likely": kind["automatic"] if kind else None,
                          **({"task_data_fields": hub.task_data_fields(info["sources"])} if args.manual and info.get("sources") else {}),
                          **({"automatic_unlikely_because": why} if why else {}),
                          "others": info.get("others", []), "next": f"falsepass audit {ref}"}, indent=1))  # fmt: skip
        return 0
    fields = hub.task_data_fields(info["sources"]) if (args.manual and info.get("sources")) else {}
    if args.policies:
        print(f"created {audit_dir}/env.toml and adapter.py — fill in solver() and exploits(), then:")
    elif args.manual:
        print(f"created {audit_dir}/env.toml and adapter.py — fill in render() and wrong_answer(), then:")
        for cls, names in fields.items():  # where the expected values live, for render()
            print(f"  task data {cls}: " + ", ".join(names))
    elif info.get("framework") == "reward":
        print(f"created {audit_dir}/env.toml — each reply goes to the reward functions the way GRPOTrainer calls them, with "
              f"the dataset row's columns:")
    elif info.get("framework") == "openenv":
        how = ("with the reference answers given" if info.get("answers") else
               "answer-free probes unless the task records carry an answer; add --answers for all")
        print(f"created {audit_dir}/env.toml — each reply goes through the environment's own reset() and step() ({how}):")
    elif info.get("framework") == "hud":
        how = "with the reference answers given" if info.get("answers") else "answer-free probes only; add --answers for all"
        print(f"created {audit_dir}/env.toml — each reply is graded by its task template ({how}):")
    elif info.get("framework") == "inspect":
        print(f"created {audit_dir}/env.toml — each reply goes through the task's own solver chain and scorers:")
    elif kind and kind["generation"] == "v1" and "tools" in kind["needs"]:
        print(f"created {audit_dir}/env.toml — a v1 tool taskset: each probe plays whole episodes through the taskset's own "
              "loop with generic tool policies (T5, T6). For policies that know the task (a solver as the control, "
              "task-aware exploits), rerun with --force --policies:")
    elif kind and not kind["automatic"]:
        print(f"created {audit_dir}/env.toml — automatic mode may not fit ({why or kind['turns']}); try it, and if it "
              f"stops, rerun with --force --manual (or --policies for a game, simulation or tool task):")
    else:
        print(f"created {audit_dir}/env.toml — no adapter needed for single-turn environments with reference answers:")
    if info.get("source"):
        print(f"  (installed from {info['source']} on every run, so edits are picked up)")
    if info.get("others"):
        print(f"  (task {info['task']}; the others can be audited with --task and --name: {', '.join(info['others'])})")
    print(f"  falsepass audit {ref}")
    return 0


def demo_source() -> Path:
    """The toy environment: bundled in the wheel as `falsepass/_demo`, in a checkout under examples/."""
    packaged = Path(__file__).resolve().parent / "_demo"
    return packaged if packaged.is_dir() else Path(__file__).resolve().parent.parent / "examples" / "toy-env"


def demo(target: Path) -> int:
    import shutil

    if not target.exists():
        shutil.copytree(demo_source(), target, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    audit_dir = Path("audits") / "falsepass-toy-env"
    if not (audit_dir / "env.toml").is_file():
        code = main(["init", str(target)])
        if code:
            return code
    print(f"auditing the toy environment in {target} (its grader pays `answer in reply`: expect several probes to fail, all from that one line)", flush=True)
    return subprocess.call(uv_command(audit_dir, ["-n", "10"]))


def main(argv: list[str] | None = None) -> int:
    from falsepass import build_version

    os.environ.setdefault("UV_HTTP_RETRIES", "10")  # a package index answering 429 is retried, not fatal (uv backs off)

    parser = argparse.ArgumentParser(
        prog="falsepass", description="Probe RL environment verifiers for false passes and false fails.",
        epilog="Results are leads to reproduce and read, not verdicts. Docs: https://github.com/Super-Eric/falsepass",
    )  # fmt: skip
    parser.add_argument("--version", action="version", version=f"falsepass {build_version()}")
    sub = parser.add_subparsers(dest="command", required=True)
    audit = sub.add_parser(
        "audit", help="probe one environment in an isolated virtualenv",
        epilog="Probe options, passed through to the run: -n N (tasks to sample, default 25), --seed N, "
               "--few N (tasks for the slower probes, default 3), --search CALLS (mutation search budget per "
               "direction and task; 120 is a good value), --search-examples N, --out DIR (write results "
               "elsewhere), --jobs N (tasks scored at once for single-turn rewards; default up to 8, 1 = one at "
               "a time), --fail-on COND (repeatable, e.g. 'ceiling>0.1' 'errors>0': exit 1 when met).",
    )  # fmt: skip
    audit.add_argument("env", help="name of a folder under audits/, or a path to one")
    audit.add_argument("--print-command", action="store_true", help="show the uv command and exit")
    audit.add_argument("--source", type=Path, help="install the environment from this local folder instead of its release")
    roll = sub.add_parser("rollout", help="score a local model's own replies (cross-check; Ollama by default)")
    roll.add_argument("env")
    roll.add_argument("--print-command", action="store_true", help="show the uv command and exit")
    roll.add_argument("--source", type=Path, help="install the environment from this local folder instead of its release")
    prop = sub.add_parser("propose", help="let a local model write wrong answers and climb on the reward (single-turn)")
    prop.add_argument("env")
    prop.add_argument("--print-command", action="store_true", help="show the uv command and exit")
    prop.add_argument("--source", type=Path, help="install the environment from this local folder instead of its release")
    init = sub.add_parser("init", help="set up audits/<name>/ for a Hub environment or a local environment folder")
    init.add_argument("env_id", help="Hub slug (owner/environment), a local folder with a pyproject.toml, an Inspect task file, "
                                     "an Inspect registry name such as inspect_evals/gsm8k, a HUD environment folder, an OpenEnv Space "
                                     "(hf:owner/space) or folder, or a file of reward functions (TRL GRPOTrainer style, with --dataset)")
    init.add_argument("--name", help="folder name under audits/ (default: the environment name)")
    init.add_argument("--dir", help="where the audit folder goes (default ./audits); later commands then take the folder's path")
    init.add_argument("--version", help="environment version (default: latest on the Hub)")
    init.add_argument("--verifiers", help="verifiers version to pin (default: inferred from the wheel)")
    init.add_argument("--manual", action="store_true", help="also write an adapter.py skeleton to fill in")
    init.add_argument("--framework", choices=["verifiers", "inspect", "hud", "openenv", "reward"], help="force the framework (default: detected)")
    init.add_argument("--dataset", metavar="SPEC", help="reward functions: the rows to score with, a .jsonl/.json/.csv/.parquet file, "
                                                        "hf:owner/name, or call:function (a function in the reward file returning rows)")
    init.add_argument("--func", action="append", metavar="NAME", help="reward functions: which to use, in order (repeatable; default: "
                                                                      "every function in the file that takes `completions`)")
    init.add_argument("--weight", action="append", type=float, metavar="W", help="reward functions: one weight per --func, in order (default 1)")
    init.add_argument("--split", help="reward functions: the dataset split (default train)")
    init.add_argument("--dataset-config", dest="dataset_config", help="reward functions: the Hub dataset's configuration name")
    init.add_argument("--answer-column", dest="answer_column", help="reward functions: the reference column (default: guessed)")
    init.add_argument("--prompt-column", dest="prompt_column", help="reward functions: the prompt column (default: prompt, question, ...)")
    init.add_argument("--map", help="reward functions: a function in the reward file applied to every row first")
    init.add_argument("--path", action="append", metavar="DIR", help="reward functions: a folder put on sys.path, for a package "
                                                                     "that is imported rather than installed (repeatable)")
    init.add_argument("--answers", help="HUD: a JSON file with reference answers, either an object from task slug to "
                                        'answer ({"add-2-3": "5"}) or a list in task order (["5", "12"])')
    init.add_argument("--force", action="store_true", help="rewrite the env.toml of an existing audit folder (adapter.py is kept)")
    init.add_argument("--policies", action="store_true",
                      help="like --manual, for games, simulations and tool tasks: an adapter skeleton built around policies")
    init.add_argument("--json", action="store_true", help="print the result as JSON (name to audit with, env.toml fields)")
    init.add_argument("--task", help="Inspect: which @task to audit when a file or project defines several")
    init.add_argument("--env-class", dest="env_class", metavar="MODULE:CLASS",
                      help="OpenEnv: the environment class (or factory function) when detection picks the wrong one")
    init.add_argument("--action-class", dest="action_class", metavar="MODULE:CLASS", help="OpenEnv: the action class")
    init.add_argument("--without", action="append", default=[], metavar="PKG",
                      help="drop this requirement from the ones installed with the environment (repeatable)")
    init.add_argument("--with", dest="extra", action="append", default=[], metavar="REQ",
                      help="extra requirement to install for the audit, e.g. 'inspect-evals[math]' (repeatable)")
    disc = sub.add_parser("discover", help="list actively maintained Hub environments and what each needs")
    disc.add_argument("--hub", choices=["prime", "hf"], default="prime",
                      help="prime: the Prime Intellect Hub (needs the prime CLI); hf: OpenEnv Spaces on the Hugging Face Hub")
    disc.add_argument("--days", type=int, default=90, help="updated within this many days")
    disc.add_argument("--min-releases", type=int, default=2, help="published versions required to count as maintained")
    disc.add_argument("--min-stars", type=int, default=0)
    disc.add_argument("--limit", type=int, default=40, help="most-starred recent environments to inspect")
    disc.add_argument("-o", "--output", type=Path, help="also write the full list as JSON")
    repro = sub.add_parser("repro", help="write a stand-alone reproduction script for the failing probes")
    repro.add_argument("env")
    repro.add_argument("--probe", action="append", help="only these probes, e.g. --probe P6 or --probe P5/all_in_one_slot (repeatable)")
    repro.add_argument("--per-group", type=int, default=2, help="examples per failing probe variant")
    repro.add_argument("--run", action="store_true",
                       help="then run it twice, each in a fresh virtualenv, compare stdout and store the first run "
                            "(<script>.output.txt, headed by the command and whether the second run was identical)")
    repro.add_argument("--script", help="with --run: run this script in the audit folder (e.g. a hand-written repro.py) "
                                        "instead of writing repro_auto.py")
    repro.add_argument("--json", action="store_true", help="with --run: print the result as JSON")
    reach = sub.add_parser(
        "reach", help="count how many tasks a probe fails on: a sample of N or every task (after an audit)",
        epilog="Scores only the probe rows asked for, plus the reference (P1) as a control, in an isolated "
               "virtualenv like `audit`, and writes audits/<env>/reach/<probe>.json; the stored audit results are "
               "not touched. Per-example probes only (P1-P9, P13-P17, A1, X1). Examples: "
               "falsepass reach my-env --probe P15/denied --tasks all; "
               "falsepass reach my-env --template '\\boxed{{answer} \\text{ is wrong}}' --template '{wrong} or {answer}'",
    )  # fmt: skip
    reach.add_argument("env")
    reach.add_argument("--probe", action="append", default=[], metavar="CODE[/VARIANT]",
                       help="a probe code, or code/variant as in the results, e.g. P15/denied (repeatable); "
                            "`known-good` counts every correct-answer row (P1, P2, P14)")
    reach.add_argument("--template", action="append", default=[], metavar="TEXT",
                       help="count a completion of your own: {answer}, {wrong}, {answer_in_format}, {wrong_in_format} "
                            "are filled per task, other braces stay (repeatable; rows U1/t1, t2, ...)")
    reach.add_argument("--expect", choices=["zero", "full"], default="zero",
                       help="what the templates should earn: zero (default; count where they are paid) or full")
    reach.add_argument("--tasks", default="all", help="how many tasks to sample, or 'all' (default)")
    reach.add_argument("--seed", default="0")
    reach.add_argument("--jobs", help="tasks scored at once where safe (default up to 8)")
    reach.add_argument("--json", action="store_true", help="print the result as JSON")
    reach.add_argument("--print-command", action="store_true", help="show the uv command and exit")
    reach.add_argument("--source", type=Path, help="install the environment from this local folder instead of its release "
                                                   "(a folder without pyproject.toml is laid over the release, as in verify-fix)")
    reach.add_argument("--out", help="folder for the result (default audits/<env>/reach/)")
    drafted = sub.add_parser("draft", help="write REPORT.draft.md: candidate findings for manual review")
    drafted.add_argument("env")
    over = sub.add_parser("overview", help="one table over every audited environment")
    over.add_argument("-o", "--output", type=Path)
    box = sub.add_parser("container", help="probe a container task by writing its key, and mutations of it, as the output file (Docker)")
    box.add_argument("env")
    box.add_argument("--print-command", action="store_true", help="show the uv command and exit")
    box.add_argument("--source", type=Path, help="install the environment from this local folder instead of its release")
    up = sub.add_parser("upstream", help="find the environment's source repository on GitHub and compare it with the audit")
    up.add_argument("env")
    up.add_argument("--repo", help="check this owner/name instead of searching")
    up.add_argument("--limit", type=int, default=5, help="search results per query")
    fix = sub.add_parser("verify-fix", help="rerun a stored repro against a patched source and show what changed")
    fix.add_argument("env")
    fix.add_argument("--source", type=Path, required=True,
                     help="the patched environment: a package folder with pyproject.toml, or a folder of its modules "
                          "(e.g. a patched copy of the audit's src/), which is laid over the release via PYTHONPATH")
    fix.add_argument("--script", help="repro script in the audit folder (default: repro.py, else repro_auto.py)")
    fix.add_argument("--print-command", action="store_true", help="show the uv command and exit")
    fix.add_argument("--known-good", nargs="?", const="all", metavar="TASKS",
                     help="also score every correct-answer row (P1, P2, P14) with and without the patch on every task "
                          "(or TASKS sampled) and show which the patch stops paying")
    res = sub.add_parser("results", help="print the last probe run of an environment (summary, or --json for scripts and agents)")
    res.add_argument("env")
    res.add_argument("--json", action="store_true", help="headline numbers, failing probes and hints as JSON")
    res.add_argument("--completion", metavar="ID", help="print the full text of one completion (a completion_id from --json)")
    trial = sub.add_parser("demo", help="audit a bundled toy environment with one planted defect (about a minute)")
    trial.add_argument("--dir", type=Path, default=Path("falsepass-demo"), help="where to write the toy environment")
    sub.add_parser("mcp", help="serve falsepass as MCP tools over stdio (needs the `mcp` extra)")
    scan = sub.add_parser("scan", help="static pre-scan of reward source (no environment install needed)")
    scan.add_argument("path", type=Path, help="a Python file or a source directory")
    scan.add_argument("--json", action="store_true", help="print the hints as JSON")
    args, extra = parser.parse_known_args(argv)

    if args.command in ("repro", "draft"):
        from falsepass import draft, repro

        audit_dir = find_audit(args.env, "probe-results.json")
        if audit_dir is None:
            return missing_audit(args.env, "probe-results.json")
        if args.command == "repro":
            if args.script and not (audit_dir / args.script).is_file():
                print(f"falsepass: no {args.script} in {audit_dir}", file=sys.stderr)
                return 2
            try:
                path = audit_dir / args.script if args.script else repro.generate(audit_dir, args.probe, args.per_group)
            except RuntimeError as stop:  # no loader, nothing failing: a message, not a traceback
                print(f"falsepass: {stop}", file=sys.stderr)
                return 1
            if args.run:
                from falsepass import verify

                result = verify.twice(audit_dir, Path(path).name)
                print(json.dumps(result, indent=1) if args.json else verify.describe_twice(result))
                return 0 if result.get("identical") else 1
            env = tomllib.loads((audit_dir / "env.toml").read_text())["env"]
            command = ["uv", "run", "--isolated", "--no-project", "--python", env.get("python", "3.12"),
                       *install_args(env), "python", str(Path(path).resolve())]
            print(f"wrote {path} — run it twice from a clean virtualenv and compare stdout (or use --run):\n  {shlex.join(command)}")
        else:
            print(f"wrote {draft.draft(audit_dir)} — candidates only, review before reporting")
        return 0

    if args.command == "results":
        from falsepass import report

        audit_dir = find_audit(args.env, "probe-results.json")
        if audit_dir is None:
            return missing_audit(args.env, "probe-results.json")
        stored = audit_dir / "completions.json"
        completions = json.loads(stored.read_text()) if stored.is_file() else {}
        if args.completion:
            if args.completion not in completions:
                print(f"falsepass: no completion {args.completion!r} in {stored}", file=sys.stderr)
                return 1
            text = completions[args.completion]
            print(text if isinstance(text, str) else json.dumps(text, indent=1))
            return 0
        payload = json.loads((audit_dir / "probe-results.json").read_text())
        print(json.dumps(report.digest(payload, completions=completions), indent=1) if args.json else report.summary(payload))
        return 0

    if args.command == "demo":
        return demo(args.dir)

    if args.command == "mcp":
        from falsepass import mcp_server

        return mcp_server.main()

    if args.command == "overview":
        from falsepass import draft

        table = draft.overview(Path("audits"))
        if args.output:
            args.output.write_text(table)
        print(table)
        return 0

    if args.command == "discover" and args.hub == "hf":
        from falsepass import hub

        spaces = hub.discover_spaces(args.days, args.limit)
        if args.output:
            args.output.write_text(json.dumps(spaces, indent=1))
        print(f"{'likes':>5} {'updated':10} init target / tags")
        for space in spaces:
            print(f"{space['likes']:>5} {space['updated']:10} {space['init']:45} {', '.join(space['tags'])}")
        print(f"{len(spaces)} OpenEnv Spaces updated in the last {args.days} days (public listing; `falsepass init <target>` to audit one).")
        return 0

    if args.command == "discover":
        from falsepass import hub

        found = hub.discover(args.days, args.min_releases, args.limit, args.min_stars)
        if args.output:
            args.output.write_text(json.dumps(found, indent=1))
        print(f"{'stars':>5} {'updated':10} {'rel':>3} {'gen':3} {'turns':11} {'auto':4} needs / environment")
        for e in found:
            if "error" in e:
                print(f"{e['stars']:>5} {e['updated']:10}   ?  ?  ?           no   {e['id']}  ({e['error']})")
                continue
            mark = "yes" if e["automatic"] and e["active"] else "no"
            needs = ",".join(e["needs"]) or "-"
            print(f"{e['stars']:>5} {e['updated']:10} {e['releases']:>3} {e['generation']:3} {e['turns']:11} {mark:4} {needs:22} {e['id']}")
        ready = sum(1 for e in found if e.get("automatic") and e.get("active"))
        print(f"{len(found)} inspected; {ready} maintained and auditable without an adapter.")
        return 0

    if args.command == "scan":
        from falsepass import static

        hints = static.scan(args.path)
        if args.json:
            print(json.dumps([h.to_dict() for h in hints], indent=1))
            return 0
        for h in hints:
            print(f"{h.file}:{h.line}: {h.rule} [{h.where}] {static.RULES[h.rule][1]}\n    {h.evidence}")
        print(f"{len(hints)} hint(s). Hints are reasons to look; run `falsepass audit` to confirm them.")
        return 0

    if args.command == "init":
        return scaffold(args)

    if args.command == "upstream":
        from falsepass import upstream

        audit_dir = find_audit(args.env)
        if audit_dir is None:
            return missing_audit(args.env)
        print(upstream.describe(upstream.locate(audit_dir, repo=args.repo, limit=args.limit)))
        return 0

    if args.command == "verify-fix":
        from falsepass import verify

        audit_dir = find_audit(args.env)
        if audit_dir is None:
            return missing_audit(args.env)
        source = args.source.resolve()
        script = args.script or verify.default_script(audit_dir)
        if not (audit_dir / script).is_file():
            print(f"falsepass: no {script} in {audit_dir}; write one with `falsepass repro {args.env}` "
                  "or name yours with --script", file=sys.stderr)
            return 2
        if not source.is_dir():
            print(f"falsepass: --source {source} is not a folder", file=sys.stderr)
            return 2
        if args.print_command:
            print(shlex.join(verify.command_for(audit_dir, script, source)))
            return 0
        if not (audit_dir / (Path(script).stem + ".output.txt")).is_file():
            stored, stderr, code = verify.baseline(audit_dir, script)
            if code != 0:
                print(f"falsepass: the unpatched baseline run exited {code}:\n{stderr}", file=sys.stderr)
                return 1
            print(f"falsepass: no stored output yet; ran the unpatched {script} once and stored it as {stored}",
                  file=sys.stderr)
        found, stderr, code = verify.run(audit_dir, source, script)
        if code != 0:
            print(f"falsepass: the patched run exited {code}:\n{stderr}", file=sys.stderr)
            return 1
        report = [verify.describe(found)]
        if args.known_good:
            counts = {}
            for label, patch in (("release", None), ("patched", source)):
                extra = ["--probe", "known-good", "--tasks", args.known_good, "--json", "--out", str(audit_dir / "reach" / f"known-good-{label}")]
                done = subprocess.run(source_command(audit_dir, extra, "falsepass.reach", patch), capture_output=True, text=True, check=False)
                if done.returncode != 0:
                    print(f"falsepass: scoring the correct answers ({label}) exited {done.returncode}:\n{done.stderr[-2000:]}", file=sys.stderr)
                    return 1
                counts[label] = json.loads(done.stdout)
            report.append(verify.describe_known_good(counts["release"], counts["patched"]))
        print("\n".join(report))
        (audit_dir / "verify-fix.txt").write_text("\n".join(report) + "\n")  # for the MCP tool
        return 0

    audit_dir = find_audit(args.env)
    if audit_dir is None:
        return missing_audit(args.env)
    if args.command == "reach":
        if not args.probe and not args.template:
            print("falsepass: reach needs --probe CODE[/VARIANT] or --template TEXT", file=sys.stderr)
            return 2
        extra = [*extra, *(a for code in args.probe for a in ("--probe", code)),
                 *(a for text in args.template for a in ("--template", text)), "--expect", args.expect, "--tasks", args.tasks, "--seed", args.seed,
                 *(["--jobs", args.jobs] if args.jobs else []), *(["--json"] if args.json else [])]  # fmt: skip
    module = {"rollout": "falsepass.rollout", "propose": "falsepass.propose", "container": "falsepass.container_probe",
              "reach": "falsepass.reach"}.get(
        args.command, "falsepass.runner"
    )
    source = args.source.resolve() if args.source else None
    if args.command == "reach" and args.out:
        extra += ["--out", str(Path(args.out).resolve())]
    command = source_command(audit_dir, extra, module, source)
    if args.print_command:
        print(shlex.join(command))
        return 0
    sys.stdout.flush()
    return subprocess.call(command)


if __name__ == "__main__":
    sys.exit(main())
