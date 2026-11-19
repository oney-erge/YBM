# YBM: the plan from here to the ultimate assistant

Written 2026-11-19, revised the same day with the owner's answers (section 8). This is direction, not description:
nothing below is built unless it says so. What is built and what was measured is in
[FINAL_REMAINING_PLAN.md](FINAL_REMAINING_PLAN.md) section 0. This program starts when that one ends (section 7, P0).

## 1. The bar

YBM is one assistant on one PC, reached through Telegram, that stays. Models will change; this stays. It runs on a
local model, so its work costs nothing, and it should behave like it: take the job, work it out, do it, and report.
Twelve things it must do:

1. **Understand instead of asking.** It works out what is meant and how big it is from the words (typed or dictated),
   states its assumptions in its first reply, and goes. A question to the owner is the last resort, used only when
   nothing on the machine or the web can answer it.
2. **Know how big the ask is.** Answer now, look a little, dig for an hour, or run for days, decided from the wording
   ("take your time", "all the daycares around me", "a large comparative analysis", "overnight"). It gives a time
   estimate and updates; it does not ask which one the owner wants.
3. **Remember, across every chat, by default.** What the owner says about himself, his family, his home, his
   preferences and his projects is kept without asking, used when it matters, and can be listed, corrected or forgotten.
4. **Hold projects, not just chats.** A pool, a robot, a daycare search stay with their decisions and reasons. Switch topics
   for a month, say "back to the pool", and it knows where things stand.
5. **Work a big build with the owner at every step.** Steps, a bill of materials with what to buy and where, a schedule,
   reminders, updates as things change.
6. **Run long, exhaustive work by itself.** A campaign plans, works, judges its own result against a measured standard,
   and does more until the result is good enough or its ceiling is reached. Nobody has to watch it.
7. **Use the computer and the web itself.** Open the app, fill the form, sign in, create an account when one is needed and
   report the username and password it chose. No "please sign in for me". The one exception: it never spends the owner's
   money on its own. It prepares a purchase up to the payment step and asks.
8. **Decide when told to decide.** "You pick" means it picks and says what it picked.
9. **Write code when nothing fits,** test it read-only, run it, keep it; recurring work becomes a monitor.
10. **Say exactly what to do when it truly cannot.** What was tried, why it failed, the routes left, its pick, and the one
    step only the owner can take. Never "I could not".
11. **Prove what it did.** Done means observed: receipts, sources, screenshots.
12. **Stay.** Survive restarts, reboots and model swaps; keep everything in open formats the owner can read and move.

Local first stays the rule: nothing goes to a hosted model without asking.

## 2. Where it stands against that bar

| Behavior | State today |
|---|---|
| 1 Understand instead of asking | Partial. Initiative 1-5 exists and level 4 makes the assistant answer options questions itself; it still asks more than it should, and approvals still wait on cards. |
| 2 Size of the ask | Missing. Nothing reads "take your time" or "all of them" from the words. |
| 3 Memory across chats | Partial. A per-conversation summary (`conversation_memory`), global facts (`memory_facts`) and a `memory.manage` tool (remember, list, forget). No search over past conversations, no facts that change over time, no automatic remembering. |
| 4 Projects | Partial. Projects are folders, instructions, corrections and criteria opened from the console. No hooks from chat, no notebook, no recall. |
| 5 Big builds | Partial. Plans with dated steps and check-ins. No bill of materials, no walk-through. |
| 6 Long exhaustive work | Partial. Research reads up to 1000 pages into a cited report. No loop that judges its own result; no entity search. The first live 600-page run was interrupted by a search throttle, fixed and resumed. |
| 7 Hands | Partial and unmeasured. `computer.use` (Windows) and `browser.control` exist; no suite measures them; no accounts. |
| 8 Decide when told | Done for routine choices. |
| 9 Write code | Partial. `code.interpreter`, `adapter.factory`, `coding.agent`, skills. No test-then-keep loop, no monitors. |
| 10 Exact blockers | Partial. Investigations end with evidenced options; not yet every failure. |
| 11 Proof | Mostly. The done-check cannot see the answer, so an honest "I cannot" is rejected. |
| 12 Stay | Partial. Restart survival, backup, additive migrations. No model-swap procedure. |

