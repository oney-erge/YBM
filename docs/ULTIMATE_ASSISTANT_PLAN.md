# YBM: the plan from here to the ultimate assistant

Written 2026-11-19. This is direction, not description: nothing below is built unless it says so.
What is built and what was measured is in [FINAL_REMAINING_PLAN.md](FINAL_REMAINING_PLAN.md) section 0.
This file is the next program. It starts when the current one ends (section 7, P0).

## 1. The bar

YBM is one assistant on one PC, reached through Telegram, that stays. Models will change; this stays.
It is the owner's own, local first, and it should be able to take anything from "what is a heat pump" to
"I want to build a pool" or "I am going to build a humanoid robot" and work it through with the person,
at the size the words ask for. Ten things it must do:

1. **Know how big the ask is.** From the wording it decides: answer now, look a little, dig for an hour, or run
   for days. It says what it will do and when it will stop, before it starts.
2. **Hold projects, not just chats.** A pool, a robot, a daycare search, a newborn checklist stay in memory.
   Switch to another topic for a month, say "back to the pool", and it knows what was decided and why.
3. **Work a big build with the person at every step.** Ask only what changes the plan, then give phases, steps,
   a bill of materials with what to buy and where, a schedule, and reminders; update it as things change.
4. **Search exhaustively and know when it is done.** "Find every daycare in Pearland, keep going overnight" means
   all of them, checked against criteria, with sources, and it stops itself when more searching stops finding
   anything new.
5. **Use the computer and the web itself.** Open the app, fill the form, download the file, place the order up to
   the limit the person set, in the person's own signed-in sessions, so no password is ever sent in chat.
6. **Decide when told to decide.** "You pick" means it picks, inside limits the person set, and says what it picked.
7. **Write code when nothing fits.** If there is no tool, it writes a script, tests it on real data read-only, runs it,
   and keeps it. A recurring job ("watch every device on my network") becomes a monitor.
8. **Say exactly what to do when it cannot.** Not "I could not": what was tried, why it failed, two or three routes
   with cost, its pick, and the exact steps for the part only the person can do.
9. **Prove what it did.** Done means observed. Receipts, sources, screenshots; no claim without a record.
10. **Stay.** Survive restarts, reboots and model swaps. All data in open formats the owner can read and move.

Local first stays the rule: nothing goes to a hosted model without asking, and nothing is bought, sent or
installed without the limits the person has set.

## 2. Where it stands against that bar

| Behavior | State today |
|---|---|
| 1 Size of the ask | Missing. Jobs have a page limit; nothing reads "overnight" or "exhaustive" from the words or states a stop rule. |
| 2 Projects that remember | Partial. Projects exist as folders, instructions, corrections and criteria opened from the console. No hooks from chat, no notebook the assistant writes, no recall. |
| 3 Big builds with the person | Partial. Plans with dated steps and check-ins exist (`plans/`). No bill of materials, no intake, no walk-through mode. |
| 4 Exhaustive search | Partial. Research reads up to 1000 pages into a cited report. No entity search (a table of every daycare), no saturation stop. First live 600-page run found 250 pages and was interrupted by a search throttle (fixed, rerunning). |
| 5 Hands | Partial and unmeasured. `computer.use` (Windows, bounded loop) and `browser.control` exist; no suite measures them as a whole; no signed-in session; no account sign-up. |
| 6 Decide when told | Done for routine choices (initiative level 1-5). No spend or reach limits. |
| 7 Write code | Partial. `code.interpreter`, `adapter.factory`, `coding.agent` hand-off, skills exist. No test-then-keep loop, no monitor job. |
| 8 Exact blockers | Partial. Investigations offer evidenced options and a goal waits for the pick. Not a standard ending for every failure. |
| 9 Prove it | Mostly. Postconditions, claim check, receipts. The done-check cannot see the answer, so an honest "I cannot" is rejected. |
| 10 Stay | Partial. Restart survival, backup, additive migrations. No model-swap procedure, no restore test, no morning health message. |

## 3. The design: no new subsystem where an existing object can carry it

Four objects are remembered, one gate decides who may act, and every request is answered by the same three questions.

**Objects.**

- **Goal** (exists): one thing wanted now, with its attempts. Short.
- **Project** (extend): the long-lived container: a name, hooks, a notebook, links to its goals, plans, jobs and files.
  Never "closed": active, parked or done.
- **Plan** (exists): ordered dated steps.
- **Job** (extend): background work. Kinds today: research, investigate, watch. Added: survey, monitor.

**Three questions, asked once per message, in the one call that already reads it (the Concierge):**

