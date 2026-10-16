---
title: Your reward function is an assertion. I tested a few dozen of them like one.
date: 2026-10-16
description: Testing the graders of RL environments the way a test engineer tests an assertion: the defects that kept turning up, what a real model did with them, and what to check before you publish an environment.
---

I've spent my career writing tests and, more usefully, distrusting them. The most
expensive bug a test suite can have isn't a failing test. It's a test that passes when
the feature is broken. A false pass ships the defect and tells you everything is fine.

Reinforcement-learning environments for language models have the same component under
another name. An environment is a set of tasks plus a verifier that turns a model's
output into a reward. That verifier is an assertion. If it can be satisfied without
solving the task, the model being trained will find out, because finding out is exactly
what the optimiser is paid for. The field calls this reward hacking. A test engineer
would call it a false positive in the oracle.

So I did what I'd do with any assertion I didn't write. I fed it inputs whose correct
verdict I already knew and counted the disagreements.

## The method

I wrote a small tool, [falsepass]({SITE}/), that loads
an environment and calls its own reward code directly on completions I construct. No
model, no rollouts, no GPU. Each probe has a known verdict:

- **Should pass:** the dataset's reference answer, written the way the prompt asks, and
  then the same answer with harmless variation (spacing, case, `8.0` for `8`).
- **Should fail:** an empty reply; the prompt echoed back; every candidate answer at
  once; a wrong answer in perfect format; the right answer stated and then denied; the
  reference answer of a *different* task.
- **Malformed:** unclosed tags, a NUL byte, two megabytes of text.
- **Repeated:** the same completion scored ten times.

On top of that there's a mutation search that pushes wrong completions toward higher
reward without ever adding the answer, a static scan of the reward source whose hints
are always printed next to the probe that confirms or clears them, and an automatic mode
that works out which answer format an environment wants by scoring the reference in
each candidate format. Multi-turn and tool environments are played by small scripted
policies instead of a model.

Why not run a model and read transcripts? Because a rollout tells you what one model
happened to do on one day. Calling the grader directly tells you what it *would* do
with an output nobody has produced yet, and that's the output an optimiser goes
looking for.

I ran it on a few dozen public environments from the Prime Intellect Environments Hub:
math and arithmetic, multiple choice, medical and finance QA, code-writing tasks graded
by heuristics, a few sandboxed coding tasks, games, a memory benchmark, a tool-use task,
a policy-compliance simulation. For almost every finding I read the reward code, found
the line responsible, and reproduced it twice from a clean virtualenv with a script that
doesn't depend on my tool. I'm not naming any of them. Many are small community
projects, and the point here is the patterns, which repeat across authors.

## What kept turning up

### Graders that count words

Several small environments ask for free-form output (document this function, write a
data-cleaning routine, explain a rule) and grade it by looking for surface features.
Does the reply contain "return"? "because"? The right method names?

For a handful of these I wrote one text that addresses no task in particular and sent
it for every task I sampled. It earned between about 40% and 96% of the maximum. In one,
a function that returns its input unchanged, with the expected method names in a
comment, beat the dataset's own reference solutions.

That's the less interesting direction. The other one: in most of these environments the
concise reference answer the dataset ships is *not* the reward maximum. The optimum is a
longer text with more of the right nouns in it. Training on that reward teaches padding.

### Rewards that pay for the form

A common design adds "format" and "reasoning" terms on top of correctness. Sensible on
paper, but in the environments I looked at those terms never check the answer. A wrong
letter with a well-formed thinking block collects about a third of the maximum every
time. One reasoning term measured length, so a real sentence of clinical reasoning
earned less than a few hundred characters of *lorem ipsum*.

And the mirror image: a correct answer written as the letter plus the option text,
instead of the bare letter, lost both the accuracy and the format credit and scored
*below* a wrong letter in clean format.

### Strict and lenient in the wrong places

One environment asks for the final answer after a marker, `#### 42`. With the marker
it's strict about some things: `#### 42.` with a trailing period scores 0. Without the
marker, a fallback hands the whole reply to a math parser, and "The answer is definitely
not 42." scores 1. A near-miss on the format is graded more harshly than ignoring the
format altogether.

### Denials inside the answer box