## 3. The design

No new subsystem where an existing object can carry it.

**Remembered objects.** Goal (exists: one thing wanted now), Project (extended: the long-lived container), Plan (exists:
dated steps), Job (extended: background work, with a new campaign loop and new kinds), Memory (rebuilt: facts that change
over time, every past conversation searchable, project notebooks, procedures kept as skills).

**Three things the assistant works out for every message, by itself, in the one call that already reads it.** These are
not questions to the owner:

1. *How big is it?* glance, look, dig or campaign, with a stop rule (M2).
2. *What is it about?* the project, goal and remembered facts it touches (M1).
3. *What may it do alone?* the autonomy level and the owner's limits (M0).

**Then the existing loop does the work** with tools, hands and, when nothing fits, code (M5), and ends with a receipt or a
blocker report.

**What this is not.** No per-domain code: a pool, a robot, a daycare search and a newborn checklist are the same machinery
with different skills loaded. Words find candidates; a model confirms them. The repository's rule stands: words never
trigger a plan or a project by themselves (ROADMAP "Not doing").

## 4. The additions

### M0. Autonomy: three levels, and asking as the last resort

`operator.initiative_level` becomes three levels (an existing 1-2 maps to 1, 3 to 2, 4-5 to 3):

| Level | Name | Behavior |
|---|---|---|
| 1 | Ask | Asks before meaningful choices, as today's level 1-2. |
| 2 | Balanced | Asks only when a missing fact blocks the work and no default is reasonable. The default for a new install. |
| 3 | Autonomous | Does not ask. Decides, states its assumptions in its first reply, acts, and reports. The owner's setting. |

