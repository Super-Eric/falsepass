"""P11 in a second process: a reward that depends on str hashing (set order) is stable within
one process and changes in the next, so the reference is scored again with another seed."""

import json
from pathlib import Path

from falsepass import runner

ADAPTER = '''
from falsepass import Adapter, Example, Score

class Scorer:
    async def score(self, example, completion):
        text = completion if isinstance(completion, str) else "".join(str(m.get("content") or "") for m in completion)
        right = text.strip().endswith(example.answer)
        bonus = {bonus}
        return Score(float(right) * (0.5 + bonus))

    async def close(self):
        pass

class Toy(Adapter):
    name = "toy"

    def load(self, n, seed):
        return Scorer(), [Example(f"ex-{{i}}", f"What is {{i}} plus 1?", str(i + 1)) for i in range(n)]

    def render(self, answer, example):
        return f"#### {{answer}}"

    def wrong_answer(self, example):
        return str(int(example.answer) + 7)

ADAPTER = Toy()
'''

BY_HASH = '(hash("first item of a set") % 2**20) / 2**21'  # what list(set(...))[0] depends on


def audit(tmp_path: Path, name: str, bonus: str) -> dict:
    folder = tmp_path / name
    folder.mkdir()
    (folder / "adapter.py").write_text(ADAPTER.format(bonus=bonus))
    (folder / "src").mkdir()
    runner.run(folder, n=4, few=2, jobs=1)
    return json.loads((folder / "probe-results.json").read_text())


def rows(payload: dict) -> list[dict]:
    return [r for r in payload["results"] if r["variant"].endswith("_other_hash_seed")]


def test_a_reward_that_depends_on_str_hashing_is_caught_in_a_second_process(tmp_path: Path):
    payload = audit(tmp_path, "by-hash", BY_HASH)
    same_process = [r for r in payload["results"] if r["probe"] == "P11" and r["variant"] == "reference"]
    assert same_process and all(r["passed"] for r in same_process)  # stable within one process
    found = {r["variant"]: r for r in rows(payload)}
    assert not found["reference_other_hash_seed"]["passed"]
    assert "PYTHONHASHSEED=" in found["reference_other_hash_seed"]["detail"]
    assert found["wrong_in_format_other_hash_seed"]["passed"]  # 0 whatever the order


def test_a_stable_reward_passes_and_a_slow_load_skips_the_check(tmp_path: Path, monkeypatch):
    stable = rows(audit(tmp_path, "stable", "0.5"))
    assert len(stable) == 4 and all(r["passed"] for r in stable)
    monkeypatch.setattr(runner, "HASH_SEED_LOAD_LIMIT", -1.0)
    payload = audit(tmp_path, "slow", BY_HASH)
    assert not rows(payload) and payload["meta"]["hash_seed_check"].startswith("not run: loading took")
