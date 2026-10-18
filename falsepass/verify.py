"""`falsepass verify-fix <env> --source DIR` — does a patch fix what the report says, and only that?

Reruns a stored repro script with the environment installed from a patched local folder
and compares its output, line by line, with the stored `*.output.txt`. A fix is ready
to send when exactly the lines of the finding change and every other line is
byte-identical.

The install line comes from the first header line of the stored output (`# uv run ...`),
which records how the stored run was made, with the environment package swapped for the
patched folder. Without such a header it is built from `env.toml`.

A patched folder without a `pyproject.toml` (an audit's own `src/`, unpacked from the Hub
wheel) is laid over the released package instead: the release stays installed, so its
dependencies are right, and the folder goes first on `PYTHONPATH`. With no stored output
yet, the unpatched script is run once first and its output stored as the baseline.
"""

from __future__ import annotations

import difflib
import shlex
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Change:
    before: str | None  # None: line added by the patch
    after: str | None  # None: line removed by the patch


def body(text: str) -> list[str]:
    """Output lines without the header lines a stored run starts with."""
    lines = text.splitlines()
    while lines and lines[0].startswith("# "):
        lines.pop(0)
    return lines


def changes(before: list[str], after: list[str]) -> list[Change]:
    found: list[Change] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        old, new = before[i1:i2], after[j1:j2]
        for k in range(max(len(old), len(new))):
            found.append(Change(old[k] if k < len(old) else None, new[k] if k < len(new) else None))
    return found


def stored_command(output: Path) -> list[str] | None:
    first = output.read_text().splitlines()[:1]
    if first and first[0].startswith("# uv run"):
        return shlex.split(first[0][2:])
    return None


def source_floor(source: Path) -> str | None:
    """The lowest verifiers version the patched source accepts, from its pyproject.toml."""
    import re

    from falsepass.hub import declared_verifiers

    pyproject = source / "pyproject.toml"
    if not pyproject.is_file():
        return None
    requirement = declared_verifiers(tomllib.loads(pyproject.read_text()).get("project", {}).get("dependencies", []))
    floor = re.search(r"(?:>=|==)\s*([\w.]+)", requirement)
    return floor.group(1) if floor else None


def patched(command: list[str], env: dict, source: Path) -> list[str]:
    """The same command with the environment package taken from `source`. If the source
    now needs a newer verifiers than the stored run used, that newer version is pinned:
    a repository's main branch often moves ahead of the release that was audited."""
    from falsepass.hub import newer

    release = f"{env['package']}=={env['version']}"
    floor = source_floor(source)
    out, swapped = [], False
    for i, part in enumerate(command):
        if part == release and i and command[i - 1] == "--with":
            out.append(str(source))
            swapped = True
        elif part.startswith("verifiers==") and floor and newer(floor, part.removeprefix("verifiers==")):
            out.append(f"verifiers=={floor}")
        else:
            out.append(part)
    if not swapped:
        at = out.index("python") if "python" in out else len(out)
        out[at:at] = ["--with", str(source)]
    return out


def default_script(audit_dir: Path) -> str:
    """`repro.py` (hand-written) if there is one, else what `falsepass repro` writes."""
    return "repro.py" if (audit_dir / "repro.py").is_file() or not (audit_dir / "repro_auto.py").is_file() else "repro_auto.py"


def overlay(source: Path | None) -> bool:
    """A folder of modules without packaging: laid over the release via PYTHONPATH."""
    return source is not None and source.is_dir() and not (source / "pyproject.toml").is_file()


