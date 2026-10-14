"""Look up what `falsepass init` needs from the Environments Hub package index."""

from __future__ import annotations

import io
import re
import urllib.request
import zipfile

HUB = "https://hub.primeintellect.ai"
DEFAULT_VERIFIERS = "0.3.1"


def _get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "falsepass"}), timeout=60) as r:
        return r.read()


def _version_key(version: str) -> tuple:
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"[.\-+]", version))


def verifiers_pin(specifier: str) -> str:
    """The verifiers version to install for a declared requirement such as `>=0.3.0`.
    An exact pin is the only version that installs. Environments built for a 0.1.15.dev
    build do not import under 0.3.x: keep their own floor. Otherwise the default."""
    exact = re.search(r"==\s*([\w.]+)", specifier)
    bound = re.search(r">=\s*([\w.]+)", specifier)
    if exact:
        return exact.group(1)
    return bound.group(1) if bound and ".dev" in bound.group(1) else DEFAULT_VERIFIERS


def describe(env_id: str, version: str | None = None) -> dict:
    """Package name, version, importable module, verifiers pin and source files for `owner/environment`."""
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
    version = version or max(wheels, key=_version_key)
    if version not in wheels:
        raise RuntimeError(f"version {version} not found; available: {sorted(wheels, key=_version_key)}")

    archive = zipfile.ZipFile(io.BytesIO(_get(wheels[version])))
    names = archive.namelist()
    metadata = archive.read(next(n for n in names if n.endswith(".dist-info/METADATA"))).decode()
    top_level = sorted({n.split("/")[0].removesuffix(".py") for n in names if ".dist-info" not in n and n.endswith(".py")})
    requirement = re.search(r"^Requires-Dist: verifiers(\[[^\]]*\])?[ \t]*(.*)$", metadata, re.M)
    pin = verifiers_pin(requirement.group(2) if requirement else "")
    project_urls = re.findall(r"^Project-URL: (.+)$", metadata, re.M)
    sources = {n: archive.read(n) for n in names if ".dist-info" not in n and n.endswith((".py", ".toml", ".md"))}
    return {
        "sources": sources,
        "releases": sorted(wheels, key=_version_key),
        "project_urls": project_urls,
        "id": env_id,
        "package": package,
        "version": version,
        "module": top_level[0] if top_level else package,
        "index": index,
        "verifiers": pin,
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
    lines = sum(content.count(b"\n") for name, content in sources.items() if name.endswith(".py"))
    # A usable reference: an `answer` field on the task data, or rows built with a non-empty answer.
    has_reference = bool(
        re.search(r"^\s+answer\s*:\s*(str|int|float)", code, re.M)
        or re.search(r"[\"']answer[\"']\s*:(?!\s*[\"']{2}\s*[,}\n])", code)
        or re.search(r"\banswer\s*=(?!=)(?!\s*[\"']{2}\s*[,)\n])", code)
    )
    automatic = generation in ("v0", "v1") and turns in ("single-turn", "multi-turn") and not needs and has_reference
    return {"generation": generation, "turns": turns, "needs": needs, "python_lines": lines,
            "has_reference": has_reference, "automatic": automatic}


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