1. *How big is this?* effort class and stop rule (section 4, M2).
2. *Whose is it?* which project or goal it belongs to (section 4, M1).
3. *Who may decide?* the authority envelope: initiative, spend, reach (section 4, M4).

**Then the existing loop does the work** with tools, hands and, when nothing fits, code (M5), and ends in a receipt or
a blocker report (M5).

**What this is not.** No per-domain code. A pool, a robot, a daycare search and a newborn checklist are the same
machinery with different skills (markdown recipes) loaded. Hooks find candidates; the model confirms them. The repository's
own rule stands: words never trigger a plan or a project by themselves (see ROADMAP "Not doing").

## 4. The additions

### M1. Projects that remember

Today a project is folders plus instructions that you open in the console. Add, on the same store:

- **Hooks.** The words, names and places that point to the project (pool, Pearland, "the robot"), collected from what
  you say and edited in the console. Hooks produce up to three *candidate* projects for a message; the decision model
  confirms ("is the person talking about this project?") as it already does for abilities. A hook alone never acts.
- **Notebook.** Dated entries the assistant writes at the end of every task, job and plan step: decisions (what, why,
  who chose), facts with their source, open questions, files made, purchases, job reports. Markdown on disk, so it is
  readable and movable. A preference is shown for a yes before it is kept (existing "reviewed project memory" rule).
- **Recall.** A confirmed project puts its one-paragraph "where we are" and the few notebook entries that fit the
  message into the Operator's context, the way "Open goals" does today. A tool, `project.recall`, answers "what did we
  decide about the pool filter?" and "what have I spent so far?" from the notebook.
- **Switching.** No open or closed. Talk about something else and the project parks itself with its state. Mention it,
  or a hook, later and the assistant offers "back to the robot project? We were choosing the actuators." Retrieval
  happens only inside that project, and entries can be edited or deleted.
- **No embedding stack** until measured recall misses justify it (ROADMAP "Not doing").

Done when: 12 scripted switch-away-and-return scenarios (a week of other topics in between) recover the right earlier
decision in at least 11, with no entry from another project shown.

### M2. How big, and when to stop

The Concierge call also returns two fields, read from the person's words:

| Class | Signals in the words | What it does |
|---|---|---|
| Glance | a plain question, "what is", "how many" | answers now, from memory or one lookup |
| Look | "find", "compare", one thing, no breadth word | a few lookups, a short answer, minutes |
| Dig | "research", "read about", a page count, "thorough" | a job, tens of pages, a cited report, up to an hour or two |
| Campaign | "all", "every", "exhaustive", "overnight", "keep going", "as long as it takes", "3 days" | a job that runs across nights until its stop rule holds |

The second field is `stop_when`: the rule the job will use. It is shown in plain words on the job card before the
single approval: "I will look for every licensed daycare in Pearland and nearby, check each against your criteria and
keep going until three rounds in a row find nothing new. Probably 6 to 10 hours, never more than 3 days. I will message
you at milestones and send a summary each morning. [OK] [Shorter] [Longer]". The person can change it in one tap or one
sentence.

What "satisfied" means is measured, per job kind, so the assistant is not guessing:

- **Survey:** the rate of new entities has stayed under 2 percent for three rounds in a row, and 95 percent of the
  entities have every criterion filled or marked "not found after N tries", and the ranking did not change between
  two passes.
- **Research:** every question has at least two independent sources and every claim is checked against its quote.
- **Budget:** a hard ceiling in time and pages the person set (default 3 days), with a stop that always works.

A campaign survives restarts (exists), asks Windows to stay awake while it runs, keeps quiet hours for messages only,
backs off when a site throttles it (built 11/19), and sends one summary a day.

Done when: five differently worded requests (glance to campaign) are put in the right class and the job card states a
stop rule that matches the wording, on the real bot.

### M3. The survey job: every one of them, checked, with sources

A new job kind in the same runner. The goal becomes an *entity type* (daycare, restaurant that serves pastel de nata,
pool builder) and *criteria* (from the words, asked once if missing). Steps:

1. Find the authority first: a licensing registry, an official list, a city or state database.
2. Then the directories, maps and review sites, for what the registry does not have.
3. Merge duplicates (name plus address) into one table, one row per entity.
4. Visit each entity's page and fill the criteria columns with a source quote; mark a cell "not found" only after
   several tries.
5. Check the top candidates harder (inspection records, complaints, recent reviews).
6. Rank by the person's weights; deliver a spreadsheet and a short written summary; keep "what changed since last time"
   so it can run again next month.

The same engine does daycares, restaurants, contractors for the pool, suppliers for the robot, and used-item hunts.
It reuses search, page reading and the claim check from the research job; its state is a table instead of chapters.

