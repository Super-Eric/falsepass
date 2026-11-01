"""Scoring examples concurrently must give the same results as one at a time, only faster,
and only for scorers that say it is safe."""

import json
import time
from pathlib import Path

import pytest

from falsepass import runner

ADAPTER = '''
import asyncio, pathlib, re
from falsepass import Adapter, Example, Score

class Slow:
    concurrent_safe = {safe}
    in_flight = 0
    most = 0

    async def score(self, example, completion):
        Slow.in_flight += 1
        Slow.most = max(Slow.most, Slow.in_flight)
        await asyncio.sleep(0.002)
        Slow.in_flight -= 1
        text = completion if isinstance(completion, str) else "".join(str(m.get("content") or "") for m in completion)
        marks = re.findall(r"^#### (\\S+)\\s*$", text, re.M)
        return Score(float(bool(marks) and marks[-1] == example.answer))

    async def close(self):
        (pathlib.Path(__file__).parent / "most-in-flight.txt").write_text(str(Slow.most))

class Toy(Adapter):
    name = "toy"

    def load(self, n, seed):
        return Slow(), [Example(f"ex-{{i}}", f"What is {{i}} plus 1?", str(i + 1)) for i in range(n)]

    def render(self, answer, example):
        return f"reasoning\\n#### {{answer}}"

    def wrong_answer(self, example):
        return str(int(example.answer) + 7)

    def with_reference(self, example, reference):
        return Example(example.key, example.prompt, reference)

ADAPTER = Toy()
'''


def audit(tmp_path: Path, name: str, safe: bool) -> Path:
    folder = tmp_path / name
    folder.mkdir()
    (folder / "adapter.py").write_text(ADAPTER.format(safe=safe))
    (folder / "src").mkdir()  # nothing for the static scan
    return folder


def outputs(folder: Path) -> tuple[list, dict | None, str]:
    payload = json.loads((folder / "probe-results.json").read_text())
    return payload["results"], payload["rewardlint"], (folder / "completions.json").read_text()


@pytest.mark.parametrize("safe", [True, False])
def test_jobs_change_speed_not_results(tmp_path: Path, safe: bool):
    folder = audit(tmp_path, f"toy-{safe}", safe)
    one, many = tmp_path / "one", tmp_path / "many"
    started = time.perf_counter()
    runner.run(folder, n=12, few=1, out=one, jobs=1)
    alone = time.perf_counter() - started
    started = time.perf_counter()
    runner.run(folder, n=12, few=1, out=many, jobs=8)
    together = time.perf_counter() - started
    assert outputs(one) == outputs(many)  # same results, order, rewardlint run and stored completions
    assert outputs(many)[1]["headline"]  # the rewardlint corpus did run (scored ahead when safe)
    most = int((folder / "most-in-flight.txt").read_text())
    if safe:
        assert together < alone * 0.6 and most > 1
    else:
        assert most == 1  # a scorer that does not say it is safe is never run concurrently


def test_two_job_starts_on_one_environment_start_one_job(tmp_path):
    """Clients that start a server per call can start two jobs at once: one must win, and
    the other must see it running instead of overwriting its job.json."""
    from concurrent.futures import ProcessPoolExecutor

    from falsepass import mcp_server

    folder = tmp_path / "audits" / "toy"
    folder.mkdir(parents=True)
    (folder / "env.toml").write_text('[env]\nid = "local/toy"\n')
    with ProcessPoolExecutor(2) as pool:
        jobs = list(pool.map(mcp_server._start_audit, ["toy", "toy"], [["-n", "1"], ["-n", "1"]], [str(tmp_path)] * 2,
                             ["audit", "reach"]))  # fmt: skip
    assert jobs[0]["pid"] == jobs[1]["pid"]
    assert json.loads((folder / "job.json").read_text())["pid"] == jobs[0]["pid"]


EPISODES = '''
import asyncio, pathlib
from falsepass import Adapter, Example, Score
from falsepass.model import Expect, Policy
from falsepass.probes import check

class Episodes:
    multi_turn = True
    episodes_independent = True  # each episode builds its own state, as v1 episodes do
    in_flight = 0
    most = 0

    async def score(self, example, completion):
        Episodes.in_flight += 1
        Episodes.most = max(Episodes.most, Episodes.in_flight)
        await asyncio.sleep(0.002)
        Episodes.in_flight -= 1
        return Score(0.0, {}, [], {"turns": 1, "stop": "done", "transcript": [], "script": [], "tools": []})

    async def close(self):
        (pathlib.Path(__file__).parent / "most-in-flight.txt").write_text(str(Episodes.most))

async def own_policy(adapter, scorer, example):
    return [await check("X9", "own", "idle", Expect.ZERO, Policy("idle", lambda messages: "Done."), adapter, scorer, example)]

class Toy(Adapter):
    name = "toy-episodes"
    tool_mode = True  # played with policies, as a v1 tool taskset is
    episode_probes = [own_policy]

    def load(self, n, seed):
        return Episodes(), [Example(f"task-{i}", f"Play round {i}.", "", None) for i in range(n)]

ADAPTER = Toy()
'''


