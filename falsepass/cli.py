"""`falsepass audit <env>` — run the probe kit against one audited environment.

Hub environments are built for different, mutually incompatible verifiers
versions, so each audit runs in its own throw-away virtualenv. The pins live in
`audits/<env>/env.toml`; this command turns them into one `uv run` invocation.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path


def install_args(env: dict, source: str | Path | None = None) -> list[str]:
    """The `uv run --with` arguments that install verifiers and the environment.

    `env.toml` may name local trees instead of releases: `source` for the environment
    package (a folder with a pyproject.toml) and `verifiers_source` for verifiers itself.
    `source` given here (a patched copy, an upstream checkout) overrides the file's."""
    source = source or env.get("source")
    if env.get("framework") == "hud":
        args = ["--with", f"hud=={env['hud']}" if env.get("hud") else "hud"]
        if source:
            args += ["--with", str(source)]
    elif env.get("framework") == "inspect":
        args = ["--with", f"inspect-ai=={env['inspect_ai']}" if env.get("inspect_ai") else "inspect-ai"]
        if source:
            args += ["--with", str(source)]
        elif env.get("package"):
            args += ["--with", f"{env['package']}=={env['version']}" if env.get("version") else env["package"]]
    else:
        args = ["--with", str(env["verifiers_source"]) if env.get("verifiers_source") else f"verifiers=={env['verifiers']}"]
        args += ["--with", str(source) if source else f"{env['package']}=={env['version']}"]
    for requirement in env.get("extra", []):
        args += ["--with", requirement]
    if env.get("index"):
        args += ["--extra-index-url", env["index"]]
    return args


def self_install_args() -> list[str]:
    """How the throw-away virtualenv gets this same copy of falsepass: the checkout it runs
    from, else the URL it was installed from (a git commit, a local wheel), else the
    release with this version number."""
    root = Path(__file__).resolve().parent.parent
    if (root / "pyproject.toml").is_file() and (root / "falsepass" / "__init__.py").is_file():
        return ["--with-editable", str(root)]
    from importlib import metadata

    try:
        dist = metadata.distribution("falsepass")
    except metadata.PackageNotFoundError:
        return ["--with", "falsepass"]
    direct = json.loads(dist.read_text("direct_url.json") or "{}")
    if direct.get("url"):
        vcs = direct.get("vcs_info")
        url = f"{vcs['vcs']}+{direct['url']}@{vcs['commit_id']}" if vcs else direct["url"]
        return ["--with", f"falsepass @ {url}"]
    return ["--with", f"falsepass=={dist.version}"]


def uv_command(audit_dir: Path, extra: list[str], module: str = "falsepass.runner", source: str | Path | None = None) -> list[str]:
    env = tomllib.loads((audit_dir / "env.toml").read_text())["env"]
    command = [
        "uv", "run", "--isolated", "--no-project", "--python", env.get("python", "3.12"),
        *self_install_args(), "--with", "rewardlint", *install_args(env, source),
    ]  # fmt: skip
    package = ["--package", env["package"]] if env.get("package") else []
    return [*command, "python", "-m", module, str(audit_dir), *package, *extra]


ADAPTER_TEMPLATE = '''"""falsepass adapter for {id}."""

import random

from falsepass import Adapter, Example
from falsepass.scorers import V0RubricScorer  # or V1TaskScorer / DevTasksetScorer


class Env(Adapter):
    name = "{id}"

    def load(self, n, seed):
        import verifiers as vf

        self.env = vf.load_environment("{package}")
        self.rows = self.env.get_dataset()
        picks = random.Random(seed).sample(range(len(self.rows)), n)
        return V0RubricScorer(self.env), [self._example(i) for i in picks]

    def _example(self, i) -> Example:
        row = self.rows[i]
        return Example(f"row-{{i}}", row["prompt"][-1]["content"], row["answer"], row)

    def hygiene_examples(self, limit):
        return [self._example(i) for i in range(min(limit, len(self.rows)))]

    def render(self, answer, example):
        """Write `answer` exactly the way the environment's prompt asks for it."""
        raise NotImplementedError

    def wrong_answer(self, example):
        """Return an answer that is definitely wrong for this example."""
        raise NotImplementedError


ADAPTER = Env()
'''


