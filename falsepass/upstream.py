"""`falsepass upstream <env>` — where does this environment's source live, and is it what we audited?

A fix can only be offered upstream when the source is in a public repository and that
repository still holds the code that was audited. This finds candidate repositories on
GitHub (repositories the package itself names: its project URLs and any github.com link in
its source or docstrings; then code search for the package name in a pyproject.toml and for
distinctive identifiers),
compares every audited Python file with the repository's default branch (line endings
ignored), and reports what a contributor needs to know: default branch, licence, last
push, open pull requests, and how many recently merged pull requests came from outside.

Read-only GitHub API calls through the `gh` CLI; nothing is written anywhere.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class GitHub(Protocol):
    def api(self, path: str, raw: bool = False) -> Any: ...


class GhCli:
    """`gh api`, with waits for GitHub's code-search limit (about ten queries a minute)."""

    def api(self, path: str, raw: bool = False) -> Any:
        import time

        command = ["gh", "api", "-X", "GET", path]
        if raw:
            command[2:2] = ["-H", "Accept: application/vnd.github.raw"]
        for attempt in range(3):
            done = subprocess.run(command, capture_output=True, text=True, check=False)
            limited = "rate limit" in done.stderr.lower() or "PROTOCOL_ERROR" in done.stderr
            if done.returncode == 0 or not limited:
                break
            time.sleep(20 * (attempt + 1))
        if done.returncode != 0:
            raise RuntimeError(done.stderr.strip()[:300] or f"gh api {path} failed")
        return done.stdout if raw else json.loads(done.stdout)


