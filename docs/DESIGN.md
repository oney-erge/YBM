# How YBM is put together, and the rule for changing it

Written 2026-11-19. This is the map every change is measured against. The plan for what to build is [PLAN.md](PLAN.md); this file says
**where** each kind of thing lives and **how** a change is allowed to touch it. Planned homes are marked (planned); everything else exists.

## 1. Why this file exists

Each failure on the real bot used to be fixed where it showed up: a pattern for one wording, a hint for one tool, an alias for one model habit.
The same job then lived in five places, and the next variant came in through a sixth. On 11/19 a plain "revise the plan" failed because no one
place owned "what the model sees", "which plan this is about" or "what to do when a call keeps failing". The cure is one home per job, and a rule
that stops new work from landing outside a home.

## 2. The homes: one per job

| Job | Its home | What still lives elsewhere and moves in |
|---|---|---|
| **Reading a message** (which goal, what it does to it, how big, what is assumed) | `channels/base.py` intake, then `llm/decision.py`, then the Concierge (`llm/classifier.py`), then `orchestration/goal_routing.py` | The phrase patterns in `clarification.py`; Telegram's own pre-routing of gaming, undo, accounts, workflows and chat commands (`channels/telegram.py`); `job_start.py` and `plans/stage.py` goal-guessing; request-fact patterns where tests show the Concierge matches them |
| **What the model sees** | `orchestration/context.py`: the notes about this person and this moment, in an order of importance, with their own room | The conversation summary and the recent turns (`channels/memory.py`) and skill text pasted whole into the prompt (`abilities/context.py`), until D5b gives them a place |
| **Asking the person** | `orchestration/autonomy.py`: one `may_ask` decision for every place that can ask | `orchestration/initiative.py` (a sentence in the prompt), `orchestration/asking.py` as a separate path, the hand-over offer in `orchestration/guards.py`, repeated-question recovery in `worker.py` |
| **Done, and true** | `orchestration/proof.py` (planned): one check over one record of what ran and changed, for a task, a plan step, a goal and a job | `auditor.py`, `claims.py`, `fulfillment.py` keyword checks, `answer_checks.py`, `number_check.py`, `grounding.py`, `plans/review.py`, `page_check.py` |
| **After a failure** | `orchestration/retry.py` (planned): detect stuck, diagnose, change the kind of approach, a fresh attempt, an honest report | The fixed hint ladder in `ladder.py` and `guards.py`, `repeats.py` stop rules, the hand-over offer, `alternative_after_repeat`, `local_recovery_step_limit` |
| **Remembering** | `memory/` (planned): files you can read (`about-you.md`, one folder per project) with the database holding only status, hooks, dates and search | The conversation summary, `channels/profile_facts.py`, `channels/memory_learning.py`, `preference_watch.py`, `persona_learning.py`, `projects_memory.py`, `tools/memory_manage.py` |
| **Turning the model's call into a real call** | `tools/call_schema.py` with the tool's own contract in `tools/contracts.py`, checked per operation | `orchestration/call_rewrites.py` keeps only rules that carry policy; alias tables that only fix the shape of a call are deleted |
| **Long work** | `jobs/` (one runner, one `Unit`, kinds: research, investigate, watch, survey, monitors) | Nothing: new long work is a new kind of job, not a new loop |
| **What it can do** | `abilities/` (one index) and `skills/` (procedures) | Nothing: a new capability is a record in the index |

Objects that already exist are reused, not paralleled: **project** (long-lived), **goal** (one thing wanted now), **plan** (dated steps),
**job** (background work), **memory fact**, **skill**. A project holds goals; a goal holds attempts (tasks, plans, jobs).

## 3. The rule: every change is Minimal or Design

A pull request that touches `backend/src/agent_control/` ticks exactly one box in its description.

**Minimal.** It fits a home above and adds none of these:

- a module;
- a database table or column;
- a config key;
- a tool or a tool operation;
- a prompt file;
- a pattern that matches wording (`re.compile` in the code that reads messages or tool calls).

It is also small: at most 150 added lines of product code. It comes with the failing case it fixes and a test. A fix for one wording is not Minimal; it is
rejected, and the home is fixed instead.