def overlay_path(audit_dir: Path, source: Path) -> Path:
    """The PYTHONPATH folder for an overlay. `init` copies only source text into src/, so a
    patched copy of it lacks the package's data files (a JSON task bank next to the
    modules), and laid over the release it hides them. Those files are added from the
    release wheel, in a temporary copy; `source` itself is not touched."""
    import shutil
    import tempfile

    env = tomllib.loads((audit_dir / "env.toml").read_text())["env"]
    if env.get("source") or "/" not in env.get("id", "") or not env.get("version"):
        return source
    from falsepass.hub import release_files

    try:
        files = release_files(env["id"], env["version"])
    except Exception:  # noqa: BLE001 - offline: run the overlay as it is
        return source
    missing = {n: data for n, data in files.items()
               if (source / n.split("/")[0]).is_dir() and not (source / n).exists()}  # fmt: skip
    if not missing:
        return source
    merged = Path(tempfile.mkdtemp(prefix="falsepass-overlay-")) / source.name
    shutil.copytree(source, merged)
    for name, data in missing.items():
        (merged / name).parent.mkdir(parents=True, exist_ok=True)
        (merged / name).write_bytes(data)
    return merged


def _absolute_script(command: list[str], audit_dir: Path, script: str) -> list[str]:
    """A stored command names the script as it was run (often relative to the repository
    root); point it at the script in the audit folder so it runs from any directory."""
    name = Path(script).name
    return [str((audit_dir / script).resolve()) if part.endswith(name) and Path(part).name == name else part
            for part in command]  # fmt: skip


def command_for(audit_dir: Path, script: str, source: Path | None) -> list[str]:
    """The command that runs `script` with the environment taken from `source` (None: the
    release, unpatched)."""
    from falsepass.cli import install_args

    env = tomllib.loads((audit_dir / "env.toml").read_text())["env"]
    output = audit_dir / (Path(script).stem + ".output.txt")
    stored = stored_command(output) if output.is_file() else None
    openenv = env.get("framework") == "openenv"  # the folder is read in place, not installed: FALSEPASS_SOURCE names the patched copy
    package = None if source is None or overlay(source) or openenv else source
    if stored:
        command = _absolute_script(patched(stored, env, package) if package else stored, audit_dir, script)
    else:
        from falsepass.hub import newer

        floor = source_floor(package) if package else None
        if floor and env.get("verifiers") and newer(floor, env["verifiers"]):
            env = {**env, "verifiers": floor}
        command = [
            "uv", "run", "-q", "--isolated", "--no-project", "--python", env.get("python", "3.12"),
            *install_args(env, package), "python", str((audit_dir / script).resolve()),
        ]  # fmt: skip
    if source is not None and openenv:
        return ["env", f"FALSEPASS_SOURCE={source.resolve()}", *command]
    return ["env", f"PYTHONPATH={overlay_path(audit_dir, source)}", *command] if overlay(source) else command


def baseline(audit_dir: Path, script: str) -> tuple[Path, str, int]:
    """Run the unpatched script once and store its output, headed by the command."""
    output = audit_dir / (Path(script).stem + ".output.txt")
    command = command_for(audit_dir, script, None)
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    if done.returncode == 0:
        output.write_text(f"# {shlex.join(command)}\n" + done.stdout)
    return output, done.stderr[-2000:], done.returncode


def twice(audit_dir: Path, script: str) -> dict:
    """Run the unpatched script twice, each time in a fresh `uv run --isolated` virtualenv,
    compare stdout and store the first run under a header that says whether the second was
    identical (what makes a repro count). Writes `repro-run.json` with the summary."""
    import hashlib
    import json
    import re
    from datetime import date

    audit_dir = audit_dir.resolve()
    output = audit_dir / (Path(script).stem + ".output.txt")
    command = command_for(audit_dir, script, None)
    runs = [subprocess.run(command, capture_output=True, text=True, check=False) for _ in range(2)]
    first, second = runs
    result: dict = {"script": script, "command": shlex.join(command), "exit_codes": [r.returncode for r in runs]}
    if first.returncode != 0 or not first.stdout.strip():
        result.update(identical=False, error=f"the first run exited {first.returncode}", stderr=first.stderr[-2000:])
    else:
        same = first.stdout == second.stdout and second.returncode == 0
        digest = hashlib.sha256(first.stdout.encode()).hexdigest()[:12]
        output.write_text(f"# {shlex.join(command)}\n# run twice on {date.today().isoformat()}, each in a fresh "
                          f"`uv run --isolated` virtualenv (stderr dropped); second run identical: {'yes' if same else 'NO'} "
                          f"({len(first.stdout.encode())} bytes, sha256 {digest}...)\n" + first.stdout)  # fmt: skip
        lines = first.stdout.splitlines()
        result.update(identical=same, output=str(output), lines=len(lines),
                      differences=[f"{c.before!r} -> {c.after!r}" for c in changes(lines, second.stdout.splitlines())][:20])  # fmt: skip
        stored = audit_dir / "probe-results.json"
        top = json.loads(stored.read_text())["meta"].get("max_reward", 1.0) if stored.is_file() else 1.0
        cases = [m.groups() for line in lines if (m := re.match(r"\s*(\S+(?: \S+)?)\s+expected (\S+)\s", line))]
        if cases:  # a script `falsepass repro` wrote: observed, expected, case
            def wrong(observed: str, expected: str) -> bool:
                try:
                    value = float(observed)
                except ValueError:
                    return True  # "raised ...": the reward code failing is itself what was seen
                return value > 1e-9 if expected == "0" else value < top - 1e-9

            result.update(cases=len(cases), reproduced=sum(wrong(o, e) for o, e in cases))
    (audit_dir / "repro-run.json").write_text(json.dumps(result, indent=1))
    return result


