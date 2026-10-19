"""Look up what `falsepass init` needs from the Environments Hub package index."""

from __future__ import annotations

import hashlib
import io
import re
import tomllib
import urllib.request
import zipfile
from pathlib import Path

HUB = "https://hub.primeintellect.ai"
DEFAULT_VERIFIERS = "0.3.1"


def _get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "falsepass"}), timeout=60) as r:
        return r.read()


def _version_key(version: str) -> tuple:
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"[.\-+]", version))


def verifiers_pin(specifier: str, releases: list[str] | None = None, newest: bool = False) -> str:
    """The verifiers version to install for a declared requirement such as `>=0.3.0`.
    An exact pin is the only version that installs. Environments built for a 0.1.15.dev
    build do not import under 0.3.x: keep their own floor. A ceiling below the default
    (`>=0.1.14,<0.2`) takes the newest release under it (`releases`, else PyPI's list),
    or the floor when the list cannot be read. With `newest` (v1 tasksets, written for
    the current API) the newest release the requirement allows is taken instead of the
    default. Otherwise the default."""
    exact = re.search(r"==\s*([\w.]+)", specifier)
    bound = re.search(r">=\s*([\w.]+)", specifier)
    ceiling = re.search(r"<(=?)\s*([\w.]+)", specifier)
    if exact:
        return exact.group(1)
    under_default = bool(ceiling) and not _below(DEFAULT_VERIFIERS, ceiling.group(2), ceiling.group(1) == "=")
    if under_default or newest:
        if releases is None:
            try:
                releases = pypi_releases("verifiers")
            except Exception:  # noqa: BLE001 - offline: the floor or the default is the best guess left
                releases = []
        floor = bound.group(1) if bound else "0"
        fitting = [
            r for r in releases
            if re.fullmatch(r"[0-9.]+", r) and not newer(floor, r)
            and (not ceiling or _below(r, ceiling.group(2), ceiling.group(1) == "="))
        ]  # fmt: skip
        if fitting:
            return max(fitting, key=_version_key)
    if bound and (".dev" in bound.group(1) or newer(bound.group(1), DEFAULT_VERIFIERS)):
        return bound.group(1)  # a floor above the default is the oldest version that installs
    if under_default and bound:
        return bound.group(1)
    return DEFAULT_VERIFIERS


def _below(version: str, ceiling: str, inclusive: bool) -> bool:
    return not newer(version, ceiling) and (inclusive or version != ceiling)


def python_for(requires: str) -> str:
    """The Python to run an environment with when its `Requires-Python` rules out 3.12
    (e.g. `>=3.13,<3.14`); '' when 3.12 is fine."""
    def allowed(minor: int) -> bool:
        for op, version in re.findall(r"(>=|<=|==|!=|~=|>|<)\s*([0-9][0-9.*]*)", requires):
            parts = [int(p) for p in version.split(".")[:2] if p.isdigit()] + [0]
            want, have = (parts[0], parts[1]), (3, minor)
            ok = {">=": have >= want, "<=": have <= want, ">": have > want, "<": have < want,
                  "==": have == want, "!=": have != want, "~=": have >= want}[op]  # fmt: skip
            if not ok:
                return False
        return True

    if allowed(12):
        return ""
    return next((f"3.{m}" for m in (13, 11, 14, 10) if allowed(m)), "")


def pypi_releases(package: str) -> list[str]:
    import json

    return list(json.loads(_get(f"https://pypi.org/pypi/{package}/json"))["releases"])


def pypi_version(package: str) -> str:
    """The latest release of `package` on PyPI, to pin an audit to."""
    import json

    return json.loads(_get(f"https://pypi.org/pypi/{package}/json"))["info"]["version"]


def newer(version: str, than: str) -> bool:
    try:
        return _version_key(version) > _version_key(than)
    except TypeError:  # a pre-release tag compared with a number
        return False


def declared_verifiers(dependencies: list[str]) -> str:
    """The verifiers requirement among `dependencies`, e.g. `verifiers>=0.4.0`, or ''."""
    return next((d.strip() for d in dependencies if re.match(r"verifiers\s*(\[|[<>=!~;]|$)", d.strip())), "")


def describe_local(path: Path) -> dict:
    """What `falsepass init` needs for an environment in a local folder (its pyproject.toml).

    For authors auditing their own environment before publishing it: the folder is
    installed as it is on every run, so edits are picked up without a new release."""
    path = path.resolve()
    project = tomllib.loads((path / "pyproject.toml").read_text()).get("project", {})
    package = project.get("name") or path.name
    requirement = declared_verifiers(project.get("dependencies", []))
    code = local_sources(path)
    kind = classify(code) if code else None
    return {
        "id": f"local/{package}",
        "package": package,
        "version": project.get("version", "0"),
        "module": local_module(path, package),
        "index": "",
        "verifiers": verifiers_pin(re.sub(r"^verifiers\s*(\[[^\]]*\])?", "", requirement), newest=bool(kind) and kind["generation"] == "v1"),
        "python": python_for(project.get("requires-python", "")),
        "declared": requirement,
        "source": str(path),
        "kind": kind,
    }


