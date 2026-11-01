"""verify-fix: which lines of a stored repro a patch changes."""

from pathlib import Path

from falsepass import verify


def test_header_lines_are_ignored_and_only_changed_lines_are_reported():
    stored = "# uv run --isolated --with x==1 python r.py\n# run twice\nA 1.0\nB 0.0\nC 0.5\n"
    found = verify.changes(verify.body(stored), ["A 1.0", "B 1.0", "C 0.5"])
    assert [(c.before, c.after) for c in found] == [("B 0.0", "B 1.0")]
    assert verify.describe([]).startswith("no line changed")
    assert "+ B 1.0" in verify.describe(found)


def test_the_stored_install_line_is_reused_with_the_package_swapped(tmp_path: Path):
    audit = tmp_path / "demo"
    audit.mkdir()
    (audit / "env.toml").write_text('[env]\nid = "o/demo"\npackage = "demo"\nversion = "0.1.1"\nverifiers = "0.3.1"\n')
    (audit / "repro.output.txt").write_text(
        "# uv run -q --isolated --no-project --python 3.12 --with verifiers==0.3.1 --with demo==0.1.1 "
        "--extra-index-url https://example/simple/ python audits/demo/repro.py\nA\n"
    )
    command = verify.command_for(audit, "repro.py", Path("/patched/demo"))
    assert "/patched/demo" in command and "demo==0.1.1" not in command
    assert command[command.index("--extra-index-url") + 1] == "https://example/simple/"
    (audit / "repro.output.txt").write_text("A\n")  # no header: built from env.toml
    command = verify.command_for(audit, "repro.py", Path("/patched/demo"))
    assert command[-2:] == ["python", str(audit / "repro.py")] and "/patched/demo" in command


def test_a_stored_relative_script_path_is_made_absolute(tmp_path):
    from falsepass import verify

    audit = tmp_path / "audits" / "demo"
    audit.mkdir(parents=True)
    command = ["uv", "run", "python", "-I", "audits/demo/repro.py"]
    assert verify._absolute_script(command, audit, "repro.py")[-1] == str((audit / "repro.py").resolve())
    assert verify._absolute_script(["python", "other.py"], audit, "repro.py") == ["python", "other.py"]


def test_an_overlay_of_a_local_source_is_used_as_it_is(tmp_path):
    from falsepass import verify

    audit = tmp_path / "audits" / "demo"
    audit.mkdir(parents=True)
    (audit / "env.toml").write_text('[env]\nid = "local/demo"\nsource = "/somewhere"\n')
    patched = tmp_path / "patched"
    (patched / "demo").mkdir(parents=True)
    assert verify.overlay_path(audit, patched) == patched  # nothing to fetch for a local environment


def test_a_repro_under_a_reference_relative_scale_prints_both_scales(tmp_path):
    """Under `scale_points` the audit's rows are scaled; the repro printed the raw reward against
    "expected 0", a false statement on the raw scale (dogfood 10)."""
    import json

    from falsepass import repro, verify

    folder = tmp_path / "e"
    folder.mkdir()
    (folder / "env.toml").write_text('[env]\nid = "o/e"\npackage = "e"\nversion = "1.0"\nmodule = "e"\n'
                                     'index = "https://example.invalid/simple/"\nverifiers = "0.4.0"\n')
    row = {"probe": "X1", "name": "policy", "variant": "one", "example": "row-98", "expected": "zero", "score": 0.5,
           "passed": False, "detail": "", "completion": "x", "completion_id": "c1", "answer_free": True, "metrics": {}, "errors": []}
    meta = {"environment": "o/e", "date": "d", "falsepass": "x", "versions": {}, "examples": 1, "max_reward": 1.0,
            "loader": {"kind": "v0-rollout", "module": "e", "load_args": {}, "getter": "get_dataset"},
            "scale": {"kind": "reference-relative", "points": {"row-98": [1.1973, 1.299]}, "excluded": {}, "tasks": 1}}
    (folder / "probe-results.json").write_text(json.dumps({"meta": meta, "results": [row], "static": [], "rewardlint": None}))
    (folder / "completions.json").write_text(json.dumps({"c1": [{"role": "assistant", "content": "buy 1"}]}))
    script = repro.generate(folder).read_text()
    assert '"floor": 1.1973' in script and '"reference": 1.299' in script and "reference-relative scale" in script
    assert "raw {raw:.6f}; floor" in script
    line = " 0.566000  expected 0     X1 policy/one on row-98  (raw 1.247660; floor 1.197300, reference 1.299000)"
    assert verify.CASE_LINE.match(line).groups() == ("0.566000", "0")  # the scale note does not break the run's count