def describe_known_good(release: dict, patched: dict) -> str:
    """Correct-answer rows (from two `reach --probe known-good` runs): what the patch stops paying."""
    before = {(r["probe"], r["variant"]): r for r in release["rows"]}
    rows = [f"correct answers, on {patched['meta']['tasks_counted']} tasks ({'every task' if patched['meta']['all_tasks'] else 'a sample'}):"]
    worse = 0
    for r in patched["rows"]:
        old = before.get((r["probe"], r["variant"]))
        lost = sorted(set(r["failed_tasks"]) - set(old["failed_tasks"] if old else []))
        gained = sorted(set(old["failed_tasks"] if old else []) - set(r["failed_tasks"]))
        worse += bool(lost)
        mark = "  now refused on " + ", ".join(lost[:10]) + (f" and {len(lost) - 10} more" if len(lost) > 10 else "") if lost else ""
        mark += f"  (now paid on {len(gained)} it refused before)" if gained else ""
        rows.append(f"  {r['probe']} {r['name']}/{r['variant']}: not paid in full on {old['failed'] if old else '?'} -> {r['failed']} of {r['tasks']}{mark}")
    rows.append("the patch stops paying no correct answer that the release pays" if not worse
                else f"the patch refuses correct answers the release pays, in {worse} row(s): read them before sending it")
    return "\n".join(rows)


def describe_twice(result: dict) -> str:
    if result.get("error"):
        return f"{result['script']}: {result['error']}; nothing stored. End of stderr:\n{result.get('stderr', '')}"
    rows = [f"{result['script']}: ran twice, second run identical: {'yes' if result['identical'] else 'NO'}; "
            f"stored in {result['output']} ({result['lines']} lines)"]
    if "cases" in result:
        rows.append(f"{result['reproduced']} of {result['cases']} cases still miss what a correct verifier gives "
                    "(expected 0 but paid, expected full but not paid in full, or the reward code raised)")
    rows += [f"  differs: {d}" for d in result["differences"]]
    return "\n".join(rows)


def run(audit_dir: Path, source: Path, script: str = "repro.py") -> tuple[list[Change], str, int]:
    """Run the patched repro; return the changed lines, its stderr tail and exit code."""
    output = audit_dir / (Path(script).stem + ".output.txt")
    if not output.is_file():
        raise FileNotFoundError(f"no stored output {output} to compare with")
    done = subprocess.run(command_for(audit_dir, script, source), capture_output=True, text=True, check=False)
    return changes(body(output.read_text()), body(done.stdout)), done.stderr[-2000:], done.returncode


def describe(found: list[Change]) -> str:
    if not found:
        return "no line changed: the patch does not touch anything this repro measures"
    rows = [f"{len(found)} line(s) changed:"]
    for c in found:
        rows.append(f"  - {c.before}" if c.before is not None else "  - (none)")
        rows.append(f"  + {c.after}" if c.after is not None else "  + (none)")
    return "\n".join(rows)