def local_sources(path: Path, limit: int = 200) -> dict[str, bytes]:
    """The Python files of a local environment folder (tests, builds and hidden folders left
    out), by relative path, for `classify`."""
    found: dict[str, bytes] = {}
    for file in sorted(path.rglob("*.py")):
        parts = file.relative_to(path).parts
        if any(p.startswith(".") or p in ("tests", "test", "build", "dist", "__pycache__") for p in parts):
            continue
        found[str(file.relative_to(path))] = file.read_bytes()
        if len(found) >= limit:
            break
    return found


def wheel_module(top_level: list[str], package: str) -> str:
    """The environment's module among a wheel's top-level modules: the one named after the
    package (verifiers imports `cortifybench_env` for `cortifybench-env`), else the first."""
    name = package.replace("-", "_").lower()
    return name if name in top_level else top_level[0] if top_level else package


def local_module(path: Path, package: str) -> str:
    """The importable module of a local environment: the package name with underscores if
    such a module exists, otherwise the first module that defines `load_environment`."""
    name = package.replace("-", "_")
    for root in (path, path / "src"):
        if (root / f"{name}.py").is_file() or (root / name / "__init__.py").is_file():
            return name
    for file in sorted(path.rglob("*.py")):
        parts = file.relative_to(path).parts
        if any(p.startswith(".") or p in ("tests", "build", "dist") for p in parts):
            continue
        if re.search(r"^def load_environment\(", file.read_text(errors="replace"), re.M):
            stem = parts[:-1] if file.name == "__init__.py" else (*parts[:-1], file.stem)
            return ".".join(p for p in stem if p != "src")
    return name


def _wheels(env_id: str) -> tuple[str, str, dict[str, str]]:
    """The Hub index of `owner/environment`, its package name, and wheel URLs by version."""
    index = f"{HUB}/{env_id}/install/simple/"
    packages = re.findall(r'<a href="([^"]+)">([^<]+)</a>', _get(index).decode())
    if not packages:
        raise RuntimeError(f"no package found at {index}")
    package_url, package = packages[0]
    wheels = {
        match.group(2): match.group(1)
        for match in re.finditer(r'<a href="([^"#]+\.whl)[^"]*">[^<]*?-([0-9][^-]*)-[^<]*</a>', _get(package_url).decode())
    }
    if not wheels:
        raise RuntimeError(f"no wheels listed at {package_url}")
    return index, package, wheels


SOURCE_SUFFIXES = (".py", ".toml", ".md")
DATA_SUFFIXES = (".json", ".jsonl", ".csv", ".tsv", ".yaml", ".yml", ".txt",
                 # code the grader may run outside Python (a game engine, a checker)
                 ".js", ".mjs", ".cjs", ".ts", ".wasm", ".sh", ".sql", ".lua", ".rs", ".go", ".c", ".h", ".cpp",
                 # files a checker or a simulator reads (proof sources, planning domains, arrays)
                 ".lean", ".v", ".pddl", ".smt2", ".npz", ".npy")
DATA_FILE_CAP = 8 * 1024 * 1024  # one data file
DATA_TOTAL_CAP = 32 * 1024 * 1024  # all of them together


def data_files(archive: zipfile.ZipFile, names: list[str]) -> dict[str, bytes]:
    """The wheel's data files (task banks, answer keys, prompt texts) for `src/`, smallest
    first, up to the caps: a patched copy of `src/` then runs without the release's data
    being filled in, and a reviewer can read what the grader compares against."""
    infos = sorted((archive.getinfo(n) for n in names if ".dist-info" not in n and n.endswith(DATA_SUFFIXES)),
                   key=lambda i: i.file_size)  # fmt: skip
    found: dict[str, bytes] = {}
    total = 0
    for info in infos:
        if info.file_size > DATA_FILE_CAP or total + info.file_size > DATA_TOTAL_CAP:
            break
        found[info.filename] = archive.read(info.filename)
        total += info.file_size
    return found


def release_files(env_id: str, version: str) -> dict[str, bytes]:
    """Every file of a released wheel except its metadata, by path."""
    _, _, wheels = _wheels(env_id)
    archive = zipfile.ZipFile(io.BytesIO(_get(wheels[version])))
    return {n: archive.read(n) for n in archive.namelist() if ".dist-info/" not in n and not n.endswith("/")}


