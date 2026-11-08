"""OpenEnv environments: init without openenv installed, grading with it (CI installs openenv for this file)."""

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest

from falsepass import cli, openenv_support, static

TOY = Path(__file__).resolve().parent.parent / "examples" / "openenv-toy"
TOOL_TOY = TOY.parent / "openenv-tool-toy"


def test_an_openenv_folder_is_recognised_and_written_as_openenv(tmp_path: Path, monkeypatch):
    from falsepass import hub

    monkeypatch.setattr(hub, "pypi_version", lambda package: "0.9.0")
    assert openenv_support.uses_openenv(TOY) and not openenv_support.uses_openenv(TOY.parent / "hud-toy")
    assert openenv_support.manifest(TOY) == {"name": "arith_env", "version": "0.1.0", "app": "server.app:app"}
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", str(TOY), "--answers", str(TOY / "answers.json")]) == 0
    audit = tmp_path / "audits" / "arith-env"
    env = tomllib.loads((audit / "env.toml").read_text())["env"]
    assert env["framework"] == "openenv" and env["task"] == str(TOY) and env["app"] == "server.app:app"
    assert env["openenv"] == "openenv==0.9.0" and "fastapi>=0.115.0" in env["extra"] and env["answers"] == str(TOY / "answers.json")
    args = cli.install_args(env)
    assert args[:2] == ["--with", "openenv==0.9.0"] and str(TOY) not in args  # the folder is imported from its path, not installed
    patched = tmp_path / "patched"
    assert cli.source_command(audit, [], "falsepass.runner", patched)[:3] == ["env", f"FALSEPASS_AUDIT_SOURCE={patched}",
                                                                               f"FALSEPASS_SOURCE={patched}"]
    assert cli.main(["init", str(TOY)]) == 0  # the same target again: nothing to do, exit 0


def test_community_manifests_name_the_app_in_other_ways(tmp_path: Path, monkeypatch):
    from falsepass import hub

    monkeypatch.setattr(hub, "pypi_version", lambda package: "0.9.0")
    (tmp_path / "openenv.yaml").write_text("name: claimwatch\nopenenv:\n  entry_points:\n    server: api.app:app\n    client: client:C\n")
    assert openenv_support.manifest(tmp_path)["app"] == "api.app:app"
    (tmp_path / "openenv.yaml").write_text('version: "1.0"\nproject:\n  name: "support"\nenvironment:\n  entrypoint: "environment:SupportEnv"\n')
    assert openenv_support.manifest(tmp_path) == {"name": "support", "version": "1.0"}  # an environment class, not an app
    (tmp_path / "requirements.txt").write_bytes("# deps\nopenenv-core==0.2.3\nfastapi\n-e .\n".encode("utf-16"))  # a Windows editor
    info = openenv_support.describe(tmp_path)
    assert info["openenv"] == "openenv==0.9.0" and info["extra"] == ["fastapi"] and info["id"] == "local/support"
    assert info["declared"] == "openenv-core==0.2.3" and info["app"] == "server.app:app"
    (tmp_path / "requirements.txt").write_text("openenv==0.8.0\n")
    assert openenv_support.describe(tmp_path)["openenv"] == "openenv==0.8.0"  # the folder's own exact pin is kept


def test_observation_text_references_and_tasks_file(tmp_path: Path):
    text = openenv_support.observation_text
    assert text({"question": "2+2?", "instructions": "number only", "status": "ready", "metadata": {"hint": "x"}}) == "2+2?\n\nnumber only"
    assert text({"status": "ready", "feedback": "", "summary": "Echo ready!"}) == "Echo ready!"
    assert text({"post": {"text": "spam", "author_id": "u1"}, "episode_id": "e"}) == "spam"
    reference = openenv_support.reference_for
    assert reference({"id": "t-1", "answer": 42}, {}, "test-1", 0) == "42"
    assert reference({"id": "t-1"}, {"t-1": "x"}, "test-1", 0) == "x" and reference({}, ["a", "b"], "seed-1", 1) == "b"
    assert reference({"id": "t-1", "prompt": "q"}, {}, "test-1", 0) == ""
    tasks = tmp_path / "tasks.json"
    tasks.write_text('[{"task_id": 1, "answer": "approve"}, {"task_id": 2, "key": "hard"}, 3]')
    assert openenv_support.read_tasks(tasks) == [("task-0", {"task_id": 1, "answer": "approve"}), ("hard", {"task_id": 2}), ("task-2", {"task_id": 3})]


