# falsepass

Probe RL environment verifiers for **false passes** (a known-bad completion is
rewarded) and **false fails** (a known-good completion is not).

An RL environment is a task dataset plus a verifier that turns a model's output into
reward. `falsepass` treats that verifier as the system under test: it calls the
environment's own reward code on completions whose correctness is known in advance and
reports every disagreement. No model, no rollouts, no GPU.

It targets environments built on Prime Intellect's
[verifiers](https://github.com/PrimeIntellect-ai/verifiers) library, published on the
Environments Hub or still in a local folder, across all three verifiers API generations
(legacy rubrics, 0.1.15.dev, v1 tasksets).

## Install

Needs [uv](https://docs.astral.sh/uv/) on the PATH (every audit runs in its own
throw-away virtualenv) and Python 3.11 or newer.

```bash
uvx --from git+https://github.com/Super-Eric/falsepass falsepass demo   # try it, nothing to install
uv tool install git+https://github.com/Super-Eric/falsepass            # or keep `falsepass` on the PATH
pip install git+https://github.com/Super-Eric/falsepass                # or into an existing environment
```

## Quick start

```bash
falsepass demo                            # audits a bundled toy environment with one planted defect (about a minute)
falsepass init owner/environment          # a Hub environment: package, version and verifiers pin read from the Hub
falsepass init ./my-environment           # or your own, from its folder (pyproject.toml); edits are picked up on every run
falsepass audit my-environment            # probe it; results in audits/my-environment/summary.md
falsepass results my-environment --json   # headline numbers and failing probes, for scripts and agents
```

`demo` writes the toy environment to `./falsepass-demo` and audits it. Its grader pays
when the reference appears anywhere in the answer, and the summary shows that one line
from several sides: the static scan points at it, and the probes that list several
answers (P5), retract the answer (P9) or deny it inside the answer slot (P15) are paid.

## For environment authors: check before you publish

Run `falsepass init ./your-env && falsepass audit your-env` locally, or add the GitHub
Action to the environment's repository so every push is checked:

```yaml
# .github/workflows/falsepass.yml
on: [push, pull_request]
jobs:
  falsepass:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: Super-Eric/falsepass@main
        with:
          path: .                         # folder with the environment's pyproject.toml
          fail-on: "ceiling>0.1 errors>0" # fail when a known-bad answer earns over 0.1, or scoring raises
```

The step summary shows the probe table. Gate names: `ceiling` (highest reward any
known-bad completion got), `lazy` (best average reward of one fixed strategy that never
uses the answer), `false_fail` (share of correct answers refused), `errors`. Datasets that
need a login or an API key at load time cannot be audited in CI without that secret.

## For coding agents

- **MCP server.** `falsepass mcp` serves the commands as tools (`falsepass_scan`,
  `_init`, `_audit`, `_results`, `_repro`, `_discover`) over stdio. Any MCP client starts
  it with `uvx --from "falsepass[mcp] @ git+https://github.com/Super-Eric/falsepass" falsepass mcp`;
  in Claude Code, for example: `claude mcp add falsepass -- uvx --from "falsepass[mcp] @ git+https://github.com/Super-Eric/falsepass" falsepass mcp`.
- **Skill.** `skills/falsepass/SKILL.md` is a ready-made audit procedure in the Agent
  Skills format: how to run an audit, how to tell a defect from a design choice, and how
  to write it up for a maintainer. Copy the folder into the agent's skills directory
  (for Claude Code, `~/.claude/skills/`).
- **Plain CLI.** Every command is non-interactive and writes files; `results --json` and
  `scan --json` print JSON; `audit --fail-on ...` exits 1 when a condition is met. The
  output formats are described in `docs/OUTPUT.md`.

Whatever drives it, the output is a list of leads. An agent should reproduce a lead and
read the reward code before calling it a defect, and should not report an environment as
broken on the strength of a probe table.

## All commands

```bash
falsepass init owner/environment          # set up audits/<name>/ for a Hub environment, or pass a local folder
falsepass audit environment               # no adapter needed for Q&A tasks: the answer format is inferred
falsepass audit environment --search 120  # add mutation search (scorer calls per direction and example)
falsepass audit environment --fail-on 'ceiling>0.1' --fail-on 'errors>0'   # CI gate, exit 1 when met
falsepass results environment [--json]    # the last run: summary.md, or a compact JSON digest
falsepass scan path/to/source [--json]    # static pre-scan only, nothing is installed or run
falsepass discover --days 90 --limit 160  # recently maintained Hub environments and what each needs
falsepass repro environment               # stand-alone script that re-scores the exact failing completions
falsepass rollout environment -n 20       # what a local model's own replies earn (Ollama)
falsepass propose environment -n 5        # a local model writes wrong answers and climbs on the reward
falsepass draft environment               # REPORT.draft.md: candidate findings for manual review
falsepass overview -o RESULTS.md          # one table over every audit on disk
falsepass container environment           # container tasks: the key, and mutations of it, written as the output file (Docker)
falsepass upstream environment            # find the source repository on GitHub and compare it with the audit
falsepass verify-fix environment --source ../patched/pkg   # rerun the stored repro on a patch: which lines change
falsepass demo                            # the toy environment above
falsepass mcp                             # MCP server over stdio (`falsepass[mcp]`)
```

Each audit runs in a throw-away virtualenv pinned by `audits/<env>/env.toml`, because
Hub environments are built for mutually incompatible `verifiers` versions. Output:
`audits/<env>/probe-results.json` and `summary.md`. In a clone of this repository, prefix
the commands with `uv run`; `uv run pytest` runs the kit's own tests.

## Checks

| ID | Check | A correct verifier… |
|---|---|---|
| P1 | reference answer, in the requested format | gives full reward |
| P2 | the same answer with harmless variation | gives full reward |
| P3 | empty / whitespace-only | gives nothing |
| P4 | the prompt echoed back | gives nothing |
| P5 | every candidate answer at once | gives nothing |
| P6 | wrong answer in perfect format | gives nothing |
| P7 | confident wrong answer | gives nothing |
| P8 | wrong answer plus a note to the grader (LLM-judge envs) | gives nothing |
| P9 | right answer buried in filler, then retracted | gives nothing |
| P10 | malformed output | gives nothing and does not raise |
| P11 | the same completion ten times | gives the same score |
| P12 | dataset: empty or duplicate prompts, conflicting labels | finds none |
| P13 | `NaN`, `Infinity`, an integer answer plus 0.9 (JSON answers: floats set to NaN, integers plus 0.9 or Infinity) | gives nothing and does not raise |
| P14 | the correct answer, with the answer tag or `\boxed{}` also named in prose before or after it | gives full reward |
| P15 | the answer denied, or named next to a wrong value, inside the answer slot (`\boxed{R \text{ is wrong}}`, `\boxed{W = R}`) | gives nothing |
| T7 | the correct answer after one no-op tool call | gives full reward (a judgement call where the harness offers no tools) |
| C0–C2 | container tasks: the key written as the output file (C0), with NaN / Infinity / fractional counts (C1), emptied, truncated or replaced by an input file (C2) | pays C0 in full and no mutation in full |
| T4 | multi-turn: replay an episode after an unrelated one | gives the same reward and transcript |
| T5 | multi-turn: a policy that never answers | is cut off and not rewarded |
| T6 | tool use: six policies built from the tool schemas alone (no tool call, every tool once with empty arguments, the finishing tool at once, …) | are not rewarded and raise nothing |
| X*, T1–T3 | environment-specific probes defined in an adapter | — |
| S1 | search: mutate known-bad completions, never adding the answer, toward higher reward | finds nothing above zero |
| S2 | search: mutate the correct completion without changing the answer, toward lower reward | finds nothing below full |
| S3 | search: completions that mention the answer without committing to it | finds nothing above zero |

**Search** (`--search N`) is a seeded beam search over chains of small named mutations.
A hit is reported as a minimised recipe, e.g. `nothing + pad_think`. S2 hits are
brittleness candidates and need a human eye: whether `"B"` in quotes should count as
`B` is a judgement call.

**Static pre-scan** runs with every audit on `audits/<env>/src` and on its own with
`falsepass scan`. Narrow AST rules, each derived from a defect found in a real audit;
every hint is printed with the verdict of the probe that tests it
(`confirmed: P6 failed 25/25`, `not confirmed`, `not checked`).

**Adapter inference.** Without an `adapter.py`, the answer format is found by scoring
the reference answer in each candidate format (from the parser and the prompt text) and
keeping the one the environment's own reward code pays best. Wrong answers come from the
label set, a shifted number, or another row. This covers environments with reference
answers, single- or multi-turn; tool-use and sandbox environments, and games whose rows
carry no answer, need `falsepass init --manual` or a hand-written script. A tag that the
prompt uses to enclose its input (`<conversation>…</conversation>`) is not taken for an
answer format, and when every format scores the same the plain answer is used. As a
control, automatic mode compares the reference with an empty completion and marks the
whole result **inconclusive** when the reward does not separate them.

**Multi-turn.** Multi-turn environments are scored by running their own rollout loop
with a scripted stand-in for the model (`V0RolloutScorer`), so follow-up prompts, stop
conditions and per-episode state are exercised. Search mutates every turn of a script.
A script entry can also carry tool calls (`{"tool_calls": [{"name": …, "arguments": {…}}]}`),
which the environment executes with its real tools. Automatic mode also accepts tool-use
environments whose tools run in-process: there is no answer format to infer, so it plays
the six T6 policies and T5 and says in the summary that no control was possible.
Policies that have to read the task to find a hole are still hand work
(`falsepass.model.Policy`: a function from the conversation so far to the next reply).

**Rollout check.** `falsepass rollout <env>` sends a sample of tasks to a model behind
an OpenAI-compatible endpoint — a local Ollama server by default, so nothing is billed —
and scores the model's own replies with the same scorer the probes use. It flags rows
for reading (paid without the reference in the reply; reference in the reply but not
paid in full; paid more than the reference; cut off at the token cap) and never calls a
flag a finding. For tool-use environments the model is driven through verifiers' own
client, so it receives the tool schemas and its tool calls are executed. `--endpoint`,
`--model` and `--tag` run a second model next to the first.

**Model-written wrong answers.** `falsepass propose <env>` asks the model for a reply
that is on the subject and wrong, scores it, and has it rewritten a few times with the
grader's score as feedback. For closed answers the reference must not occur in the
reply (checked in code); for open-ended ones the wrongness is the model's own opinion,
so every row is something to read, not a finding.

**Discovery.** `falsepass discover` lists Hub environments updated recently with more
than one published version, and reads each wheel to say what it needs (sandbox, tools,
LLM judge, API key, reference answers). It needs the `prime` CLI for the listing.

Three headline numbers per environment: the **false-pass ceiling** (highest reward any
known-bad completion received), the **lazy-policy reward** (best average reward of one
fixed wrong strategy across examples) and the **false-fail rate** (share of known-good
completions denied full reward). `summary.md` also shows which reward term paid on
known-bad completions.

## From a probe run to a report

A probe run produces **leads, not findings**. To turn one into a finding:

1. `falsepass repro <env>` writes a script that depends only on verifiers and the
   environment package and re-scores the exact completions (kept in `completions.json`).
   Run it twice from a clean virtualenv; the output must be identical.
2. Read the reward code and find the line that causes it. Decide whether it is a defect
   or a design choice: a grader that is exploitable on purpose is an observation.
3. `falsepass draft <env>` writes `REPORT.draft.md` with each failing probe group as a
   candidate, the confirmed static hints attached, and the places marked where a person
   has to judge. Rate what holds with `docs/SEVERITY.md`.
4. Have someone who did not write the report check every number against the stored output.

## From a report to a fix upstream

`falsepass upstream <env>` searches GitHub for the environment's source (package name in a
`pyproject.toml`, then its most distinctive class and function names), compares every
audited file with the repository's default branch, line endings ignored, and prints what
a contributor needs: default branch, licence, last push, open pull requests, how many
recent merges came from outside. `falsepass verify-fix <env> --source <patched folder>`
reruns the stored repro with the patched package and prints the lines that changed; a fix
is ready when only the finding's lines move.