def describe(env_id: str, version: str | None = None) -> dict:
    """Package name, version, importable module, verifiers pin and source files for `owner/environment`."""
    index, package, wheels = _wheels(env_id)
    version = version or max(wheels, key=_version_key)
    if version not in wheels:
        raise RuntimeError(f"version {version} not found; available: {sorted(wheels, key=_version_key)}")

    wheel = _get(wheels[version])
    archive = zipfile.ZipFile(io.BytesIO(wheel))
    names = archive.namelist()
    metadata = archive.read(next(n for n in names if n.endswith(".dist-info/METADATA"))).decode()
    top_level = sorted({n.split("/")[0].removesuffix(".py") for n in names if ".dist-info" not in n and n.endswith(".py")})
    requirement = re.search(r"^Requires-Dist: verifiers(\[[^\]]*\])?[ \t]*(.*)$", metadata, re.M)
    requires_python = re.search(r"^Requires-Python:[ \t]*(.*)$", metadata, re.M)
    project_urls = re.findall(r"^Project-URL: (.+)$", metadata, re.M)
    sources = {n: archive.read(n) for n in names if ".dist-info" not in n and n.endswith(SOURCE_SUFFIXES)}
    sources.update(data_files(archive, names))
    pin = verifiers_pin(requirement.group(2) if requirement else "", newest=classify(sources)["generation"] == "v1")
    return {
        "sources": sources,
        "releases": sorted(wheels, key=_version_key),
        "project_urls": project_urls,
        "wheel_sha256": hashlib.sha256(wheel).hexdigest(),  # which bytes were audited, for the report
        "id": env_id,
        "package": package,
        "version": version,
        "module": wheel_module(top_level, package),
        "index": index,
        "verifiers": pin,
        "python": python_for(requires_python.group(1)) if requires_python else "",
        "declared": f"verifiers{requirement.group(1) or ''}{requirement.group(2)}".strip() if requirement else "",
    }


INFRASTRUCTURE = {
    # "harbor" alone also matched a street name and a hostname in task data: require the code forms.
    "sandbox": r"SandboxEnv|PythonEnv|prime_sandboxes|CliAgentEnv|HarborEnv|HarborTaskset|(?:from|import) harbor\b|NEEDS_CONTAINER\s*=\s*True"
               r"|docker_image|image_prefix|image_template|[\"']docker[\"']|import modal",
    "tools": r"ToolEnv|StatefulToolEnv|MCPEnv|BrowserEnv|vf\.Toolset|@vf\.tool",
    "llm judge": r"JudgeRubric|vf\.Judge|judge_model|JudgeConfig",
    "api key": r"API_KEY|api_key_var|OPENAI_|ANTHROPIC_",
}


HTTP_CLIENT = r"\brequests\.(?:get|post|put|patch|request|Session)\b|\bhttpx\.(?:get|post|put|request|Client|AsyncClient)\b" \
              r"|aiohttp\.ClientSession|\burlopen\(|websockets?\.connect"
# Hosts that serve data, packages or documentation, not a service that takes part in an episode.
DATA_HOSTS = ("huggingface.co", "hf.co", "github.com", "githubusercontent.com", "pypi.org", "pythonhosted.org", "arxiv.org",
              "example.com", "example.org", "kaggle.com", "zenodo.org", "readthedocs.io", "wikipedia.org")


def outside_service(code: str) -> list[str]:
    """Hosts an environment's code may call while it runs: an HTTP client in the source and a
    URL that is not local and not a data or documentation host. A game server, a judge service
    or a live API decides the outcome there, and an audit must not reach it (falsepass blocks
    outside hosts during v1 episodes)."""
    if not re.search(HTTP_CLIENT, code):
        return []
    hosts = set(re.findall(r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})", code))
    return sorted(h for h in hosts if not h.endswith(DATA_HOSTS) and h not in ("localhost",))


def task_data_fields(sources: dict[str, bytes]) -> dict[str, list[str]]:
    """The annotated fields of the package's task-data classes (a subclass of verifiers'
    `TaskData`, or any class named `*TaskData` / `*Data`), read from the source: where a
    hand-written adapter finds the expected values (`verdad: list[dict]`, `answer: str`)."""
    import ast

    found: dict[str, list[str]] = {}
    for name, content in sources.items():
        if not name.endswith(".py"):
            continue
        try:
            tree = ast.parse(content.decode(errors="replace"))
        except SyntaxError:
            continue
        for node in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            bases = " ".join(ast.unparse(b) for b in node.bases)
            if "TaskData" in bases or node.name.endswith(("TaskData", "Data")):
                fields = [f"{item.target.id}: {ast.unparse(item.annotation)}" for item in node.body
                          if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)]  # fmt: skip
                if fields:
                    found[node.name] = fields
    return found