def test_the_action_plan_fills_what_a_reply_cannot_say():
    pydantic = pytest.importorskip("pydantic")
    from typing import Literal, Optional

    class Decision(pydantic.BaseModel):
        claim_id: str
        decision: Literal["approve", "reject", "escalate"]
        urgent: bool = False
        confidence: float = pydantic.Field(0.5, ge=0.0, le=1.0)
        issues: list[str]
        rationale: Optional[str] = None

    plan = openenv_support.action_plan(Decision)
    assert plan["field"] == "rationale" and plan["blockers"] == []
    assert plan["fills"] == {"claim_id": "", "decision": "approve", "issues": []}
    assert plan["choices"] == {"decision": ["approve", "reject", "escalate"], "urgent": [False, True]}
    assert plan["reports"] == {"confidence": (0.0, 1.0)}

    class Free(pydantic.BaseModel):
        answer: str

    assert openenv_support.action_plan(Free) == {"field": "answer", "fills": {}, "choices": {}, "reports": {}, "blockers": [], "decision_text": []}

    class Opaque(pydantic.BaseModel):
        payload: dict[str, int]
        when: object

    assert openenv_support.action_plan(Opaque)["blockers"] == ["when"]


def test_the_static_scan_points_at_the_toy_defect():
    hints = static.scan(TOY)
    assert [(h.rule, h.file.split("/")[-1]) for h in hints] == [("reference_substring_match", "arith_environment.py")]


def _fresh_imports(monkeypatch, folder: Path):
    """One process audits one folder; the tests load several: forget the previous one's modules."""
    for name in [n for n in sys.modules if n.split(".")[0] in ("server", "models", "client", "aliased_env", "dec_env_app", "guess_env_app")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if not p.startswith(str(TOY.parent))])
    monkeypatch.chdir(os.getcwd())


def test_the_toy_environment_is_graded_by_its_step(monkeypatch):
    pytest.importorskip("openenv")
    _fresh_imports(monkeypatch, TOY)
    answers = json.loads((TOY / "answers.json").read_text())
    scorer, pool, found = openenv_support.load(str(TOY), 100, str(TOY / "answers.json"))
    assert [e.key for e in pool[:2]] == ["test-0", "test-1"] and len(pool) == 12  # the evaluation split first
    assert pool[0].prompt.startswith("What is 12 + 19?") and pool[0].answer == answers["test-0"]
    assert found["field"] == "answer" and found["task_api"] and found["env_class"].endswith(":ArithEnvironment")
    assert found["alias"] == "" and found["factory"] == "" and found["resets"]["test-1"] == {"split": "test", "index": 1}

    def score(text):
        return asyncio.run(scorer.score(pool[1], text))

    try:
        assert score("42").reward == 1.0 and score("41").reward == 0.0 and score("").reward == 0.0
        assert score("41 or 42").reward == 1.0  # the planted defect
        assert score('{"answer": "42"}').reward == 1.0  # a JSON object naming the action's fields is the action
        assert score("42").metrics == {} and score("42").info == {"done": True}
    finally:
        asyncio.run(scorer.close())
    scorer, pool, _ = openenv_support.load(str(TOY), 100, None)
    assert all(e.answer == "" for e in pool)  # the Task API does not expose the answers: answer-free
    asyncio.run(scorer.close())


DECISION_ENV = '''
import random
from typing import Literal, Optional

from openenv.core.env_server.http_server import create_app
from openenv.core.env_server.interfaces import Environment
from openenv.core.env_server.types import Action, Observation, State
from pydantic import Field

TICKETS = [("refund please", "billing"), ("app crashes", "tech"), ("where is my order", "shipping")]


class TriageAction(Action):
    category: Literal["billing", "tech", "shipping"]
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    reply: Optional[str] = None


class TriageObservation(Observation):
    ticket: str = ""


class TriageEnvironment(Environment):
    def __init__(self):
        super().__init__()
        self._state = State(episode_id="e", step_count=0)
        self._rng = random.Random(7)
        self._truth = ""

    def reset(self, seed=None, episode_id=None, **kwargs):
        ticket, self._truth = self._rng.choice(TICKETS)  # a random ticket per reset, seed ignored
        return TriageObservation(ticket=ticket, done=False, reward=None)

    def step(self, action, timeout_s=None, **kwargs):
        right = action.category == self._truth
        return TriageObservation(done=True, reward=(1.0 if right else 0.0) + 0.1 * action.confidence)  # trusts confidence

    @property
    def state(self):
        return self._state


def create_triage_environment():
    return TriageEnvironment()


app = create_app(create_triage_environment, TriageAction, TriageObservation, env_name="dec_env")
'''


