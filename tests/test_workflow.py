"""From results to a reviewable report: repro scripts, drafts, overview."""

import json

import pytest

from falsepass import draft, repro


def result(probe, name, variant, expected, score, passed, cid="", example="row-3", detail=""):
    return {"probe": probe, "name": name, "variant": variant, "example": example, "expected": expected,
            "score": score, "passed": passed, "detail": detail, "completion": "preview", "metrics": {},
            "errors": [], "answer_free": probe in ("P3", "P6"), "completion_id": cid}  # fmt: skip


@pytest.fixture
def audit_dir(tmp_path):
    folder = tmp_path / "audits" / "demo"
    folder.mkdir(parents=True)
    (folder / "env.toml").write_text(
        '[env]\nid = "owner/demo"\npackage = "demo-env"\nversion = "0.2.0"\nmodule = "demo_env"\n'
        'index = "https://example.invalid/simple/"\nverifiers = "0.3.1"\n'
    )
    results = [
        result("P1", "ground_truth", "reference", "full", 1.0, True, "aaa"),
        result("P6", "format_only", "wrong_in_format", "zero", 0.3, False, "bbb", detail="expected zero, got 0.3"),
        result("P6", "format_only", "wrong_in_format", "zero", 0.3, False, "bbb", example="row-7"),
        result("P2", "equivalent_forms", "trailing_period_inside_slot", "full", 0.0, False, "ccc"),
        result("P10", "exception_path", "huge_2mb", "zero", None, False, ""),  # too large to keep
        result("P3", "empty", "empty", "zero", 0.0, True, "ddd"),
    ]
    meta = {"environment": "owner/demo", "date": "2026-10-12", "examples": 2, "seed": 0, "python": "3.12",
            "versions": {"verifiers": "0.3.1", "demo-env": "0.2.0"}, "falsepass": "0.1.0", "max_reward": 1.0,
            "bad_tolerance": 0.0, "weights": {}, "loader": {"kind": "v0", "module": "demo_env", "load_args": {}, "getter": "get_dataset"},
            "adapter": {"format": "boxed", "answers": "closed set: A, B", "validity": "ok",
                        "reference_reward_by_format": {"boxed": 1.0}, "examples_tried": 2}}  # fmt: skip
    static = [{"rule": "term_ignores_reference", "file": "demo.py", "line": 12, "where": "fmt (weight 0.3)",
               "evidence": "def fmt(completion, **kwargs):", "meaning": "m", "confirm_with": ["P6"],
               "verdict": "confirmed: P6 failed 2/2"}]  # fmt: skip
    (folder / "probe-results.json").write_text(json.dumps({"meta": meta, "results": results, "static": static, "rewardlint": None}))
    (folder / "completions.json").write_text(json.dumps({"aaa": "\\boxed{A}", "bbb": "\\boxed{B}", "ccc": "\\boxed{A.}", "ddd": ""}))
    return folder


def test_repro_script_is_standalone_and_carries_exact_completions(audit_dir):
    path = repro.generate(audit_dir)
    script = path.read_text()
    compile(script, str(path), "exec")
    assert "falsepass" not in script.split('"""')[2]  # nothing after the docstring imports the tool
    assert "--with demo-env==0.2.0" in script and "env.get_dataset()" in script
    cases = json.loads(script.split("CASES = json.loads(")[1].split(")\nMODULE")[0].strip().strip("'").encode().decode("unicode_escape"))
    assert [c["probe"] for c in cases] == ["P6", "P6", "P2"]  # failing rows with a stored completion only
    assert cases[0]["completion"] == "\\boxed{B}" and cases[2]["expected"] == "full"
    assert len(repro.select(json.loads((audit_dir / "probe-results.json").read_text())["results"],
                            {"bbb": "x"}, ["P6"], per_group=1)) == 1  # fmt: skip


def test_repro_refuses_audits_without_a_recorded_loader(audit_dir):
    payload = json.loads((audit_dir / "probe-results.json").read_text())
    payload["meta"]["loader"] = None
    (audit_dir / "probe-results.json").write_text(json.dumps(payload))
    with pytest.raises(RuntimeError, match="hand-written adapter"):
        repro.generate(audit_dir)


