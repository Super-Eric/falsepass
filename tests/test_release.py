"""Release metadata stays in step: the MCP Registry entry and the PyPI README."""

import json
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not (Path(__file__).resolve().parent.parent / "server.json").is_file(), reason="not a release checkout")
def test_server_json_matches_the_package():
    server = json.loads((ROOT / "server.json").read_text())
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert server["version"] == server["packages"][0]["version"] == version
    assert len(server["description"]) <= 100
    # the registry verifies ownership by finding this line in the README PyPI shows
    assert f"mcp-name: {server['name']} " in (ROOT / "README.md").read_text()