def test_decision_actions_random_tasks_and_the_openenv_probes(tmp_path: Path, monkeypatch):
    pytest.importorskip("openenv")
    folder = tmp_path / "dec_env"
    folder.mkdir()
    (folder / "openenv.yaml").write_text("spec_version: 1\nname: dec_env\napp: dec_env_app:app\n")
    (folder / "dec_env_app.py").write_text(DECISION_ENV)
    _fresh_imports(monkeypatch, folder)
    from falsepass.auto import AutoAdapter

    adapter = AutoAdapter({"id": "local/dec-env", "framework": "openenv", "task": str(folder), "_audit_dir": str(tmp_path)})
    scorer, examples = adapter.load(10, 0)
    assert len(adapter.pool) == 3 and adapter.answer_free and any("tasks.json" not in n for n in adapter.load_notes)
    assert adapter.loader["factory"].endswith(":create_triage_environment") and adapter.loader["fills"] == {"category": "billing"}
    assert scorer.plan["choices"] == {"category": ["billing", "tech", "shipping"]} and scorer.plan["reports"] == {"confidence": (0.0, 1.0)}
    billing = next(e for e in adapter.pool if e.prompt == "refund please")
    try:
        # a random ticket per reset: the scorer resets until the recorded one comes back, so the grade is for this task
        assert asyncio.run(scorer.score(billing, '{"category": "billing"}')).reward == 1.05
        assert asyncio.run(scorer.score(billing, "some text")).reward == 1.05  # text goes to `reply`; category filled with a constant
        rows = asyncio.run(openenv_support.o1_fixed_choice(adapter, scorer, billing))
        assert [(r.variant, r.score, r.passed) for r in rows] == [("category=billing", 1.05, True), ("category=tech", 0.05, True), ("category=shipping", 0.05, True)]
        assert all(r.answer_free and r.expected.value == "lazy" for r in rows)
        (report,) = asyncio.run(openenv_support.o2_self_report(adapter, scorer, billing))
        assert not report.passed and "confidence=1.0 scores 1.1" in report.detail and report.variant == "confidence"
    finally:
        asyncio.run(scorer.close())
    # tasks.json pins the tasks and may carry answers; the answers are JSON actions here
    (tmp_path / "tasks.json").write_text(json.dumps([{"key": "crash", "seed": 1, "answer": {"category": "tech"}}]))
    adapter = AutoAdapter({"id": "local/dec-env", "framework": "openenv", "task": str(folder), "_audit_dir": str(tmp_path)})
    scorer, examples = adapter.load(10, 0)
    assert [e.key for e in examples] == ["crash"] and examples[0].answer == '{"category": "tech"}' and adapter.json_references
    assert set(adapter.equivalent_forms(examples[0])) == {"surrounding_whitespace", "crlf_line_endings", "padding_inside_slot"}
    asyncio.run(scorer.close())


def test_a_space_laid_out_as_its_own_package_imports_under_an_alias(tmp_path: Path, monkeypatch):
    pytest.importorskip("openenv")
    folder = tmp_path / "aliased_env"
    (folder / "server").mkdir(parents=True)
    (folder / "openenv.yaml").write_text("spec_version: 1\nname: aliased_env\napp: server.app:app\n")
    (folder / "models.py").write_text(textwrap.dedent('''
        from openenv.core.env_server.types import Action, Observation

        class EchoAction(Action):
            answer: str

        class EchoObservation(Observation):
            prompt: str = ""
    '''))
    (folder / "server" / "__init__.py").write_text("")
    (folder / "server" / "app.py").write_text(textwrap.dedent('''
        from openenv.core.env_server.http_server import create_app
        from openenv.core.env_server.interfaces import Environment
        from openenv.core.env_server.types import State

        from aliased_env.models import EchoAction, EchoObservation  # absolute import of the package itself

        class EchoEnvironment(Environment):
            def __init__(self):
                super().__init__()
                self._state = State(episode_id="e", step_count=0)

            def reset(self, seed=None, episode_id=None, task_id: int = 0, **kwargs):
                return EchoObservation(prompt=f"say {task_id}", done=False)

            def step(self, action, timeout_s=None, **kwargs):
                return EchoObservation(done=True, reward=1.0 if action.answer.strip() == "0" else 0.0)

            @property
            def state(self):
                return self._state

        app = create_app(EchoEnvironment, EchoAction, EchoObservation, env_name="aliased_env")
    '''))
    _fresh_imports(monkeypatch, folder)
    scorer, pool, found = openenv_support.load(str(folder), 20, None)
    assert found["alias"] == "aliased_env" and found["env_class"] == "aliased_env.server.app:EchoEnvironment"
    assert [e.key for e in pool] == ["reset-0"] and pool[0].prompt == "say 0"  # one task without tasks.json
    assert any("task_id" in note and "tasks.json" in note for note in found["notes"])
    assert asyncio.run(scorer.score(pool[0], "0")).reward == 1.0
    asyncio.run(scorer.close())