Done when: a daycare survey on the real bot finds at least 90 percent of the entries in the state registry for the city
(a checkable ground truth), every row has a source, and it stops by itself.

### M4. Hands: computer use you can trust, sessions without passwords, accounts, purchases

`computer.use` and `browser.control` exist; nobody has measured them as one capability. This is the foundation for
"order it", "sign up", "fill that in".

1. **Measure first.** A Hands suite of 20 scenarios on the real PC: open an app and change a setting, fill and submit a
   web form, download a file and rename it, move something in Explorer, create a free account up to the CAPTCHA, put an
   item in a cart and stop before paying, read a page behind a cookie wall, recover when a button is not where it was.
2. **Make it reliable by structure, not by prompt.** Read the accessibility tree first and the screenshot second; after
   every action, observe and check the effect; stop on a mismatch and say so; a site that works becomes a saved recipe
   (a skill) so the second time is a script, not an improvisation.
3. **Sessions without passwords.** A separate browser profile for the assistant. You sign in to your sites in it once, by
   hand, on your own screen; YBM drives that profile and never sees or stores those passwords. Per-site grants: which
   sites it may use, which it may never touch (banks, by default).
4. **Accounts it makes itself.** For its own accounts (the mailbox) it signs up through the browser with credentials it
   generates and keeps in the vault. Anything that needs a human (CAPTCHA, a text message, an ID check) is handed to you as a
   screenshot and the exact thing to do, and it continues after. It never bypasses these. Because you own a domain, a
   mailbox on it may avoid phone verification at the large providers; checking what your host offers is part of this step.
5. **The authority envelope (the one gate).** Three dials in Settings: *decide* (initiative 1-5, built), *spend* (per
   order cap, monthly cap, allowed merchants; default 0 means always ask) and *reach* (which sessions, sites and folders).
   An order inside the envelope goes through with a receipt and a screenshot; one outside it is a one-tap card showing
   item, price, shipping and merchant. Money, anything irreversible, anything legal or medical is a card unless the
   person raised that dial on purpose. YBM never holds card numbers; payment is whatever the signed-in browser already has.
6. **"You pick."** A delegated choice is made inside the envelope, recorded with its reason, and reversible where it can be.

Honest limits: some sites block automation, and then it says so and gives the manual steps. Desktop control is
Windows-only today.

Done when: the Hands suite passes at least 17 of 20 on the real PC, an order is placed end to end on a test merchant at
the cap, and a mailbox exists without the owner typing a password.

### M5. Write the code, keep it, monitor with it, and end every failure with a blocker report

When no tool fits:

1. Write a script in the sandboxed workspace.
2. Test it on real data, read-only, before it is allowed to change anything.
3. Run it, show what it did, and offer to keep it as a skill with its test ("I can do this again").
4. If the request is recurring, register it as a **monitor** job: the saved read-only script runs every N minutes,
   compares with the last snapshot and speaks only on a change. "Connect to every device on my network and monitor them"
   becomes: discover (the probes built for the TV investigation), build an inventory (device, address, vendor, first and
   last seen), alert on a new or missing device. Anything intrusive (port scans beyond the home subnet, credentials) is a
   card. Installs need a yes. New code never runs outside the workspace without approval.

When it still cannot, it ends with a **blocker report**, the standard ending for any failure:

- what was tried, with the evidence;
- why it failed, in one sentence;
- two or three routes: A you do this, with exact steps; B I build this, with the cost; C hand it to Claude Code, with what
  it would see;
- its pick and the one thing it needs from you.

The goal stays open (built); the chosen route is tried next.

Done when: the network-monitor request and a request with no possible tool (a phone call) both end correctly on the real
bot, and the done-check accepts an honest "I cannot" (today it does not).

### M6. Companion mode for big builds

This is Projects, Plans, Jobs, Survey and Hands working together, with one skill that tells the model how to run it:

1. **Intake.** At most five questions, one at a time, only those that change the plan (budget, site, skill, deadline,
   constraints), with a default per initiative level.
2. **Plan.** Phases, then steps; each step has why, time, cost, tools and materials, risks, permits and a "done when". A
   **bill of materials** as a table: item, spec, quantity, estimated price, two or three candidate sources, status
   (to buy, ordered, arrived).
3. **Walk.** "Next step", reminders on the schedule, a request for a photo or a measurement to check a step, a notebook
   update, and a replan when something changes.
4. **Side jobs.** Permits, contractor survey, cost comparison run as jobs; ordering runs through Hands inside the envelope;
   arrivals are tracked through the assistant's mailbox.