def test_independent_episodes_run_at_once_with_the_same_results(tmp_path: Path):
    """Tool-mode policies (T5, T6, an adapter's own) ran one episode at a time even when the
    scorer builds each episode afresh: a 25-task run of a 10 s episode took 45 minutes."""
    folder = tmp_path / "episodes"
    folder.mkdir()
    (folder / "adapter.py").write_text(EPISODES)
    (folder / "src").mkdir()
    one, many = tmp_path / "one", tmp_path / "many"
    runner.run(folder, n=12, few=1, out=one, jobs=1)
    alone = int((folder / "most-in-flight.txt").read_text())
    runner.run(folder, n=12, few=1, out=many, jobs=8)
    together = int((folder / "most-in-flight.txt").read_text())
    assert outputs(one) == outputs(many)
    assert alone == 1 and together > 1
    rows = json.loads((many / "probe-results.json").read_text())
    assert rows["meta"]["timings"]["jobs"] == 8 and {r["probe"] for r in rows["results"]} >= {"T5", "T6", "X9"}
    assert json.loads((one / "probe-results.json").read_text())["meta"]["timings"]["jobs"] == 1


def test_the_results_say_where_the_time_went(tmp_path: Path, monkeypatch, capsys):
    from falsepass import report

    monkeypatch.setenv(runner.LAUNCHED_AT, str(time.time() - 3))  # the CLI sets it before uv builds the virtualenv
    folder = audit(tmp_path, "timed", True)
    payload = runner.run(folder, n=4, few=1, out=tmp_path / "timed-out", jobs=1)
    timings = payload["meta"]["timings"]
    phases = timings["phases"]
    assert set(phases) >= {"load", "prepare", "probes", "slower_probes", "hygiene", "rewardlint", "close", "static"}
    assert timings["set_up_s"] >= 3 and timings["jobs"] == 1
    assert abs(sum(phases.values()) - timings["total_s"]) < 1.0  # the phases account for the run
    words = report.timing_words(timings)
    assert words.endswith("; one task at a time") and " in all (" in words and "set-up (virtualenv)" in words
    assert f"- **Time:** {words}" in (tmp_path / "timed-out" / "summary.md").read_text()
    assert report.digest(payload)["timings"]["words"] == words
    assert f"falsepass: {words}" in capsys.readouterr().err
    assert [report.duration(s) for s in (8.46, 42, 130, 3600, 3900)] == ["8.5 s", "42 s", "2 min 10 s", "1 h", "1 h 5 min"]


def test_a_running_job_can_be_cancelled(tmp_path: Path, monkeypatch):
    from falsepass import mcp_server

    folder = tmp_path / "audits" / "toy"
    folder.mkdir(parents=True)
    (folder / "env.toml").write_text('[env]\nid = "local/toy"\n')
    slow = tmp_path / "slow.sh"  # stands in for the interpreter: a job that would run for a minute
    slow.write_text("#!/bin/sh\nsleep 60\n")
    slow.chmod(0o755)
    monkeypatch.setattr(mcp_server.sys, "executable", str(slow))
    job = mcp_server._start_audit("toy", ["-n", "1"], str(tmp_path))
    assert job["state"] == "running" and mcp_server._alive(job["pid"])
    stopped = mcp_server._cancel("toy", str(tmp_path))
    assert stopped["status"] == "cancelled" and stopped["command"] == "audit" and not mcp_server._alive(job["pid"])
    assert mcp_server._job_state(str(tmp_path), "toy")["state"] == "cancelled"
    assert mcp_server._await_audit("toy", str(tmp_path), 0)["status"] == "cancelled"
    assert mcp_server._cancel("toy", str(tmp_path))["note"] == "no job was running"
    again = mcp_server._start_audit("toy", ["-n", "1"], str(tmp_path))  # a cancelled job does not block the next
    assert again["state"] == "running" and again["pid"] != job["pid"]
    mcp_server._cancel("toy", str(tmp_path))