def test_init_names_the_classes_and_drops_requirements(tmp_path: Path, monkeypatch):
    from falsepass import hub

    monkeypatch.setattr(hub, "pypi_version", lambda package: "0.9.0")
    (tmp_path / "space").mkdir()
    (tmp_path / "space" / "openenv.yaml").write_text("spec_version: 1\nname: pinned\napp: server.app:app\n")
    (tmp_path / "space" / "requirements.txt").write_text("openenv-core==0.2.3\nfastapi==0.115.2\nstarlette==0.40.0\nnumpy==1.26.4\nrequests\n")
    info = openenv_support.describe(tmp_path / "space")
    assert info["extra"] == ["fastapi", "starlette", "numpy==1.26.4", "requests"]  # openenv's own dependencies lose their pins
    assert "pins relaxed to openenv's own: fastapi==0.115.2, starlette==0.40.0" in info["declared"]
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "space", "--without", "numpy", "--env-class", "env.core:PinnedEnv", "--action-class", "env.models:PinnedAction"]) == 0
    env = tomllib.loads((tmp_path / "audits/pinned/env.toml").read_text())["env"]
    assert env["extra"] == ["fastapi", "starlette", "requests"]
    assert env["env_class"] == "env.core:PinnedEnv" and env["action_class"] == "env.models:PinnedAction"
    assert cli.install_args(env) == ["--with", "openenv==0.9.0", "--with", "fastapi", "--with", "starlette", "--with", "requests"]


def test_init_clones_a_space_under_dir_and_names_lfs_pointers(tmp_path: Path, monkeypatch, capsys):
    from falsepass import hub

    monkeypatch.setattr(hub, "pypi_version", lambda package: "0.9.0")
    monkeypatch.chdir(tmp_path)

    def fake_clone(command, **kwargs):
        if command[:2] == ["git", "-C"]:  # the commit recorded after the clone
            return subprocess.CompletedProcess(command, 0, "0123abcd\n", "")
        assert command[:4] == ["git", "clone", "--depth", "1"] and command[4] == "https://huggingface.co/spaces/owner/Some_Space"
        clone = Path(command[5])
        (clone / "data").mkdir(parents=True)
        (clone / "openenv.yaml").write_text("spec_version: 1\nname: some_space\napp: server.app:app\n")
        (clone / "requirements.txt").write_text("openenv-core==0.2.3\n")
        (clone / "data" / "tasks.jsonl.gz").write_bytes(
            b"version https://git-lfs.github.com/spec/v1\noid sha256:0123\nsize 4242\n")
        (clone / "data" / "small.json").write_text("{}")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(cli.subprocess, "run", fake_clone)
    asked = []

    def fake_fetch(folder, owner, repo):  # the object could not be fetched: the pointer stays and is named
        asked.append((folder.name, owner, repo))
        return [], ["data/tasks.jsonl.gz"]

    monkeypatch.setattr(openenv_support, "fetch_lfs_pointers", fake_fetch)
    assert cli.main(["init", "hf:owner/Some_Space", "--dir", str(tmp_path / "work")]) == 0
    assert (tmp_path / "work" / "some-space" / "space" / "openenv.yaml").is_file()  # the clone lands under --dir, not ./audits
    assert not (tmp_path / "audits").exists()
    env = tomllib.loads((tmp_path / "work" / "some-space" / "env.toml").read_text())["env"]
    assert env["id"] == "hf/owner/Some_Space"
    err = capsys.readouterr().err
    assert asked == [("space", "owner", "Some_Space")]
    assert "1 file(s) in the Space are still Git LFS pointers" in err and "data/tasks.jsonl.gz" in err and "lfs pull" in err
    assert openenv_support.lfs_pointers(tmp_path / "work" / "some-space" / "space") == ["data/tasks.jsonl.gz"]
    # --policies on a Space: the OpenEnv policy skeleton (episodes through reset() and step())
    assert cli.main(["init", "hf:owner/Some_Space", "--dir", str(tmp_path / "work"), "--force", "--policies"]) == 0
    adapter = (tmp_path / "work" / "some-space" / "adapter.py").read_text()
    assert "open_episodes" in adapter and "import verifiers" not in adapter
    compile(adapter, "adapter.py", "exec")


