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
    assert probes.ONLY == set()


def test_a_known_bad_reply_identical_to_the_known_good_one_is_not_a_false_pass():
    from falsepass.model import Expect, Policy, ProbeResult
    from falsepass.report import headline

    probes.STORE.clear()
    solver, echo = Policy("solver", lambda m: "a b c"), Policy("echo", lambda m: "a b c")
    good = probes.remember_episode(solver, {"script": ["a b c"]})
    same = probes.remember_episode(echo, {"script": ["a b c"]})
    other = probes.remember_episode(echo, {"script": ["c b a"]})
    rows = [
        ProbeResult("X1", "policy", "solver", "row-1", Expect.FULL, 1.0, True, completion_id=good),
        ProbeResult("X1", "policy", "echo", "row-1", Expect.ZERO, 1.0, False, completion_id=same),  # already sorted
        ProbeResult("X1", "policy", "echo", "row-2", Expect.ZERO, 1.0, False, completion_id=other),  # no solver row here
    ]
    assert good != same  # different policies, so different ids: the replies are what is compared
    assert probes.mark_same_as_good(rows) == 1
    assert rows[1].same_as_good and rows[1].passed and not rows[2].same_as_good and not rows[2].passed
    head = headline([r.to_dict() for r in rows])
    assert head["same_as_good"] == 1 and head["known_bad"] == 1 and head["false_pass_ceiling"] == 1.0
    assert "false_pass_note" not in head


def test_a_policy_whose_last_reply_is_the_right_one_is_labelled_not_hidden():
    from falsepass.model import Expect, Policy, ProbeResult
    from falsepass.report import headline

    probes.STORE.clear()
    good = probes.remember_episode(Policy("solver", None), {"script": ["a b c"]})
    ladder = probes.remember_episode(Policy("ladder", None), {"script": ["c b a", "a b c"]})
    weak = probes.remember_episode(Policy("reverse", None), {"script": ["c b a"]})
    rows = [
        ProbeResult("X1", "policy", "solver", "row-1", Expect.FULL, 1.0, True, completion_id=good),
        ProbeResult("X1", "policy", "ladder", "row-1", Expect.ZERO, 1.0, False, completion_id=ladder, answer_free=True),
        ProbeResult("X1", "policy", "reverse", "row-1", Expect.ZERO, 0.3, False, completion_id=weak, answer_free=True),
    ]
    assert probes.mark_same_as_good(rows) == 0
    assert rows[1].final_as_good and not rows[1].passed and not rows[2].final_as_good
    head = headline([r.to_dict() for r in rows])
    assert head["false_pass_ceiling"] == 1.0 and head["false_pass_ceiling_other"] == 0.3 and "by chance" in head["false_pass_note"]


def test_known_good_counts_every_correct_answer_row_and_a_patch_that_refuses_one_is_shown(tmp_path: Path):
    from falsepass import verify

    folder = audit(tmp_path, "toy", safe=True)
    release = reach.run(folder, ["known-good"], tasks=6, jobs=1, out=tmp_path / "a")
    assert {r["expected"] for r in release["rows"]} == {"full"} and not any(r["control"] for r in release["rows"])
    assert all(r["failed"] == 0 for r in release["rows"])  # the toy pays every correct answer
    patched = json.loads(json.dumps(release))
    patched["rows"][0].update(failed=1, failed_tasks=["ex-3"])
    text = verify.describe_known_good(release, patched)
    assert "0 -> 1 of 6  now refused on ex-3" in text and "refuses correct answers the release pays, in 1 row(s)" in text
    assert "stops paying no correct answer" in verify.describe_known_good(release, release)


def test_templates_are_filled_per_task_and_counted(tmp_path: Path):
    from tests import test_concurrency

    folder = test_concurrency.audit(tmp_path, "strict", safe=True)  # pays only a last `#### N` line equal to the answer
    templates = ["#### {wrong}\n#### {answer}", "#### {answer}\n#### {wrong}", "{answer_in_format}", r"\boxed{{answer}}"]
    counted = reach.run(folder, [], tasks=5, jobs=1, out=tmp_path / "t", templates=templates)
    rows = {r["variant"]: r for r in counted["rows"] if r["probe"] == "U1"}
    assert [rows[f"t{i}"]["template"] for i in range(1, 5)] == templates
    assert rows["t1"]["failed"] == 5 and rows["t1"]["first_completion"] == "#### 8\n#### 1"  # ex-0: answer 1, wrong 8
    assert rows["t2"]["failed"] == 0
    assert rows["t3"]["failed"] == 0 and rows["t3"]["same_as_good"] == 5  # the reference itself, not a false pass
    assert rows["t4"]["first_completion"] == r"\boxed{1}" and rows["t4"]["full_reward"] == 0  # other braces stay
    assert counted["meta"]["templates"] == templates and counted["file"].endswith(".json") and "template-" in counted["file"]
    assert 'U1 template/t1 "#### {wrong}\\n#### {answer}": paid on 5 of 5' in reach.describe(counted)
    full = reach.run(folder, [], tasks=5, jobs=1, out=tmp_path / "f", templates=["#### {answer}"], expect="full")
    assert [r["failed"] for r in full["rows"] if r["probe"] == "U1"] == [0]
    assert probes.TEMPLATES == [] and probes.ONLY == set()
    with pytest.raises(RuntimeError, match="nothing to count"):
        reach.run(folder, [], tasks=1, out=tmp_path / "x")