def scaffold(args) -> int:
    from falsepass import hub

    from falsepass import hud_support, inspect_support

    local = Path(args.env_id)
    if local.exists() and (args.framework == "hud" or (args.framework is None and hud_support.uses_hud(local))):
        info = hud_support.describe(local, args.answers)
    elif local.is_file() and local.suffix == ".py" and inspect_support.uses_inspect(local):
        info = inspect_support.describe_file(local, args.task)
    elif (local / "pyproject.toml").is_file() and (args.framework == "inspect" or inspect_support.uses_inspect(local)):
        info = inspect_support.describe_folder(local, args.task)
    elif (local / "pyproject.toml").is_file():
        info = hub.describe_local(local)
    elif not local.exists() and (args.framework == "inspect" or args.env_id.startswith("inspect_evals/")):
        info = inspect_support.describe_registry(args.env_id)
    elif local.exists() or args.env_id.count("/") != 1:
        print(f"falsepass: {args.env_id} is neither a Hub slug (owner/environment), an Inspect task (a .py file, or "
              "a registry name with --framework inspect) nor a folder with a pyproject.toml", file=sys.stderr)
        return 1
    else:
        info = hub.describe(args.env_id, args.version)
    if args.verifiers:
        info["verifiers"] = args.verifiers
    if args.extra:
        info["extra"] = [*info.get("extra", []), *args.extra]
    name = args.name or info["id"].split("/")[-1].lower().replace("_", "-")
    audit_dir = Path("audits") / name
    if audit_dir.exists():
        print(f"falsepass: {audit_dir} already exists", file=sys.stderr)
        return 1
    audit_dir.mkdir(parents=True)
    keys = ("id", "framework", "task", "package", "version", "module", "index", "verifiers", "inspect_ai", "hud", "source", "answers", "extra")
    required = ("index",) if info.get("framework", "verifiers") == "verifiers" else ()
    lines = [f"{key} = {json.dumps(info[key])}" for key in keys if info.get(key) or key in required]
    declared = f"# declared by the package: {info['declared']}\n" if info.get("declared") else ""
    (audit_dir / "env.toml").write_text("[env]\n" + "\n".join(lines) + "\n" + declared)
    for source, content in info.get("sources", {}).items():  # the wheel's source, for the static pre-scan
        target = audit_dir / "src" / source
        if target.resolve().is_relative_to((audit_dir / "src").resolve()):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    if args.manual:
        (audit_dir / "adapter.py").write_text(ADAPTER_TEMPLATE.format(id=info["id"], package=info.get("module") or info.get("task", "")))
        print(f"created {audit_dir}/env.toml and adapter.py — fill in render() and wrong_answer(), then:")
    elif info.get("framework") == "hud":
        how = "with the reference answers given" if info.get("answers") else "answer-free probes only; add --answers for all"
        print(f"created {audit_dir}/env.toml — each reply is graded by its task template ({how}):")
    elif info.get("framework") == "inspect":
        print(f"created {audit_dir}/env.toml — each reply goes through the task's own solver chain and scorers:")
    else:
        print(f"created {audit_dir}/env.toml — no adapter needed for single-turn environments with reference answers:")
    if info.get("source"):
        print(f"  (installed from {info['source']} on every run, so edits are picked up)")
    if info.get("others"):
        print(f"  (task {info['task']}; the others can be audited with --task and --name: {', '.join(info['others'])})")
    print(f"  falsepass audit {name}")
    return 0


def demo_source() -> Path:
    """The toy environment: bundled in the wheel as `falsepass/_demo`, in a checkout under examples/."""
    packaged = Path(__file__).resolve().parent / "_demo"
    return packaged if packaged.is_dir() else Path(__file__).resolve().parent.parent / "examples" / "toy-env"


