# Severity scale used in the audit reports

Every audit report rates its findings against this scale. It was written after an
independent review found an earlier, shorter scale had been applied unevenly (the same
kind of defect rated High in one report and Low in another).

Two terms first.

- **Answer-free completion** — text produced without knowing the task's answer: empty,
  the prompt echoed back, one fixed text used for every task, a wrong value in the right
  format, the reference of a different task.
- **Non-committal completion** — text that contains the correct answer but denies it,
  retracts it, or offers it among alternatives ("definitely not 18", "either 25 or 18").

Rewards are read relative to the maximum the rubric can pay.

| Severity | Applies when |
|---|---|
| **Critical** | An answer-free completion earns at least 0.75 of the maximum — or more than the dataset's own reference answers while earning at least 0.75. Also: a completion that plainly violates the rule the environment exists to test is scored as compliant. |
| **High** | An answer-free completion earns 0.25–0.75 of the maximum on average. Or a non-committal completion receives full correctness credit on all sampled tasks. Or the dataset's reference answers earn less than about half the maximum. Or a correct answer scores below a wrong one. Or a reward term carrying a quarter or more of the weight never fires. |
| **Medium** | An answer-free completion earns 0.10–0.25. A non-committal completion gets full credit on a subset of task types. A correct answer in the requested format is rejected *and* a less compliant form is accepted. Dataset defects (tasks that need no skill, duplicate or conflicting rows). Non-deterministic scoring or environment replies. A reward term that is constant. |
| **Low** | Defensible strictness about format (a period inside the answer slot, letter case). Which of several answers in one reply is graded — a policy choice, as long as it is consistent. Reward code that raises on unusual input. Documentation that disagrees with the code. Anything below 0.10. |

Notes on applying it.

- **Similarity graders.** Partial credit is their design. The severity follows the
  reward an answer-free completion gets, using the rows above; the report says that the
  design is intentional.
- **Disclosure does not lower the rating.** If maintainers document a weakness, the
  report says so prominently and the rating stays where the scale puts it — the default
  configuration is what gets trained on.
- **Sample size is stated, not folded into the rating.** A finding seen on 2 of 2
  searched tasks is rated by the scale and labelled with the 2.
- **Environments that only test for the absence of a violation.** Doing nothing is
  compliant by such an environment's own definition, so inaction scoring full is rated
  High, not Critical, and the report says why it matters only if the reward is trained on.
- **Closed answer sets.** On a task whose answer is one of a few labels, one fixed label
  used for every task earns that label's share of the dataset under any correct grader
  (0.5 on a balanced yes/no task). That base rate is subtracted before a fixed guess is
  rated, and the label balance itself is an observation, not a finding.
- **Reach.** When a defect needs the completion to take a particular form, the report
  says how often that form arises from the environment's own data. If it arises on
  fewer than about one task in twenty, the finding is rated one level below what the
  table gives. The form must be counted in the
  environment's own data (its tasks, references, inputs). How often a model's replies take
  the form is an observation and never moves a rating: a few dozen model replies cannot show "under one in
  twenty" anyway.
- **Graders that are exploitable on purpose.** Some environments are built to study
  reward hacking and ship a grader their own source describes as the exploitable one.
  What that grader lets through is recorded as an observation and not rated; findings
  are rated against the grader the source presents as the sound one. The report says
  which of the two is the default.
- **Results obtained with a substitute.** When a component could not be run as the
  environment runs it — a hosted LLM judge replaced by a local model — what was measured
  with the substitute is reported as an observation and not rated. A finding that
  depends on how that component behaves is rated on what could be measured and marked
  provisional.
- **Non-default configurations.** A finding that exists only under a setting the user
  has to choose (a tier, an alternative grader the source presents as sound) is rated by
  the table like any other, and its heading and the summary say "non-default". The scale
  has no discount for it and none for a narrow margin: a mean that clears a threshold by
  a few thousandths is rated at that level, and the report states the margin and what
  it rests on.
- **One root cause, one finding.** Two symptoms of the same defect are reported
  together and rated once, at the higher of the two.
- **"No false pass"** in a summary means: no answer-free completion scored above 0.10
  of the maximum and no non-committal completion received correctness credit.