def test_draft_lists_candidates_with_hints_and_marks_judgement_calls(audit_dir):
    text = draft.draft(audit_dir).read_text()
    assert "DRAFT" in text and "Not reviewed" in text
    assert "a wrong answer in the right format is rewarded" in text and "2 of 2 runs failed" in text
    assert "demo.py:12" in text  # the confirmed static hint is attached to its candidate
    assert "Judgement needed" in text  # P2 is strictness-versus-defect
    assert "| P1 ground_truth | 1 |" in text and text.count("### C") == 3


def test_overview_has_one_row_per_audit_with_status(audit_dir):
    table = draft.overview(audit_dir.parent)
    assert "`owner/demo`" in table and "probe run only" in table and "1/1" in table
    draft.draft(audit_dir)
    assert "| draft only |" in draft.overview(audit_dir.parent)
    (audit_dir / "REPORT.md").write_text("# x\n\n### F1 — a thing  [High]\n\n### F2 — another  [Low]\n\n### F3 — third  [Low, non-default]\n")
    table = draft.overview(audit_dir.parent)
    assert "1 High, 2 Low" in table and "1 environments with a reviewed report; 3 findings: 0 Critical, 1 High, 0 Medium, 2 Low" in table


def test_overview_does_not_count_an_unreviewed_report(audit_dir):
    (audit_dir / "REPORT.md").write_text("# x\n\n> **DRAFT — not reviewed.** Written by an agent.\n\n### F1 — a thing  [High]\n")
    table = draft.overview(audit_dir.parent)
    assert "0 environments with a reviewed report; 0 findings" in table
    assert "report not reviewed yet (1 High)" in table


def test_rollout_summary_flags_rows_to_read_without_calling_them_findings():
    from falsepass import rollout
    from falsepass.model import Example

    assert rollout.mentions("So the total is 18 dollars.", "18") and not rollout.mentions("It is 180.", "18")
    row = {"prompt": [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]}
    assert [m["role"] for m in rollout.prompt_messages(Example("k", "q", "a", row))] == ["system", "user"]
    assert rollout.prompt_messages(Example("k", "q", "a", object())) == [{"role": "user", "content": "q"}]
    base = {"reference_reward": 1.0, "answer": "18", "metrics": {"correct": 1.0}, "errors": [], "turns": None}
    payload = {
        "environment": "toy/env", "model": "m", "endpoint": "http://localhost", "max_tokens": 10, "seed": 0,
        "max_reward": 1.0, "open_ended": False, "multi_turn": False, "format": "bare",
        "rows": [
            {**base, "example": "a", "reward": 1.0, "mentions_reference": True, "flag": "", "reply": "#### 18"},
            {**base, "example": "b", "reward": 1.0, "mentions_reference": False,
             "flag": "paid without the reference in the reply", "reply": "x | y"},
            {**base, "example": "c", "reward": None, "mentions_reference": True, "flag": "reward code raised", "reply": ""},
        ],
    }  # fmt: skip
    text = rollout.summary(payload)
    assert "Mean reward of the model's replies:** 1.000" in text
    assert "Paid without the reference in the reply:** 1" in text and "not a finding" in text
    assert "| c | raised |" in text and "x \\| y" in text


def test_overview_keeps_upstream_reaudits_out_of_the_totals(audit_dir):
    (audit_dir / "REPORT.md").write_text("# x\n\n### F1 — a thing  [High]\n")
    again = audit_dir.parent / (audit_dir.name + "-upstream")
    again.mkdir()
    (again / "REPORT.md").write_text("# Audit: owner/demo upstream @ 1\n\n### F1 — a thing  [High]\n\n### F2 — new  [Low]\n")
    table = draft.overview(audit_dir.parent)
    assert "1 environments with a reviewed report; 1 findings: 0 Critical, 1 High" in table
    assert "Not in these totals: 1 re-audit(s) of an unreleased upstream version (2 findings: 0 Critical, 1 High, 0 Medium, 1 Low)" in table


def test_a_dataset_check_is_not_labelled_as_a_raise():
    rows = [{"probe": "P12", "score": None}]
    assert draft._stats(rows) == "no score (a dataset check: nothing is scored)"
    assert draft._stats([{"probe": "P10", "score": None}]) == "no score (reward code raised)"


def test_verifiers_pin_honours_an_exact_requirement():
    from falsepass import hub

    assert hub.verifiers_pin("==0.3.0") == "0.3.0"
    assert hub.verifiers_pin(">=0.1.15.dev189") == "0.1.15.dev189"
    assert hub.verifiers_pin(">=0.1.3") == hub.DEFAULT_VERIFIERS
