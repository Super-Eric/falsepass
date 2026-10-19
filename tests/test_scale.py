"""Reference-relative scores: a cost-style reward (maximum 0, no floor) put on a 0-1 scale
per task, from the adapter's floor and reference strategies."""

import json
from pathlib import Path

from falsepass import report, runner

ADAPTER = '''
import re
from falsepass import Adapter, Example, Score

class Cost:
    concurrent_safe = True

    async def score(self, example, completion):
        text = completion if isinstance(completion, str) else "".join(str(m.get("content") or "") for m in completion)
        marks = re.findall(r"^#### (\\S+)\\s*$", text, re.M)
        try:
            x = float(marks[-1])
        except (IndexError, ValueError):
            x = 0.0  # no position: the same as not hedging
        return Score(-10 * (x - float(example.answer)) ** 2, {"tracking_error": (x - float(example.answer)) ** 2})

    async def close(self):
        pass

class Hedge(Adapter):
    name = "hedge"
    scale_floor = "no hedge"
    scale_reference = "the textbook hedge"

    def load(self, n, seed):
        return Cost(), [Example(f"t-{i}", f"Hedge ratio for case {i}?", str(i / 10)) for i in range(n)]

    def render(self, answer, example):
        return f"working\\n#### {answer}"

    def wrong_answer(self, example):
        return str(float(example.answer) + 7)

    def scale_points(self, example):
        return self.render("0", example), self.render(example.answer, example)

ADAPTER = Hedge()
'''


def test_a_cost_reward_is_scored_between_the_floor_and_the_reference(tmp_path: Path):
    folder = tmp_path / "hedge"
    (folder / "src").mkdir(parents=True)
    (folder / "adapter.py").write_text(ADAPTER)
    payload = runner.run(folder, n=6, few=1, jobs=1)
    scale = payload["meta"]["scale"]
    assert scale["tasks"] == 5 and list(scale["excluded"]) == ["t-0"]  # ratio 0: the reference is the floor
    assert "does not beat the floor" in scale["excluded"]["t-0"]
    assert scale["points"]["t-5"] == [-2.5, 0.0]
    rows = payload["results"]
    assert {r["example"] for r in rows if r["probe"] == "P1"} == {f"t-{i}" for i in range(1, 6)}
    p1 = [r for r in rows if r["probe"] == "P1"]
    assert all(r["passed"] and r["score"] == 1.0 and r["metrics"]["raw_reward"] == 0.0 for r in p1)
    wrong = [r for r in rows if r["probe"] == "P6"]
    assert wrong and all(r["passed"] and r["score"] < 0 for r in wrong)  # far worse than not hedging
    assert all(r["score"] == 0.0 for r in rows if r["probe"] == "P3")  # an empty reply holds no position: the floor
    assert payload["meta"]["max_reward"] == 1.0
    text = (folder / "summary.md").read_text()
    assert "Reward scale: reference-relative" in text and "0 = no hedge, 1 = the textbook hedge" in text
    assert "1 left out (first: t-0" in text
    assert "raw_reward" not in {term for term, *_ in report.term_leak(rows, {})}
    assert json.loads((folder / "probe-results.json").read_text())["meta"]["scale"]["tasks"] == 5


def test_without_scale_points_nothing_changes(tmp_path: Path):
    folder = tmp_path / "plain"
    (folder / "src").mkdir(parents=True)
    (folder / "adapter.py").write_text(ADAPTER.replace("    def scale_points(self, example):", "    def unused(self, example):"))
    payload = runner.run(folder, n=3, few=1, jobs=1)
    assert "scale" not in payload["meta"]
    assert not any(r["passed"] for r in payload["results"] if r["probe"] == "P1" and r["example"] != "t-0")  # raw maximum is 0


def test_the_pooled_share_divides_total_gains():
    rows = [{"probe": "X1", "name": "policy", "variant": "half", "example": "a", "score": 3.0, "metrics": {"raw_reward": -0.7}},
            {"probe": "X1", "name": "policy", "variant": "half", "example": "b", "score": 0.5, "metrics": {"raw_reward": -5.0}}]
    points = {"a": [-1.0, -0.9], "b": [-10.0, 0.0]}  # on a, the reference barely beats the floor: the ratio swings to 3
    assert abs(report.pooled_share(rows, points, "X1 policy/half") - (0.3 + 5.0) / (0.1 + 10.0)) < 1e-12