def demo(target: Path) -> int:
    import shutil

    if not target.exists():
        shutil.copytree(demo_source(), target, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    audit_dir = Path("audits") / "falsepass-toy-env"
    if not (audit_dir / "env.toml").is_file():
        code = main(["init", str(target)])
        if code:
            return code
    print(f"auditing the toy environment in {target} (its grader pays `answer in reply`: expect several probes to fail, all from that one line)", flush=True)
    return subprocess.call(uv_command(audit_dir, ["-n", "10"]))


def main(argv: list[str] | None = None) -> int:
    from falsepass import __version__

    parser = argparse.ArgumentParser(
        prog="falsepass", description="Probe RL environment verifiers for false passes and false fails.",
        epilog="Results are leads to reproduce and read, not verdicts. Docs: https://github.com/Super-Eric/falsepass",
    )  # fmt: skip
    parser.add_argument("--version", action="version", version=f"falsepass {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    audit = sub.add_parser("audit", help="probe one environment in an isolated virtualenv")
    audit.add_argument("env", help="name of a folder under audits/, or a path to one")
    audit.add_argument("--print-command", action="store_true", help="show the uv command and exit")
    audit.add_argument("--source", type=Path, help="install the environment from this local folder instead of its release")
    roll = sub.add_parser("rollout", help="score a local model's own replies (cross-check; Ollama by default)")
    roll.add_argument("env")
    roll.add_argument("--print-command", action="store_true", help="show the uv command and exit")
    roll.add_argument("--source", type=Path, help="install the environment from this local folder instead of its release")
    prop = sub.add_parser("propose", help="let a local model write wrong answers and climb on the reward (single-turn)")
    prop.add_argument("env")
    prop.add_argument("--print-command", action="store_true", help="show the uv command and exit")
    prop.add_argument("--source", type=Path, help="install the environment from this local folder instead of its release")
    init = sub.add_parser("init", help="set up audits/<name>/ for a Hub environment or a local environment folder")
    init.add_argument("env_id", help="Hub slug (owner/environment), a local folder with a pyproject.toml, an Inspect task file, "
                                     "an Inspect registry name such as inspect_evals/gsm8k, or a HUD environment folder")
    init.add_argument("--name", help="folder name under audits/ (default: the environment name)")
    init.add_argument("--version", help="environment version (default: latest on the Hub)")
    init.add_argument("--verifiers", help="verifiers version to pin (default: inferred from the wheel)")
    init.add_argument("--manual", action="store_true", help="also write an adapter.py skeleton to fill in")
    init.add_argument("--framework", choices=["verifiers", "inspect", "hud"], help="force the framework (default: detected)")
    init.add_argument("--answers", help="HUD: a JSON file with reference answers (slug -> answer, or a list in task order)")
    init.add_argument("--task", help="Inspect: which @task to audit when a file or project defines several")
    init.add_argument("--with", dest="extra", action="append", default=[], metavar="REQ",
                      help="extra requirement to install for the audit, e.g. 'inspect-evals[math]' (repeatable)")
    disc = sub.add_parser("discover", help="list actively maintained Hub environments and what each needs")
    disc.add_argument("--days", type=int, default=90, help="updated within this many days")
    disc.add_argument("--min-releases", type=int, default=2, help="published versions required to count as maintained")
    disc.add_argument("--min-stars", type=int, default=0)
    disc.add_argument("--limit", type=int, default=40, help="most-starred recent environments to inspect")
    disc.add_argument("-o", "--output", type=Path, help="also write the full list as JSON")
    repro = sub.add_parser("repro", help="write a stand-alone reproduction script for the failing probes")
    repro.add_argument("env")
    repro.add_argument("--probe", action="append", help="only these probe codes, e.g. --probe P6 (repeatable)")
    repro.add_argument("--per-group", type=int, default=2, help="examples per failing probe variant")
    drafted = sub.add_parser("draft", help="write REPORT.draft.md: candidate findings for manual review")
    drafted.add_argument("env")
    over = sub.add_parser("overview", help="one table over every audited environment")
    over.add_argument("-o", "--output", type=Path)
    box = sub.add_parser("container", help="probe a container task by writing its key, and mutations of it, as the output file (Docker)")
    box.add_argument("env")
    box.add_argument("--print-command", action="store_true", help="show the uv command and exit")
    box.add_argument("--source", type=Path, help="install the environment from this local folder instead of its release")
    up = sub.add_parser("upstream", help="find the environment's source repository on GitHub and compare it with the audit")
    up.add_argument("env")
    up.add_argument("--repo", help="check this owner/name instead of searching")
    up.add_argument("--limit", type=int, default=5, help="search results per query")
    fix = sub.add_parser("verify-fix", help="rerun a stored repro against a patched source and show what changed")
    fix.add_argument("env")
    fix.add_argument("--source", type=Path, required=True, help="the patched environment package (a folder with pyproject.toml)")
    fix.add_argument("--script", default="repro.py", help="repro script in the audit folder (default: repro.py)")
    fix.add_argument("--print-command", action="store_true", help="show the uv command and exit")
    res = sub.add_parser("results", help="print the last probe run of an environment (summary, or --json for scripts and agents)")
    res.add_argument("env")
    res.add_argument("--json", action="store_true", help="headline numbers, failing probes and hints as JSON")
    trial = sub.add_parser("demo", help="audit a bundled toy environment with one planted defect (about a minute)")
    trial.add_argument("--dir", type=Path, default=Path("falsepass-demo"), help="where to write the toy environment")
    sub.add_parser("mcp", help="serve falsepass as MCP tools over stdio (needs the `mcp` extra)")
    scan = sub.add_parser("scan", help="static pre-scan of reward source (no environment install needed)")
    scan.add_argument("path", type=Path, help="a Python file or a source directory")
    scan.add_argument("--json", action="store_true", help="print the hints as JSON")
    args, extra = parser.parse_known_args(argv)

    if args.command in ("repro", "draft"):
        from falsepass import draft, repro

        audit_dir = Path(args.env) if (Path(args.env) / "probe-results.json").is_file() else Path("audits") / args.env
        if not (audit_dir / "probe-results.json").is_file():
            print(f"falsepass: no probe-results.json in {audit_dir} — `falsepass audit {args.env}` has not completed there "
                  "(if it stopped at the control, this environment needs a hand-written script)", file=sys.stderr)
            return 1
        if args.command == "repro":
            path = repro.generate(audit_dir, args.probe, args.per_group)
            print(f"wrote {path} — run it twice from a clean virtualenv (command in its docstring)")
        else:
            print(f"wrote {draft.draft(audit_dir)} — candidates only, review before reporting")
        return 0

    if args.command == "results":
        from falsepass import report

        audit_dir = Path(args.env) if (Path(args.env) / "probe-results.json").is_file() else Path("audits") / args.env
        if not (audit_dir / "probe-results.json").is_file():
            print(f"falsepass: no probe-results.json in {audit_dir}; run `falsepass audit {args.env}` first", file=sys.stderr)
            return 1
        payload = json.loads((audit_dir / "probe-results.json").read_text())
        print(json.dumps(report.digest(payload), indent=1) if args.json else report.summary(payload))
        return 0

    if args.command == "demo":
        return demo(args.dir)

    if args.command == "mcp":
        from falsepass import mcp_server

        return mcp_server.main()

    if args.command == "overview":
        from falsepass import draft

        table = draft.overview(Path("audits"))
        if args.output:
            args.output.write_text(table)
        print(table)
        return 0

    if args.command == "discover":
        from falsepass import hub

        found = hub.discover(args.days, args.min_releases, args.limit, args.min_stars)
        if args.output:
            args.output.write_text(json.dumps(found, indent=1))
        print(f"{'stars':>5} {'updated':10} {'rel':>3} {'gen':3} {'turns':11} {'auto':4} needs / environment")
        for e in found:
            if "error" in e:
                print(f"{e['stars']:>5} {e['updated']:10}   ?  ?  ?           no   {e['id']}  ({e['error']})")
                continue
            mark = "yes" if e["automatic"] and e["active"] else "no"
            needs = ",".join(e["needs"]) or "-"
            print(f"{e['stars']:>5} {e['updated']:10} {e['releases']:>3} {e['generation']:3} {e['turns']:11} {mark:4} {needs:22} {e['id']}")
        ready = sum(1 for e in found if e.get("automatic") and e.get("active"))
        print(f"{len(found)} inspected; {ready} maintained and auditable without an adapter.")
        return 0

    if args.command == "scan":
        from falsepass import static

        hints = static.scan(args.path)
        if args.json:
            print(json.dumps([h.to_dict() for h in hints], indent=1))
            return 0
        for h in hints:
            print(f"{h.file}:{h.line}: {h.rule} [{h.where}] {static.RULES[h.rule][1]}\n    {h.evidence}")
        print(f"{len(hints)} hint(s). Hints are reasons to look; run `falsepass audit` to confirm them.")
        return 0

    if args.command == "init":
        return scaffold(args)

    if args.command == "upstream":
        from falsepass import upstream

        audit_dir = Path(args.env) if (Path(args.env) / "env.toml").is_file() else Path("audits") / args.env
        print(upstream.describe(upstream.locate(audit_dir, repo=args.repo, limit=args.limit)))
        return 0

    if args.command == "verify-fix":
        from falsepass import verify

        audit_dir = Path(args.env) if (Path(args.env) / "env.toml").is_file() else Path("audits") / args.env
        source = args.source.resolve()
        if args.print_command:
            print(shlex.join(verify.command_for(audit_dir, args.script, source)))
            return 0
        found, stderr, code = verify.run(audit_dir, source, args.script)
        if code != 0:
            print(f"falsepass: the patched run exited {code}:\n{stderr}", file=sys.stderr)
            return 1
        print(verify.describe(found))
        return 0

    audit_dir = Path(args.env)
    if not (audit_dir / "env.toml").is_file():
        audit_dir = Path("audits") / args.env
    module = {"rollout": "falsepass.rollout", "propose": "falsepass.propose", "container": "falsepass.container_probe"}.get(
        args.command, "falsepass.runner"
    )
    source = args.source.resolve() if args.source else None
    command = uv_command(audit_dir, extra, module, source)
    if args.print_command:
        print(shlex.join(command))
        return 0
    sys.stdout.flush()
    return subprocess.call(command)


if __name__ == "__main__":
    sys.exit(main())