**Design.** Anything else. In the same series of pull requests it:

1. adds an entry to section 5 of this file: the problem, which home changes, what it replaces;
2. **deletes what it replaces**, not "later";
3. re-records the recorded scenario fixtures when it changes a prompt, a tool schema or the workspace layout;
4. compares the same live cases before and after, and says the numbers.

A change that touches no product code (docs, tests, CI, dependencies) needs no label.

## 4. What enforces it

- `scripts/check_design_change.py`, run by the `design-rule` job of CI on every pull request (seconds). It fails when no label or two labels are
  ticked, when a Minimal change adds something only a Design change may, and when a Design change has no entry here.
- `docs/design_baseline.json`: two counts that may only go down: the patterns that match wording in the code that reads messages and tool calls, and the
  lines of `call_rewrites.py`. A change that lowers a count writes it down (`python scripts/check_design_change.py --update-baseline`) so it cannot creep
  back; a count goes up only in a Design change that says so.
- The pull request template carries the two boxes, and `AGENTS.md` carries the rule for anyone working in the repository.

## 5. Design entries

One entry per Design change, newest last. Format: **what failed**, **which home**, **what it replaces and deletes**, **how it was measured**.

### D0. The change rule (2026-11-19)

**What failed.** Fixes landed where the failure showed, so one job (reading a message, asking, deciding done, recovering, remembering) lived in several
places, and a new variant needed a new patch. **Home.** This file and the `design-rule` check. **Replaces.** The habit of a pattern or an alias per
wording. **Measured.** The counts in `docs/design_baseline.json` on the day the rule started: 100 wording patterns, 854 lines in `call_rewrites.py`.

### D5a. What the model sees (2026-11-19)

**What failed.** On 11/19 "revise the plan" failed twice. The notes about the person and the moment (the plan open in the chat, the goal, the project, what bears on
the request, today's date) were glued on behind the tool catalog and the whole was cut to one size from the end. With a catalog over the limit, the open plan, the
one thing that said what "it" was, went first; the decision preference was cut in the middle of a sentence; the last exchange was never in the prompt in full (the
rolling summary keeps the gist). The model searched the disk for a notebook. **Home.** `orchestration/context.py`; the Operator takes the notes as `situation`
and cuts only the catalog to what is left. **What it adds.** An order of importance (the date, what was said just before, the goal, the project, then what bears
on the request, with the open plan and job first inside it), shortened from the least important at a line with a note, never mid-sentence; and the last exchange
word for word (what they said, what was answered) when it was within six hours. **What it replaces and deletes.** The notes no longer travel inside `config_context`
(`_planner_context` is kept only for sub-tasks); the one-size cut of the notes is gone. **Measured.** Unit tests reproduce the failure (a 70-tool catalog and an open
plan: the plan is whole, the preference is whole, every tool is named). The live case is `cont_plan_revised_twice_stays_the_plan` in the continuity suite.

### P1. Revising a plan (2026-11-19)

**What failed.** "Revise the plan" had no operation. The Operator had to edit steps one at a time with exact field names (`position`, `title`), and on 11/19 it wrote
`step_id` and `step_text`, was refused twice, repeated the same call, and gave up; the plan it had saved never changed, and what it showed in the chat was text it
wrote on the side. The plan card showed step titles only, so even a good revision could not show "how" (the owner asked where the pictures and the animation come from).
**Home.** `plan.manage`, the planner (`plans/planner.py`) and the plan store. **What it adds.** One operation, `revise`, with one argument, the user's words: the planner
that wrote the plan is given the plan as it stands and the instruction, and rewrites the steps that have not started in one transaction (steps that started stay, the
plan keeps its id, a "Revised (version N)" line is kept with it). The plan card of a proposed plan, the plan the stage shows and `show` carry each step's detail (the
sentence that says how it will be done). **What it replaces and deletes.** Nothing is deleted: `add_step` and `update_step` stay for one small edit, and the open-plan
note now says when to use which. **Measured.** Unit tests (`test_plan_revise.py`); live, `cont_plan_revised_twice_stays_the_plan` before and after.
