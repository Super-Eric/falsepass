"""OpenEnv environments: init without openenv installed, grading with it (CI installs openenv for this file)."""

import asyncio
import json
import os
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest

from falsepass import cli, openenv_support, static

TOY = Path(__file__).resolve().parent.parent / "examples" / "openenv-toy"


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
    assert cli.source_command(audit, [], "falsepass.runner", patched)[:2] == ["env", f"FALSEPASS_SOURCE={patched}"]
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

    assert openenv_support.action_plan(Free) == {"field": "answer", "fills": {}, "choices": {}, "reports": {}, "blockers": []}

    class Opaque(pydantic.BaseModel):
        payload: dict[str, int]
        when: object

    assert openenv_support.action_plan(Opaque)["blockers"] == ["when"]


def test_the_static_scan_points_at_the_toy_defect():
    hints = static.scan(TOY)
    assert [(h.rule, h.file.split("/")[-1]) for h in hints] == [("reference_substring_match", "arith_environment.py")]


def _fresh_imports(monkeypatch, folder: Path):
    """One process audits one folder; the tests load several: forget the previous one's modules."""
    for name in [n for n in sys.modules if n.split(".")[0] in ("server", "models", "client", "aliased_env", "dec_env_app")]:
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
