"""upstream: locate the audited tree in a repository and compare it file by file."""

from pathlib import Path

from falsepass import upstream


class FakeGitHub:
    def __init__(self, files):
        self.files = files

    def api(self, path, raw=False):
        if path.startswith("search/code") and "filename:pyproject.toml" in path:
            return {"items": [{"repository": {"full_name": "someone/envs"}}]}
        if path.startswith("search/code") and "language:python" in path:
            return {"items": [{"repository": {"full_name": "other/unrelated"}}]}
        repo = path.split("/")[1] + "/" + path.split("/")[2]
        if path == f"repos/{repo}":
            return {"default_branch": "master", "license": None, "pushed_at": "2026-09-19T00:00:00Z", "has_issues": True}
        if "/git/trees/" in path:
            tree = self.files if repo == "someone/envs" else {"README.md": ""}
            return {"tree": [{"path": p, "type": "blob"} for p in tree]}
        if "/contents/" in path:
            return self.files[path.split("/contents/")[1].split("?")[0]]
        if "/pulls?state=open" in path:
            return [{}] * 3
        if "/pulls?state=closed" in path:
            return [{"merged_at": "x", "author_association": "OWNER"}, {"merged_at": "x", "author_association": "CONTRIBUTOR"}, {}]
        raise AssertionError(path)


def test_finds_the_tree_under_a_prefix_ignores_line_endings_and_reports_facts(tmp_path: Path):
    audit = tmp_path / "demo"
    (audit / "src" / "demo").mkdir(parents=True)
    (audit / "env.toml").write_text('[env]\nid = "o/demo"\npackage = "demo"\nmodule = "demo"\nversion = "1"\nverifiers = "0.3.1"\n')
    (audit / "src" / "demo" / "env.py").write_text("a = 1\r\nb = 2\r\n")
    (audit / "src" / "demo" / "grade.py").write_text("x = 1\n\nclass DemoGradingTask:\n    pass\n")
    gh = FakeGitHub({"environments/demo/demo/env.py": "a = 1\nb = 2\n", "environments/demo/demo/grade.py": "x = 2\n\nclass DemoGradingTask:\n    pass\n",
                     "environments/demo/pyproject.toml": '[project]\nname = "demo"\nversion = "0.1.0"\ndependencies = ["verifiers>=0.4.0", "datasets"]\n'})
    [match] = upstream.locate(audit, gh)
    assert match.repo == "someone/envs" and match.prefix == "environments/demo/"
    assert match.same == ["demo/env.py"] and match.differ == ["demo/grade.py"]
    assert match.verdict == "partly changed since the audit"
    text = upstream.describe([match])
    assert "default branch master" in text and "licence none" in text and "3 open PRs" in text
    assert "1 of the last 2 merged PRs from outside" in text
    assert "upstream pyproject version 0.1.0 (audited release 1, DIFFERENT)" in text and "datasets" in text


def test_identifiers_prefer_long_public_names():
    files = {"a.py": "class MathsTask:\n    def inner_method(self): pass\n\ndef load_environment(): pass\ndef score_one_answer(): pass\n"}
    assert upstream.identifiers(files) == ["score_one_answer", "inner_method", "MathsTask"]


def test_repositories_the_package_names_are_tried_first(tmp_path: Path):
    audit = tmp_path / "demo"
    (audit / "src" / "demo").mkdir(parents=True)
    (audit / "env.toml").write_text('[env]\nid = "o/demo"\npackage = "demo"\nmodule = "demo"\nversion = "1"\n'
                                    'project_urls = ["Source, https://github.com/Named/home.git"]\n')
    (audit / "src" / "demo" / "env.py").write_text('"""Lives in github.com/someone/envs; see https://github.com/sponsors/x. Also github.com/someone/envs."""\na = 1\n')
    assert upstream.declared(audit / "src", ["Source, https://github.com/Named/home.git"]) == ["Named/home", "someone/envs"]
    gh = FakeGitHub({"hub/demo/demo/env.py": '"""Lives in github.com/someone/envs; see https://github.com/sponsors/x. Also github.com/someone/envs."""\na = 1\n'})
    [match] = upstream.locate(audit, gh)
    assert match.repo == "someone/envs" and match.same == ["demo/env.py"]


def test_of_two_copies_of_the_tree_the_identical_one_is_compared(tmp_path: Path):
    audit = tmp_path / "demo"
    (audit / "src" / "demo").mkdir(parents=True)
    (audit / "env.toml").write_text('[env]\nid = "o/demo"\npackage = "demo"\nmodule = "demo"\nversion = "1"\n')
    (audit / "src" / "demo" / "env.py").write_text("a = 1\n")
    gh = FakeGitHub({"demo/env.py": "a = 2\n", "hub/demo/demo/env.py": "a = 1\n"})
    [match] = upstream.locate(audit, gh, repo="someone/envs")
    assert match.prefix == "hub/demo/" and match.verdict == "identical to the audit"


def test_a_search_hit_without_the_audited_files_is_not_presented_as_the_source(tmp_path: Path):
    audit = tmp_path / "demo"
    (audit / "src" / "demo").mkdir(parents=True)
    (audit / "env.toml").write_text('[env]\nid = "o/demo"\npackage = "demo"\nmodule = "demo"\nversion = "1"\n')
    (audit / "src" / "demo" / "env.py").write_text("a = 1\n")
    text = upstream.describe(upstream.locate(audit, FakeGitHub({"other/file.py": ""}), repo="other/unrelated"))
    assert text.startswith("no repository found that holds the audited files")
