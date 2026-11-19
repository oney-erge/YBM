# YBM: the plan

Written 2026-11-19. This is the one plan. It replaces `FINAL_REMAINING_PLAN.md` (11/18) and `ULTIMATE_ASSISTANT_PLAN.md` (11/19), which are kept in
[archive/](archive/) for the evidence and the exam candidates they hold. Where things live and how a change may touch them is [DESIGN.md](DESIGN.md).
Planned behavior is not implemented behavior: the "State" column below says which is which.

## 1. The bar

One assistant on one PC, reached through Telegram, that stays while models change. It runs on a local model, so its work costs nothing, and it behaves
like it: take the job, work it out, do it, report. Twelve things:

1. **Understand instead of asking.** It works out what is meant and how big it is from the words (typed or dictated), states its assumptions in its first
   reply, and goes. A question is the last resort.
2. **Know how big the ask is.** Answer now, look a little, dig for an hour, or run for days, read from the wording, with an estimate and updates.
3. **Remember, across every chat, by default.** Facts about the owner, his family, home and preferences, kept without asking, used when they matter,
   listable, correctable, forgettable.
4. **Hold projects, not just chats.** A pool, a robot, a daycare search keep their decisions and reasons. After weeks on other topics, "back to the
   pool" brings it back. "Which projects are active?" is answered from an index, not from memory of the chat.
5. **Work a big build with the owner**: steps, a bill of materials, a schedule, reminders.
6. **Run long, exhaustive work by itself**, judging its own result against a measured standard.
7. **Use the computer and the web itself**, including sign-in and sign-up with the login told to the owner. It never spends his money on its own.
8. **Decide when told to decide.**
9. **Write code when nothing fits**, test it read-only, run it, keep it; recurring work becomes a monitor.
10. **Say exactly what to do when it truly cannot**, after trying genuinely different approaches. Never a bare "I could not"; never a hand-over question for work
    that is not code.
11. **Prove what it did.** Done means observed.
12. **Stay.** Survive restarts, reboots and model swaps; keep everything in open formats.

Local first stays the rule: nothing goes to a hosted model without asking. Money: nothing is bought, no card is used, at any level, without a yes to the
exact order.

## 2. Where it stands (11/19, measured on the real bot and PC, one runner at a time)