def classify(sources: dict[str, bytes]) -> dict:
    """What kind of environment a wheel contains, read from its source text only."""
    code = "\n".join(content.decode(errors="replace") for name, content in sources.items() if name.endswith(".py"))
    generation = "v1" if "verifiers.v1" in code else "dev" if re.search(r"vf\.Taskset\[", code) else "v0"
    if re.search(r"SingleTurnEnv", code):
        turns = "single-turn"
    elif re.search(r"MultiTurnEnv|TextArenaEnv|env_response", code):
        turns = "multi-turn"
    else:
        # v1: the harness decides the turn structure; a reward on the last reply is single-answer
        turns = "single-turn" if generation == "v1" and "last_reply" in code else "unknown"
    needs = [name for name, pattern in INFRASTRUCTURE.items() if re.search(pattern, code)]
    services = outside_service(code)
    if services:
        needs.append("outside service")
    lines = sum(content.count(b"\n") for name, content in sources.items() if name.endswith(".py"))
    # A usable reference: an `answer` field on the task data, or rows built with a non-empty answer.
    has_reference = bool(
        re.search(r"^\s+answer\s*:\s*(str|int|float)", code, re.M)
        or re.search(r"[\"']answer[\"']\s*:(?!\s*[\"']{2}\s*[,}\n])", code)
        or re.search(r"\banswer\s*=(?!=)(?!\s*[\"']{2}\s*[,)\n])", code)
    )
    automatic = generation in ("v0", "v1") and turns in ("single-turn", "multi-turn") and not needs and has_reference
    return {"generation": generation, "turns": turns, "needs": needs, "python_lines": lines,
            "has_reference": has_reference, "automatic": automatic, "outside_hosts": services}


SPACES_API = "https://huggingface.co/api/spaces"


def discover_spaces(days: int = 90, limit: int = 40, tag: str = "openenv") -> list[dict]:
    """OpenEnv environments on the Hugging Face Hub: Spaces tagged `openenv`, updated within
    `days`, most liked first. The public listing only; nothing is cloned or run."""
    import json
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    url = f"{SPACES_API}?filter={tag}&sort=lastModified&direction=-1&limit={min(max(limit * 4, 100), 1000)}"
    rows = json.loads(_get(url).decode())
    recent = [r for r in rows if (r.get("lastModified") or "")[:10] >= cutoff]
    recent.sort(key=lambda r: (-(r.get("likes") or 0), r["id"]))
    return [
        {"id": r["id"], "likes": r.get("likes") or 0, "updated": (r.get("lastModified") or "")[:10],
         "tags": [t for t in r.get("tags", []) if t != tag and not t.startswith("region:") and t not in ("docker", "space")][:5],
         "init": f"hf:{r['id']}"}
        for r in recent[:limit]
    ]  # fmt: skip


def discover(days: int = 90, min_releases: int = 2, limit: int = 40, min_stars: int = 0) -> list[dict]:
    """Public Hub environments with a maintenance record: updated within `days` and at
    least `min_releases` published versions. Uses the `prime` CLI for the listing."""
    import json
    import subprocess
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    recent: list[dict] = []
    for page in range(1, 30):
        out = subprocess.run(
            ["prime", "env", "list", "--sort", "updated_at", "--order", "desc", "-n", "100", "-p", str(page), "--output", "json"],
            capture_output=True, text=True, check=True,
        ).stdout  # fmt: skip
        rows = json.loads(out).get("environments", [])
        fresh = [r for r in rows if r["updated_at"][:10] >= cutoff]
        recent += fresh
        if len(fresh) < len(rows) or not rows:
            break
    recent = [r for r in recent if r.get("stars", 0) >= min_stars]
    recent.sort(key=lambda r: (-r.get("stars", 0), r["environment"]))
    found = []
    for row in recent[:limit]:
        entry = {"id": row["environment"], "stars": row.get("stars", 0), "updated": row["updated_at"][:10],
                 "description": (row.get("description") or "")[:100]}  # fmt: skip
        try:
            info = describe(row["environment"])
            entry.update(classify(info["sources"]))
            entry.update(version=info["version"], releases=len(info["releases"]), verifiers=info["declared"],
                         active=len(info["releases"]) >= min_releases)  # fmt: skip
        except Exception as exc:  # no wheel, private index, network
            entry.update(error=f"{type(exc).__name__}: {exc}"[:120], active=False, automatic=False)
        found.append(entry)
    return found