def test_lfs_pointers_are_fetched_and_checked(tmp_path: Path):
    import hashlib

    space = tmp_path / "space"
    (space / "data").mkdir(parents=True)
    good = b"real gzip bytes"
    (space / "data" / "good.gz").write_bytes(b"version https://git-lfs.github.com/spec/v1\noid sha256:" + hashlib.sha256(good).hexdigest().encode()
                                             + b"\nsize 15\n")
    (space / "data" / "bad.gz").write_bytes(b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"0" * 64 + b"\nsize 3\n")
    urls = []

    def fetch(url):
        urls.append(url)
        return good if url.endswith("good.gz") else b"xyz"

    assert openenv_support.fetch_lfs_pointers(space, "owner", "Some_Space", fetch=fetch) == (["data/good.gz"], ["data/bad.gz"])
    assert urls == ["https://huggingface.co/spaces/owner/Some_Space/resolve/main/data/bad.gz",
                    "https://huggingface.co/spaces/owner/Some_Space/resolve/main/data/good.gz"]
    assert (space / "data" / "good.gz").read_bytes() == good and openenv_support.lfs_pointers(space) == ["data/bad.gz"]
    (space / "openenv.yaml").write_text("name: s\ndeclared_tools:\n  - get_prices\n")
    assert openenv_support.declares_tools(space) and not openenv_support.declares_tools(tmp_path)


def test_decisions_spelled_as_text_are_not_the_free_text_field():
    pydantic = pytest.importorskip("pydantic")
    from typing import Optional

    class Triage(pydantic.BaseModel):
        action_type: str
        cause_explanation: Optional[str] = None
        remediation_steps: Optional[str] = None

    plan = openenv_support.action_plan(Triage)
    assert plan["field"] == "cause_explanation" and plan["fills"] == {"action_type": ""} and plan["decision_text"] == ["action_type"]
    note = openenv_support.describe_plan(Triage, plan)
    assert "a plain reply goes into `cause_explanation`" in note and "action_type look like decisions spelled as text" in note


WRAPPED_APP = '''
from openenv.core.env_server.http_server import create_app
from openenv.core.env_server.interfaces import Environment
from openenv.core.env_server.types import Action, Observation, State
from pydantic import Field


class SumAction(Action):
    answer: str


class SumObservation(Observation):
    question: str = ""
    correct_answer: str = ""


class SumEnvironment(Environment):
    """Reveals the expected answer only after the step, as reasoning tasks do."""

    PROBLEMS = [("1+1", "2"), ("2+2", "4"), ("3+3", "6")]

    def __init__(self):
        super().__init__()
        self._state = State(episode_id="e", step_count=0)
        self._cursor = 0
        self._expected = ""

    def reset(self, seed=None, episode_id=None, **kwargs):
        question, self._expected = self.PROBLEMS[self._cursor % len(self.PROBLEMS)]  # sequential, seed ignored
        self._cursor += 1
        return SumObservation(question=question, done=False)

    def step(self, action, timeout_s=None, **kwargs):
        right = action.answer.strip() == self._expected
        return SumObservation(correct_answer=self._expected, done=True, reward=1.0 if right else 0.0)

    @property
    def state(self):
        return self._state


def _build_app():
    """A wrapper around create_app, as some Spaces have: not the environment factory."""
    return create_app(SumEnvironment, SumAction, SumObservation, env_name="sum_env")


app = _build_app()
'''