## Adding an environment

`falsepass init owner/environment` writes `audits/<name>/env.toml` (package, version,
module, index, verifiers pin — all read from the Hub wheel) and unpacks the wheel's source
into `audits/<name>/src` for the static scan. `falsepass init ./folder` does the same from a
local `pyproject.toml` and records the folder as `source`, so the folder itself is
installed and scanned on every run. For Q&A environments that is all.
Otherwise add `--manual` and fill in the generated `adapter.py`: how to load examples, how
to write an answer the way the prompt asks, and what a wrong answer looks like.

## Track record

Used on 35 public Hub environments between 2026-10-12 and 2026-10-14: 104 findings after
review (9 Critical, 32 High, 21 Medium, 42 Low on `docs/SEVERITY.md`), each with a
stand-alone repro run twice. Most environments were found and first probed in automatic
mode; every finding was then confirmed by reading the code and checked by a second
reviewer. Individual environments are not named here: maintainers are told first, and
fixes go to them as pull requests.

## Limits

- Leads need a person. The tool finds where a grader pays the wrong thing; deciding
  whether that is a defect, and how bad, is manual.
- Search is shallow: fixed text mutations, a few hundred scorer calls. It does not train
  a model against the reward.
- Container tasks have no automatic mode, and environments on hosted sandboxes or with a
  paid LLM judge are not covered without credentials.
