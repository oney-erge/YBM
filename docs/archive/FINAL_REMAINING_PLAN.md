# YBM final remaining plan

Archived 2026-11-19: [PLAN.md](../PLAN.md) is the one plan now. Updated 2026-11-18. This replaces the November 15 plan page (R0-R7) and the November 17 version
of this file (P0-P8). Everything still open from both is in here once; nothing else is pending.

The goal: one assistant on this PC, reached through Telegram, that holds on to what the person
wants until it is done or honestly blocked; decides routine things itself as far as the person
allows; finds another way when one fails; and proves a result before it says "done".

## 0. Status at the end of 2026-11-18

Written after the work below ran on the real bot. Numbers are from runs on this PC; nothing here is rounded up.

| Phase | State |
|---|---|
| P0 land, release, baseline | Done: working tree landed (#247), v0.1.7 tagged and its assets verified (a clean install was **not** run), Verdict 3 sealed (#248) and run once, continuity suite (6 dev cases). `lifecycle-implementation` and `lifecycle-current-*` are **not** integrated. |
| P1 goals and understand | Done and live: goals table, `goal_ref`/`act` in the one classification call (53/56 goal right, 52/56 act right on the 9B), "You choose." and a bare answer reach the goal's question (#249). The typed `reply task_…:` syntax is no longer asked for when the question has a goal; it is still there for questions without one. |
| P2 evidence and initiative | Done: an install is done when an install ran; `operator.initiative_level` 1-5 is a prompt preference plus a gate (an options question at level 4 or 5 is answered by the assistant, at most twice) (#249, #254, #255). Your setting is 4. |
| P3 investigate and the TV | Investigation done and live: five read-only checks, findings, up to three options, the goal waits for the pick (#252). The picked option is **not** carried out reliably by the 9B (it asks redundant questions); no setting on the TV or PC was changed by any run. |
| P4 research claims, 600 pages | Claim check done (#250): on the 9B all wrong claims were removed and 2 right ones lost. Page cap is 1000. The 600-page overnight run has **not** been done. |
| P5 mailbox and watches | `mail.manage` built and tested against scripted servers only (#253); off until you give a username and password. Watches built (#256, `tell-me-when` skill, installed); the exam's watch-a-folder case passed, the fires-once case is manual and was not run. |
| P6 learning and speed | A chat reply no longer waits for the conversation summary (#251): 23 s became about 7-10 s; the 6 s target is **not** met. Learning beyond the existing cards is not built. |
| P7 final exam | Run, not passed (below). |

| Done check | Result |
|---|---|
| 1 Nothing broke: 32 frozen cases | 29 of 32 at level 4 on the final build (27 of 32 at level 3 before the last fixes). Failing: `v_cannot_order` every time (the done-check rejects an honest "I cannot" because it never sees the answer), and one or two of `h4_cannot_phone`, `h4_three_files`, `v_web_titles` that change from run to run. Before 11/18 the same cases passed 32/32; the build is not back to that. |
| 2 Challenge | Not rerun. |
| 3 Verdict 3 (sealed, once) | **12 of 17** run cases (3 manual cases not run); target 18 of 20 **not** met. Failed: `a3` (a Word file named in the answer that no write recorded), `a5` (a correction to the folder just made), `b2` (declined hand-over follow-up: no task seen), `r1` and `d2` (jobs left open by earlier cases; #258 fixes the cause). A used exam is not scored again. |
| 4-9 | Not run: goal chain end to end on the TV, overnight research, mailbox round trip, speed. |

What the runs found and fixed on the way (all merged, with tests): a reply the model called "continue" to a goal's question started a second task; independent cases shared goals and jobs; a bare file name with a `root` was written beside the program; reading a folder said "file does not exist"; an options question at level 4 was answered by asking again. Each is in `CHANGELOG.md`.

Still open, in order of what it costs you: the done-check (`orchestration/auditor.py`) should see the answer so an honest "cannot" completes; the Operator carrying out a picked option; the 600-page run; the mailbox credentials; the lifecycle branches; 0.1.8.

## 1. Where YBM stands today

Checked on 2026-11-18 from the repository, the running instance and its database.

| Item | State |
|---|---|
| `main` | `4331876` "Prepare YBM 0.1.7". The tag `v0.1.7` was never pushed; GitHub's latest release is 0.1.6. |
| Working tree of `C:\for fun\YBM` | 73 changed files (+3,477 / -387), plus new `initiative.py`, `jobs/report.py`, an initiative settings card and tests. Not committed. **The running bot uses it:** it restarted at 12:18 today from this folder. Backend suite on it: 3,195 passed, 2 skipped, 0 failed. Ruff: clean. |
| Branch `lifecycle-current-20261116-9d214a` | 4 commits not on main: research controls during in-flight work, unknown-intent block, cross-process publication check, setup-lock release. |
| Branch `job-intent-safety-20261116-b87c1a` | 1 commit: an earlier version of the unknown-intent block above (different patch). |
| Branch `launcher-lock-ybm-20261117` | 1 commit: the same patch as the setup-lock commit above. |
| Branch `lifecycle-implementation` | 1 commit, "Preserve earlier lifecycle work [skip ci]" (17 files: signals, task status, trace page). |
| Scratch worktrees `fix/exact-paths-in-memory`, `feat/research-chapters` | Uncommitted. Superseded by the working tree (`recent_artifacts_context`, chapters, Word report, quiet hours). |
| Frozen everyday cases | 32/32 on 2026-11-15. Not rerun on the working tree. |
| Challenge | 34 of 36 auto-graded cases on 2026-11-15; 3 cases need a person. Target 35/39. |
| Verdict 2 (sealed, run once) | 14/20. Its six failures were fixed and spot-checked. A used exam is not scored again. |
| Verdict 3 | Not written. |
| Research | A 150-page run read 96 of 101 pages and delivered a cited report. Its summary called a used GPU's price an "offloading cost": the quotation was real, the claim was wrong. |
| `operator.initiative_level` | Exists (1-5, default 3, Settings card). Your config has no value, so it is 3. |

### What the November 16 TV conversation proved

| What happened | Root cause |
|---|---|
| Asked to cast to the Samsung TV, YBM opened 8 browser pages (guessed `chrome://` addresses), then offered Claude Code. You said no; the task ended. | **An unfinished attempt ends the goal.** The only way out was "hand it over" or "tell what I found". And **nothing looked before acting**: it never found out what the TV supports or what the network does. |
| "Ok give me write me an optimum plan for it" made a plan titled with that sentence. | **No goal identity across messages.** "It" pointed at nothing, so a follow-up became new work. |
| The plan's steps were marked done by "Add googlecast to allowlist", "Use googlecast" and "No thanks". The install never happened and the script was never written. | **A reply was treated as a result.** |
| It asked which casting library to use; "You choose" started an unrelated data-analysis plan. | **Routine choices go to the person**, and the answer had no target. |
| "Tell me the contents of that file" read a garbled path and said the file did not exist. | Object references came from a model-written summary, not from records. |

Five root causes. Each one shows up in more than one feature, so each is fixed once, in one place.

### What the working tree already does, and what of it is patchwork

**Keep** (sound mechanisms, finish and land them):

- Guarded state transitions for clarifications, plans, plan steps and jobs (revisions, atomic admission, cancellation and settlement rechecks).
- A reply to an assistant-owned step resumes the step instead of completing it, and skipped prerequisites block their dependent steps.
- Questions and buttons bound to the question they belong to.
- Exact recent artifacts from the artifact records (`recent_artifacts_context`).
- Research in chapters up to 1,000 pages, batched search rewrites, a Word report beside the Markdown, delivery state that survives restarts, and quiet hours.
- The initiative setting: config, Settings card, and a per-task snapshot.

**Replace** (they fix one phrase at a time, and the person pays for it):

- The phrase regexes `delegates_choice`, `defers_plan_step` and the "no, you do it" pattern.
- The `reply task_…:` and `new:` syntax that a person must type when two questions are open, and the "Is that an answer or a new request?" fallback. Nobody should type a task id into Telegram.
- Initiative as one sentence added to the prompt.
- Recovery as an extra prompt hint plus three more steps (`alternative_after_repeat`, `local_recovery_step_limit`).

These treat symptoms per wording. The three mechanisms below remove the causes, and the patches are deleted when the mechanisms land (section 5).

## 2. The design: three mechanisms and two rules

### A. Goals: one durable record per thing the person wants

A **goal** holds:

- the objective, in resolved words, plus the person's own words;
- its status: open, waiting for the person, blocked, done or dropped;
- its **attempts** (the tasks, plans and jobs that worked on it);
- the methods tried, the methods declined (for example Claude Code), the evidence gathered and the artifacts made;
- the one decision or question it is waiting on;
- its next options.

One new table, plus a `goal_id` on tasks, plans and jobs. Old records get a goal on first touch. Plans, jobs and tasks stay what they are: they become attempts of a goal, not owners of it.

This is what makes "plan for it", "you choose", "no, you do it", "that file" and "how is the TV thing going?" mean something.

### B. Understand: one structured reading of every message

Before any routing, one call reads:

- the message;
- the open goals, each with what it is waiting on;
- the latest attempt's outcome;
- the recent artifacts.

It returns:

- **goal**: an existing goal's id, or new;
- **act**: one of answer, delegate the choice, decline a method, continue, make or revise a plan for the goal, control a job, ask about status, new request, or chat;
- the resolved objective, the referenced objects, and a confidence.

**Deterministic first.** Buttons and slash commands never go through the model.

**Fast.** The local decision model answers the yes/no parts. The 9B fills the structured part in about 1-2 s, and only when the decision model is unsure or a goal is open.

**Ambiguous means one plain question with buttons naming the candidates** ("The TV casting" / "Something new"). It never shows task ids.

This becomes the single front door. It replaces:

- the reply-or-new assessment;
- the phrase regexes above;
- the job-start action classification;
- the plan stage's goal-guessing;
- the per-step "is this a plan" check.

That finishes old R5, the "one understanding step". Request facts (deletes, moves, counts, dates) stay as they are; only who answers them moves later, in P6.

### C. Next route: an unfinished attempt never ends the goal

When an attempt ends without its result, the goal stays open and a **next-route step** runs. It is a job on the existing runner (saved, pausable, survives restarts), of a new kind, `investigate`:

1. **Record.** What was tried, the exact failures, useful outputs, and declined methods.
2. **Look before acting.** Read-only probes gather the facts the goal depends on: network adapters and profile, discovery services, firewall, devices answering on the network, one device's ports and self-description, Miracast support, browser policies. A first version of these probes exists in the session scratchpad (`system_diagnose.py`).
3. **Candidates.** Materially different methods come from:
   - the ability index;
   - quick web research with the research engine;
   - a small generated program;
   - acquiring a package or ability (existing `ability.acquire` and `dependencies.install` flow);
   - computer use for native screens such as Win+K;
   - Claude Code, as one option among them, never the only exit.
4. **Act by initiative level** (rule E). Try the next candidate, or offer at most two with a recommendation and the evidence for it.
5. **Verify** each attempt against its postcondition (rule D).
6. **Stop honestly.** When the candidates are exhausted or the budget is spent, say:
   - the exact blocker;
   - what was achieved and retained;
   - the smallest thing that would unblock it.

   The goal stays open for "keep trying" or a new idea.

Declining a method excludes it for this goal. It does not end the goal and is not consent to anything else.

### D. Rule: done means observed, not replied

Each kind of step has a postcondition that code checks:

| Step | Postcondition |
|---|---|
| Install | The package imports in the task's environment. |
| Write a file | It exists at the chosen path with the expected content. |
| Run a script | Exit code 0 and the expected output. |
| Find devices | The probe saw them. |
| Cast something | The device reports playback, or the person taps "I see it" (recorded as user-reported). |

A reply is an event:

- an answer resumes the step;
- a delegation lets YBM decide;
- a decline excludes a method;
- a user's "done" completes only user-owned steps, labeled as such.

The plan card shows what is done, what is waiting, and what evidence is missing.

### E. Rule: initiative decides who decides

The setting becomes a gate at every place that can ask the person: the planner's questions, the Operator's `ask_user`, library and package choices, and recovery. It is not a prompt sentence.

| Level | Optional questions YBM asks | Recovery after a failed attempt |
|---|---|---|
| 1 Guided | Before each meaningful step | Offers options, waits |
| 2 Collaborative | Before a change of method | Offers ≤2 options, waits |
| 3 Balanced | Only material trade-offs | Offers ≤2 options with a recommendation |
| 4 Proactive (your setting) | Almost none | Tries the next authorized method, reports milestones |
| 5 Delegated | None | Works through every authorized method within budget |

"You choose" delegates the pending decision on that goal, not the global setting. At every level, these still follow their configured approval policy:

- installs;
- writes outside the workspace;
- accounts;
- money;
- external agents;
- going past a budget.

## 3. Phases

Each phase lands as reviewed pull requests with the full backend suite and Ruff passing locally and on CI, and ends with a live check on the real bot and a rerun of the 32 frozen cases. Sizes: S is about one working session, M two, L three or more.

### P0. Land what exists and set the baseline (S)

1. Land the working tree as four reviewed PRs:
   - lifecycle and transitions;
   - plan steps resume and are blocked by skipped prerequisites;
   - research chapters, Word report, delivery state and quiet hours;
   - the initiative setting and artifact context.

   Leave the phrase regexes and typed syntax out of the PRs; P1 replaces them.
2. Integrate `lifecycle-current-20261116-9d214a` and `lifecycle-implementation` once, against the landed work. Drop `job-intent-safety` and `launcher-lock`: the first is an older version of a commit in lifecycle-current, the second is an exact duplicate. Remove the two scratch worktrees.
3. Publish **v0.1.7** from the reviewed tag point `4331876` (dry run passed on 2026-11-15). Verify the assets, the checksums and a clean install. The landed work ships as 0.1.8 after P1.
4. Write and seal **Verdict 3**: 20 new cases that include:
   - a follow-up chain like the TV one;
   - a delegated choice;
   - a declined handoff;
   - an investigation;
   - a 600-page research job;
   - the mailbox.

   Its hash goes in the repo before P1 starts.
5. Turn the November 16 chain into a regression suite (`continuity`): the same mechanisms with varied wording and topics (TV, printer, a script, a document).
6. Set `operator.initiative_level: 4` in your config.

**Done when** main is what runs, CI is green, v0.1.7 is published and verified, and Verdict 3 is sealed.

### P1. Goals and the understand step (L)

1. Add the goal record and links (A), with the migration and lazy creation for old records.
2. Add the understand step (B) as the only intake path for Telegram, web chat and WhatsApp. It writes `goal_id` and `act` on the task it starts or resumes.
3. Route:
   - "plan for it" makes or revises a plan **for the goal**;
   - "you choose" delegates the goal's pending decision;
   - "no, do it yourself" declines the method and continues the goal;
   - "keep trying" continues it;
   - "that file" resolves from the goal's and the conversation's artifacts, and is rechecked before use.
4. Disambiguate with one question and buttons. Delete the typed `reply task_…:` and `new:` paths, the phrase regexes, and the job-start and plan-stage goal guessing.
5. Measure on a labeled set of at least 150 messages: the November 16 chain, the continuity suite, real past messages, and unrelated requests sent while a goal is open.

**Done when**:

- target and act are right on at least 95% of the labeled set;
- the TV chain replayed on the real bot stays on one goal;
- an unrelated request during an open plan starts its own work;
- nothing ever asks you to type an id.

### P2. Done means observed; initiative decides who decides (M)

1. Add postconditions per step kind (D), built on the existing fulfillment and postcondition checks. Answers become events.
2. Show evidence on the plan card. Review the saved November 16 TV plan through an inspectable repair: keep its history and reopen the first step that has no evidence.
3. Make initiative a gate (E) at the planner, `ask_user`, package and library choice, and recovery.
4. Library choice belongs to YBM at level 3 and above. A package outside the allowlist becomes an acquisition card ("install pychromecast into the task's environment"). Policy still decides the install, and the import check proves it.

**Done when**:

- the same scenario at levels 1, 3 and 5 asks fewer optional questions at each step up, with identical approval cards;
- no reply can complete an install, file, script or device step.

### P3. Next route and investigation, proven on the TV (L)

1. Add the `investigate` job kind (C): hypotheses, experiments, evidence and options, one unit at a time.
2. Add the read-only probes as an internal tool: network overview, services, firewall, Miracast, browser policy, LAN devices, host check. Polish them from the scratchpad draft; only private addresses are allowed.
3. Add methods as real actions:
   - push a video to a DLNA renderer;
   - drive Win+K mirroring with computer use;
   - talk to the TV's own control API;
   - otherwise, a small generated program.

   Every change to your PC goes on a card with how to undo it, for example switching the network profile to Private or starting a stopped service.
4. Connect the next-route step to every attempt end: budget reached, stuck, declined handoff, or a failed plan step. Claude Code becomes one candidate.
5. Accept:
   - the TV;
   - a printer that stopped printing;
   - an app that won't start;
   - one impossible request, which must end with an honest blocker and the next step.

The TV starts from evidence, not a diagnosis. What one minute of checks showed on 2026-11-15/18:

- the network is marked Public;
- the WSL virtual adapter has a lower metric (15) than Ethernet (25), so discovery traffic can leave by the wrong adapter;
- the Function Discovery services are stopped;
- SSDP got no replies;
- the PC supports Miracast.

YBM must establish the TV's supported methods itself. Samsung TVs often support DLNA, Miracast and AirPlay rather than Google Cast.

**Done when** YBM finds why casting fails, offers or tries evidenced fixes at your level, and a video or your screen appears on the TV. Completion means playback observed, or your "I see it". If hardware prevents it, the honest blocker counts as handled but not passed.

### P4. Research at any size, with claims that hold (M plus overnight runs)

1. Land the chapters work (P0) and align every limit: the job tool, the config and the start parser accept 5 to 1,000 pages, default 60. Show the measured time estimate on the card.
2. Add a claim check. Each cited sentence is checked against its quotation for the same subject, quantity, unit and condition (a yes/no decision-model call per sentence, the 9B on doubt). A sentence that fails is dropped or marked uncertain. Add traps for quantity and subject to the tests (the GPU price case).
3. Report coverage honestly: sources found, read, useful and cited.
4. Run 150 pages by day, then 600 pages overnight with a restart in the middle and a question asked during the run.

**Done when** the 600-page run finishes with a Word report whose sampled claims check out, survives the restart without re-reading pages, and answers questions while it runs.

### P5. Accounts, the mailbox and watches (M)

1. Assisted sign-up with your chosen name and password: its browser fills the forms, credentials go to the vault, and CAPTCHA, SMS and consent come to you in Telegram with a screenshot. It never tries to beat those.
2. Order of providers:
   - Gmail first, with 2-step verification through an authenticator code it keeps, then an app password for IMAP;
   - Outlook.com second, read through its browser, because personal Outlook accounts no longer accept passwords in mail apps;
   - otherwise ask you.

   A refused sign-up stays an open goal with the next option.
3. Mail: read, use confirmation links, draft, and send only after your yes. Revoke access on request.
4. Watches on the scheduler ("tell me when …"): remember what was seen, notify only on a real change, stay quiet at night, survive a restart. A failed check is reported as a failure, not as "nothing changed".

**Done when**:

- its mailbox receives a test mail and it reports the mail;
- it sends a reply you approved;
- a watch notifies once for one change, across a restart.

### P6. Learning, speed and staying up (M)

1. Verified successes become procedures, for example "cast to the living-room TV: method X, verified 11/2x". Failures become one proposed lesson by card.
2. Keep facts about this PC with their source and age, and let you correct them. Show a weekly note with what it learned, what failed and what it needs.
3. Move request facts (deletes, moves, counts, dates) into the understand step's output where the labeled set shows parity, and delete their phrase patterns.
4. Measure the time to first sign, a chat reply and a simple file question (targets 2 s, 6 s and 15 s) with and without a background job. Fix the slowest step.
5. Behave correctly when the model server stops, the internet drops or the GPU is busy. Batch approvals into one card per batch.

**Done when** an approved lesson prevents a repeat failure the next day, and the time targets hold on a full run.

### P7. Final exam and release (S)

Run on one identified build and config:

- the 32 frozen cases;
- the continuity suite;
- all 39 Challenge cases, the manual ones included;
- Verdict 3, once;
- the live journeys:
  - TV;
  - a plan revision;
  - a delegated choice;
  - a declined handoff that continues;
  - an exact-file follow-up;
  - 600-page research;
  - the mailbox and a watch;
  - the house plan with notes and check-ins.

Then release 0.1.9 or 0.2.0, update the README, write the site post, and pin the scorecard.

## 4. Done checks for the whole plan

| # | Check | Pass |
|---|---|---|
| 1 | Nothing broke | 32/32 frozen cases, plus the continuity suite |
| 2 | Hard everyday requests | Challenge ≥ 35/39 by rubric, manual cases counted |
| 3 | Fresh sealed exam | Verdict 3 ≥ 18/20, every safety case passed |
| 4 | Holds the goal | The TV chain, "plan for it", "you choose", "no, you do it" and "that file" all stay on the goal |
| 5 | Finds another way | Every unfinished attempt ends with a tried alternative, or ≤2 evidenced options, or an exact blocker. Never "I could not, bye" |
| 6 | Proves results | No step or goal completed without its postcondition, and zero false claims |
| 7 | Long work | 600-page research overnight, restart survived, claims checked |
| 8 | Real world | TV playback (or an honest hardware blocker), mailbox round trip, a watch firing once |
| 9 | Speed | 2 s / 6 s / 15 s on the measured workload |

## 5. Deleted when the replacements land

| Removed | Replaced by |
|---|---|
| `delegates_choice`, `defers_plan_step`, the "no, you do it" regex (`clarification.py`) | Understand step (B) |
| Typed `reply task_…:` and `new:` syntax and their prompts (`channels/base.py`) | Understand step with button disambiguation |
| Reply-or-unrelated decision-model thresholds | Understand step |
| `job_start` action classification | Understand step (`act: control a job`) |
| Plan stage goal-guessing from `original_message_text` | Goal record (A) |
| `alternative_after_repeat` hint and `local_recovery_step_limit` | Next-route step (C) |
| "Answer now with what you have" after a declined offer | Next-route step |
| Initiative as a prompt sentence only | Initiative gates (E) |
| Request-fact phrase patterns, where the labeled set shows parity (P6) | Understand step output |

## 6. Working rules

- One PR per slice, never a broad refactor. Existing owners are extended: the job runner, the plan runner, the ability index, the vault, the scheduler and the guarded repositories. Nothing becomes a second orchestration framework.
- Full backend suite and Ruff locally, then CI. The UI needs a TypeScript build and the interaction check.
- Live checks run on the real bot, time-boxed. Same-case numbers before and after. Test-made plans, facts and tasks are cleaned out of your live memory.
- Never auto-approve an outward action to make a test pass. Never repeat a release or delete your data because an old plan says so.
- Long runs happen overnight on a released build while work continues, not in sequence with it.

Size: about 12 to 16 working sessions. P0 first, because everything later builds on what is landed and measured; P1 and P2 before P3, because recovery needs a goal to recover and a postcondition to check.

## 7. Evidence for this revision

- Repository: `git status` and `git diff --stat` on the working tree, `git log` on main and the four branches, and `git patch-id` comparisons of the duplicate commits.
- Running instance: the process list (started 12:18 today from this folder) and `/health`.
- Database, read-only: the November 16 task chain (the user's words, tool calls, answers and plan steps) and the research job's saved state.
- Tests on the working tree: the full backend suite (6 parallel processes) ran 3,195 passed, 2 skipped, 0 failed or errored; Ruff clean.
- Not checked: the frontend build, a live run of the working tree's new behavior, and the GitHub CI on the uncommitted work.
