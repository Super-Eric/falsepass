"""`falsepass verify-fix <env> --source DIR` — does a patch fix what the report says, and only that?

Reruns a stored repro script with the environment installed from a patched local folder
and compares its output, line by line, with the stored `*.output.txt`. A fix is ready
to send when exactly the lines of the finding change and every other line is
byte-identical.

The install line comes from the first header line of the stored output (`# uv run ...`),
which records how the stored run was made, with the environment package swapped for the
patched folder. Without such a header it is built from `env.toml`.
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


def patched(command: list[str], env: dict, source: Path) -> list[str]:
    """The same command with the environment package taken from `source`."""
    release = f"{env['package']}=={env['version']}"
    out, swapped = [], False
    for i, part in enumerate(command):
        if part == release and i and command[i - 1] == "--with":
            out.append(str(source))
            swapped = True
        else:
            out.append(part)
    if not swapped:
        at = out.index("python") if "python" in out else len(out)
        out[at:at] = ["--with", str(source)]
    return out


def command_for(audit_dir: Path, script: str, source: Path) -> list[str]:
    from falsepass.cli import install_args

    env = tomllib.loads((audit_dir / "env.toml").read_text())["env"]
    output = audit_dir / (Path(script).stem + ".output.txt")
    stored = stored_command(output) if output.is_file() else None
    if stored:
        return patched(stored, env, source)
    return [
        "uv", "run", "-q", "--isolated", "--no-project", "--python", env.get("python", "3.12"),
        *install_args(env, source), "python", str(audit_dir / script),
    ]  # fmt: skip


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
