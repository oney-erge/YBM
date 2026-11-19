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
| **After a failure** | `orchestration/retry.py`: notice it is stuck, diagnose once, change the kind of approach, start the next attempt from a short list of what failed; then the blocker report | The evidence hints of `ladder.py` stay (they are facts about files and forms); `repeats.py` stop rules and the hand-over question for the levels that ask are still separate until D3 and D1 |
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

### D4. After a failure: try differently before giving up (2026-11-19)

**What failed.** A plan edit failed twice with the same wrong call and the task went on the way a fixed ladder goes: search the disk with other spellings, three sub-tasks
that ran out of steps, "write a Python program", then a question to the owner. Nothing asked why it was failing: the person meant the plan in the last reply, and every
attempt assumed a file. The ladder was a list of hints for finding files; the same list was shown for every kind of task. **Home.** `orchestration/retry.py`, called by
the guards before each model decision. **What it adds.** (1) Noticing in plain code: the same call failing with the same error twice, two lookups in a row that found
nothing, three failed steps, a sub-task out of steps. (2) One short model call, once for those steps, that says what the person asked, what the attempts assumed and which
assumption was the first wrong one, with a cause. (3) A kind of approach chosen by code from that cause, never one already tried, only one that fits this task: answer
from the conversation, use the part that owns it (the planner for a plan), fix the call from its error, do one small part, another tool, a short program, look it up, or
step back and say what result would satisfy the person. (4) A note in the history that says what went wrong, what to do instead and what already failed; the prompt then
shows what came before it as a short list of what failed, not as the transcript. At most three changes; then the task ends as it always did, with the blocker report.
**What it replaces and deletes.** The generic "write a short program after three failures" hint (`try_code`) and the "use a materially different method" nudge
(`alternative_after_repeat`); the evidence hints about files and forms stay. **Measured.** Unit tests (`test_retry.py`), including the 11/19 chain; live, the continuity case
`cont_plan_revised_twice_stays_the_plan` and the stuck set before and after.

### D5b-1. Projects the person can ask about (2026-11-19)

**What failed.** "Which projects are we working on?" had nothing to be answered from: the model saw a project only when a hook word of the message matched, and no project had
a hook (the small model gave none). A project was "active" forever. The notebook could be read only as five latest lines in the chat context; the owner could not open anything.
**Home.** `projects_memory.py` (projects, hooks, notes), with the tool `memory.manage`. **What it adds.** (1) Where a project stands is the calendar's, never stored: active when touched
in the last two weeks, parked after; done and dropped are the person's word (`set_project`). (2) An index in what the model always sees: one line per active project, the names
of the parked ones, a count of the finished (`orchestration/context.py`, priority with the last exchange). (3) Three operations on `memory.manage`: `projects` (active, parked,
done, dropped, with their words), `project` (one project's page: where we are, decisions, open questions, bought, steps, results) and `set_project`. (4) A readable copy
under `.agent_control/memory` (INDEX.md, about-you.md, `projects/<name>/PROJECT.md`), rewritten by the daily upkeep and whenever a list or a page is asked for; the notes stay in
the database for search. (5) Words that point to a project when the model gave none: its name, then the longer words of what was said. **What it replaces and deletes.** Nothing yet:
the hook-confirmed notebook lines in the chat context stay as the second layer until the first live comparison; the nightly rewrite of each page's "where we are" by the model
is the next piece. **Measured.** Unit tests (`test_projects_index.py`); live, project questions ("which are active", "tell me about the pool") and the switch-and-return cases.