def normalise(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip()


@dataclass
class Match:
    repo: str
    prefix: str = ""  # where the audited tree sits inside the repository
    same: list[str] = field(default_factory=list)
    differ: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        if not self.same and not self.differ:
            return "no audited file found"
        if not self.differ and not self.missing:
            return "identical to the audit"
        if self.same:
            return "partly changed since the audit"
        return "rewritten since the audit"


def audited_files(src: Path) -> dict[str, str]:
    return {
        p.relative_to(src).as_posix(): p.read_text(errors="replace")
        for p in sorted(src.rglob("*.py"))
        if "__pycache__" not in p.parts
    }


GITHUB_REPO = re.compile(r"github\.com[/:]([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)")
NOT_A_REPO = {"sponsors", "orgs", "settings", "features", "topics", "marketplace", "apps", "login"}


def declared(src: Path, project_urls: list[str]) -> list[str]:
    """Repositories the package names itself (project URLs, links in its source, docstrings
    or README), in order of first mention. A link may point at a dependency rather than the
    source; `compare` sorts that out."""
    texts = list(project_urls)
    texts += [p.read_text(errors="replace") for p in sorted(src.rglob("*")) if p.is_file() and p.suffix in (".py", ".md", ".toml", ".txt")]
    found: list[str] = []
    for text in texts:
        for owner, repo in GITHUB_REPO.findall(text):
            name = f"{owner}/{repo.rstrip('.').removesuffix('.git')}"
            if owner.lower() not in NOT_A_REPO and name.lower() not in (f.lower() for f in found):
                found.append(name)
    return found


COMMON_NAMES = {"load_environment", "main", "Task", "Taskset", "Env", "Config", "setup", "score", "reward", "parse"}


def identifiers(files: dict[str, str], count: int = 3) -> list[str]:
    """The most distinctive class and function names in the audited source, which a code
    search can find verbatim."""
    import ast

    names: list[str] = []
    for text in files.values():
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.name not in COMMON_NAMES:
                names.append(node.name)
    # Public names first (a search for them is less noisy), then the longest.
    return sorted(set(names), key=lambda n: (n.startswith("_"), -len(n), n))[:count]


def candidates(gh: GitHub, package: str, module: str, owner: str, files: dict[str, str], limit: int = 10) -> list[str]:
    """Repositories that may hold the source, the Hub owner's own first."""
    found: list[str] = []
    queries = [f"search/code?q={package}+filename:pyproject.toml&per_page={limit}"]
    if module and module != package:
        queries.append(f"search/code?q={module}+filename:pyproject.toml&per_page={limit}")
    queries += [f"search/code?q={name}+language:python&per_page={limit}" for name in identifiers(files)]
    for query in queries:
        try:
            items = gh.api(query).get("items", [])
        except RuntimeError:
            continue
        for item in items:
            name = (item.get("repository") or item).get("full_name")
            if name and name not in found:
                found.append(name)
    return sorted(found, key=lambda r: r.split("/")[0].lower() != owner.lower())[: 2 * limit]


def packaging(gh: GitHub, repo: str, tree: list[str], prefix: str, branch: str) -> dict[str, Any]:
    """Version and dependencies the repository's pyproject.toml declares next to the code.
    The code can match a release while its packaging has moved on (same Python files,
    but a dependency declared only upstream)."""
    import tomllib

    path = prefix + "pyproject.toml"
    if path not in tree:
        return {}
    try:
        project = tomllib.loads(gh.api(f"repos/{repo}/contents/{path}?ref={branch}", raw=True)).get("project", {})
    except (RuntimeError, ValueError):
        return {}
    return {"upstream_version": project.get("version"), "upstream_dependencies": project.get("dependencies", [])}


def compare(gh: GitHub, repo: str, files: dict[str, str]) -> Match:
    match = Match(repo)
    try:
        info = gh.api(f"repos/{repo}")
        branch = info["default_branch"]
        tree = [t["path"] for t in gh.api(f"repos/{repo}/git/trees/{branch}?recursive=1")["tree"] if t["type"] == "blob"]
    except (RuntimeError, KeyError) as exc:
        match.facts["error"] = str(exc)
        return match
    # Locate the audited tree: the prefix under which most audited paths exist.
    prefixes: dict[str, int] = {}
    for rel in files:
        for path in tree:
            if path == rel or path.endswith("/" + rel):
                prefixes[path[: len(path) - len(rel)]] = prefixes.get(path[: len(path) - len(rel)], 0) + 1
    if not prefixes:
        match.missing = list(files)
    else:
        # Several copies of the tree (a development folder and a published one): the one with
        # the most files identical to the audit, then the shortest path.
        most = max(prefixes.values())
        best: tuple[int, int] | None = None
        for prefix in sorted((p for p in prefixes if prefixes[p] == most), key=len)[:5]:
            same, differ, missing = [], [], []
            for rel, text in files.items():
                if prefix + rel not in tree:
                    missing.append(rel)
                    continue
                upstream = gh.api(f"repos/{repo}/contents/{prefix + rel}?ref={branch}", raw=True)
                (same if normalise(upstream) == normalise(text) else differ).append(rel)
            if best is None or (len(same), -len(prefix)) > best:
                best = (len(same), -len(prefix))
                match.prefix, match.same, match.differ, match.missing = prefix, same, differ, missing
    match.facts = facts(gh, repo, info)
    if match.prefix or match.same:
        match.facts |= packaging(gh, repo, tree, match.prefix, branch)
    return match


def facts(gh: GitHub, repo: str, info: dict[str, Any]) -> dict[str, Any]:
    out = {
        "default_branch": info.get("default_branch"),
        "licence": (info.get("license") or {}).get("spdx_id") or "none",
        "pushed": (info.get("pushed_at") or "")[:10],
        "archived": bool(info.get("archived")),
        "issues_enabled": bool(info.get("has_issues")),
    }
    try:
        out["open_prs"] = len(gh.api(f"repos/{repo}/pulls?state=open&per_page=100"))
        merged = [p for p in gh.api(f"repos/{repo}/pulls?state=closed&per_page=60") if p.get("merged_at")]
        inside = {"OWNER", "MEMBER", "COLLABORATOR"}
        out["merged_recently"] = len(merged)
        out["merged_from_outside"] = sum(1 for p in merged if p.get("author_association") not in inside)
    except RuntimeError:
        pass
    return out


def locate(audit_dir: Path, gh: GitHub | None = None, repo: str | None = None, limit: int = 10) -> list[Match]:
    """Candidate repositories, best match first. Each match's facts carry the audited
    release's version (`audited_version`) next to the upstream pyproject's."""
    gh = gh or GhCli()
    env = tomllib.loads((audit_dir / "env.toml").read_text())["env"]
    files = audited_files(audit_dir / "src")
    if not files:
        raise RuntimeError(f"no audited Python files under {audit_dir / 'src'}")
    named = declared(audit_dir / "src", env.get("project_urls", []))
    searched = [] if repo else candidates(gh, env["package"], env.get("module", ""), env["id"].split("/")[0], files, limit)
    repos = [repo] if repo else [*named, *(r for r in searched if r.lower() not in {n.lower() for n in named})]
    matches = [compare(gh, r, files) for r in repos]
    for m in matches:
        m.facts["audited_version"] = env["version"]
    matches.sort(key=lambda m: (len(m.same), -len(m.differ)), reverse=True)
    return [m for m in matches if m.same or m.differ] or matches[:1]


def describe(matches: list[Match]) -> str:
    if not matches:
        return "no candidate repository found"
    rows = []
    if not any(m.same or m.differ for m in matches):
        rows.append("no repository found that holds the audited files (the nearest search hit, for reference:)")
    for m in matches:
        f = m.facts
        rows.append(f"{m.repo}  {m.verdict}  (path {m.prefix or '/'}: {len(m.same)} same, {len(m.differ)} differ, {len(m.missing)} missing)")
        if m.differ:
            rows.append(f"    differ: {', '.join(m.differ)}")
        if f.get("upstream_version") or f.get("upstream_dependencies"):
            moved = f.get("upstream_version") not in (None, f.get("audited_version"))
            rows.append(
                f"    packaging: upstream pyproject version {f.get('upstream_version')} (audited release "
                f"{f.get('audited_version')}{', DIFFERENT' if moved else ''}), dependencies {f.get('upstream_dependencies')}"
            )
        if "error" in f:
            rows.append(f"    {f['error']}")
        elif f:
            rows.append(
                f"    default branch {f['default_branch']}, licence {f['licence']}, last push {f['pushed']}"
                + (", ARCHIVED" if f["archived"] else "")
                + (f", {f['open_prs']} open PRs" if "open_prs" in f else "")
                + (f", {f['merged_from_outside']} of the last {f['merged_recently']} merged PRs from outside" if "merged_recently" in f else "")
            )
    return "\n".join(rows)