| Behavior | State |
|---|---|
| 1, 2, 8 Understand, size, decide | Built. Frozen everyday cases at the top autonomy level: 30 of 32. `v_cannot_order` failed all 8 runs, including after its fix. |
| 3, 4 Memory, projects | Built, **not proven**: one real remembered fact (the owner's name); the projects in the database are test-made, with every note repeated four times; no project has a hook. Never run across days. |
| 5 Big builds | Partial: a skill that makes a bill of materials; no walk-through; the five-session check never ran. |
| 6 Long work | Partial: the survey ran once (12 percent of its table filled; the fixes in #270 are not rerun); the 600-page research ran once and its report was thin (178 of 238 cited sentences removed by the claim check). |
| 7 Hands | Measured: 9 of 10 on the hands suite. |
| 9 Code, monitors | Built; `ult_devices` passes. |
| 10 Blockers | Built at the end of a task; **the retry before it is a fixed ladder of generic hints**, and it offered a hand-over to Claude Code on 11/19 for a text edit. |
| 11 Proof | Mostly: an honest "I cannot" completes; "created" and "paid" claims need a record. Five separate checks do it. |
| 12 Stay | Partial: backup, restore check, morning line, `ybm calibrate`. No model-swap procedure, no rollback, no three-day run. |

Measured speeds (11/19): backend tests about 37 seconds; CI backend 0.8 to 1.9 minutes; pull request open to merge about 2 minutes; a budgeted live check
5 minutes. No wait over 5 minutes is the rule.

### What went wrong on 11/19 ("revise the plan", twice)

From the database, 21:05 to 21:10 UTC:

1. **The follow-up was not tied to the plan.** Each "revise it" became a new goal with no route; nothing sent it to the planner that made the plan.
2. **The model did not see what the owner saw.** His last exchange was not in the prompt; memory was one rolling summary cut to 2,000 characters by removing its
   middle; the tool list alone was allowed 9,500; the message was matched to the wrong skill ("what can you do") whose full text was pasted in.
3. **The plan tool's contract did not match what the model was shown.** Steps appear as "1. 2. 3."; the tool wants a position and exact field names; the strict
   call schema does not encode which fields each operation requires. Three edits failed on that.
4. **Retry was a fixed ladder** (other spellings, sub-tasks, "write Python", hand-over). Nothing asked why it failed. About 17 file searches looked for a notebook
   file, when the owner meant the plan in the last reply.
5. **The hand-over offer bypassed autonomy** and is not limited to code work.
6. **Test data sat in the live memory.**

## 3. How a change is made

[DESIGN.md](DESIGN.md): every change to product code is **Minimal** (inside an existing home, adds no module, table, config key, tool operation, prompt file or
wording pattern, at most 150 lines) or **Design** (an entry in DESIGN.md, deletes what it replaces in the same series, re-records the fixtures, compares the same
live cases before and after). `scripts/check_design_change.py` enforces it in CI, and two counts may only go down.

## 4. What the model sees (design D5a)

Today the prompt is a tool list, an ability note, a pasted memory summary and the history, each cut by its own cap. The model should see, in this order, with
a budget and no cut through the middle of anything:

| Layer | What | When | Size |
|---|---|---|---|
| 0 Always | The owner's words; **his last message and the assistant's last reply, word for word**; an "about you" block; the **index**: one line per active project and open goal, "+N parked" | Every message | about 1,500 characters of context besides the words |
| 1 On a hook | One project's `PROJECT.md`: what it is, status, decisions and why, open questions, next step, links | A hook matches and the decision model confirms | about 60 lines |
| 2 On demand | `plan.md`, `notebook.md`, buying lists, reports, past-chat search, a skill's full text | When the model opens it with a tool | nothing until read |

Principles (from the sources in section 9): the smallest set of high-signal context; pointers kept when content is dropped; skills load by name and
description first, the body only when they apply.

## 5. Memory, projects, hooks, skills and goals (design D5b)

**One hierarchy.** A project (long-lived) holds goals (what the owner wants now); a goal holds attempts (tasks, plans, jobs); a one-off goal needs no
project. Plans have versions.

**On disk, readable and movable** (`.agent_control/memory/`, backed up):

```
INDEX.md                  one line per project and open goal (generated)
about-you.md              facts with their source and date; changed facts keep their history
projects/<name>/PROJECT.md  plan.md  notebook.md  files/
```

The files hold the content; the database keeps status, hooks, dates, paths and search. One source of truth for each thing.

**Active, parked, done.** Active: touched in the last 14 days or with an open plan step or job. Parked: after that, set automatically. Done or dropped: when the
owner says so. "Back to the pool" makes it active again. "What projects are we working on?" is answered by a `project.list` call from the index.

**Hooks** are collected automatically (names, things, people, places, products) and editable. They find candidates; the decision model confirms; a word never
triggers a project by itself.

**One write path.** After each reply, when the model is free, one extraction call proposes: a fact added, updated or closed; a project note; a status change; a
hook. Code applies it, removes duplicates, and the owner sees "(remembered: ...)". **A nightly tidy-up** rewrites each `PROJECT.md` from its notebook so it stays
short, parks stale projects, merges duplicates and flags contradictions.

**Skills** keep one catalog line each; the full text is read when needed. A procedure is saved only after a verified real success, never from a test.

**Test data never enters live memory.** The test runner removes what each case made.

Done when: the memory suite (one session, across sessions, time, changed facts, "you never told me") scores at least 90 percent on the real bot; 6 project
questions ("which are active", "which are parked", "tell me about X") pass; 11 of 12 switch-away-and-return scenarios recover the earlier decision; and in
every recorded prompt the owner's words, the last exchange and the index arrive whole.

## 6. After a failure: try differently before giving up (design D4)

1. **Detect stuck, in code**: the same kind of call found nothing twice, the same error twice, three failed steps in a row, or a sub-task out of steps.
2. **Diagnose**: one short model call re-reads the request and the last exchange and returns what was asked, what each attempt assumed, the earliest wrong
   assumption, and a cause: misread the request, missing context, wrong tool, wrong arguments, the thing does not exist, refused by the environment, too big.
3. **Change the kind of approach**, from a menu with the kinds already tried and the kinds that do not fit removed by code:

| Kind | Allowed when |
|---|---|
| A Answer from what is already here (conversation, plan, memory) | Always first after a misread |
| B Use the home that owns it (a plan goes to the planner, a project to `PROJECT.md`, a job to its controls) | The thing has a home |
| C Fix the call from the error (one try) | The error names the fix |
| D Break the step down | The step was too big |
| E Another tool, ability or skill | One fits |
| F Write and run code (syntax checked first) | Computing, searching, automating; not for editing text or plans |
| G Look up how, on the web | A method is unknown |
| H Step back: what result would satisfy the goal? | Always |
| I A coding agent (Claude Code, Codex) | Only code work, after A to H; never a question at the top autonomy level; named in the final report |

4. **A fresh attempt** starts from the request, the diagnosis, the chosen kind and a short "tried and failed" list, without the failed transcript.
5. **Stop** after at most three changes of approach or the time budget, with the blocker report. Never a question at the top level.
6. **Learn**: when the second or third kind works, record a lesson ("'revise it' about a plan goes to the planner"); after review it becomes a procedure.

Sources behind this: Reflexion (a short note on why it failed, carried into the next try), AgentDebug (errors cascade; find the earliest wrong step), ADaPT
(break a step down only when it fails), LATS (go back and try other actions), OpenHands' stuck detector (same action and result repeated), and the finding that
loops persist even with summaries, so the next approach must be a different kind, not the same one reworded.

Done when: a set of 8 live journeys ("revise it" twice, "that did not work, try another way", a missing file, a site that blocks, a tool refusing its
arguments, a text-only request, and two more) gets at least 7 right or ends with an honest report; hand-over offers for non-code work are zero; questions at the
top level are zero.

## 7. The work, in order

Sizes: S is about one session, M two, L three or more. **Type** is the change rule's label.

| # | What | Type | Size | State |
|---|---|---|---|---|
| G0.1 | `DESIGN.md`, the change rule, the CI check, the counts | Design | S | done (#271); `design-rule` is a required check |
| G0.2 | This plan replaces the two | Minimal | S | done (#271) |
| G0.3 | Write and seal Verdict 4 before the work | Minimal | S | done: 20 cases, `e2e/verdict4_cases.sha256`, `--suite verdict4` |
| G0.4 | Clean test leftovers from live memory; the runner removes what each case made | Minimal | S | |
| G0.5 | Re-record the fixtures in the owner's config; merge or drop the lifecycle branches | Minimal | S | |
| D5a | What the model sees (section 4) | Design | M | built: notes keep their place, the catalog gives way, the last exchange is shown; the index and `PROJECT.md` layers come with D5b |
| P1 | "Revise the plan" goes to the planner with the old plan; plans get versions | Minimal | S | |
| D2 | One asking gate, including the hand-over offer | Design | S | |
| D4 | The retry loop (section 6); deletes the fixed hint ladder and two old recovery pieces | Design | M to L | |
| D3 | One proof check that sees the answer | Design | M | |
| D6 | Tool calls checked per operation; then delete the alias tables | Design | S | |
| D1 | One front door for every channel; deletes the phrase patterns and Telegram-only routing | Design | L | |
| D5b | Memory files, projects, index, hooks, `project.list`, nightly tidy-up (section 5) | Design | L | |
| G2 | Proof runs, unattended: survey rerun (at least 90 percent of the registry's daycares), 600-page research with two fixes in `jobs/research.py` (search again for unanswered questions; write from the collected quotes), the mailbox, speed (2, 6 and 15 s) | Minimal | S each | |
| G3 | Big-build walk-through ("next step", reminders, "what did I spend"); a model swap tested on a copy and rollbackable updates; one export command | Minimal, one Design | M | |
| G4 | Verdict 4 once, a three-day run with a restart, release 0.2.0, README, site post | | S plus 3 days | |

D5a, P1 and D2 come first, so "revise it" works and the hand-over offer stops within the first sessions. D3 and D6 are small and fix live failures. D1 gives D4 the
goal it needs. D5b is the biggest, so it goes last.

## 8. Tests: nothing the owner waits on runs over 5 minutes

| Check | When | Wait |
|---|---|---|
| Unit tests and the recorded conversations, in the owner's config | Every change | 5 to 40 s |
| Live change check on the real bot, 5-minute budget (`scripts/run_all_e2e_tests.py --budget-seconds 300 --changed-since <ref>`) | A change that affects behavior | 5 min or less |
| The design change's area, same cases before and after | Each Design change | none: unattended, in chunks |
| Nightly slice of the live cases | Off until the owner allows messages at night | none |
| Release exam and three-day run | A release | none |

New frozen sets, each sealed before the work it measures: **context** (the prompts of the recorded conversations), **diagnosis** (10 recorded failures through the
diagnosis call), **intake** (150 or more messages), **size of the ask** (20 wordings), **proof** (planted false answers), **tool-call shape** (every recorded call
replayed), and live overnight sets for **stuck** (8), **project questions** (6), **switch and return** (12), **memory**, and **asking** (30 requests at three levels).

Pass marks: 32 of 32 frozen everyday cases; Challenge at least 35 of 39; Verdict 4 at least 18 of 20 with every safety case passed; hands at least 17 of 20;
memory at least 90 percent; projects 11 of 12; survey at least 90 percent of the registry; zero false claims; speeds 2, 6 and 15 seconds.

Verdict 4 candidates: `archive/ULTIMATE_ASSISTANT_PLAN.md`, section 5. Its twenty requests are written and sealed (hash in the repository) before the work they
measure, and their wording is not used while building.

## 9. Sources

[Agent Skills specification](https://agentskills.io/specification.md) (name and description always loaded, the body on use, files on demand);
[Anthropic, effective context engineering](https://anthropic.com/engineering/effective-context-engineering-for-ai-agents);
[Anthropic memory tool](https://platform.claude.com/en/docs/agents-and-tools/tool-use/memory-tool) (memory as files the agent reads and edits);
[Letta memory](https://www.letta.com/blog/agent-memory) (small blocks always loaded, the rest searched, tidying in the background);
[Manus, context engineering](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus?hl=en-GB) (files as memory, keep a pointer, keep errors visible);
[ChatGPT projects](https://help.openai.com/articles/10169521);
[Reflexion](https://arxiv.org/pdf/2303.11366); [AgentDebug](https://www.alphaxiv.org/abs/2509.25370v1); [ADaPT](https://arxiv.org/pdf/2311.05772.pdf);
[LATS](https://arxiv.org/pdf/2310.04406); [OpenHands stuck detector](https://docs.openhands.dev/sdk/guides/agent-stuck-detector).
