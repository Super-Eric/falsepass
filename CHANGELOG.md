# Changelog

## 0.2.0 — 2026-10-14

- Local environments: `falsepass init ./folder` audits an environment from its
  `pyproject.toml` before it is published; the folder is reinstalled on every run.
- Runs when installed from git or a wheel (`uvx --from git+…`), not only from a checkout.
- `falsepass demo`: a bundled toy environment with one planted defect.
- `falsepass results [--json]` and `falsepass scan --json` for scripts and agents;
  output formats in `docs/OUTPUT.md`.
- `falsepass mcp`: the commands as MCP tools (`pip install 'falsepass[mcp]'`).
- GitHub Action (`uses: Super-Eric/falsepass@main`) for environment repositories.
- Agent skill in `skills/falsepass/`.
- verifiers pin: a declared floor above 0.3.1 (e.g. `>=0.4.0`) is now used instead of
  0.3.1, and `verify-fix` raises the pin when the patched source needs a newer verifiers.
- `--version`.

## 0.1.0 — 2026-10-14

First public release.
