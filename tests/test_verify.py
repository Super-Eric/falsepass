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
