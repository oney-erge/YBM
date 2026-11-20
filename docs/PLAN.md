# YBM: the plan

Written 2026-11-19. This is the one plan. It replaces `FINAL_REMAINING_PLAN.md` (11/18) and `ULTIMATE_ASSISTANT_PLAN.md` (11/19), which are kept in
[archive/](archive/) for the evidence and the exam candidates they hold. Where things live and how a change may touch them is [DESIGN.md](DESIGN.md).
Planned behavior is not implemented behavior: the "State" column below says which is which. Updated 2026-11-20 after a full read of the
repository and the live database: section 2 is where it stands now, section 8 is the cleanup cycle, section 9 is everything left, in order.

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

## 2. Where it stands (11/20, measured on the real bot and PC, one runner at a time)

| Behavior | State |
|---|---|
| 1, 2, 8 Understand, size, decide | Built. Frozen everyday cases 30 of 32 on 11/19 (not rerun since; `v_cannot_order` passes alone since #289 and #296). The sealed Verdict 4, run once: 15 of 20. |
| 3, 4 Memory, projects | Built: the index, project pages, readable files, the fact keeper. Measured 11/20: the memory suite 8 of 8, the continuity suite 11 of 11 (projects listed and returned, a fact that changes). **Not built:** the nightly rewrite of each project page; the project-questions (6) and switch-and-return (12) sets; never run across days. |
| 5 Big builds | Partial: a bill of materials, the money walk, "what did I spend" passes live. **Not built:** the walk-through ("next step", reminders); the five-session check never ran. |
| 6 Long work | Built, **not proven**: the survey and the 600-page research have had no full run since their fixes (both were cancelled on 11/19 by other live runs). |
| 7 Hands | The hands suite 6 of 10 on 11/19 (two lost to the locked screen, two fixed and passing alone since). **Gaps found 11/20:** "make a folder and put these files in it" blocked in 5 of the 6 runs there have been; a login made for the owner is never told to him (that code is only on the unmerged branch `feat/tell-the-logins`). |
| 9 Code, monitors | Built; `ult_devices` passes; code is asked for without the grammar (D6-2). |
| 10 Blockers | Built: the retry loop (D4); the hand-over only for code work (#311) and never asked at the autonomous level. **Not built:** recording a lesson when a second approach works. |
| 11 Proof | Five separate checks (`answer_checks`, the auditor, `claims`, `fulfillment`, `number_check`). The claims check fired falsely on drafts three times in a week. D3 reviewed, not built. |
| 12 Stay | Backup, restore check, morning line, `ybm calibrate`, the computer kept awake during work (#315), one restart in the middle of a long job passed. **Not done:** a three-day run, a model swap tested on a copy. When idle, the laptop's standby takes the bot offline (the owner's decision, section 9). |

Measured speeds (11/19): backend tests about 37 seconds; CI backend 0.8 to 1.9 minutes; pull request open to merge about 2 minutes; a budgeted live check
5 minutes. No wait over 5 minutes is the rule.

### Verdict 4, run once (11/19, on `72ba1e7`): 15 of 20, below the pass mark of 18

Both safety cases passed. [MEASURED.md](MEASURED.md) lists each miss. Three were bugs and are fixed: the claims check took "will be generated" for a write (#299); a short remark made the 9B write
1,366 tokens into an open field, and the model server's one slot kept every other call waiting for three minutes (#300; same four messages now 2.9 s, 3.0 s, 2.4 s, 3.1 s); a date asked for with
"tell me when" was taken for a watch (#301). One was the sealed phrase list, not the bot (`v4_retry_missing_notes`: the reply was "No price quote was found in the notes folder ... There is no file named
or containing 'electrician'"). One was not the assistant: `v4_script_rename` stalled while the laptop was in connected standby (19:59:35 to 20:11:51 by Windows' power log; corrected in #316).

After the fixes, on the real bot: the memory suite 8 of 8 (it was 7 of 8); the continuity suite 9 of 11. The two that failed are not the fixes: a QR-code case that ended after a package install and said the image was
made (the claims check was right; the case is flaky), and a remark answered in chat in 10 to 18 seconds, as it should be, where the runner's stored classifier verdict said "task". The sealed exam is not rerun.

### After the exam (11/20): what each miss turned into

Each miss was traced to its kind, and the fix went to the whole kind, not the one case:

| Kind | What it was | State |
|---|---|---|
| Our check | The claims check took a future tense for a write (#299); the blocker report offered Claude Code for any task, a plan edit too (#311: code work only) | Fixed |
| Our use of the grammar | An open field let the 9B write to the token cap: the fact keeper, then the planner (three minutes a plan) | Fixed for every structured answer, with a test that keeps it so (D6-2, #300, #310) |
| Our skill question | "Tell me when it ends" scored as a watch (#301); near misses added for every routed skill, all still separate (#312) | Fixed |
| The test | Phrase lists judged wording (`v4_retry_missing_notes`); the runner trusted a stored verdict over a chat reply it could see (#313) | Runner fixed; the sealed exam stays as sealed |
| The computer | The laptop goes into connected standby, which drops the network: the twelve-minute rename stall was this (correction #316) | A task being worked on and a live test run now keep it awake (#315) |
| Found on the way | `job.manage status` named a day-old job: the list kept a chat's oldest jobs (#314) | Fixed |

Measured on the same cases, the computer awake: the continuity suite 11 of 11 (9 of 11 on 11/19); the plan made and revised twice 189.5 s (533 s); no model call ran to the
token cap in 235 (19 in 2,523 on 11/19). **Open:** (1) "make a folder and put these files in it" blocked in 5 of the 6 runs there have been: the file tool has no way to say it
(a move into a new folder makes it, but nothing says so) and the case accepts "blocked", so it hid this; a Design change to the file tool, with the recorded fixtures re-recorded.
(2) The owner's decision: keep the computer awake whenever YBM runs, so a message never finds it asleep (a laptop's battery is the cost). (3) The claims check fired on drafts
three times in a week (a capability sentence, a file Claude Code wrote in a sub-task); not reproducible from the final answers, watched. (4) The survey rerun and the 600-page
research each need their own window with the computer awake and no other live runs.

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
| G0.4 | Clean test leftovers from live memory; the runner removes what each case made | Minimal | S | done (#273): backup first, duplicate notes and two test skills cleaned, `e2e/residue.py` removes what each live case makes |
| G0.5 | Re-record the fixtures in the owner's config; merge or drop the lifecycle branches | Minimal | S | not done: the recorded scenarios still use the code defaults (abilities, plans and vision off) and are re-keyed when a prompt changes (`ybm scenario rekey --all`); the old lifecycle branches were read on 11/20: five are in main (squashed into `32f97f0`) and can go; `feat/tell-the-logins` is not, and holds the only code that tells the owner a login made for him (cleanup C4) |
| D5a | What the model sees (section 4) | Design | M | built: notes keep their place, the catalog gives way, the last exchange is shown; the index and `PROJECT.md` layers come with D5b |
| P1 | "Revise the plan" goes to the planner with the old plan; plans get versions | Design (a new operation) | S | built: `plan.manage revise`, a version line, step detail on the card |
| D2 | One asking gate, including the hand-over offer | Design | S | the hand-over question is one test in one place and never asked at the autonomous level (#274); a question at the autonomous level is sent back twice, and a third one is not let through either: one last step may only answer or stop (the schema has no ask branch), so it ends with what was done, what could not be and the nearest thing (the fix for `v_cannot_order`, measured live next) |
| D4 | The retry loop (section 6); deletes the fixed hint ladder and two old recovery pieces | Design | M to L | built: noticing, one diagnosis, kinds of approach, a fresh attempt; learning a lesson comes with D5b |
| D3 | One proof check that sees the answer | Design | M | reviewed, not built: the done path already runs one ordered pipeline (`answer_checks.py`) then the auditor then the fulfillment gap; merging the auditor, claims and fulfillment into one module is a large change to the path every task takes, with no live failure waiting on it that the retry loop and the honest-cannot rule do not already cover; to do after the first full live exam shows where it fails |
| D6 | Tool calls checked per operation; then delete the alias tables | Design | S | part 1 built: the strict call schema parses again (it was refused on every call); part 2 built (D6-2, 11/20): every field the model writes under the grammar has a size (an open field ran to the token cap, three minutes a call), code is asked for without the grammar (its field is the one that cannot have a size), and a slow structured call names its schema; next: measure with the schema in force, then delete the alias tables it makes unnecessary |
| D1 | One front door for every channel; deletes the phrase patterns and Telegram-only routing | Design | L | reviewed, not built: Telegram's command chain (gaming, undo, accounts, workflows, chat) is scoped to Telegram by design (the account vault must not be readable from another channel), so a shared chain would change who can do what; the goal and Concierge path is already shared; the phrase patterns in `clarification.py` need the labeled intake set (150 messages) to show parity before they go |
| D5b | Memory files, projects, index, hooks, `project.list`, nightly tidy-up (section 5) | Design | L | first part built: the index, `memory.manage projects/project/set_project`, the readable files, hooks when the model gives none, the nightly tidy; next: the model's nightly rewrite of each page, the memory suite |
| G2 | Proof runs, unattended: survey rerun (at least 90 percent of the registry's daycares), 600-page research with two fixes in `jobs/research.py` (search again for unanswered questions; write from the collected quotes), the mailbox, speed (2, 6 and 15 s) | Minimal | S each | research: the gap stage is built (an unanswered question is searched again in other words before it is written up; the writer already works from the quotes); the long suite (`--suite long`) started both jobs on 11/19; the 600-page research was cancelled four seconds later by the second case (one open job per chat: the two cannot run together, so the research needs its own run after the survey); the survey **went through a restart in the middle** (11/19, 22:02: same job, its 6 queries kept, revision 39 to 49) and was then cancelled at 22:06 by another session's live run on the same bot; its result, the 600-page run, the mailbox and the speed numbers are not measured yet |
| G3 | Big-build walk-through ("next step", reminders, "what did I spend"); a model swap tested on a copy and rollbackable updates; one export command | Minimal, one Design | M | export built (`ybm export`, D7); the model swap and going back are written down in USING.md and use the checks the repository already has (`ybm calibrate`, the five-minute live check, `ybm backup`), not run with a second model; the big-build walk-through is not built |
| G4 | Verdict 4 once, a three-day run with a restart, release 0.2.0, README, site post | | S plus 3 days | Verdict 4 run once: 15 of 20, below the pass mark (above). Release 0.2.0 published 11/19 (`v0.2.0` on `bb3e1ec`; it has #299 but not #300, the stall fix). The restart in the middle of a long job passed; the three days have not run. The README is rewritten (#303); the site post is not done. |

D5a, P1 and D2 come first, so "revise it" works and the hand-over offer stops within the first sessions. D3 and D6 are small and fix live failures. D1 gives D4 the
goal it needs. D5b is the biggest, so it goes last.

## 8. Cleanup cycle (11/20)

From a full read of the repository, its documents and the live database, without running anything. Each item says what goes, the evidence, and its type under the rule.
Nothing here removes a behavior the owner uses without saying so; where it would, the item is a decision for him.

| # | What goes or is merged | Evidence | Type | Size |
|---|---|---|---|---|
| C1 | **Done (11/20).** Dead code: `plans/review.py` and its test are deleted (nothing in the product called it; the fault it repaired was fixed at its source). The read said `scripts/make_demo_gif.py` was dead: it is not, `docs/screenshots/README.md` documents it as how the console demo GIF is made, so it stays | Evidence: import graph, then a search of every document | Minimal | S |
| C2 | **Done (11/20).** The empty legacy tables `plans`, `subtasks` and `plan_steps` are dropped by the additive migration (a table with rows is left alone); schedules the person deleted are removed by the daily upkeep after 30 days (the live database held 59, all tests') | Live database | Minimal | S |
| C3 | **Done (11/20).** Ten settings nothing read are removed: `identity.owner_label`, `approval_policy.approval_token_ttl_seconds`, `limits.tool_timeout_seconds`, `limits.max_log_chars`, the whole `adapters.terminal` block (its `blocked_command_patterns` promised a protection that no code applied; the real control is the `terminal.run` capability and its approval), the code interpreter's `jupyter` block and `session_ttl_seconds`, and the desktop adapter's `screenshot_interval_seconds`. A config.yaml that still sets one loads: the key is dropped with a warning (`REMOVED_SETTINGS`). `server.public_base_url` stays: the read missed that `config.py` itself uses it | Re-read of every key | Minimal | S |
| C4 | Branches: delete the five remote branches whose work is in main (`feat/autonomy-levels`, `blocker-report`, `memory-that-lasts`, `monitors`, `survey-job`); review and merge `feat/tell-the-logins` (59 lines with its test; bar 7); prune the 60 local branches (28 merged, 7 closed, 25 never opened as a pull request: read before deleting) | git | none, then Minimal for the login | S |
| C5 | Documents: one plan. ROADMAP keeps Built, Not doing and Known limits; its two "Next" sections fold into section 9 here; AGENTS.md and the README point here for what is next. ARCHITECTURE's account of the ladder follows D4. The archive stays: code cites it as evidence | Read of every document | none | S |
| C6 | `call_rewrites.py` (854 lines, about 20 rules; the safety rules stay: a delete asked for by words only, an unrequested move, a blind persona overwrite): count each rule's firings; after a week of normal use, delete the alias and habit rules that never fired under the strict schema (D6 part 3). Its line count may only go down | Nothing records which rule fires today | Design | M |
| C7 | The ladder and the retry loop: the escalation rungs (a wider search, a script, the hand-over) are the retry loop's kinds (D4); `ladder.py` keeps only its evidence checks (write receipts, move scope, forms and captchas, tallies), and its stale header goes | Read of both | Design | S |
| C8 | The rolling chat summary: one 9B call before every reply. With the last exchange word for word (D5a), the facts, the past-chat search and the project index, it may say nothing new. Turned off on the same frozen cases (including `v_move_then_undo`, the reason it stayed) and the continuity suite; removed if they hold | `memory.summarize_memory_in_background` note; intake log | Design | M |
| C9 | Saved workflows and folder triggers: a second way to keep a procedure (learned skills are the first) and a second folder watch (`jobs/watch.py` is the first); the live database has 0 workflows and 0 watched folders; 6 wording patterns. Retire them, or fold "run a saved procedure" into the watch job: the owner's decision | Live database | Design | M |
| C10 | Five proof checks into one ordered check (D3): `answer_checks`, the auditor, `claims`, `fulfillment`, `number_check` (23 wording patterns between the last three). The claims check's false alarms on drafts are the live failure D3 was waiting for | Three false alarms, 11/14 to 11/20 | Design | L |

Not in this cycle: splitting `admin.py` (4,154 lines) and `worker.py` (1,616): large moves with no defect waiting on them. The 17 e2e case files stay: the retired exams are the
evidence of the same-case numbers.

## 9. What is left, in order

1. **The owner's decisions** (they change scope; nothing below waits on them). (a) Keep the computer awake whenever YBM runs, at least on mains power, so a message
   never finds it asleep. (b) C9: retire saved workflows and folder triggers, or fold them into the watch job. (c) Release 0.2.2 with #310 to #318. (d) The mailbox
   needs his credentials.
2. **Quick cleanup, C1 to C5** (S each; Minimal or no label). C4 brings back the one missing piece of bar 7: telling the owner a login made for him.
3. **The folder gap** (Design, M). A way to put chosen files into a new folder (an `organize_plan` strategy that takes a folder name and which files, applied by
   `apply_manifest` as today, which already makes the folder); the continuity case made strict (the files really in the folder, "blocked" no longer a pass). Done
   when the case passes 3 of 3 live; it was 1 of 6.
4. **G0.5, the recorded fixtures in the owner's config** (abilities, plans and vision on). Any change to a tool's schema or description needs it: the folder gap and C6.
5. **The proof runs (G2)**, each in its own window with the computer awake and no other live run: the survey (at least 90 percent of the registry's daycares), the
   600-page research, and the speeds (2, 6 and 15 seconds).
6. **The rest of D5b and D4**: the nightly rewrite of each project page; the project-questions (6) and switch-and-return (12) sets; a lesson recorded when a second
   approach works, which becomes a procedure after review.
7. **The Design cleanups, by evidence**: C8 (a model call saved on every message), C6 (after a week of counts), C7, C10 (D3), C9 (after the decision).
8. **G3**: the big-build walk-through ("next step", reminders); a model swap tested on a copy and rolled back.
9. **G4**: the three-day run with a restart, the computer awake; the site post.
10. **The frozen sets of section 10 that do not exist yet**: intake (150 messages), size of the ask (20), diagnosis (10), stuck (8), asking (30), proof (planted false
    answers), tool-call shape. Each is sealed before the work it measures; D1 waits on the intake set.

## 10. Tests: nothing the owner waits on runs over 5 minutes

| Check | When | Wait |
|---|---|---|
| Unit tests and the recorded conversations, in the owner's config | Every change | 5 to 40 s |
| Live change check on the real bot, 5-minute budget (`scripts/run_all_e2e_tests.py --budget-seconds 300 --changed-since <ref>`) | A change that affects behavior | 5 min or less |
| The design change's area, same cases before and after | Each Design change | none: unattended, in chunks |
| Nightly slice of the live cases | Off until the owner allows messages at night | none |
| Release exam and three-day run | A release | none |

New frozen sets, each sealed before the work it measures (most are not built yet: section 9, item 10): **context** (the prompts of the recorded conversations), **diagnosis** (10 recorded failures through the
diagnosis call), **intake** (150 or more messages), **size of the ask** (20 wordings), **proof** (planted false answers), **tool-call shape** (every recorded call
replayed), and live overnight sets for **stuck** (8), **project questions** (6), **switch and return** (12), **memory**, and **asking** (30 requests at three levels).

Pass marks: 32 of 32 frozen everyday cases; Challenge at least 35 of 39; Verdict 4 at least 18 of 20 with every safety case passed; hands at least 17 of 20;
memory at least 90 percent; projects 11 of 12; survey at least 90 percent of the registry; zero false claims; speeds 2, 6 and 15 seconds.

Verdict 4 candidates: `archive/ULTIMATE_ASSISTANT_PLAN.md`, section 5. Its twenty requests are written and sealed (hash in the repository) before the work they
measure, and their wording is not used while building.

## 11. Sources

[Agent Skills specification](https://agentskills.io/specification.md) (name and description always loaded, the body on use, files on demand);
[Anthropic, effective context engineering](https://anthropic.com/engineering/effective-context-engineering-for-ai-agents);
[Anthropic memory tool](https://platform.claude.com/en/docs/agents-and-tools/tool-use/memory-tool) (memory as files the agent reads and edits);
[Letta memory](https://www.letta.com/blog/agent-memory) (small blocks always loaded, the rest searched, tidying in the background);
[Manus, context engineering](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus?hl=en-GB) (files as memory, keep a pointer, keep errors visible);
[ChatGPT projects](https://help.openai.com/articles/10169521);
[Reflexion](https://arxiv.org/pdf/2303.11366); [AgentDebug](https://www.alphaxiv.org/abs/2509.25370v1); [ADaPT](https://arxiv.org/pdf/2311.05772.pdf);
[LATS](https://arxiv.org/pdf/2310.04406); [OpenHands stuck detector](https://docs.openhands.dev/sdk/guides/agent-stuck-detector).
