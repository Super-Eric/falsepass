"""`falsepass reach` counts chosen probe rows on many tasks; the counts must agree with a
full probe run on the same tasks, with one job or several."""

import json
from pathlib import Path

import pytest

from falsepass import probes, reach, runner
from tests.test_concurrency import ADAPTER


def audit(tmp_path: Path, name: str, safe: bool) -> Path:
    """The concurrency test's toy, with a planted defect: any mention of the answer pays."""
    folder = tmp_path / name
    folder.mkdir()
    lenient = ADAPTER.replace("float(bool(marks) and marks[-1] == example.answer)", "float(example.answer in text)")
    assert lenient != ADAPTER
    (folder / "adapter.py").write_text(lenient.format(safe=safe))
    (folder / "src").mkdir()
    return folder


def test_reach_counts_what_a_full_run_finds_and_nothing_else(tmp_path: Path):
    folder = audit(tmp_path, "toy", safe=True)
    full = runner.run(folder, n=40, jobs=1, out=tmp_path / "full")
    failing = sorted({f"{r['probe']}/{r['variant']}" for r in full["results"] if not r["passed"] and r["probe"] != "P1"})
    assert failing, "the toy reward must fail some probe for this test to mean anything"
    asked = failing[0]
    one = reach.run(folder, [asked], tasks=40, jobs=1, out=tmp_path / "one")
    many = reach.run(folder, [asked], tasks=40, jobs=8, out=tmp_path / "many")
    assert one["rows"] == many["rows"]
    rows = {f"{r['probe']}/{r['variant']}": r for r in one["rows"]}
    assert set(rows) == {"P1/reference", asked} and rows["P1/reference"]["control"]
    expected = [r for r in full["results"] if f"{r['probe']}/{r['variant']}" == asked]
    assert rows[asked]["tasks"] == len(expected) == 40
    assert rows[asked]["failed"] == sum(not r["passed"] for r in expected)
    assert rows[asked]["failed_tasks"] == [r["example"] for r in expected if not r["passed"]]
    assert rows[asked]["examples"] and rows[asked]["examples"][0]["completion"]
    assert json.loads((tmp_path / "one" / "latest.json").read_text())["rows"] == one["rows"]
    assert probes.ONLY == set()  # the filter is reset for whatever runs next


def test_reach_says_what_can_be_asked_for(tmp_path: Path):
    folder = audit(tmp_path, "toy", safe=True)
    with pytest.raises(RuntimeError, match=r"P99.*this environment has: .*P15/"):
        reach.run(folder, ["P99"], tasks=3, out=tmp_path / "x")
    with pytest.raises(RuntimeError, match="--tasks N"):
        reach.run(folder, ["P15"], tasks=None, out=tmp_path / "x")
    assert probes.ONLY == set()