5. **Safety.** For electrical, structural, excavation, pressure and battery work it says what needs a licensed person or a
   permit, cites its sources, and does not improvise.

Done when: a scripted pool project and a scripted humanoid-robot project, each run as five sessions with topic switches in
between, keep the decisions, the bill of materials and the schedule consistent, and the final answer to "what did we spend and
what is left to buy" matches the notebook.

### M7. Staying power

- **Model changes.** Every judgment goes through one of three interfaces: chat model, decision model, vision model.
  Thresholds and prompts are calibrated by scripts (as the skill thresholds were measured) and stored with the model id. A
  swap is: run `ybm calibrate`, then the frozen suites; it ships only if they hold.
- **Data.** SQLite, markdown notebooks and plain files. Nightly backup (built) plus a monthly restore test. One command
  exports everything to a folder.
- **Operation.** Autostart and a watchdog (partly built), a morning message with one line of health (model up, search
  works, browser profile signed in, mailbox reachable, disk space, jobs running), staged updates with rollback.
- **Dependencies.** Standard library first, pinned, no cloud requirement.

Done when: a model swap on a copy passes calibration and the frozen suites without code changes, and a restore from backup
brings back a project with its notebook.

## 5. The exam: what you might ask

These are the requests the assistant is judged on. Each has its class (section 4, M2) and what "good" looks like. Twenty of
them become a sealed exam (Verdict 4) written before the work starts; none of the wording is used while building.

**Big builds**

| Request | Class | Good looks like |
|---|---|---|
| "I want to build a pool in my backyard from scratch." | Campaign | Intake of at most five questions, local permit and contractor facts with sources, DIY versus contractor costs, a plan, a bill of materials, a project that persists |
| "I am going to build a humanoid robot. What are the steps and what should I buy?" | Dig, then project | Phases (frame, actuators, compute, power, control, safety), three budget tiers, a bill of materials with sources, what to buy first, what can go wrong |
| "Turn my old PC parts into a home server." | Dig | Inventory of what the parts are, what is missing, steps, a shopping list |
| "Plan our daughter's first birthday." | Look | Date, budget, guest list questions, checklist, reminders |
| "Plan a trip to Portugal in May with a baby." | Dig | Itinerary options, baby logistics, costs, bookings left as cards |
| "Set up a vegetable garden for our climate." | Dig | Zone, calendar, bed plan, shopping list, reminders |

**Exhaustive search**

| Request | Class | Good looks like |
|---|---|---|
| "Find every licensed daycare in Pearland and nearby, check them against my criteria, keep going overnight." | Campaign | Registry first, table with sources, stops by itself, morning summary |
| "Find the best pastel de nata within 40 miles. Be exhaustive. Take as long as you need." | Campaign | Every candidate, evidence of quality, a ranked list, what it could not verify |
| "Find pediatricians near me who take new patients on my plan." | Dig | Table, sources, calls it cannot make left as steps for you |
| "List every pool builder in the county with their licenses and complaints." | Campaign | Registry check, complaint records, ranking |
| "Tell me when a used standing desk under 150 dollars shows up." | Monitor | A watch with a stated check rate, one message per hit |

**Ordering and money**

| Request | Class | Good looks like |
|---|---|---|
| "Order the cheapest USB-C cable that does 100 watts and is 2 meters." | Look | Compares, picks, stops at the cap or shows the card |
| "Reorder diapers like last time." | Look | Finds the last order in memory or mail, same item, card or order within the cap |
| "Buy everything on the robot parts list that costs under 30 dollars from one store." | Dig | Groups by store, shows the basket, card |
| "Tell me when my package ships." | Monitor | Reads the mailbox, reports once per change |

**The computer and the network**

| Request | Class | Good looks like |
|---|---|---|
| "Connect to every device on my network, list them, and tell me when a new one appears." | Monitor | Inventory, a saved read-only monitor, alerts, nothing intrusive without a card |
| "My PC is slow in the morning. Find out why and fix what you can." | Dig | Read-only checks first, evidence, safe fixes, cards for the rest |
| "Back up my documents every night and tell me if it fails." | Monitor | A scheduled job with a check that the copy exists |
| "Clean my downloads folder. Ask before deleting anything." | Look | A plan card, nothing deleted without a yes |
| "My Samsung TV will not take casts." | Dig | Built; the picked option must also be carried out |
| "Write a script that renames my photos by date, and run it." | Look | Writes, dry-runs, shows the result, offers to keep it |

**Memory and projects**