def test_the_served_app_names_the_environment_and_answers_revealed_after_a_step_are_read(tmp_path: Path, monkeypatch):
    pytest.importorskip("openenv")
    folder = tmp_path / "sum_env"
    folder.mkdir()
    (folder / "openenv.yaml").write_text("spec_version: 1\nname: sum_env\napp: sum_env_app:app\n")
    (folder / "sum_env_app.py").write_text(WRAPPED_APP)
    _fresh_imports(monkeypatch, folder)
    scorer, pool, found = openenv_support.load(str(folder), 20, None)
    assert found["env_class"].endswith(":SumEnvironment") and found["factory"] == ""  # _build_app() was not taken for the factory
    assert [(e.prompt, e.answer) for e in pool] == [("1+1", "2"), ("2+2", "4"), ("3+3", "6")]
    assert all(e.key.startswith("task-") for e in pool) and any("does not pick its task" in n for n in found["notes"])
    assert any(note.startswith("reference answers read from what the environment reveals after a step (3 of 3") for note in found["notes"])
    assert found["retries"] >= 150
    try:
        assert asyncio.run(scorer.score(pool[2], "6")).reward == 1.0  # a sequential environment: reset until task 3 comes round
        assert asyncio.run(scorer.score(pool[0], "6")).reward == 0.0
    finally:
        asyncio.run(scorer.close())


def _policy(name, act):
    from falsepass.model import Policy

    return Policy(name, act, wants_tools=True)


def _call(name, **arguments):
    return {"content": "", "tool_calls": [{"name": name, "arguments": arguments}]}


def test_init_policies_writes_the_openenv_skeleton_and_manual_does_too(tmp_path: Path, monkeypatch, capsys):
    from falsepass import hub

    monkeypatch.setattr(hub, "pypi_version", lambda package: "0.9.0")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", str(TOOL_TOY), "--policies"]) == 0
    adapter = (tmp_path / "audits" / "refund-desk" / "adapter.py").read_text()
    assert "open_episodes(HERE, n, seed, REWARD)" in adapter and "import verifiers" not in adapter
    compile(adapter, "adapter.py", "exec")
    assert "fill in solver() and exploits()" in capsys.readouterr().out
    assert cli.main(["init", str(TOY), "--manual", "--name", "arith-manual"]) == 0  # --manual: a one-reply adapter is a one-step policy
    assert "open_episodes" in (tmp_path / "audits" / "arith-manual" / "adapter.py").read_text()


def test_tool_episodes_are_played_through_the_environment_and_replayed(tmp_path: Path, monkeypatch):
    pytest.importorskip("openenv")
    pytest.importorskip("fastmcp")
    from falsepass import probes, repro

    _fresh_imports(monkeypatch, TOOL_TOY)
    audit = tmp_path / "audit"
    audit.mkdir()
    (audit / "env.toml").write_text(f'[env]\nid = "local/refund-desk"\nframework = "openenv"\ntask = "{TOOL_TOY}"\n')
    scorer, examples, found = openenv_support.open_episodes(audit, 6, 0)
    assert isinstance(scorer, openenv_support.OpenEnvEpisodeScorer) and len(examples) == 6
    assert found["loader"]["kind"] == "openenv-episode" and found["loader"]["action_class"].endswith(":CallToolAction")
    assert "played through MCP tool calls" in found["notes"]
    ex = next(e for e in examples if e.key == "test-1")
    assert "A-1004" in ex.prompt and ex.raw["reset"] == {"split": "test", "index": 1}

    def solver(messages, tools):
        turn = sum(1 for m in messages if m["role"] == "assistant")
        if turn == 0:
            assert [t["name"] for t in tools] == ["finish", "get_order", "issue_refund", "list_orders"] or \
                sorted(t["name"] for t in tools) == ["finish", "get_order", "issue_refund", "list_orders"]
            assert messages == [{"role": "user", "content": ex.prompt}]
            return _call("get_order", order_id="A-1004")
        if turn == 1:
            price = json.loads(messages[-1]["content"])["price"]
            assert messages[-1]["role"] == "tool" and messages[-1]["name"] == "get_order"
            return _call("issue_refund", order_id="A-1004", amount=price)
        return _call("finish") if turn == 2 else "Done."

    def refund_everything(messages, tools):
        turn = sum(1 for m in messages if m["role"] == "assistant")
        if turn == 0:
            return _call("list_orders")
        orders = json.loads([m for m in messages if m["role"] == "tool"][0]["content"])
        return _call("issue_refund", order_id=orders[turn - 1], amount=1e9) if turn <= len(orders) else _call("finish")

    try:
        good = asyncio.run(scorer.score(ex, _policy("solver", solver)))
        assert good.reward == 1.0 and good.errors == [] and good.info["stop"] == "done" and good.info["steps"] == 3
        assert good.info["turns"] == 3 and good.info["done"] and "[tool call finish({})]" in good.info["transcript"][-2]["content"]
        bad = asyncio.run(scorer.score(ex, _policy("refund_everything", refund_everything)))
        assert bad.reward == 1.0 and bad.info["steps"] == 8  # the planted defect: a policy that never read the ticket is paid
        idle = asyncio.run(scorer.score(ex, "Done."))  # a text reply ends the episode in a tool environment
        assert idle.reward == 0.0 and idle.info["stop"] == "policy ended its turn without a tool call" and idle.info["steps"] == 0
        unknown = asyncio.run(scorer.score(ex, [{"role": "assistant", "content": "", "tool_calls": [{"name": "nope", "arguments": {}}]},
                                                {"role": "assistant", "content": "Done."}]))  # fmt: skip
        assert unknown.reward == 0.0 and unknown.info["transcript"][2]["role"] == "tool"
        assert unknown.info["transcript"][2]["content"].startswith("error") or "nope" in unknown.info["transcript"][2]["content"]
        again = asyncio.run(scorer.score(ex, _policy("solver", solver)))
        assert again.reward == 1.0  # nothing carried over from the previous episodes
        rows = asyncio.run(probes.t6_tool_policies(type("A", (), {"tool_mode": True, "bad_tolerance": 0.0, "max_reward": 1.0,
                                                                   "task_max": {}, "format_floor": 0.0})(), scorer, ex))  # fmt: skip
        assert rows and all(r.score == 0.0 and r.passed for r in rows)
    finally:
        asyncio.run(scorer.close())
    # repro: the stored script, replayed through the environment by a script that imports openenv only
    script = bad.info["script"]
    assert script[0]["tool_calls"][0]["name"] == "list_orders"
    payload = {"meta": {"loader": found["loader"], "falsepass": "test", "date": "today"},
               "results": [{"probe": "X1", "name": "policy", "variant": "refund_everything", "example": ex.key, "expected": "zero",
                            "passed": False, "completion_id": "c1", "score": 1.0}]}  # fmt: skip
    (audit / "probe-results.json").write_text(json.dumps(payload))
    (audit / "completions.json").write_text(json.dumps({"c1": {"policy": "x", "script": script, "transcript": []}}))
    path = repro.generate(audit)
    text = path.read_text()
    compile(text, str(path), "exec")
    assert "FIELD = json.loads(" in text and 'REWARD = json.loads("\\"sum\\"")' in text
    run = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, timeout=120, cwd=tmp_path)
    assert run.returncode == 0, run.stderr[-2000:]
    assert run.stdout.split()[0] == "1.000000" and "X1 policy/refund_everything on test-1" in run.stdout