- Only verifiers-based environments are supported.

## Related tools

- [rewardlint](https://github.com/junglezke/rewardlint) — adversarial corpus for single
  reward functions. `falsepass` runs it through an environment's reward code when the
  reference can be substituted, and prints its verdict in `summary.md`.
- ratctl — static and LLM-assisted scanner of environment source for sandbox exploit
  patterns.
- [VerifierLab](https://github.com/fraware/verifierlab) — attack campaigns against a
  wrapped grader under a declared budget, with a ground-truth oracle.
- [vlabs-integrity](https://github.com/verifiablelabs/vlabs-integrity) — mutation-kill and
  isomorphic-relabelling audit for coding rewards with a trusted reference.
- [harden-v0](https://github.com/few-sh/harden-v0) — LLM hacker–fixer loop that hardens
  Harbor-format agent benchmark verifiers.

## Layout

```
falsepass/           the probe kit (falsepass/mcp_server.py: the MCP tools)
tests/               its tests
examples/toy-env/    the demo environment, with its planted defect
skills/falsepass/    agent skill: the audit procedure
action.yml           the GitHub Action
docs/SEVERITY.md     the rating scale; docs/OUTPUT.md: the output files
audits/<env>/        created by `falsepass init`; ignored by git
```

## Licence

Apache-2.0, see `LICENSE`.