Math graders that extract `\boxed{...}` and compare symbolically held up well against
everything outside the box. Inside it was another story. In a few environments that
share a common math-checking library, `\boxed{12 \text{ is wrong}}` (with 12 the right
answer) was paid in full on nearly every task, because a normalisation step drops text
after a number (it's there for units). A false equation like `\boxed{13 = 12}` was
accepted too, because only the right-hand side got compared. I'd first cleared two of these environments. My probes
had put the denial outside the box.

Substring matching has the same problem without any parsing at all. If the check is
"the reference appears in the reply", then "not Lisbon" is Lisbon, and a reply that
lists every option is always right. In one environment the questions listed their own
candidate answers, so echoing the question back earned full reward on every task.

### The exception nobody saw

One environment's largest reward term called `re.search` in a module that never imports
`re`. Every call raised `NameError`. The framework caught it, logged it and recorded
zero, so every run finished with plausible-looking numbers and a ceiling nobody
intended. I only noticed because the tool counts scoring exceptions, and one short run
had hundreds of them. A single unit test that scores one reference answer would have
caught it.

### A violation that reads as compliance

An agent is tested on whether it respects a spending limit, and the reward is 1 unless
it goes over. The amount parser didn't cope with a currency sign plus a thousands
separator, so a large amount written the ordinary way was read as zero and scored as
the limit being respected. On the environment's own data this fires rarely. Real
defect, narrow reach, and I only measured the reach during a review of my report.

### The game that keeps scoring after it ends

A word-game environment doesn't stop its episodes. After a win or a loss it keeps
replying to every further turn until the turn limit, and on most losses it reveals the
word. The reward functions rebuild the game by re-reading every assistant message. With
small scripted players: an honest solver scored in the middle of the range. A player
that lost, saw the word and typed it in scored about two thirds as much. A player that
won and then kept making moves in the spare turns picked up extra reward and scored
highest. A player that won and then said nothing scored lowest, because an empty last
message erased the win. The best reward went to whoever kept talking.

### A grader that never reads the answer

A tool-use environment has the model keep notes in files and answer questions from
them later. Its stricter grader checks that each true fact is somewhere on disk. It
never looks at the answer. A policy that never deletes an old value and answers
"unknown" to everything gets full marks, although the grader's docstring says stale
values get no credit. A single file listing every possible value gets 0.97, a limitation
the docstring does name.

### The ones that held

Not everything leaked. One multiple-choice environment gave up nothing at all: wrong,
empty, echoed, listed and mutated completions scored zero, and so did hedges written
inside the answer box. A large suite of procedurally generated reasoning tasks came
close, with one narrow class of false pass on a few task types. Every audit I wrote
lists the checks that passed, not only the ones that failed.

## Does a real model actually hit these?

Everything above uses completions I built. So I also ran a mid-sized local model on
about twenty tasks per environment and scored its own replies with the same code, and a
second small model on some of them. One sample per task, so it's an observation, not a
measurement.

On most exactly graded tasks its normal output didn't reach the defects. On one `####`
arithmetic set it answered cleanly every time. On another set of the same kind it wrote
things like `#### $10` and `#### 16 hours` on a few tasks and was graded wrong.

Elsewhere it walked straight in. In the word game it kept chatting after each game
("That was a great game!"), the environment kept asking, and the chatter was scored:
games the environment had declared lost were paid as solved. One of its replies crashed
the environment outright, which led to something no probe had found: a guess followed by
a full stop crashes it too. In the note-keeping environment it saved the facts correctly
and then gave non-answers to about half the questions, and the stricter grader still
paid 0.95.
In most of the word-counting environments its long, fluent replies outscored the
reference answers.

The probes say where a grader can be wrong. The rollout says which of those places a
model is already standing in.

## Checking my own work

The automatic mode found leads fast and stated several of them wrongly. In one
environment the "reference" it compared against was a function name. In another every
flagged row was an artefact of how I'd built the probe. Reading the reward code fixed
those.

Then every report got a separate claim-by-claim check against the source and the stored
output. The main findings held up. The reports didn't come through clean: I'd rated the same
kind of defect High in one place and Low in another, some reports quoted numbers from
runs I'd since redone, two suggested fixes wouldn't have fixed anything, and one "generic"
probe text happened to contain three of the answers. I wrote a severity scale down,
re-rated everything, stored every output next to its report, and now generate the totals
instead of typing them.

An audit is a test artefact too. It needs its own oracle.

## If you maintain an environment

- **Write the negative cases first.** Few of the packages I audited checked that a
  wrong answer in perfect format scores zero. The ones that tested their grader mostly
  checked only that the right answer passes.
- **Score your own reference answers.** If the dataset's answer isn't the maximum,
  something is off, either in the grader or in the data.
- **Ask what the laziest answer earns.** Echo the prompt, list every option, send one
  fixed text for every task.
- **Test the sum, not the terms.** Multi-part rewards fail in how the parts interact.
- **Make "it raised" visible.** A swallowed exception is a silent zero.
- **Put the hedge inside the box.** If you parse a region of the reply, probe inside it.

falsepass does most of this in one command, and there's a GitHub Action for running it
in CI. It works with verifiers environments (published or local), Inspect AI tasks and
HUD environments.

## Sending fixes upstream

Where a maintainer had an active public repository and the fix was small, I sent a
pull request, with a test wherever the repository had tests for that code. One has been
merged; the others are still open. Where the right behaviour was a judgment call, I
asked instead. Strictness the prompt asks for isn't a defect on its own, and I only
noted it as low priority or where it sat next to leniency in the same grader. A grader
that's exploitable on purpose, because the environment studies reward hacking, I didn't
report at all.

## Limits

The findings come from synthetic probes. The model check is mainly one mid-sized local
model, one sample per task. I didn't re-solve datasets to check their labels. Sandboxed
code-execution environments, probably the most hackable kind, still need policies
written by hand, and I only covered a few. LLM-judged
environments were only tested with stand-ins for the judge, so nothing here says how a
real judge behaves. No domain expert has gone over the medical or finance reports.

The tool is open source under Apache-2.0: [source on GitHub](https://github.com/Super-Eric/falsepass),
[documentation]({SITE}/docs/). If you run it on your own environment and it's wrong
about something, I'd like to hear about it.