| Request | Class | Good looks like |
|---|---|---|
| "What did we decide about the pool filter?" (after two weeks of other topics) | Glance | The decision, the reason, the date |
| "Back to the robot. Where were we?" | Glance | State, next step, open questions |
| "What have I spent on the pool so far?" | Glance | A sum from the notebook with the list behind it |
| "Remember that I prefer short answers." | Glance | Asks to keep it, then applies it |
| "Every Monday at 8, tell me what is open across my projects." | Monitor | A schedule, a real summary |

**Life and admin**

| Request | Class | Good looks like |
|---|---|---|
| "Make a checklist for the first six months with a newborn and remind me at each stage." | Dig | Staged list, reminders set |
| "Find the dishwasher warranty and tell me when it ends." | Look | Finds the file, quotes the date |
| "Draft an email to my landlord about the leak and send it after I approve." | Look | Draft, a card with the text, sends from its mailbox |
| "Compare these two quotes over five years." | Look | A table with the arithmetic done by code |
| "Teach me electronics from zero over eight weeks for the robot, and quiz me." | Campaign | A course plan, lessons on a schedule, quizzes, progress in the notebook |

**Code and work**

| Request | Class | Good looks like |
|---|---|---|
| "Build me a small app that tracks my expenses, and run it." | Dig | Builds, tests, runs, tells how to use it |
| "Fix the failing test in my repository." | Dig | Hands to a coding agent with a card, reports what changed |

**Honesty**

| Request | Class | Good looks like |
|---|---|---|
| "Call my mom and tell her I am late." | Glance | Says it cannot call, offers the nearest thing, as a finished answer |
| "What can you do, and what can't you?" | Glance | Reads its own ability list, not a guess |
| "That did not work yesterday. Try another way." | Look | Continues the same goal with a different route |

## 6. Ways this can fail, and what limits it

- **The model is small.** The 9B is the planner and the hands. The answer is structure in code (tables, stop rules,
  verification, saved recipes) and a decision model for yes/no judgments, with every threshold measured. Where that is
  not enough, the blocker report says so instead of guessing.
- **The web fights back.** Search engines throttle and sites block automation. The runner backs off and reads what it has;
  a site that blocks gets a manual-steps answer, never a workaround that evades its checks.
- **Money and accounts.** Ordering is the riskiest thing here. Default spend is zero, every order leaves a receipt and a
  screenshot, and the owner can read the log. CAPTCHA, text-message and ID checks are always a person's.
- **Safety in the physical world.** A pool, wiring and a robot with motors can hurt people. The assistant plans and
  cites, names what needs a licensed person or a permit, and does not improvise.
- **Scope.** This is single-owner software on one PC. It is not a hosted service.

## 7. The order

| Phase | What | Size | Depends on |
|---|---|---|---|
| P0 | Finish the current program: merge the open PRs, the 600-page run to the end, the done-check seeing the answer, the picked option carried out, release 0.1.8 | S | nothing |
| P1 | Projects that remember (M1) | M | P0 |
| P2 | How big and when to stop, then the survey job (M2, M3) | L | P1 for the notebook |
| P3 | Hands: measure, sessions, accounts, the envelope, purchases (M4) | L | P0 |
| P4 | Write the code, keep it, monitors, blocker reports (M5) | L | P0 |
| P5 | Companion mode (M6) | M | P1, P2, P3 |
| P6 | Staying power (M7) | M | any |
| P7 | The exam: sealed Verdict 4, a three-day soak with a restart in the middle, release | S | all |

Why this order: projects come first because everything else attaches its work to one. The survey comes second because it
delivers the overnight results you asked for and needs only the job runner that exists. Hands come before the companion
because ordering and account creation are what make a build project useful. P3 and P4 can run in parallel with P1 and P2
if two lines of work are open at once.

Each phase lands as reviewed pull requests with the full backend suite and Ruff passing, a live check on the real bot,
and a rerun of the frozen cases with the same cases before and after. The sealed exam is run once, on the final build.

## 8. Decisions needed from the owner

1. **Spend dial.** Recommended: always ask until the Hands suite passes; then a 50 dollar per-order cap for merchants you
   list, and a monthly cap you choose.
2. **Browser profile.** Recommended: a separate profile for YBM that you sign in to once, by hand, for the sites it may use.
3. **Mailbox.** Recommended: a mailbox on your own domain if your host offers one without phone verification; otherwise a
   free provider account that YBM signs up for while handing you any CAPTCHA or text message.
4. **Campaigns.** May a campaign keep this PC awake overnight and for up to 3 days? Messages stay quiet 23:00 to 08:00.
5. **Home facts.** May it keep "Pearland, Texas" as a remembered fact for local searches, and your daughter's age for
   planning checklists?
