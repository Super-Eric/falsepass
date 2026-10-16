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