- **Understand, do not ask.** The step that reads a message returns *assumptions* ("I take 'around me' as Pearland and
  the towns next to it"), not questions. They are said once, in the first reply, so a wrong one can be corrected in a word.
- **The clarify tool has a budget.** At level 3 it may be used only for something no route can produce: a code sent to the
  owner's phone, a password that exists nowhere on this machine when making a new account is no answer, a choice with a
  legal consequence, and the yes for a purchase. The worker enforces the budget; a question outside it goes back to the model as "decide this
  yourself and say what you chose" (the gate that exists today for options questions, widened).
- **Approvals follow the level.** At level 3 routine approvals are granted by the assistant itself and recorded on the
  receipt as such. Two things always wait for the owner, at every level: any purchase or payment (M4, "Money"), and deleting
  something that cannot be restored.
- **When the words are ambiguous about size, it does both:** answers now and keeps working in the background ("here is a
  first answer; I am continuing for a fuller one, about three hours"). That removes the one question it would otherwise ask.

Done when: at level 3, a 30-request set (from section 5) produces no question that is outside the budget, and the frozen
cases hold.

### M1. Memory that lasts, and projects that remember

Memory is on by default (`memory.enabled: true`) and remembers without asking (`memory.auto_remember: true`); both are
configuration settings the owner can turn off. One memory serves every chat and channel (Telegram, console, WhatsApp).

The design follows what the published work on agent memory converges on, kept to what a local model and SQLite can do well:

- **Four kinds of memory.**
  - *Facts about the owner*: people, places, preferences, dates ("lives in Pearland, Texas", "a daughter born in
    autumn 2026", "prefers short answers"). Each fact keeps its source message, when it became true and when it stopped
    being true. A newer fact that contradicts an older one closes the old one instead of deleting it, so "where did we
    live before" still has an answer. This bi-temporal handling is the model used by Zep's Graphiti: the language model
    judges whether two facts contradict, and code does the invalidation.
  - *Episodes*: every message, reply, task and job result is already stored. Add a full-text index (SQLite FTS5, part of
    SQLite, no new dependency) and a short summary per day, and a `memory.search` tool: "what did we say about the custard
    place last month?" comes back with dates and quotes.
  - *Project notebooks*: below.
  - *Procedures*: how a site or an account works is kept as a skill after it worked once (M4), so it is a script next time.
- **Write path.** After each reply (the summary is already written after the reply, for speed), one extraction call
  proposes operations on the facts: add, update, close, or nothing. Code applies them and keeps the history; the model
  never edits the store directly. The owner sees a short "(remembered: ...)" line, not a question.
- **Read path.** For each message: candidates from the full-text index over facts, project hooks and recent episodes,
  weighted by recency; the decision model confirms which bear on this message; up to a handful of short lines go into the
  context ("What you know about the owner"). Questions about the past use `memory.search`.
- **Forgetting.** "Forget that" closes the fact; "erase that" removes it with its history. The console lists everything kept.
- **No embedding stack at first.** Full-text plus recency plus a confirming model; add embeddings only if the memory suite
  shows misses that keywords cannot fix (ROADMAP "Not doing").

**Projects** are the long-lived part of the same memory. Today a project is folders and instructions opened from the
console. Add: *hooks* (the words and names that point to it, collected from what the owner says and editable),
a *notebook* (dated entries the assistant writes after every task, job and step: decisions with reasons, facts with
sources, open questions, files, purchases, reports; markdown on disk), *recall* (a confirmed project puts its "where we
are" paragraph and the relevant notebook lines into context; `project.recall` answers "what did we decide about the pool
filter" and "what have I spent so far"), and *switching* (projects are never just open or closed: they park themselves
when the talk moves on, and come back when they are mentioned).

Done when: a memory suite on the owner's own kind of conversation, modeled on the five abilities LongMemEval tests
(a fact from one session, combining sessions, time, updated facts, and saying "you never told me" when it does not
know), scores at least 90 percent on the real bot; and 12 switch-away-and-return project scenarios recover the right earlier
decision in at least 11. Published vendor scores on these benchmarks disagree widely, so the measure is our own suite.

### M2. How big: read from the words, never asked

The Concierge call returns an effort class and a stop rule, read from the words, including dictated ones:

| Class | Signals | What it does |
|---|---|---|
| Glance | a plain question | answers now from memory or one lookup |
| Look | "find", "compare" for one thing, no breadth or time words | a few lookups, minutes |
| Dig | "research", "analyze", "a comparative analysis", a page count, "thorough" | a job, tens to hundreds of pages, a cited report, an hour or two |
| Campaign | "all", "every", "around me", "exhaustive", "take your time", "long", "overnight", "keep going", "as long as it takes", "for days" | a campaign (M3) that runs across nights until its stop rule holds |

The first reply says what it understood and how long it expects: "This is a long one: I will find every licensed daycare
in Pearland and the towns around it, compare them on safety record, ratios, hours, cost and reviews, and keep going until
new searches stop turning up new ones. Probably 6 to 10 hours. I will tell you how it is going a few times and send a
summary each morning." No buttons to choose a size; the owner can still say "make it quick" or "go deeper" at any time.

Done when: 20 differently worded requests, typed and dictated, land in the right class on the real bot, and none of them
produces a question about size.

### M3. Campaigns: a loop that judges its own result and keeps going

A campaign is a job that runs a loop, not a fixed recipe:

1. **Plan**: what to find, the criteria, the stop rule, the ceiling (default 3 days).
2. **Work** one unit: a search, a page read, a site visited, a table row filled, a script written and run.
3. **Judge**: a scorecard computed by code where it can be (coverage, how many new results the last rounds found,
   empty cells, independent sources per claim) and by the model where it must (is this source about the right place, is this
   answer good enough).
4. **Decide the next move**: broaden (new searches, new kinds of source), deepen (the top candidates, harder checks),
   repair (fill the gaps the scorecard shows), or stop.
5. **Update the report** and, at milestones, the owner.

"Good enough" is measured, not felt. For a search over things: new results under 2 percent for three rounds in a row, 95
percent of cells filled or marked "not found after N tries", and a ranking that did not change between two passes. For a
research question: every question answered by at least two independent sources, every claim checked against its quote.

The **survey** kind is the campaign for "find every one of them": the goal becomes a kind of thing (daycare, a bakery with
pastel de nata, a pool builder, a robot parts supplier) and criteria. Authority first (licensing registries, official
lists), then directories, maps and reviews; duplicates merged into one table; each row filled with a source quote; the top
candidates checked harder; ranked by the owner's weights; delivered as a spreadsheet and a short summary; rerunnable as
"what changed since last time".

A campaign keeps the PC awake while it runs (approved by the owner on 11/19), survives restarts, backs off when a site
throttles it, keeps quiet hours for messages only, and can be steered ("focus on the ones with a pool", "stop").

Done when: a daycare survey on the real bot finds at least 90 percent of the entries in the state registry for the area,
every row has a source, it runs overnight unattended and stops by itself.

### M4. Hands: the assistant signs in, signs up and gets things done

The owner's direction (11/19): use computer use freely; it is a local model on his own PC and costs nothing. Do not ask him to
sign in. Create accounts when needed, choose the username and password, store them, and tell him.

1. **Measure first.** A Hands suite of 20 scenarios on the real PC: change a setting in an app, fill and submit a web form,
   download a file and rename it, sign in to a site with stored credentials, create a free account, put items in a cart and
   check out within the limit, read a page behind a cookie wall, recover when a button moved.
2. **Reliable by structure.** Accessibility tree first, screenshot second; after every action observe and check the effect;
   stop on a mismatch and try another way; a site that worked becomes a saved procedure (a skill), so the second time is a
   script.
3. **Its own browser profile.** A separate profile the assistant owns and signs in to itself. Where the owner is already
   signed in on this PC (his Gmail in his own browser profile), it uses that session instead of asking for a password.
4. **Accounts it makes.** It picks the username, generates a strong password, signs up through the browser, stores the
   credentials in the local secret store (the `.env`-backed vault the mail tool already reads), writes or updates a skill
   that says how to sign in there, and tells the owner the username and password. Telegram carries that message, so the
   message deletes itself after a day by default and the console keeps the credentials.
5. **What it will not do.** It does not solve CAPTCHAs or other checks built to stop automated sign-ups. When one blocks a
   route it takes another (an existing signed-in session, a different provider, the owner's own domain); only when no
   route is left does it send one message with the single step needed, for example a code texted to the owner's phone.
6. **Money: it never buys on its own.** At every autonomy level, including 3, the assistant does not purchase anything,
   does not submit a payment, and does not use a saved card without the owner's yes for that exact purchase. It may do
   everything up to that point by itself: research, compare, choose, fill the cart, and stop on the page before payment.
   Then it sends one message with exactly what it is about to do:

   - the item or items, with links;
   - the seller;
   - the total with tax and shipping;
   - which payment method it would use (the card's last four digits as the site shows them);
   - the delivery address and date.

   It buys only after a plain yes to that message, and that yes covers that one order only. A change in price or items
   after the yes means a new message. Free sign-ups and free trials that ask for a card count as a purchase.

   The safeguard is written in two places, so it does not depend on the model remembering it:

   - **In the skill.** The purchase skill (`skills/starter/buy-something.md`, shipped with this phase) carries it as its
     first rule: "Never complete a purchase or submit any payment without the owner's yes to the exact order. Stop at the
     page before payment and send the order details: items, seller, total with tax and shipping, the card's last four
     digits, delivery address. A yes covers that one order. Anything that asks for a card is a purchase."
   - **In code.** A browser or desktop action that submits a payment is an approval-only action at every level: the gate in
     the worker refuses it without a recorded yes for that order, the same way an unrecoverable delete is refused today.

   YBM never stores card numbers. Payment is only ever what the owner's browser already holds, used after the yes.

Done when: the Hands suite passes at least 17 of 20, an account is created and used without the owner typing anything,
an order is prepared to the payment page and completed only after a yes, and a test where the model tries to pay without
one is refused by the code.

### M5. Write the code, keep it, monitor with it, and end every failure with a blocker report

When no tool fits: write a script in the workspace, test it on real data read-only, run it, show what it did, and keep it
as a skill with its test. Recurring requests become a **monitor** job: a saved read-only script that runs every N minutes,
compares with its last snapshot and speaks only on a change. "Connect to every device on my network and monitor them"
becomes: discover (the probes built for the TV investigation), an inventory (device, address, vendor, first and last seen),
an alert on a new or missing device.

When it still cannot: a **blocker report** as the standard ending. What was tried with the evidence, why it failed, the
routes left (what the owner can do in exact steps, what it can build and the cost, a hand-off to a coding agent), its pick,
and the one thing it needs. The goal stays open and the chosen route runs next. At level 3 it picks a route itself and
only reports.

Done when: the network-monitor request and a request no tool can do (a phone call) both end correctly on the real bot, and
the done-check accepts an honest "I cannot" (today it does not).

### M6. Companion mode for big builds

Projects, plans, campaigns and hands together, driven by one skill. The steps:

1. **Understand.** From what the owner said and what memory holds (budget, house, skill, deadlines), with assumptions
   stated, not asked.
2. **Plan.** Phases, then steps; each with why, time, cost, tools and materials, risks, permits and "done when". A **bill of
   materials**: item, spec, quantity, estimated price, two or three sources, status (to buy, ordered, arrived).
3. **Walk.** "Next step" and reminders on the schedule; a photo or a measurement checks a step; the notebook is updated; the
   plan changes when facts change.
4. **Side work.** Permits, contractors and price comparisons run as campaigns; orders go through hands; deliveries are
   tracked through the assistant's mailbox.
5. **Safety.** For electrical, structural, excavation, pressure and battery work it says what needs a licensed person or a
   permit and cites its sources.

Done when: a pool project and a humanoid-robot project, each run over five sessions with other topics in between, keep their
decisions, bill of materials and schedule consistent, and "what did we spend and what is left to buy" matches the notebook.

### M7. Staying power

- **Model changes.** Every judgment goes through one of three interfaces (chat model, decision model, vision model).
  Thresholds and prompts are calibrated by scripts and stored with the model id. A swap runs `ybm calibrate` and the frozen
  suites, and ships only if they hold.
- **Data.** SQLite, markdown notebooks and plain files; nightly backup (built) plus a monthly restore test; one command
  exports everything.
- **Operation.** Autostart and a watchdog; a morning message with one line of health (model, search, browser profile
  signed in, mailbox, disk, campaigns); staged updates with rollback.

Done when: a model swap on a copy passes calibration and the frozen suites without code changes, and a restore brings back a
project with its notebook and memory.

## 5. The exam: what the owner might ask

Each request has its class (M2) and what "good" looks like. Twenty become a sealed exam (Verdict 4) written before the work
starts; their wording is not used while building. At level 3 none of them may produce a question outside the budget (M0).

**Big builds**

| Request | Class | Good looks like |
|---|---|---|
| "I want to build a pool in my backyard from scratch." | Campaign | Assumptions stated, local permits and builders with sources, DIY versus contractor cost, a plan, a bill of materials, a project that persists |
| "I am going to build a humanoid robot. What are the steps and what should I buy?" | Dig, then project | Phases (frame, actuators, compute, power, control, safety), three budget tiers, a bill of materials with sources, what to buy first |
| "Turn my old PC parts into a home server." | Dig | What the parts are (read from the machine), what is missing, steps, a shopping list |
| "Plan our daughter's first birthday." | Look | A plan with a date, budget, checklist and reminders, assumptions stated |
| "Plan a trip to Portugal in May with the baby." | Dig | Itinerary options, baby logistics, costs, bookings within the limit |
| "Set up a vegetable garden for our climate." | Dig | Zone from the remembered location, calendar, bed plan, shopping list, reminders |

**Long and exhaustive**

| Request | Class | Good looks like |
|---|---|---|
| "Go search all the daycares around me and do a large comparative analysis." | Campaign | Registry first, a table with sources, an estimate up front, updates, stops by itself |
| "Find the best pastel de nata within 40 miles. Take your time." | Campaign | Every candidate, evidence of quality, a ranked list, what could not be verified |
| "Find pediatricians near me who take new patients on my plan." | Dig | Table with sources; calls it cannot make become exact steps |
| "List every pool builder in the county with their licenses and complaints." | Campaign | Registry check, complaint records, ranking |
| "Tell me when a used standing desk under 150 dollars shows up." | Monitor | A watch with a stated check rate, one message per hit |

**Ordering, accounts and money**

| Request | Class | Good looks like |
|---|---|---|
| "Order the cheapest USB-C cable that does 100 watts and is 2 meters." | Look | Compares, fills the cart, stops before payment, sends the exact order and card, buys only after a yes |
| "Reorder diapers like last time." | Look | Finds the last order in memory or mail, prepares the same order, asks once, buys after a yes |
| "Set me up an account on that site and tell me the login." | Look | Creates it, stores it, writes the sign-in skill, reports username and password |
| "Buy everything on the robot parts list under 30 dollars from one store." | Dig | Groups by store, fills the cart, one message with the basket and total, buys after a yes |
| "Just buy it, you pick." | Look | Picks, prepares the order, and still asks for the yes to pay: the one question level 3 always asks |
| "Tell me when my package ships." | Monitor | Reads the mailbox, one message per change |

**The computer and the network**

| Request | Class | Good looks like |
|---|---|---|
| "Connect to every device on my network, list them, and tell me when a new one appears." | Monitor | Inventory, a saved read-only monitor, alerts |
| "My PC is slow in the morning. Find out why and fix what you can." | Dig | Evidence first, safe fixes done, the rest explained |
| "Back up my documents every night and tell me if it fails." | Monitor | A scheduled job that checks the copy exists |
| "Clean up my downloads folder." | Look | Done at level 3 with a list of what moved; nothing unrecoverable deleted |
| "My Samsung TV will not take casts." | Dig | Investigation (built) and the chosen fix carried out |
| "Write a script that renames my photos by date, and run it." | Look | Writes, dry-runs, runs, offers to keep it |

**Memory and projects**

| Request | Class | Good looks like |
|---|---|---|
| "What did we decide about the pool filter?" (after two weeks of other topics) | Glance | The decision, the reason, the date |
| "Back to the robot. Where were we?" | Glance | State, next step, open questions |
| "What have I spent on the pool so far?" | Glance | A sum from the notebook with the list behind it |
| "What did I tell you about the daycare we visited?" (in a different chat) | Glance | The facts and the date, from any chat |
| "We moved to a new house." | Glance | The old address closed, the new one kept, local searches use it |
| "Every Monday at 8, tell me what is open across my projects." | Monitor | A schedule and a real summary |

**Life and admin**

| Request | Class | Good looks like |
|---|---|---|
| "Make a checklist for the first six months with a newborn and remind me at each stage." | Dig | Staged list from the remembered birth date, reminders set |
| "Find the dishwasher warranty and tell me when it ends." | Look | Finds the file, quotes the date |
| "Email my landlord about the leak." | Look | Writes and sends from its mailbox at level 3, reports what it sent |
| "Compare these two quotes over five years." | Look | A table with the arithmetic done by code |
| "Teach me electronics from zero over eight weeks for the robot, and quiz me." | Campaign | A course plan, lessons on a schedule, quizzes, progress kept |

**Code and work**

| Request | Class | Good looks like |
|---|---|---|
| "Build me a small app that tracks my expenses, and run it." | Dig | Builds, tests, runs, tells how to use it |
| "Fix the failing test in my repository." | Dig | Hands to a coding agent, reports what changed |

**Honesty**

| Request | Class | Good looks like |
|---|---|---|
| "Call my mom and tell her I am late." | Glance | Says it cannot call, does the nearest thing it can, as a finished answer |
| "What can you do, and what can't you?" | Glance | Reads its own ability list |
| "That did not work yesterday. Try another way." | Look | Continues the same goal with a different route |

## 6. Ways this can fail

- **The model is small.** The 9B plans and acts. The answer is structure in code (tables, scorecards, stop rules,
  verification, saved procedures) and a decision model for yes/no judgments, with every threshold measured.
- **Acting without asking can act wrongly.** Level 3 trades questions for stated assumptions; the receipts, the
  "(remembered: ...)" lines and the first-reply assumptions are how the owner catches a wrong turn quickly. Purchases and
  unrecoverable deletes always wait.
- **Money.** The assistant never pays on its own; the rule is in the purchase skill and enforced by the worker's gate.
- **The web fights back.** Engines throttle and sites block automation. The runner backs off and works with what it has;
  a blocked site gets another route.
- **Memory can be wrong.** A fact can be misheard. Every fact keeps its source and can be listed, corrected or forgotten;
  closed facts keep their history.
- **Credentials in chat.** Telegram is not local; the credentials message deletes itself and the console keeps the record.
- **Physical safety.** Pools, wiring and motors can hurt people; the assistant names what needs a licensed person.

## 7. The order

| Phase | What | Size | Depends on |
|---|---|---|---|
| P0 | Finish the current program: merge the open PRs, the 600-page run to the end, the done-check seeing the answer, the picked option carried out, release 0.1.8 | S | nothing |
| P1 | Autonomy at three levels and understanding instead of asking (M0), with how big (M2) | M | P0 |
| P2 | Memory that lasts and projects that remember (M1) | L | P0 |
| P3 | Campaigns and the survey job (M3) | L | P1 |
| P4 | Hands: measure, profile, accounts, money (M4) | L | P1 |
| P5 | Code that is kept, monitors, blocker reports (M5) | M | P1 |
| P6 | Companion mode (M6) | M | P2, P3, P4 |
| P7 | Staying power (M7) | M | any |
| P8 | The exam: sealed Verdict 4, a three-day soak with a restart in the middle, release | S | all |

Why this order: autonomy and size come first because they change every reply and make the rest feel right. Memory comes
next because everything after it attaches to it. Campaigns and hands can then run side by side. Each phase lands as reviewed
pull requests with the backend suite and Ruff passing, a live check on the real bot, and the same frozen cases before and
after. The sealed exam is run once, on the final build.

## 8. Settled with the owner (11/19)

- Remember projects, plans, jobs, and what the owner says, across chats; a setting that is on by default.
- Do not ask. Understand from the words and dictation; a clarify tool only when absolutely necessary.
- Three autonomy levels, not five; level 3 never asks.
- Long and exhaustive work runs as a loop that judges its result and keeps going; give an estimate and updates.
- Use computer use freely. Sign in and sign up itself; choose usernames and passwords, store them, write a skill for each
  site, and tell the owner. The owner's existing Gmail may be used through the session already signed in on this PC; create
  a new account if needed.
- Campaigns may keep the PC awake.
- Never buy anything or use the owner's cards without asking. Every purchase waits for a yes to the exact order, at every
  level; the rule is in the purchase skill and in the worker's gate. Everything else at level 3 is truly autonomous.

Defaults chosen here without asking, all in Settings: the credentials message deletes itself after 24 hours; local searches
use the remembered home location; messages from campaigns are quiet from 23:00 to 08:00 except a morning summary.