GUESS_APP = '''
from openenv.core.env_server.http_server import create_app
from openenv.core.env_server.interfaces import Environment
from openenv.core.env_server.types import Action, Observation, State


class GuessAction(Action):
    guess: str


class GuessObservation(Observation):
    prompt: str = ""
    feedback: str = ""


class GuessEnvironment(Environment):
    """Guess a number in three tries; 1.0 on the right guess, a 0.1 consolation for each wrong one."""

    def __init__(self):
        super().__init__()
        self._state = State(episode_id="e", step_count=0)
        self._secret, self._tries = 0, 0

    def reset(self, seed=None, episode_id=None, **kwargs):
        self._secret, self._tries = 3 + (seed or 0) % 5, 0
        return GuessObservation(prompt=f"Guess my number between 1 and 9 (game {seed or 0}).", done=False, reward=None)

    def step(self, action, timeout_s=None, **kwargs):
        self._tries += 1
        right = action.guess.strip() == str(self._secret)
        done = right or self._tries >= 3
        hint = "right" if right else ("higher" if int(action.guess or 0) < self._secret else "lower")
        return GuessObservation(feedback=hint, done=done, reward=1.0 if right else 0.1)

    @property
    def state(self):
        return self._state


app = create_app(GuessEnvironment, GuessAction, GuessObservation, env_name="guess_env")
'''


def test_text_episodes_take_several_steps_and_sum_or_keep_the_last_reward(tmp_path: Path, monkeypatch):
    pytest.importorskip("openenv")
    folder = tmp_path / "guess_env"
    folder.mkdir()
    (folder / "openenv.yaml").write_text("spec_version: 1\nname: guess_env\napp: guess_env_app:app\n")
    (folder / "guess_env_app.py").write_text(GUESS_APP)
    _fresh_imports(monkeypatch, folder)
    scorer, pool, found = openenv_support.load(str(folder), 3, None, episodes=True)
    last, _, _ = openenv_support.load(str(folder), 3, None, episodes=True, reward="last")
    ex = pool[0]  # seed 0: the secret is 3

    def bisect(messages, tools):
        assert tools == []
        heard = [m["content"] for m in messages if m["role"] == "user"]
        assert heard[0].startswith("Guess my number")
        return ["5", "2", "3"][len(heard) - 1]

    try:
        played = asyncio.run(scorer.score(ex, _policy("bisect", bisect)))
        assert played.info["steps"] == 3 and played.info["stop"] == "done" and played.reward == pytest.approx(1.2)
        assert [m["content"] for m in played.info["transcript"]][1:4] == ["5", "lower", "2"]
        assert asyncio.run(last.score(ex, _policy("bisect", bisect))).reward == 1.0
        wrong = asyncio.run(scorer.score(ex, "9"))  # a fixed script repeats its last message until the environment stops
        assert wrong.info["steps"] == 3 and wrong.reward == pytest.approx(0.3)
        assert asyncio.run(scorer.score(ex, [{"role": "assistant", **_call("guess", guess="3")}])).info["stop"] == "tool call in an environment without tools"
    finally:
        asyncio.run(scorer.close())
        asyncio.run(last.close())
    with pytest.raises(ValueError, match="sum"):
        openenv_support.OpenEnvEpisodeScorer(None, object, {}, reward="mean")


def test_a_step_override_the_server_never_calls_is_named(monkeypatch):
    pytest.importorskip("openenv")
    pytest.importorskip("fastmcp")
    _fresh_imports(monkeypatch, TOOL_TOY)
    monkeypatch.syspath_prepend(str(TOOL_TOY))
    from server.refund_environment import RefundEnvironment  # overrides step() and step_async(): fine

    class StepOnly(RefundEnvironment.__mro__[1]):  # an MCPEnvironment subclass that puts its reward in step() alone
        def step(self, action, timeout_s=None, **kwargs):
            return None

        def _step_impl(self, action, timeout_s=None, **kwargs):
            return None

    assert openenv_support.served_step_note(RefundEnvironment) == ""
    note = openenv_support.served_step_note(StepOnly)
    assert "overrides step() but inherits step_async() from MCPEnvironment" in note and "never runs" in note


def test_tool_actions_with_their_own_type_field_are_filled_and_listed():
    pytest.importorskip("openenv")
    from typing import Any, Literal, Optional

    from openenv.core.env_server.types import Action

    class MCPAction(Action):  # a Space's own tool action: one class for listing and calling
        action_type: Literal["ListToolsAction", "ToolCallAction"]
        tool_name: Optional[str] = None
        arguments: Optional[dict[str, Any]] = None

    assert openenv_support.is_tool_action(MCPAction) and openenv_support.lists_through_action(MCPAction)
    assert openenv_support.tool_fills(MCPAction, "call") == {"action_type": "ToolCallAction"}
    assert openenv_support.tool_fills(MCPAction, "list") == {"action_type": "ListToolsAction"}
    assert openenv_support.tool_fills(__import__("openenv.core.env_server.mcp_types", fromlist=["x"]).CallToolAction, "call") == {}


def test_tool_spaces_are_recognised_from_their_source_and_aliases_avoid_installed_modules(tmp_path: Path):
    assert openenv_support.declares_tools(TOOL_TOY) and not openenv_support.declares_tools(TOY)
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "app.py").write_text("try:\n    from textarena_env.models import A\nexcept ImportError:\n    from models import A\n")
    assert openenv_support.alias_names(tmp_path, "server.app:app", "json") == ["textarena_env"]  # `json` is installed
    assert openenv_support.alias_names(tmp_path, "server.app:app", "my-env") == ["textarena_env", "my_env"]


def test_a_constant_prompt_is_said(tmp_path: Path, monkeypatch):
    pytest.importorskip("openenv")
    folder = tmp_path / "same_env"
    folder.mkdir()
    (folder / "openenv.yaml").write_text("spec_version: 1\nname: same_env\napp: same_env_app:app\n")
    (folder / "same_env_app.py").write_text(GUESS_APP.replace("(game {seed or 0})", "").replace("guess_env", "same_env"))
    _fresh_imports(monkeypatch, folder)
    monkeypatch.setattr(sys, "modules", {k: v for k, v in sys.modules.items() if not k.startswith("same_env_app")})
    scorer, pool, found = openenv_support.load(str(folder), 10, None, episodes=True)
    assert len(pool) == 1 and any(n.startswith("every reset() showed the same text") for n in found["notes"])
    asyncio.run(scorer.close())
