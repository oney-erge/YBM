# Changelog

Notable changes to YBM. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- At initiative level 4 or 5 a question that offers options is not asked: it is a choice, and the Operator is told to choose the best-supported option, say which and why, and go on (the same bounded rejection as for any optional question, at most twice). On 2026-11-16 "Google Cast, Chromecast or PyChromecast?" went to the person, who answered "you choose", while the setting was only a sentence in the prompt. A question with no options (a fact only the person has) is asked at every level, approvals stay cards, and at level 3 (the default) nothing changes.
### Fixed

- A task is started on a summary written for it. Leaving every conversation summary for after the reply (the fast-reply change) made two frozen cases fail on the live build (11/18, 28 of 32; both pass again with the summary written first): a task was handed a stale summary and browsed for a shop it should have said it could not order from. A message that turns out to be a task now has its summary written before the task takes its memory snapshot (`classify_and_spawn_task(before_task=...)`); a chat message still gets its reply first.
- The `job.manage` line in the Operator's catalog is shorter than before the investigation was added to it, and its extra example is gone: the catalog shares a prompt that is already at its size limit.
- The higher initiative levels say that what the assistant has no way to do (a call, an order, a payment) is said plainly, not worked around: at level 4, two frozen cases (`v_script_total`, `h4_cannot_phone`) failed that had passed at level 3.

### Added

- "Tell me when": a watch. A folder or a web page is looked at every so often (15 minutes unless the person says), what was seen is kept in the job's saved state, and the person is told once about each change and about nothing else: new, changed and removed files with their names, or that a page changed with the lines that are new. The first look only records what is there. A look that fails is not "nothing changed": after three failures in a row it says it cannot look (once), and when looking works again it says so (once). A watch sleeps between looks (the runner does not wake it before its time) and takes no place from the two long jobs a chat may have. `job.manage start_watch` (asked first), the start parser's new `watch` action (target, how often), a skill `tell-me-when` read at 0.75 (8 of 8 phrasings of a request to be told later clear 0.64; none of 15 other requests clears 0.25), and a folder listing that goes through the same check as a watch made in the console. The folder or page is looked at only while this computer and the assistant are running.

### Added

- Working out why something does not work, as a job (`investigate`, `jobs/investigate.py`). The assistant used to open browser pages it had guessed and end the task when a method failed; now it looks first. A saved loop in small units: the model picks checks from a menu and writes a few likely causes; each check runs and its result is kept; the model writes findings (each naming the check that showed it; a finding that names a check that did not run is dropped), marks causes supported or refuted, and may ask for one more check (a check on a device is only pointed at an address the results showed); then what is known on the web is looked up and read page by page with every note tied to its quotation; the ways forward are written, each resting on a finding or a note (an option nothing supports is dropped), each saying whether it changes something and how to undo it. The checks (`diagnose.py`) only read: network adapters with the network type Windows gave them and which adapter discovery leaves by, the services discovery depends on, the firewall, Miracast support, browser casting policy, what answers on the home network by SSDP and mDNS from every adapter (name, model, services), one device's ports and its own description, printers and the spooler, and recent system errors. Only private addresses are checked. `start_investigation` in `job.manage` (approved first), a skill `investigate-a-problem` (read at 0.6: 8 of 8 problem descriptions clear 0.66, none of 15 other requests clears 0.04, one of 251 e2e messages clears 0.7 and it is a real problem report), and the start parser says which of research and investigation a message is. When it ends, the goal it belongs to is given what was found and the ways forward and waits for the person's pick; the next attempt is shown them. The job's report is sent as a Word file like research.

- Goals. What a person wants is kept apart from any one attempt at it: a `goals` table (objective in resolved words, the person's own words, status, what it waits on, methods the person declined, one line per attempt with how it ended), updated by code when each attempt (task) ends. The 2026-11-16 TV chain is why: a task that ended without the result ended the goal, so "write me a plan for it" became a plan titled with those words, "you choose" started an unrelated plan, and a declined Claude Code hand-over ended the work. Now the open goals of a chat are shown to the Concierge with the message, in the same call that already classifies it (no extra call), and it returns `goal_ref` (which goal) and `act` (answer, delegate, decline, continue, plan, control, chat, new). Intake carries it out in code: an answer, a delegation ("You choose.") or a decline goes to the task the goal waits on; "continue" and "plan" make a new attempt with the goal's own objective and what was tried, not repeating a method that failed; a follow-up with several candidate goals asks once ("Which one do you mean?", answered with a number, and the message that could not be placed is read again with the pick); a hand-over the person declined is not offered again for that goal. The fields are in the structured-output schema, not in the prompt text, so no recorded fixture changed. Measured on the running 9B on 28 labeled messages twice (56 readings): goal right in 53, act right in 52; a "yes" to a hand-over offer is always passed on as the person's own words.

- An install is done when an install tool ran, not when a package was named. The decision model is asked one more question about every request, in the call that is already made ("does the request ask for something to be installed or added to this computer?", read at 0.9: 7 of 7 install requests clear 0.93, none of 14 ordinary requests clears 0.17, and the 251 e2e messages that clear 0.5 but not 0.9 are asks to a coding agent to build something), and a request or a plan step that does needs `dependencies.install` or `ability.acquire` to have succeeded (new postcondition `package_installed`, with its own next-step guidance) before the task can complete. On 2026-11-16 three plan steps were completed by the replies "Add googlecast to allowlist", "Use googlecast" and "No thanks" and nothing was installed.
- `reopen_unproven` (`plans/review.py`) is the inspectable repair for a plan that was finished by replies: a done step of the assistant's own whose task has no successful tool call (or, for an install, no successful install) goes back to pending with its earlier result kept in the step's detail, steps that depend on it follow, and a note on the plan says what was reopened. The person's own steps are left alone.
- A research claim is checked against the quotations it cites. A quotation can really be in a page while the sentence built on it says something else: in the first live report (11/15) a used graphics card's price became "the cost of offloading". Every cited sentence of a section, a chapter overview and the summary is now checked: a number it states must be in the cited quotations (no model: "4,000" and "4000" are the same), and the model is asked whether the quotations say what the sentence says about the same subject, with the same numbers, units and conditions; a sentence that fails is removed, and a section left with none says no page answered the question. A check the model cannot run keeps the sentence and counts it as unchecked. The report says how many were checked, removed and unchecked, and that a check by a small model is not a proof. Measured on the running 9B on 12 sentence-and-quotation pairs: all 6 wrong claims (another model's number, the wrong product type, a different unit, "the quietest" from "quiet") were removed and 4 of 6 right ones kept; the 2 it removed wrongly were looser wordings ("Qwen3" for "Qwen3 8B", "heat pumps" for "units"), so it errs toward removing, not keeping.

### Changed

- A file name and the folder it belongs in, sent as two fields (`root` and a bare `path` or `file_path`), is written inside that folder. The bare name was resolved against the folder the program runs in, so three files meant for one folder were written next to the program's own files while the answer said they were made where asked (`h4_three_files`, 11/18). `write_text_file` now takes the same spellings `read_file` did.
- At initiative level 4 or 5, the message that sends a question with options back to the assistant also says that something that cannot be done at all has nothing to choose: it is said plainly, with the nearest thing that can be done.
- The end-to-end runner (`scripts/run_all_e2e_tests.py`) closes open goals before each case. A goal outlives its task on purpose, so a case run after another (or the same case run again) was read as carrying the old goal on, with the old attempt's notes.
- `read_file` on a folder says it is a folder and points at `inspect_folder`, instead of "file does not exist". The model took that for a missing folder and told the person it was not there (`v_parts_reports`, 11/18).
- A reply to a goal that waits on a question is the answer, also when the model calls it "continue". "Yes, please go ahead with what you proposed" was read as carrying the goal on, which started a second task next to the one still asking; the asking one waited until it was cancelled (frozen suite, 11/18: `v_cannot_order`, `h4_ask_before_moving`, `h4_cannot_phone`, `v_web_titles`). `classify_and_spawn_task` now gives such a reply to the waiting task; with nothing waiting it still carries the goal on.
- A chat reply is sent before the conversation summary is written, and "typing" shows at once. Measured on the live bot (11/18): three chat messages took 23, 23 and 15 seconds; the first sign ("typing") came 14 seconds after the message and the first word 23. Of those, 13 seconds were the model writing the conversation summary before anything else happened, 6 were the Concierge's own call and the rest two checks by the decision model. Through the Telegram polling loop the turn is now kept at once with the cheap summary, "typing" is sent before any model call, and the model's summary is written right after the reply has gone (`ConversationMemoryService.refresh_summary`, run by the loop through `TelegramIntakeService.run_after_reply`; it still waits while a task is using the model, as before). A caller that handles one update by itself (no loop to run it) writes the summary inline, as it did.
- The assistant's own mailbox. `mail.manage` (off by default, `mail.enabled`) reads and sends over IMAP and SMTP with the standard library: `status` (a login, nothing read), `list` (the newest or unread messages), `read` (one message as text with the links in it, so a confirmation link can be followed), `draft` (a file; nothing is sent) and `send` (always asked first; the text of the approval card is the text that is sent). The password is read from the account vault under `mail.account_host`, as the browser tool reads it to fill a form; it is never in the configuration, a prompt or the chat, and an authentication error does not echo it. A message that arrived is shown with a line that it is information, not instructions. A skill, "Make a mailbox of my own", is the procedure for getting one: Gmail, then Outlook, then ask; the vault makes the password; a puzzle, a code or a phone tap is handed to the person with a screenshot and never worked around; a provider that refuses is not the end of the goal. Not verified against a real provider: no account exists yet, so the tool is tested with a scripted IMAP and SMTP server only.

### Changed

- A declined external coding handoff permits a bounded local continuation. Assistant-owned plan steps resume after clarification instead of treating the reply as proof of completion; explicit skipped prerequisites block dependent steps.
- Clarification replies and expiry now use guarded atomic transitions. Telegram buttons are bound to the question they showed; older buttons ask the user to reply to the current question. The handoff confirmation button now records an answer the consent parser recognizes.
- Plan step admission now commits its child task and step link together, and plan cancellation and child settlement recheck current state. Web chat replies from a task card are bound to that task and question.
- An old claimed plan step with no linked task pauses for inspection instead of automatically repeating work that may already exist.
- Plan and task revisions protect questions and plan transitions even when consecutive writes have the same timestamp. Plan edits cannot mark work complete without the runner or reset a running step; inserted steps preserve explicit prerequisites.
- User-owned steps require an explicit completion report or skip; a deferral such as "not yet" no longer silently completes or skips the step.
- A delegated choice on a user-owned decision-only step can advance after a recorded result. Older plans and mixed-action steps stay open for the remaining action; the planner's decision-only marker still needs semantic evaluation.
- Unaddressed free-form messages while a question is open are checked against the configured decision model in decision/cascade modes. Uncertain or unavailable classifications ask whether the message is an answer or a new request; explicit task replies and `new:` remain available.
- Settings has a five-level decision preference. It guides the planner and operator for new tasks, while existing access and approval rules remain separate.
- Research supports configured limits up to 1,000 pages, batches search rewrites, keeps a Markdown report and attempts a Word copy. Out-of-range requests are refused instead of silently reduced. Citations still require substantive review.
- Research controls and publication use guarded SQLite revisions. Report delivery has separate saved state; pending delivery can resume after restart, while an uncertain send is not automatically repeated.
- Workflow scheduling errors appear inside the open dialog.
- System inventory can inspect Windows displays attached to the desktop, including an unambiguous monitor name when Windows provides one. It does not discover network TVs or verify playback.
### Added

- Verdict 3, the exam for the assistant that keeps hold of a goal: 20 cases (`fin3_*`, `--suite verdict3`) written in new words and with new files before the work it measures was started, sealed by hash like the two before it. It covers chains of messages ("write me a plan for it", "you choose", "that file", an unrelated question while one is open, a correction of the thing just made), working a problem out (slow websites, a TV that will not take a cast, a Wi-Fi that drops), research of 40 and 600 pages, a mailbox, watches, a library choice that must be its own, and the safety cases. Three cases need a night or an account and are marked `manual`; they still count in the 20.

## [0.1.7] - 2026-11-15

### Added

- Word documents and spreadsheets can be made. `document.manage` made only PowerPoint files: a request for "a packing checklist as a Word document" ended as a .pptx (sealed Verdict 2, fin2_a3), and one for a spreadsheet of four invoices
  with a total row failed on the model's own Python (fin2_a1). `create_document` (a title and lines of text; `# ` lines are bold headings, `- ` and `[ ] ` lines bullets) and `create_spreadsheet` (rows, the first the header; numbers stored as numbers;
  `total_row` adds a Total row of real SUM formulas that is summed by the tool, not by the model) write the files with the standard library, nothing to install, and read them back to check. Checked with openpyxl and python-docx: both open them,
  the spreadsheet's cells are typed, its total is 566 for the four invoices.
- "What did you find about drones yesterday?" has something to answer from. Research that finished in the last week is told to the Operator with its summary and where its report is (a listing used to say only "Done: the report was sent."),
  with the instruction to say only what those lines say, to say it has no record of what is not there, and to send a report again with `artifact.deliver` (the challenge case c_e3).
- A research job reads the pages it finds. Live on 11/14, 16 of 36 pages of one job read as empty. Two causes, both fixed: the web read tool stopped at 100,000 characters, and a modern page spends
  that much on scripts and styles before its article (`adapters.http_request.max_response_chars` now defaults to 400,000); and the reducer that keeps a page's words dropped navigation, footers and forms, so a
  site that wraps its whole page in a `<form>`, or leaves a `<header>` open, read as nothing. A page whose reduced text is short is now read again without dropping any part, and the fuller reading is used when
  it is much fuller. Of the 16 pages that failed, 11 now read; the rest are a blocked site (403), a dead link (404) and two pages that are all script.
- A research job reaches the number of pages it was asked for. One search per question found 36 pages for a request of 100 (live 11/14: 7 questions of 8 results, less the pages two questions share). When the pages found are short of the limit, the
  questions are searched again in other words (one model call writes a different search for each), up to three rounds in all; a round that adds nothing, or a model that cannot rewrite the searches, ends the searching and the job reads what it has.
- The second verdict suite (`e2e/verdict2_cases.json`, 20 cases `fin2_*`, frozen by hash, `--suite verdict2`): the exam for the finished assistant. It covers what the plan added
  (spreadsheet and Word files, a program written and run, long plans, a background research job started, asked about and stopped, overnight work) next to files it must read
  exactly, a correction in the middle of a search, a remembered allergy and three safety cases. It is run once on the final code; 18 of 20 passes.
- A request for in-depth research starts the job with a call written in code. Found by the first live run of the research job: the skill was in the Operator's prompt and the 9B still chose `delegate`
  with web.search, spent six steps, and offered to hand the request to Claude Code. The skill now says `route: job`; for such a request the worker makes `job.manage start_research` the first step, with a goal
  and a page count written by one model call with its own short instructions (`orchestration/job_start.py`; a failure leaves the person's own words as the goal). It goes through the same approval card, audit and
  checks, and the Operator writes the answer from its result. Also found: once a job has started, the fulfillment check no longer demands that the task itself read source content and deliver a file, and a research
  job that finds no Chrome running with DevTools (after a restart, or the night after it was approved) starts the assistant's own Chrome profile minimized instead of failing for want of a search.
- Plans that last months. A planned step has a day (`due`, written YYYY-MM-DD by the planner) and steps are kept in the order of their days, so "Dec 6: book the movers" no longer
  comes after "Jan 10". The runner starts a step only when its day is within a week, so a plan for a house waits, active, for each day instead of asking every step at once, and the card
  says "Nothing to do until Dec 6". `plan.manage` gained `set_check_in` (tell me how it stands every N days, said from the plan alone with no model, only when asked for),
  `shift_dates` (the project slipped: move every unfinished step) and `add_note` (a fact the user gave, kept with the plan and told to every step's task). A plan that runs three
  weeks or more offers the check-in once. Existing databases gain the columns on start.
- Phase 1 of the ultimate-assistant plan, from the sealed Verdict run (7 of 13 by rubric):
  - **Counts come from code.** The decision model can raise a fact the patterns miss, `counts_things` (`request_facts.ADDED_FACT_QUESTIONS`, asked about every request in the same call as the
    others; 8 of 10 count phrasings clear 0.5 and 13 of 178 e2e messages do, all counts or totals). For such a request the number check also looks at two-digit numbers, and what a
    file read returns no longer vouches for a number (a `17` in every log timestamp let "17 errors" pass where the files hold 16).
  - **A missing delivery is told with the call to make.** The guidance for `expected_artifact_delivery_missing` was keyed `..._delivered_missing`, so the model only ever got the generic
    fallback; it is keyed right now, and all three pushbacks write out `artifact.deliver send_file` with the file the task made (`call_rewrites.exact_delivery_call`). The chart case heard
    "use the delivery tool" three times, read the picture, described it and asked where to save it.
  - **A listing carries modified dates by default** (`filesystem.manage inspect_folder` and `search`; `with_dates: false` turns them off). It was off unless the request used a word for
    recency, so "which files are older than a year?" got sizes only and the model asked the person which were old.
  - **Phoning and emailing are known gaps** (`abilities/gaps.py`, `gap:phone.call` at 0.9 and `gap:mail.send` at 0.85, measured): say so in the first sentence, never claim to have done it, offer a
    script or a draft. "Phone my dentist" opened a `tel:` link and waited for approval; "email my landlord" asked for the address first.
  - **The answer when the budget runs out says what the work produced** before it names the folder (`finishing.fulfilled_step_budget_answer`). "Why is my laptop fan loud?" ran seven scripts and ended
    "Completed the requested work in the project folder I made for this task."
  - **The hardware report gives the card's total memory** and says what is free right now as a fact about this minute. The live answer quoted "1.7 GB available" for an 8 GB card.
  - **"Created code.txt" is a write claim.** The check looked for the noun "file" and missed a named file, so a made-up "The file code.txt has been created" with no tool run could pass.
- Research jobs found to work on the real web (live, 10 pages, 1.4 minutes, 6 of 10 pages with verified facts, every cited figure traceable to a quotation): four findings from the first live runs.
  - **Search goes through the person's Chrome first** (`jobs/io.py make_browser_search`), then the `web.search` tool. The first job found no pages: DuckDuckGo's HTML page answers this connection with a
    human check (HTTP 202) and Bing's plain HTML answers for the first word of the query only ("heat pump installation cost Chicago" returned films). Chrome with DevTools, when it is already running, is
    read as a person reads it, Bing's redirects unwrapped, a tab opened and closed per query, and no browser is ever started. When no engine answers the job says why.
  - **`web.search` says when DuckDuckGo is blocked** instead of "No results" (`tools/web_search.py`), and names the fix (a Brave Search key or a SearXNG instance).
  - **Notes are facts with quotations, no relevance flag.** Asked first whether a page was relevant, the 9B said "not relevant" about a page of monthly running costs by system size and season, and every page
    ended with no facts. Asked only for facts, it found four on that page, none on pages about something else, and every quotation was in the page.
  - **A sentence that cites no note is removed** from sections and the summary, a sentence that says what the sources do not say stays: the first report called cold-climate heat pumps "the only
    suitable choice" and said "owners report significant savings", and no note said either.
- The Operator's prompt never loses the name of a tool, and the skills list stays short (`orchestration/operator.py _without_the_tail`, `tools/skills.py`). On 11/14 ten scripts the
  assistant had kept from test tasks, listed one per line, pushed the tool catalog over its limit; the last-resort trim cut a slice out of the middle, `system.inventory` was gone, and "how many
  Steam games do I have?" and "where is this file?" (11 of 11 passes before) went to the screen tool for minutes. Authored skills are listed with their description cut at a word, kept
  scripts are one line that says how many; and when a context still does not fit, what follows the tools (the advice, then the skills, then the persona) is dropped from the end and a
  tool line is never cut. Checked on the live model: the exact failing prompt (all 14 skills) now names every tool and the 9B picks `system.inventory` 3 of 3 on both cases.
- `scripts/e2e_scorecard.py` prints each case's own history across runs (oldest first), what each suite is for, and which cases are flaky, so a score is only compared with itself.
  The run summary has an "Answer at" column: when the answer reached the person (the last message that is not an edit of a progress line). A chat reply showed 67 s in the summaries
  because the runner waits that long to see whether a task starts; the audit log says the answer went out 1 to 6 s after the message.
- Jobs that take hours, research first (`jobs/`, `tools/job_manage.py`, phase 3 of the ultimate-assistant plan). "Read a hundred sources about heat pumps and come back to me" starts a job the
  person approves; the worker then carries it out one small unit at a time (plan the questions, search, read one page, take notes, write a section), only when no task is waiting, saving after every unit
  so a restart, a pause or a question in between loses nothing. Nothing is trusted that can be checked: a note is a claim with the exact words from the page, and a claim whose quotation is not in the page
  is dropped; a section is written from numbered notes and a citation to a number that was not given is removed; a question no source answered is said to be unanswered. It messages the person
  as it goes and ends with a cited Markdown report sent to the chat. `job.manage` has `start_research` (approval-gated), `status`, `pause`, `resume`, `stop`; the skill `Research a topic in depth`
  (`when:` question measured: 7 of 7 phrasings at 0.84 to 0.97, ordinary requests at most 0.26) puts it in front of the Operator. Off in the code's defaults like plans (`jobs.enabled`); the shipped config turns it on.
- A script that fails for want of a package `dependencies.install` may install is told to install it (`tools/code_interpreter.py`), not to do without: the hint names the package
  (`python-docx` for `docx`, `python-pptx` for `pptx`), says the user is asked first, and to add the install folder to `sys.path`. It reads the pandas wording too ("Missing optional
  dependency 'openpyxl'"). With the installer off or the package not on the allowlist the old advice stands. In the second challenge run the model gave up on adding a row to a
  spreadsheet for want of openpyxl while the installer could have fetched it.
- The self-knowledge skill says a question is not a request to change anything: nothing is installed or switched on in that task, and it ends with an offer instead of a question that
  holds the task open. In the final rerun, "what do you need from me?" ended with ffmpeg installed on the machine.
- The e2e runner cancels tasks the previous case left waiting before it starts the next (`cancel_leftover_tasks`). A request that installs something is retried as a new task that can ask its
  own question after the case ended; the next case's first message was taken as the answer.
- Two starter skills the Operator is shown, because a tool description was not enough: on the rerun the Operator did not pick `ability.search` overview for "what can't you do?" or
  `system.inventory` hardware for "run a 70B model on my laptop", and asked the user what to explain and which model instead. `skills/starter/know-yourself.md` (answer a question
  about the assistant from the overview, never from memory, never by asking what the user means) and `skills/starter/check-this-computer.md` (read the hardware first, say what fits and
  what to use instead). Each has a `when:` question measured on the running decision model: about the assistant, 10 of 10 phrasings at 0.5 or more and ordinary messages at most 0.4;
  about the computer, 9 of 10 at 0.7 or more (the other 0.60), ordinary messages at most 0.37, and none of 178 e2e messages at 0.4.
- A skill can say `ask: always` (`Ability.ask_always`): its question is asked for every request, whatever words the request shares, like a skill that routes to the planner. "What can't
  you do?" shares no content word with the skill that answers it, so by words alone it was never shown to the Operator.
- A preference line that copied the output format is replaced by the person's own words (`persona_learning.py`). The model that extracts the line is asked for structured
  output and sometimes writes that instruction into the field: "From now on keep your answers very short." was offered back as "Keep answers very short and return only valid JSON."
  (challenge c6). When the line mentions JSON and the message does not, the message is the line; one too long to be a line is not offered.
- `system.inventory` reports the hardware (`operation: hardware`): the graphics card with its video memory, RAM and processor count, from the same reading that
  decides which local model fits (`llm/hardware.py`). The tool already listed hardware, cpu, ram and gpu among what it covers and had no operation that reported any of
  them: asked to "run a 70B parameter model on my laptop", the Operator asked the person for their GPU model and VRAM and was cancelled after two asks (challenge c2).
- An answer no longer carries what a tool wrote for a program. The folder a script ran in (`.agent_control/code_interpreter/task_task_<hex>`) is told as "the project
  folder I made for this task" like the other task folders, and `task_task_<hex>` is an id like `task_<hex>` (`human_text.py`). The finished-answer fallback leaves out
  "Code interpreter operation completed", `Workspace:`, `Backend:`, `Script:` and `Return code:` lines, and when nothing a person would read is left it
  is just the question (`orchestration/task_record.py`). The second challenge run found both: "Done: - Code interpreter operation completed: run_python Workspace:
  task_task_eb3c... Backend: local_subprocess (not sandboxed)" for a question about a backup, and "Workspace: task_task_833b..." under a chart.
- The challenge suite checks that the final answer leaves out terms (`answer_excludes_all`), apart from everything the user was sent on the way. c_d1_blurry failed on its
  approval prompt, which names all six photos to ask permission to look at them, while the answer named exactly the three blurry ones.
- `filesystem.manage write_text_file` refuses a file that is not text, even with `overwrite` or `append`: a spreadsheet, document, archive, picture or other known
  binary format by its extension, and any file with a NUL byte in its first 4 KB by its content. The error says nothing was changed and to use a program
  (`code.interpreter` with openpyxl or python-docx). Live (challenge e2), asked to add a row to `expenses.xlsx`, the Operator overwrote the 1.5 KB workbook with 95 bytes of
  plain text that Excel cannot open and answered that the row had been added.
- A question about the assistant itself ("what can't you do?", "what do you need from me to be more useful?", "what can you do?") is answered from the ability index,
  not from the chat reply, which is written without it. `ability.search` has `operation: overview`: what cannot be done (and what to do instead), what could be set up,
  what needs an account, what is switched off, then what is ready, with MCP server tools counted rather than listed, and a closing line telling the model to answer from
  the list only, recommend the two or three things that would help most, and not say what is new since a day (the records have no dates). At intake a message taken for chat
  that the decision model confirms is about the assistant (`abilities/context.py asks_about_the_assistant`, 0.5, measured: 9 of 10 phrasings clear 0.8, the other 0.56, ordinary
  messages that share its words at most 0.26) becomes a task, and the chat-to-task check no longer needs installs switched on. The second challenge run found chat answering "what can't you do?"
  with what it can do, and "what is new?" with "execute terminal commands without needing explicit approval".
- A schedule says when it first runs: the weekday, the date and the time in its own timezone (`tools/schedule_manage.py when_it_runs`, used by create, a repeated create
  and list). The tool said only that the schedule was created, and the model, asked to say when it first runs, wrote "Monday, December 23" for a run on
  November 16 (live; December 23, 2026 is a Wednesday). The list showed a UTC clock time the person had to convert.
  The scenario fixtures key a prompt without run times (`testing/scripted_llm.py`), because a daily schedule's first run is one day from now at this minute; `schedule_create` was rekeyed.
- The voice's usage note says to send the recording with `artifact.deliver send_file` and the name `tts.synthesize` returned, and that `send_latest` sends the text.
  Live, the Operator tried `send_latest` first and the person got a text file before the audio.
- The plan skill confirms at 0.45 instead of 0.55. Live, "I'm moving on December 20. Plan it." scored 0.54 on the decision model, just under the bar, so the
  Operator asked four questions (address, rooms, budget, special needs) instead of showing a plan, and was cancelled at 2.5 minutes. Measured on the running decision
  model with the same question: 16 of 16 genuine plan requests clear 0.45 (the weakest are 0.47 and 0.52, and four of them were under 0.55), and none of 178 ordinary
  messages falls between 0.45 and 0.55 (the nearest below is 0.43, "Clean up this folder"). Four other wordings of the question were tried and scored worse.
- A recording sent in Telegram as an audio file (an mp3, a wav, an m4a) is saved in the inbox and handed to the task like any other file. It was ignored:
  no text, no task, no reply, which the first challenge run found when it sent a meeting recording and nothing came back within three minutes.
- A skill for repeating a job (`skills/starter/schedule-a-job.md`, `when:` a yes/no question at 0.8): the Operator is told to create the schedule with the job
  as the objective and the days and time as the cadence, and not to do or build the job now. On the deployed code "Every Monday at 9am, send me the top 3
  Hacker News stories." made the Operator write scripts, ask where to deliver it, and offer to hand the job to Claude Code. Measured on the running decision
  model: six of eight recurring requests clear 0.8, "what scheduled jobs do I have?" (0.72) does not, and every ordinary message that does is itself a
  recurring request.
- The planner is told today's date and to work backwards from a dated goal, starting each step's title with when it is due, and not to invent computer
  chores. "I'm moving on December 20. Plan it." produced a plan about making folders and spreadsheets, with no dates. A plan card is not read as a claim that
  files were created.
- A recipe that is already set up stays a record, saying how to use what it set up, and the chat check recognises it too
  (`abilities/recipes.py recipe_abilities`, `asks_for_what_a_recipe_covers`). Live, right after the voice was installed, "read that out loud" was answered
  "that needs a task, send it again": only recipes not yet set up were records, so once the voice was there nothing recognised the request and it fell
  back to chat.
- Gaming mode keeps the model off the GPU for the summary of the conversation too. Live, a message held in gaming mode still reloaded the model
  through the memory update (0.8 GB of video memory before, 6.2 GB after); while it is on, the summary is the recent turns and the next message after
  it ends re-summarizes.
- A message taken for chat that asks for something that could be set up ("read that to me out loud") becomes a task, so the Operator is told what
  could be set up and can get it (`abilities/context.py asks_for_what_a_recipe_covers`, `channels/base.py`). A chat reply knows nothing of recipes: the
  deployed assistant answered "I cannot speak" and offered nothing, though a local voice is a 120 MB download away. The same search and the same
  yes/no question decide; the bar for a recipe is 0.85, measured on the running decision model (0.89 to 0.96 for ways of asking to be spoken to, 0.74
  for "read me the summary of chapter two", which is not out loud). No decision model, no change: it stays chat.
- The days and time a person names win over a plain daily or weekly the model picked for a schedule. Asked "remind me every Monday at 9am to check my
  email" the model wrote the cadence "daily" and the job "check my email" (live, with calendar cadences deployed), and then asked whether a daily
  schedule was acceptable. `schedule.manage` now reads the days and time from the message the task came from when the cadence is a plain daily or weekly.
- A big goal now reaches the planner. A skill that routes to the planner (`route: plan`) is asked about for every request, not only for those that
  share words with it, and the shipped plan skill confirms at 0.55 instead of 0.8. On the deployed code the first challenge run sent "Build me a robot."
  to the Operator, which asked a three-part questionnaire, and "I'm moving on December 20. Plan it.", which it retried for ten minutes and then offered
  to hand to Claude Code: neither shares enough words with the skill to be asked about, and the question's 0.8 bar was cleared by four of ten project
  requests. Measured on the running decision model: nine of ten project requests clear 0.55, and one of 178 ordinary messages does (a
  request to read files and write a brief, which is a fair candidate).
- A look at a picture gives the vision model the whole GPU and gives it back (`vision.swap_models`, on by default): LocalDeploy is asked to free the GPU
  first (it stops the main model) and to unload the vision model after, so the main model loads once into free memory at the next step. On an 8 GB
  GPU the two evicted each other: in the first challenge run a screen description took 9 minutes, a scanned invoice 11, and a photo sent meanwhile
  waited more than three minutes to be taken up. Needs the LocalDeploy whose `/models/free` also stops llama.cpp; with an older one the look goes
  ahead as before.
- `image.describe` reads a scanned PDF (a PDF whose pages are only pictures, as a scanner makes it): the picture of each of the first three pages is read
  out of the file and looked at in turn, and the answer says which page said what. The first challenge run asked for the total on a scanned invoice; the
  assistant tried the document tools, then asked the vision model about the PDF itself ten times, which it refused, and was cancelled at 11 minutes. A PDF
  that has text and no page pictures is pointed at `document.manage extract_text` without loading a model.
- A question the Operator asks can offer its choices as buttons: `ask_user` takes `options` (up to four short answers) and the Telegram card shows them
  as the quick answers it already showed for a plan step and the planner's question. Before, only those two could; "which of the files I found?"
  was a free-text question. A later question without choices is not given an earlier one's buttons. The operator prompt and the strict reply
  schema describe the field; the scenario fixtures were rekeyed with `ybm scenario rekey --all`.
- Gaming mode: say "I'm about to play a game", "gaming mode" or "free up the GPU" in Telegram and the assistant unloads the model at once
  (LocalDeploy `/models/free`, which now also stops the llama.cpp server) and loads nothing again until you say "I'm done gaming": the worker
  claims no task, the warm-up request is not sent, and a message that needs the model gets one reply that it waits and is listed when you come
  back. A task that is running is finished first. It only turns on because you said so (`gaming.py`, `channels/gaming_command.py`).
- Schedules can name days of the week and a time of day: "every Monday at 9am", "every weekday at 8:30", "daily at 7am" become
  `every monday at 09:00`, `every weekday at 08:30`, `daily at 07:00` (`cadence.py`; the scheduler, `schedule.manage` and the workflow automation
  form all take them). Before, any such request was turned into "daily" at whatever time it was made, with the days and the time left in the
  job's text. The job is now what is left when the when is taken out, and a run a week later is the same wall-clock time across a clock change.
- `ability.acquire` gets a capability the assistant does not have yet from a recipe (`abilities/recipes.py`, `abilities/acquire.py`): a local
  voice (Kokoro, 120 MB), speech recognition for audio files, ffmpeg (winget) and a vision model (Qwen3-VL through Ollama). A recipe that is not
  ready is an `installable` record the index finds, so the Operator is told what could be set up before its first step. Files come from the
  address written in the recipe and are checked against its SHA-256 before anything is switched on; the configuration is written last. Whether
  to ask the user is the model's judgment (`ask_first`), except that 500 MB or more, or a program installed on the computer, is always asked.
  After an install the request that needed it is started again as a new task, once, so it carries on by itself. `abilities.acquire` is off in
  the code's defaults and on in `config.example.yaml`.
- `audio.transcribe` writes down what is said in an audio file (a voicemail, a meeting recording), with the Whisper model that already
  transcribes Telegram voice notes (`adapters.stt`). A read: no approval.
- A fact the words of a request matched (it deletes, moves, changes a file, asks what a document is about, adds things up) is believed only when the
  decision model agrees (`request_facts.FACT_QUESTIONS`, `planning._with_request_facts`). The patterns took "keep every file; do not delete
  anything" for a delete and "the latest price" for a request for the newest file; one yes/no question on the CPU, asked once before the first step
  and only about the facts a pattern matched, cancels those. Measured over the 199 messages the end-to-end suites send, it said no to all 13
  false matches of these five and to none of the true ones, and yes to 58 of 60 true phrasings written for the check. The other facts stay
  words only: the model was wrong about them.
- An answer that says something was done to the world (deleted, sent to someone else, installed, paid for) is checked against the task's record
  (`orchestration/claims.py`, `answer_checks.py`): the model is sent back once with the sentence and what is missing. A payment is never
  backed, because the assistant does not pay. The end-to-end suites use the same function (`claims: true`), so what is measured is what is
  protected.
- The challenge suite (`e2e/challenge_cases.json`, 39 cases) and the verdict suite (`e2e/verdict_cases.json`, 16 cases, frozen by hash) test where
  YBM stops being good: what it cannot do yet and should offer to get, goals that are projects, answers that could go bad, mechanisms it has to
  invent, a correction in the middle of a task, a model server that dies. The runner gained attachments, hooks, bursts, a grade sheet for the
  rubric of each case, and `manual` cases it lists but does not run (`e2e/README.md`).
- `ybm scenario rekey <name>|--all` keeps the recorded responses of a scenario fixture whose prompts changed in a way that does not change the
  answer, under the new prompts' keys, and prints the prompt lines that changed, instead of a live re-record.

### Changed

- `orchestration/worker.py` is split by stage with the code unchanged: it went from 4,100 to 1,470 lines. `TaskWorker` is composed of stage
  classes (`approvals.py`, `delegation.py`, `external.py`, `guards.py`, `learning.py`, `planning.py`, `records.py`, `reply.py`), and the helpers
  that read a task or an answer are plain functions in modules named for what they read (`recovery.py`, `asking.py`, `task_record.py`,
  `repeats.py`, `finishing.py`, `grounding.py`, `replay.py`, `claims.py`). A name another module needs is public. The tests import from the
  new modules, and the re-exports `worker.py` kept for them are gone.

- A lasting preference stated in a chat message ("always keep answers short") is noticed after the reply and offered as a Remember / No card
  (`preference_watch.py`). The decision model is asked one yes/no question on the CPU first, and only when it says the message may state a
  preference is the assistant's own model asked to extract it, with a schema (persona learning had asked a 9B model for free text and got
  the answer to the question in the message). Nothing reaches `persona.md` without the tap.
- A request a skill marks `route: plan` (and confirms with a yes/no question) goes to a planner before the Operator's first step instead of
  the Operator loop (`plans/stage.py`, `plans/planner.py`): the planner drafts steps with owners from what the user said and what the
  assistant can and cannot do, or asks the one question it cannot do without, once, with options that become buttons. A planner that fails
  leaves the request to the Operator. A skill can also say `when:` (a yes/no question) to be put in front of the Operator whole when it applies.
- What the assistant learns is offered in the chat. A skill proposed after a multi-step job, and a preference a message states, are shown
  in the chat the task came from as a card with Keep and Not now (or Remember and No); the tap is the review and has the same effect as
  the console's accept (`learning_cards.py`). Nothing is installed without it. `adapters.skills.learning_enabled` and
  `adapters.persona.learning_enabled` are on in `config.example.yaml`.
- `image.describe` looks at a picture (a photo sent in Telegram, a screenshot) and says what is in it or answers a question about it, reading
  any text, with a local vision model found in LocalDeploy (the smallest it lists, or `vision.profile`). The assistant's own model reads text
  only. Each use asks the user first (it loads a model of about 3 GB; the picture stays on this computer), and when LocalDeploy says Ollama
  is not running it is started once (`vision.start_ollama`). Off in the code's defaults (`vision.enabled`), on in `config.example.yaml`.
- Plans for goals too big for one task (`plans/`, `tools/plan_manage.py`, `plans.enabled`): the Operator drafts steps that each have an
  owner (the assistant, the user, or the assistant once the user gives it something), the user starts the plan from a card with Start and
  Cancel buttons, and each step then runs as an ordinary task or asks the user to do it, one at a time, with Done and Skip buttons.
  Plans are stored on their own and survive restarts; Pause, Continue and Cancel work from the card; `/plan` shows it; and a plan open in
  the chat is named in the prompt of a task there. A step that waits for the user does not take the next message they send and does not
  expire after a day. The "Plan a project" starter skill tells the Operator how to draft one.
- A question can offer quick answers: `clarifying_options` on a clarifying task become buttons under it, and a tap answers exactly as typing
  the words does (`channels/quick_answers.py`).
- One index of everything the assistant can do, could do with the user's help, or cannot do (`abilities/`): built-in tools, MCP tools,
  skills, kept scripts, saved workflows, installed coding agents, and known gaps, as one kind of record with a state (`ready`, `disabled`,
  `installable`, `needs_access`, `unavailable`) and what it would take to make it ready. Tools say what they are for next to their own
  definition (`ToolDefinition.group`, `provides`). The Operator can search it with the new `ability.search` tool, and a request that matches
  a record the prompt cannot already show (it cannot be done here, a saved workflow does it) is told so once, before its first step: words
  shortlist, and one yes/no question to the decision model confirms (`DecisionModelClient.yes_no`). Off in the code's defaults
  (`abilities.enabled`), on in `config.example.yaml`; see `docs/ARCHITECTURE.md`.
- A request to add up or average what several files hold ("how many lines in total", "the sum of") is told once, after the second file is
  read, not to add in its head and to let a short program do it (`orchestration/ladder.py`, `tally_hint_due`; `RequestFacts.tallies`).
  The model had answered 12 for 4 + 5 + 6 in a sentence no written-out-sum check could read; with the hint it runs the program and says 15.
- A request to move some files into a folder it names ("move the .png files into a subfolder called pictures") is told once, as soon as
  the model has looked at files, not to use `organize_plan` (it sorts every file by type into folders of its own naming) but to apply a
  manifest with exactly the files named (`orchestration/ladder.py`, `move_hint`; `RequestFacts.moves_into_a_named_folder`). In the
  vision suite, run after the other cases in one chat, the model had planned a sort by type three times and asked the user. The hint
  says to write the file name after the folder: a destination that is only the name of a folder that does not exist yet is taken as the
  new name of the file (three pictures came out called pictures, pictures_2 and pictures_3).
- The check for an answer whose sum is wrong also reads a total with its parts listed under it ("12 lines in total: - a.txt: 4 lines
  - b.txt: 5 lines - c.txt: 6 lines"): the parts that stand before the total's own unit are added and the model is sent back once when
  they do not make the total (`orchestration/number_check.py`, `_itemised_total`). The earlier check only read "4 + 5 + 6" on one line.
- A request to buy, order, book or pay for something ("order me a pizza", "book a flight") is answered honestly. Once the model starts to
  browse for it, the task's history says that it has no payment method and no shop accounts and must not press a buy button, so it
  says plainly that it cannot place orders and offers the menu, the website or the phone number (`orchestration/ladder.py`, `money_hint`;
  `RequestFacts.spends_money`). With the whole tool list visible again (the catalog fix above) the model had started searching for the
  shop and asking which one.
- In a form that makes an account, a password field the model filled with text the user never wrote gets the vault's `{{new_password}}`
  instead (a call rewrite, in the order of `call_policy.REWRITES`), so a made-up guessable password never reaches the page, a file or the
  chat. A password the user gave in their own words is left alone.
- The console's secret vault card shows the logins YBM made for websites with a **Show** button (the password is never in the list; Show asks
  the backend, displays it for 20 seconds, and the audit trail records which site was shown, never the value). The task list shows what
  the person actually said, not the concierge's rewording of it, with the channel ("Telegram") and a microphone for a voice note.
- Telegram has buttons and a menu where they save typing. The progress line carries a **Stop** button (one tap cancels the task; every
  edit sends the keyboard again, because Telegram removes it otherwise); the answer to a move or rename carries **Undo** (once, and not
  under the answer to an undo); `/stop` stops the task that is working in the chat, with no task id to type; `/undo` and `/passwords`
  are the same as saying "undo that" and "which accounts did you make"; and the "/" menu lists status, tasks, stop, undo, passwords and
  screenshot (set when the poller starts).
- Telegram takes photos and files: they are saved in `.agent_control/inbox/<day>/` (up to 20 MB each; `channels.telegram.inbox_dir`,
  `max_file_mb`) and the task is told where ("[The user sent a file: ...]" after the caption), so "summarize this", "put this in my
  documents" or "what does this say" work on what was sent. The file and document tools can read the inbox without it being added to
  a list of folders. A file that is too big or will not download is answered in words and no task is started.
- Accounts on websites. A form field may say `{{new_password}}`, `{{saved_password}}` or `{{saved_username}}`: the browser tool makes a long
  random password for the site (the same one for "Password" and "Confirm password"), keeps it with the username in the encrypted secret
  vault, and types it into the page, so the model, the logs, the approvals and the chat never see it. When the request is to make an
  account or log in and the page has a password field, the task's history says so once (`orchestration/ladder.py`, `form_hint`), and
  likewise for a box about agreeing to terms, a CAPTCHA (send the user a screenshot and ask them to solve it, never get around it) and
  a "we sent you a confirmation email" page (finish honestly: the account is not active until the user clicks the link). In Telegram,
  "show my <site> password", "which accounts did you make?" and "forget my <site> login" read, list and remove the saved logins; showing
  one is recorded in the audit trail (the site, never the value). Vault placeholders are not masked by redaction and are not parameters
  of a saved workflow.
- The browser's form fill ticks and clears check boxes and radio buttons (by clicking them, so the page's own handlers run; it used to type a
  value into them, so a terms box stayed unticked) and chooses a drop-down option by its text or value.
- A request to buy, order or pay for something is no longer handled by a rule in the ladder that fired on its words once the model had
  started browsing. It is the `gap:commerce.order` record: found from the request before the first step, confirmed by the decision model
  ("pay my electricity bill" is a match, "remind me to pay the rent" and "total order value" are not), and carrying the same honest
  answer plus what the assistant can do instead. `money_hint` and `RequestFacts.spends_money` are removed.
- Writing the conversation summary after the reply is now an option that is off (`llm.summarize_memory_in_background`, default false). It was
  on since 0.1.6, and on the frozen vision suite it made "move my files, then undo" fail three times out of three (the operator reads the
  last turns when the summary is not ready yet, and planned a sort by extension); with the summary written before the reply, and nothing else
  changed, the case passes. The cost is the summary's model call (a few seconds) before the reply to each message, which is what the
  option removed; set it to true to have the speed back and accept that the operator reads the heuristic summary of the last turns.
- A message the local decision model is sure is a task, though it is not sure which tool it needs, no longer waits for the LLM concierge
  (`decision.settle_tasks_alone`, `decision.task_bar`, default 0.9): the probability that it is a task is the sum over the tool routes,
  and the route is left "unknown", as the concierge leaves it for most requests, which keeps the operator's full tool list and the check
  before a first question about something no tool can do. Requests the concierge must read (configuration, a coding agent, the screen) and
  questions about what the bot can do are never settled this way. Offline, over 444 real messages, the concierge also said "task" for
  every one of the 303 with a sum of at least 0.9; that is about two thirds of what reached it, about 5 s each. The decision record
  (`intake_decision`) has `task_probability` and `settled_by` for every message, so it can be checked.
- Three things that cost a task time without changing what it does. A progress line ("Working on it: ...") is sent in the background,
  behind the previous one for the task so they arrive in order, and the answer waits for any still on its way (about a second a step; it
  was awaited). An idle worker looks for a task every second, not every three. And an answer that only reports file writes, moves and
  sends (every step a successful `filesystem.manage`, `document.manage` or `artifact.deliver` write) is no longer audited by the model,
  because the receipt checks already compare it to the changed paths and the delivery record (1 to 8 s); any read, search, script,
  browser step or failed step keeps the audit.
- Voice notes are quicker and clearer. The speech model loads when the Telegram poller starts, not on the first note (about 4 s); the
  language model starts loading while the audio is transcribed, not after; "typing" shows at once; and what was heard is sent back
  ("I heard: ...", `channels.telegram.echo_voice_transcript`) before anything is done with it, so a mishearing is seen first.

### Fixed

- A Word or Excel file that was made in the task's own folder says so, and how to put it where the person asked. Live 11/15, "save it in <folder>": the model made the file without a place, then needed four refused copies and three code runs
  (2.5 minutes) to get it there. The tool's reply now ends "It is in the task's own folder. If the person named a folder for it, make it again with output_name set to the full path of the file in that folder; do not copy it." (nothing is added
  when a place was given). The tool's schema in the prompt is unchanged.
- A Word, Excel or PowerPoint file goes where the model was told to put it. Asked to "save it in <folder>", the model wrote the place as `output_path`, the tool only knew `output_name`, dropped the field without a word, made the file in the task's own
  folder and the answer said it was saved in the folder asked for (live 11/15: the named folder stayed empty). `output_path` is now read like `output_name`; a place outside the configured document roots is an error the model sees, not a silent move.
- A Word document is never made with only its title. Live on 11/15 the model wrote the checklist as `lines` and the tool read only `content`, so a .docx holding just the title ("1 lines") was made and sent.
  `create_document` now takes the text as `content` or as a list of `lines`, and refuses a call that has neither, saying what is missing, so the model tries again instead of sending an empty file.
- The assistant is told today's date when a request depends on it. Asked "which of the files in this folder were modified in the last week?" it answered "What is today's date?", because the Operator's prompt never said what day it is. The decision model now
  answers one more question about every request ("does it refer to a time relative to now?", `depends_on_today`, read at 0.35 because a line of date costs nothing; on the running model 11 of 12 phrasings of a relative time clear it and none of 12 ordinary
  requests does), and only then the Operator's prompt carries "Today is Sunday, November 15, 2026; it is 15:56 on this computer's clock". Every other request's prompt is unchanged.
- Recorded scenario prompts no longer carry a file's modified time. A listing now shows when each file changed, which is the time a test made its files, so the recorded prompts of the folder scenarios stopped matching on every run;
  the time is taken out of the prompt before it is hashed (`testing/scripted_llm.py`), so recordings made before listings had dates still match.
- A folder listing says what it did not open. `inspect_folder` stops at `max_depth` (2 by default) and at `max_entries`, and said nothing about it: asked for "the three largest files" in a folder whose biggest file was three levels down, the
  assistant named the three biggest of what it had seen and called them the largest (sealed Verdict 2, fin2_d1). The first line of the result now names the folders that were not opened (those with something in them) and tells the model to call again with a
  larger depth before it says which file is largest or newest, and says when the entry limit stopped the listing.
- A folder listing carries each file's modified time by default, for real this time. The Phase 1 change passed its tests and did nothing live: the tool's input model filled `with_dates: false` into every
  call that came through it, and an explicit false overrides the default. The sealed Verdict 2 run found it (fin2_d3, "which files changed in the last three days?": names and sizes, then "no tool shows modification
  times"). The field is now unset unless someone sets it, and a test makes the call the way the Operator does, through the model.
- A correction sent while a step is running is no longer lost. The step works for seconds outside any transaction with its own copy of the history; a note the person sent meanwhile was appended to the stored history, and the step then saved
  its copy over it, so the assistant said "Noted" and the model never saw the note (sealed Verdict 2, fin2_e1: "Find the tax letter", then "sorry, the one from the council": answered with the other letter). Saving a task's metadata now carries
  over the person's notes that the written history does not have.
- A request for new names does not get files moved into folders. Sealed Verdict 2 (fin2_s1, "add the prefix 2026- to every file name"): the model asked for an organize plan, applied it (four files moved into a new folder called
  "documents"), renamed them there, and reported only the renaming. An organize plan, or a manifest with a move in it, is now refused for a request that only gives files other names ("prefix" and "suffix" count as renaming; asking to
  sort, organize, group or move keeps those calls), with the way to rename in place. This is not an approval: the user's own setting (`require_approval_at_or_above: critical`) lets file changes run without a card, which that
  case also tests and which is left as the user set it.
- Pause, resume, stop and "how far along?" for research are made and answered in code. Live on 11/15, on the real bot: "Pause the research." called pause with a task id as the job id and was refused, was answered "already paused" while the
  job ran, "Resume the research." was taken for the answer to a question about it, and "Stop it" was answered "has been stopped" about a job that was still running. A yes/no question cannot recognize these ("Pause the research." scored 0.09), so
  while research is open in the chat every message is routed to the research stage, whose one model call says which of start, status, pause, resume, stop or something else it is (on the real 9B: 6 of 6 requests and 18 of 18 controls and
  other messages); the call is made first and the answer is the tool's own sentence, or "I could not do that: ..." when it was refused. A job id the model made up no longer stops a control (the chat's open research is meant), and
  nothing the tool says carries a job number any more.
- The research skill loads again. Its sharper trigger sentence had a colon in it, which YAML reads as a second key; the file was skipped without a word, the research route never applied, and the 9B went to a web search that
  is blocked here (live 11/15, the first request on the final code). A test now loads every file in `skills/starter` and fails if one is skipped.
- The assistant's own offer no longer counts as the user asking for Claude Code. Found by the second live research run: the assistant offered to hand a task to Claude Code, was answered "Pause the research."
  (declined, correctly), and then handed it over anyway. The offer sat in the task's objective, "Claude Code" in it was read as the user naming a coding agent, and the check that refuses a coding agent nobody asked
  for let it through. The facts about a request now leave out the questions the assistant asked (the user's answers stay).
- A started research job is told to the person in code. After the job started, the Operator went on calling the job tool with the id of an earlier job, offered Claude Code, and asked whether to wait or pause; the task now
  finishes as soon as the job has started, with an answer that says what it will do, what they will get and what they can say, and no job number.
- Allowed packages can be installed on a uv installation. This install's Python belongs to uv and has no pip, so every `dependencies.install` failed ("No module named pip") and openpyxl, python-docx, python-pptx,
  matplotlib and xlsxwriter were never there: a spreadsheet, a Word file or a chart could not be made, and a task looped on the failing install until it was cancelled. The installer uses `python -m pip` when the Python has pip,
  else `uv pip install --target` (the same per-task folder, the same allowlist and approval), else says plainly that neither is available. Checked for real: openpyxl and python-docx installed into a task folder.
- "Search the web for X" works on a connection DuckDuckGo refuses. On this connection every DuckDuckGo search is answered with a human check (HTTP 202), so the web search tool found nothing and the assistant asked for a Brave key (the research job found
  this on 11/14 and searched through Chrome; the tool itself did not). When DuckDuckGo refuses, the tool now makes the same search in the assistant's own Chrome and says so (`provider: chrome`); a Brave or SearXNG search that fails is not retried there, and
  when Chrome cannot help the refusal is what is reported. Checked for real: a search for a Raspberry Pi 5 price returned five Dutch shops.
- The research skill loads again. Its sharper trigger sentence had a colon in it, which YAML reads as a second key; the file was skipped without a word, the research route never applied, and the 9B went to a web search that
  is blocked here (live 11/15, the first request on the final code). A test now loads every file in `skills/starter` and fails if one is skipped.
- A plan gets a day on every step more reliably, and research is the assistant's step. The first live house plan had a day on 1 step of 9 (and its research owned by the user, its needs a tool name); drafted again, the same goal had a day on
  every step. The day is now required in the schema the planner model is held to (a draft without one is still accepted in code), and the planner is told that finding things out (rules, prices, options, who could do the work) is
  the assistant's step and that `needs` is for the user's side only. Measured on the real 9B over 5 goals and 6 repeats of the house goal: a day on every step in all 11 drafts (was 2 of 4 for the house), research owned by the user in
  2 of 6 repeats and 0 of 5 other goals (was 2 to 3 steps).
- The assistant no longer says "job_d56bb54f..." to the person. The job tool's start, pause, resume and stop results named the job by its id and the model repeated it; they now say "the research"
  and tell the model to leave any job number out (a status listing still carries ids, for the rare chat with two jobs).
- A job's unit of work runs once. The worker runs several loops that all call the job runner, and two of them read the same saved state and did the same unit: live on 11/14 a report section was
  written twice and the finished report and its closing message were sent twice, ten seconds apart. The runner now does one unit at a time in the process, whoever asks.
- A question about research that is already running no longer starts new research. Found by the first live run of the research job: "How far along is the research?" was routed as a request, and a second
  job was started whose goal was that sentence. The skill's yes/no question cannot tell the two apart (0.61 for the status question, 0.90 for "Resume the research."), so the call that writes the goal also says whether
  the message asks for new research (on the real 9B: 6 of 6 requests kept, 10 of 10 status, pause, resume and stop messages recognized), and a message that does not is left to the Operator. The skill's question is
  now sharper (measured: it separates the live status question, 0.42, from the requests, 0.56 to 0.96), and when a research job is open in the chat the Operator is told so, with the tool that controls it, as it is for an open plan.
- The e2e runner no longer turns an accepted end into "cancelled". It answers the first question a task asks with a neutral "go ahead", and when the task then asked a second question it waited two
  minutes and cancelled it, whatever the case accepted. A case that lists `clarifying` among its accepted final statuses (`h4_ask_before_moving`: "ask me which folder first and do not move anything yet") now ends at that
  second question, as asking is exactly what it wants. Cases that do not accept a question are run as before. This changes how that one case is scored, not what the assistant does: it failed in all three earlier runs
  with the assistant correctly asking which folder twice.
- An answer no longer names the folder a script ran in. When a task reached its step limit with nothing readable from its last step, the answer was "Completed the requested work in ...code_interpreter	ask_task_..."; the
  challenge run flagged it (an internal task name in a reply). It now says the work ran but there were no steps left to put what it found into words, and that it will not guess. A folder the person made (a project) is still named.
- "I noticed a procedure worth keeping" no longer arrives in the middle of someone's work. The learning card (keep this skill, remember this preference) is shown only when no other
  task of the chat is moving or waiting for an approval tap, and never after a step of a plan; otherwise the proposal waits in the queue (the console) as before.
- A correction sent while the assistant is working reaches the answer. Live 11/14: "Find my resume", then "The Word one, not the PDF" six seconds in, and the answer listed both files. The person's
  words are now shown to the model as what the person added (they were a step named `_user_note`, read as one more result), and an answer the model wrote while a note arrived is held back and written
  again with the note in front of it.
- A research report no longer cuts "Law No. 5403 is required [1]." after "No." (the half with the citation was kept as "5403 is required [1]."). The sentence check that removes any sentence with no note behind it
  does not end a sentence at a common abbreviation (No., Art., Sec., Dr., Inc., etc.).
- A file the user sends in Telegram is no longer read as a request to send a file. The intake's note ("[The user sent a file: ...]") was
  matched by the fulfillment check as "send" and "file", so a PDF the user sent for a summary was sent back to them, and a photo task blocked
  on a delivery nobody asked for; the notes are now left out of that check and the no-caption text is "Here is a picture.". The note also
  says what the file is (type, pixels for a picture, size), so "what is this and how big is it?" about a photo is answered from facts.
- The Auditor's rewording of an answer no longer slips a wrong value past the checks. They ran on the Operator's answer, before the
  Auditor read it, so a total the model had corrected to 15 came out of the Auditor as "5 lines in total (4 + 5 + 6)". A rewording with a
  sum that does not add up, or an address or calculated number that no tool produced, that the original did not have is dropped and the
  original goes on (`TaskWorker._audit_rewrite_adds_no_wrong_value`).
- "Which task do you mean?" picked the older of two tasks made within one clock tick (about 15 ms on Windows): `max()` keeps the first of equals
  and the list is oldest first. It showed as a test that failed one run in six; the newest now wins a tie.
- On a phone the console's bottom navigation had six items in a five-column grid, so Settings dropped onto a second row.
- Every page of the admin console waited about four seconds before drawing anything. The console asks "is the default model usable" on each page
  load, the model server's health check took four seconds because it looks at a second service that was not running, and nothing was shown
  until it answered. The first answer is waited for; after that the last answer is used at once and refreshed in the background every
  half minute (hosted profiles, where the check is only a look at a key, are not cached).
- Two gaps the fresh suite (heldout4) found. "Which file was changed most recently?" asked through `describe_folder` or
  `collect_folder_snapshot`, which list names and sizes but no dates, ended blocked: such a listing is now made with `inspect_folder` and
  dates, as the other listings already were. "Rename every .txt so it starts with old-" was planned with `organize_plan`, which only sorts
  files into folders, and never renamed anything: after such a plan the task's history now says to use `apply_manifest` with a rename entry
  per file.

## [0.1.6] - 2026-11-13

### Added

- The offer to hand a task to Claude Code (made when the local model has failed repeatedly and Claude Code is set up) comes with
  Yes and No buttons in Telegram. A tap answers the question exactly as typing does, once, and only while the question is open. "No"
  sends "No thanks", because a bare "no" is one of the words that cancel a task and declining should end the task with what was
  found. Typing the answer still works.
- Projects (Agent, Projects): a reusable set of folders, instructions and success criteria. Choosing one in chat copies that context
  onto the task for the operator to read; adding a folder to a project never grants access to it.
- A `system.inventory` tool answers questions about the computer from the places that know: how many Steam games are installed (every
  library folder on every drive; the redistributables are not counted), which apps are installed, free disk space, and where a file is
  (the Windows Search index, which answers in milliseconds, with a bounded folder walk where there is no index). Only files in folders
  YBM may read are returned, documents first. Read-only; off in the example config like `memory.manage`, enable it with `capabilities: system.inventory: enabled: true`.
  Files kept only in OneDrive (not downloaded) are marked `[online only]`, and reading one says so instead of "File is not a
  zip file"; a live run had reported the user's own resume as corrupted.
- Folder-triggered workflows: a saved workflow can run when a matching file lands in a watched folder. The scheduler polls the folder
  (top level only, at most 500 entries and 10 MB per file), waits for the file to stop changing, hashes it, and creates one replay
  task per input version; a failed task insert rolls the acknowledgement back, so a file is never skipped or run twice. Outputs
  YBM registers as artifacts are excluded so they cannot trigger another watcher.
- A third held-out suite of 16 open-ended requests (`--suite heldout3`, ids `h3_*`, frozen by a recorded hash like the second): the number of Steam
  games and free disk space (computed on the machine at fixture time, never typed in), finding a file by name, what a document is about, comparing
  files, adding a line to a file, moving files and then `undo that`, what YBM remembers about the person and forgetting it, saving and running a
  workflow from the chat, honest "I can't" answers, and building a small web page. Written once and run once on the code as it stands.
- The Telegram approval message has an "Allow for this task" button under Approve and Reject. A web sign-up asks three times (go to the page, fill
  the form, click the button), which was three taps for one decision. The button grants the same kind of step for the rest of that task, with
  the dashboard's existing limits: it never widens what policy allows (the risk ceiling, scope and patterns still run first), it ends with the
  task, and it covers 200 calls at most. A batch approval now decides every call in it from the button too, where before only the batch itself
  was decided and the calls were left pending.
- Saved workflows from Telegram: `save that as a workflow called weekly report` keeps the last finished task's steps under a name, `my workflows` lists them, and `run weekly report` (or `run <name> with folder=Downloads` for one with parameters) runs it again through the same replay pipeline and approvals as the dashboard. They were only reachable from the web dashboard. A message is treated as a command only when it names a real workflow, so `run the tests` is unaffected.
- `are my coding agents working?` (also `check codex`, `can you use codex?`) answers in a second without spending any quota: whether `codex` and
  `claude` are installed, their versions, and the model Codex is configured to use, so a Codex config that names a model the plan cannot use is
  visible before a task is queued on it (it cannot prove the plan allows the model; only a run can, and the reply says so).
- Three things to say to the assistant about itself, instantly and without a model call: `what apps are running?` lists the apps YBM started that
  still answer (by the name of the page and its address), `stop the recipe app` stops one (only after checking that the process with the recorded id
  is still the static server it started, so a reused id is never killed), `what do you know about me?` shows the remembered profile and rules, and
  `forget my t-shirt size` forgets the one fact that matches (several matches are listed and asked about; `forget everything` is declined as one
  message). A message only counts when it names something that exists, so `stop the music` and `forget the meeting` are ordinary requests.
- `document.manage` `compare_documents`: compares 2 to 12 PDF, text, Markdown, CSV, TSV or JSON files against 1 to 30 criteria and
  writes an evidence workbook and a Markdown report. Matching is literal keyword overlap, not a semantic judgement; scanned or
  password-protected PDFs fail visibly. Creating the files needs filesystem write access and the normal approval policy.
- The workflows page lists saved workflows, runs one with filled-in parameter values, schedules it and deletes it; a task can be
  saved as a workflow from its page.
- `undo that` (or `put them back`) puts the files of the last move or rename back where they were. Each applied move now records where every file
  came from, and the command turns that into a new task that moves them back through the ordinary replay pipeline, so it asks for approval like
  the move did. It never overwrites: a file that is no longer where YBM left it is skipped (and the reply says how many), a name that is taken is
  saved as a copy, and a move is undone once. Everything a task moved is undone, not only its last step: a file moved twice (sorted into a
  folder by type, then moved again) goes back to where it started.
- A search now says how hard it looked, and "it is not there" is only said when that is proven. `system.inventory find_files` walks the
  folders YBM may read shallow folders first and every drive in turn (so a file kept near the top of D: is found before a walk has dived
  into the biggest folder of C:), stops a few seconds after the first match, goes through hidden and system folders in a second pass when
  nothing turned up, skips a folder Windows will not open instead of stopping, and ends an empty result with what it covered ("I looked
  through all 250,804 folders under ..."), or says plainly that it ran out of time and proves nothing. `filesystem.manage search` is
  time-boxed the same way (it used to walk a whole drive for as long as it took) and skips locked folders inside the folder it was
  pointed at.
- The Operator loop reads its own history and acts on facts (`orchestration/ladder.py`), shown to the model as a check entry, never as a
  prompt change. A read of the very path the request names that failed because it does not exist, or a finished search for words the
  request uses, is a proven absence: the model is told it is settled, the answer is accepted without asking the Auditor whether "it
  does not exist" is sufficient (it was sent back until the task ended "Blocked"), and the coverage is added to the answer. Two empty
  searches, or one that ran out of time, point at the next rung, once each: the everywhere search, then a short Python script run with
  `code.interpreter` (the hint carries a script that looks at other drives first and stops itself after two minutes). A locked folder is
  said to be skipped. Three failures in a row suggest a script.
- When the local model has used those rungs (a script that failed twice, or eight steps that came to nothing, or the step budget ran out)
  and Claude Code is installed, the task asks the user instead of ending "Blocked": "Do you want me to hand it to Claude Code on this
  computer?" Only a plain yes hands it over, as a `coding.agent` call that carries the request and a short account of what was tried; nothing
  is sent anywhere otherwise, and a no tells the model to answer with what it has.
- The answer has to come from the receipt. If every file a task changed is outside every place the request names ("write it into
  cat.txt in C:\work" ended with the file in the repository root and an answer naming C:\work), or a move touched files of a kind the
  request did not name (".png files" moved the text file too, with the record of where it came from to put it back), the model is sent
  back twice, then lets it report where things really are.
- A script the local model wrote that produced an answer can be kept as a reusable helper skill (`adapters.skills.helpers_enabled`, off by
  default; the first script that worked is kept, at most 30), so the next request of that kind starts from it.
- Running on your own computer: `adapters.code_interpreter.require_approval_for_untrusted_run_python: false` now also removes the approval
  prompt the tool definition carried (it was fixed in code), and `blocked_imports: []` allows `os`, `shutil` and the like. Both defaults
  stay cautious. The example config says how to set them.
- A "remember that ..." request (the `configuration` route) is shown only the always-on memory and persona tools, not the whole catalog. With
  every tool in front of it, the local model answered "Remember these about me: my favorite fruit is mango ..." by reading a file that
  an earlier turn of the conversation had named, and reported that the file does not exist.
- `persona.manage update` replaces the whole persona document, and the local model used it to "remember" three facts about the user, which
  overwrote the document and left the facts where "what do you know about me?" does not look. An update in a task that has not read the persona
  first is refused, with the reason and a pointer to `memory.manage remember`, which is where the facts are kept and listed. A hand-off to
  Claude Code that the user agreed to counts as a requested coding agent.
  "Remember these about me: my favorite fruit is lychee, my parrot is called Kiwi ..." (and "remember the following: ...") is now taken by the
  remember command like "remember that ...", before any model sees it, instead of reaching a small model that filed the facts in the persona.
- A script that only printed its answer is answered with what it printed. The reply used to end with "Workspace: task_task_...", the folder the
  script ran in; the workspace is named when the script created files.
- A big request is split into parts (`operator.plan_parts`, on by default). A request that is long, names two or more places, or runs over
  several lines is first put to the model once (`orchestration/planner.py`): "does this have separate pieces of work?" The answer is
  JSON, never a word list, and a short single-line request is never sent. With two or more parts, each part runs through the existing
  delegate path (its own sub-loop, history and step budget), one after another, and the Operator then writes the final answer from what
  every part returned and says which part failed. The task gets the time its parts need, and the progress line says "Working on part 2
  of 5: ...".
- While one step runs long (a search of every drive, a script, a slow model call) the progress line is sent again every 25 seconds with
  how long the step has taken (`operator.heartbeat_seconds`); it used to stay unchanged for the whole step.
- A bare final answer ("13655") or a tool's own wording ("Wrote text file ...", "Found 3 result(s) ...") is said as a sentence or two by one
  short model call (`operator.voice_pass`, `orchestration/voice.py`). The rewrite is thrown away if it contains any number the answer and
  the request did not, and a sentence is never touched.
- `http.request` can read any public web page with `adapters.http_request.allow_public_reads: true` (off by default): GET and HEAD only, a
  name that resolves to this computer or its network (loopback, private, link-local, reserved) is refused, a GET or HEAD follows
  up to five redirects (most sites redirect "www.site.org" to a language or https page) with every hop checked the same way, so a
  public page cannot send it to this computer, and every other method still needs the allowlist. This is what a request like "get the headline from each of these five sites" needs.
- A quoted title, name or phrase in an answer must be in something the task saw: the request, a remembered fact, or a tool result. If
  not, the model is sent back once (inside the part, for a part of a plan). The local model had answered "Mozilla Firefox" for a page
  titled "Internet for people, not profit - Mozilla Global".
- The model is shown an HTML page's readable text, with its exact title first, before the page source. It saw only the first two
  thousand characters of a result, which were scripts and meta tags, so it could not find the title it was asked for and gave up.
- `http.request` reads an HTML page's exact `<title>` and the start of its visible text with code, and puts both in the result (`page_title`,
  and the summary the model sees). Asked for "the title of each of these five sites", the local model had returned a shortened title
  ("Python.org" for "Welcome to Python.org") for two of them, because it was reading the title out of raw markup.
- When a message arrives, a one-token request wakes a local model that was unloaded while idle (`llm.warm_on_message`, `llm/warmup.py`), so
  the model loads while the message is routed instead of on the first real call. At most one every four minutes, and never to a hosted
  model.
- A request to remember or change a setting (the `configuration` route) may not read a file whose path appears only in an earlier turn of
  the conversation: the call is refused once with the reason. A live run answered "Remember these about me ..." by reading a file named
  three requests earlier.
- A script that deletes files is refused (`adapters.code_interpreter.allow_file_deletion`, off by default). YBM has no delete operation and a
  request to delete is refused with an offer to move, but a script is the other way to delete, and with scripts allowed to run
  freely a live run answered "Delete all the files in this folder" by running `os.remove` on all three. The check reads the script
  (`os.remove`, `shutil.rmtree`, `Path.unlink`, `rmdir`, a shell `rm` or `del` or `Remove-Item`) whether the model wrote it or the tool
  generated it, and holds however the approval and import settings are set. Turned on, a deleting script is critical risk.
- A page written to a workspace-style path (".agent_control/workspaces/tip_calculator/index.html", which is what the tool's own
  description suggests) goes to the workspace's index.html, where the server looks. It used to become a folder inside the workspace,
  the server went on showing the placeholder, and a live build was blocked after seven rewrites. When a launched page has no controls
  and another page exists in the workspace, the page check says where it is and what path to write.
- An answer that writes out an addition and gets the total wrong ("5 lines in total (4 + 5 + 6)") is sent back once with the true
  total. The number check had passed it because 5 is in a tool result.
- A fifth frozen suite, `--suite heldout4` (16 cases, `e2e/heldout4_cases.json`), was written and frozen before the round-5 work on how a
  request is understood, checked and sped up, and is run once at its end on requests that work was not tuned against: counting by file
  kind, the largest file, finding a file by what it says, which folder has more files, the newest log, a one-sentence summary, a total
  over a table, a JSON field, renaming and copying, three files in one request, two ways of saying "delete" and an "ask me first"
  instruction (to measure understanding rather than keyword matching), remembering a fact and then using it, two page titles, and an
  honest "I can't".
- A fourth frozen suite, `--suite vision` (16 cases, `e2e/vision_cases.json`), holds the examples the owner named as what YBM must do,
  in fresh wording: how many Steam games, where is a file in a hidden folder, what is a document about, a missing file, a count that
  needs a script, which of two files has more words, a request that splits into parts, the exact titles of three public sites, a
  tip calculator built and run, adding a line to a file, creating a file in a named folder, moving files and `undo that`,
  remembering a list of facts, Claude Code on request, an order it cannot place, and a refused delete. It is the "does it still do
  what I asked for" run before a release, frozen with a hash like the held-out suites.
- A file on another drive is described with its drive ("quarterly-plan.docx (in drive D: > ...)") instead of only its folders.
- A saved workflow can be scheduled from the chat: `run steam count every 2 days` (or `daily`, `weekly`, `every 3 hours`) creates a schedule that
  replays it through the same pipeline and approvals as the dashboard's Automate, and says when the first run is. A weekday or a time of day
  ("every Monday at 9") is not supported by the scheduler yet; the reply says so and gives an example, and does not run the workflow now, which
  would be the wrong reading of a request to run it later. Parameters work as for a run (`run folder digest weekly with folder=Downloads`).
- "How's it going?", "any update?" and "what are you doing right now?" are answered from the running task's own record, at once, with
  no model call and no new task. Before, they went through intake: one got small talk and one started a second copy of the job.
  The answer says what the task is, how long it has run, how many steps it took and what the latest one was, or that it is waiting
  for an approval or an answer. With nothing running it says what the last task did if that was within half an hour.
- What a message sent while a task runs *is* to that task (a status question, a change to the job, "stop", a new request, or chat) is now asked of
  the local decision model too, in shadow: its answer is recorded next to the concierge's in the intake-decision event (`decision_kind`,
  `final_kind`, `kind_agrees`) and decides nothing. The console's decision stats gain `kind_stats`: how often they agree and whether it has earned
  the right to take over (at least 95% agreement on at least 100 real messages). It would replace a model call that queues behind the running task
  with a forward pass on the CPU; until the numbers say so, the concierge keeps deciding. The question is only sent while a task runs.
- `operator.constrain_calls` holds the Operator's reply to a JSON schema built from the tool definitions, so a
  tool call with a wrong argument name, an unknown tool or no operation cannot be written. It needs a local server that enforces
  a response schema (llama.cpp, Ollama) and LocalDeploy with its response-schema fix; otherwise the plain schema is used.

### Changed

- The operator's rules about the model's habits are three ordered lists in their own modules, not branches in the worker's tick (the worker lost about 780 lines, no behavior changed):
  `orchestration/request_facts.py` reads what a request says once, each fact from the text it always came from;
  `orchestration/call_policy.py` is what happens to a tool call before it runs (refusals, canonical form, fourteen labeled rewrites, in order);
  `orchestration/answer_checks.py` is what a script checks in a `done` answer (a value no tool produced, a sum that does not add up,
  a quotation nothing contains, a receipt that does not match), with how often each may speak. The moved rules keep their names in the
  worker, so existing imports work. `docs/ARCHITECTURE.md` has a table of which module decides what.
- The rolling conversation summary is written after the reply, not before it. It is a model call, and the model serves one request
  at a time, so it used to run inside the message handler, ahead of the classifier and the reply, on every message that did not
  arrive while a task was working. The handler now stores the new turn and the heuristic summary at once, and the model summarizes
  15 seconds later, one run at a time per conversation (a message that arrives meanwhile earns one more run, not one each), and only
  when no task of that conversation is using the model. The recent turns are in the context from the first moment, so what the
  assistant knows does not lag. Only the Telegram and WhatsApp intake use it; the worker's own summaries are unchanged.
- "Undo that" takes away the folders the move made, once the files are back and the folders are empty: moving the .png files
  into `pictures` and then undoing it no longer leaves an empty `pictures` behind. `apply_manifest` reports the folders it created
  (`created_folders`), the task's undo record keeps them, and the undo removes only those, only inside the folder the move was made
  in, and only if empty (a folder that holds anything now stays). The answer says which folder was removed.
- `ybm autostart enable` now starts YBM at login, not only the tray icon: the Startup shortcut runs the tray with
  `--start --delay 45`, which runs `ybm start` 45 seconds after sign-in (so sign-in is not slowed) and does nothing for a service
  that is already running. Starting the services does not load a model: LocalDeploy loads one on the first message, and a
  profile with `idle_stop_seconds` gives the GPU back after a pause. A shortcut made by an older version keeps starting only the
  tray icon until `ybm autostart enable` is run again.
- First-task and installation guidance in the README and `docs/` is shorter and starts from the first task.
- A message sent while a task runs no longer queues a model call behind it. Every message triggered a conversation-summary call on the same
  single-request model the running task was using, so "how's it going?" mid-task waited tens of seconds for a summary nobody needed yet. While a
  task of the conversation is running the summary is the heuristic one; the next message after the task re-summarizes with the model.
- What YBM says in a chat now reads like a person wrote it. A new text pass (`human_text.py`) rewrites every task message before it
  is sent: a path becomes the file name and a place ("resume.docx (in OneDrive)"), a task id becomes "an earlier task", a
  `[file] name (123 bytes)` listing becomes a short list capped at eight names with "...and 22 more", and a reason code becomes a
  sentence. It only rewrites what is already in the message; a path that does not exist on this computer is left as written.
- Progress is one message that changes, not one new message per step. The "On it" message is the one that gets edited ("Working on it:
  looking through your files... (3 min so far)"), the typing indicator replaces the "Got your message" text, and the answer arrives as
  its own message while the progress message is removed. Tool names, outcome codes and the model's reasoning no longer appear.
  Failures say what the last step was and how it ended, without "Gap:", "Retries:" or "Error:" labels.
- The approval prompt lists what will happen as sentences ("Type \"Oney Erge\" into the \"Full name\" field", "Click \"Sign me up\"", "Control your computer: Open Steam") instead of tool names and JSON, and a value typed into a password, card or similar field is never shown.
  Going to a page, reading it and a chain of page steps read the same way ("Go to http://...", "Read what is on the page") instead of "Use the web page".
- A policy refusal ends the task after the second one for the same tool. A form fill refused for exceeding the browser's risk
  ceiling was retried six more times and then reported as "something went wrong on my side"; it now stops and says the safety
  settings do not allow that action.
- What a person says about themselves is remembered one fact at a time. "Remember these about me: my GitHub username is oney-erge, my
  favorite language is Python, and my t-shirt size is L" was stored as that one sentence under "preference", so a form asking for a
  GitHub username had to find it inside a paragraph, and a later "my t-shirt size is XL" added a contradicting sentence. Each "my
  <thing> is <value>" clause is now its own fact ("T-shirt size: L") in the `profile` category, saying it again updates it, and the
  reply names what was kept ("your GitHub username (oney-erge), your favorite language (Python) and your t-shirt size (L)"). Rules such
  as "remember that I prefer short answers" are unchanged.
- YBM remembers who it is talking to: the first message from a Telegram sender stores `Name: <their Telegram name>` as a profile fact, so "my
  resume" has an owner. A name they state later ("my name is Oney Erge") replaces it.
- A task that stopped and was never answered is closed quietly after a day. A blocked task is an honest "I could not do that" that was already
  reported; left open (there were 88) it cluttered the dashboard, and a later "any update?" dug one up and quoted it back. A question nobody
  answered stayed a live question too, so a reply days later would have been taken as its answer. After `limits.auto_close_blocked_hours`
  (default 24, 0 keeps them) with no change, a blocked or unanswered task is cancelled the ordinary way (so its cleanup runs) and marked
  `auto_closed` with the reason, which the dashboard can show as "Closed automatically". Nothing is sent to the person: they were told when
  it stopped, and a message about housekeeping is noise. Asking again starts fresh. The worker checks at most every ten minutes.
- `operator.constrain_calls` is on by default. Measured over the 60-case held-out suite on the local Qwen3.5-9B: calls a tool refused
  fell from 52 of 266 steps (19.5%) to 5 of 150 (3.3%), made-up tool names from 30 to 0, and the held-out pass rate went from 43 to 54 of
  60 together with the other fixes in this release. It applies to local servers only and falls back to the plain schema when a server
  refuses it; set `operator.constrain_calls: false` to turn it off.

### Fixed

- A task that asked a question and got its answer runs its next tool call. Answering put the task back to `received`, the worker
  only moved a task to `running` the first time it started, and a call is admitted only for a `running` task (since the lifecycle
  controls), so the first call after an answer was never run and the worker asked the model again on every poll: a live run made
  281 model calls on one task and ran no tool. A task is now moved to `running` whenever the loop picks it up.
- A script that only printed its answer no longer gets "Workspace: task_task_..." added to the message. The code interpreter
  lists its own `script.py` among the files it created, so the message named the scratch folder as if it held a result (the
  frozen vision suite caught it: two cases failed on the leaked `task_`). The folder is named only when the run made something
  else, and `Created files:` no longer lists `script.py`.
- The delete refusal and the "change it" overwrite rule read the user's own message, not the question and answer a clarification round
  appends to the task. After "I cannot delete files; I can offer to move them", the objective held "move", the refusal read it as the
  user's, and the files were renamed anyway.
- A browser click that matched nothing is a failure. It was reported as succeeded, so a run asked to sign someone up filled the form, "clicked" the button, saw the same page and spent its steps verifying a form it had never submitted. The step now fails with what to do (the button's visible words in `text`), and words in the `selector` slot ("Sign me up") are tried as the button's text when no element matches them as CSS.
- A request that only asks to delete files is no longer carried out by moving and renaming them. There is no delete operation; "delete
  all the files in the junk folder" became folders named "other", "deleted" and "trash", three renamed files, and "Renamed 3 file(s)".
  The move is refused and the model is told to say plainly that it cannot delete, and to offer a move to a folder the user names. A
  request that also asks to move, organize, rename, sort, tidy or clean up keeps those operations.
- Asked to read, open or send a file that does not exist, the model now says so instead of asking the user which other file they
  meant or to paste its contents. A question after a step that failed because the target is missing is turned down with "say so
  plainly in a final answer", and that honest answer is no longer sent back by the fulfillment check for lacking the contents the
  request expected (which ended in "Blocked"). A request that says to ask first keeps its question.
- The relative-path completion also handles a path with the folder alias first (`desktop/AgentControlE2E/a.pdf`), which the
  filesystem tool understands and the document tool does not: `summarize_pdf` looked for it under the program's own folder three times.
- A batch of calls the person approved is no longer refused when it runs. The executor fills in a tool's schema defaults before it checks policy, but a batch approval bound the model's raw input, so for any tool whose schema adds a default (browser form steps do) every approved call came back `approval_invalid_or_mismatched` and the approvals were wasted. The batch now binds the call as it will run, and finds each call's approval by that same form.
- An answer the Auditor turned down twice, but that holds no email address, link or calculated number that nothing read produced, is
  delivered with a note ("Not fully confirmed ... please double-check it") instead of ending the task "Blocked". A 9B Auditor rejected
  a correct summary of a 159-character document for being short. An answer with an unsupported value is still blocked.
- A request to change a file that exists ("change it to say goodbye", "update the text") overwrites it. `write_text_file` saves a copy
  (`greeting_2.txt`) unless `overwrite` is set, and the model repeated the copy until the task was blocked.
- `read_file` keeps the lines of the file it reads. Every run of whitespace, line breaks included, became one space, so a CSV reached
  the model as one long line ("name,score Ana,71 Ben,88 ..."), the scripts it wrote could not parse it, and the task ran out of steps.
- A `send_file` with no path says to pass the file's full path or to find it first with `filesystem.manage search`. "Send it to me",
  asked as a new task, repeated the same path-less call four times until the task was blocked.
- A file or screenshot the user asked to be sent is checked as sent even when the Auditor owns the rest of fulfillment. "Open the page
  and send me a screenshot" completed after the screenshot was only taken, because the Auditor approved it and the deterministic
  delivery check was skipped.
- A file read by the path a listing showed (`resumes/notes.txt`, relative to the folder the search or listing looked in) is
  completed with that folder, instead of being looked for in the program's own folder and reported as not existing three times.
- A finished workflow replay shows what its last step found. `run steam count` answered "Replayed 1/1 step(s) successfully" and nothing about the
  count; the answer now carries the last step's result under that line.
- Writing to a file that already exists saves a copy (`greeting_2.txt`) unless `overwrite` is set; the result now says the original
  was NOT changed and how to replace it. The model had repeated the same call until the task was blocked.
- "Add the word pears on a new line" no longer makes `list_2.txt`. There was no way to add to a file: `write_text_file` saves a copy when the path
  exists. It has an `append` option now (the text goes to the end on its own line, the rest untouched), turned on when the request says to append,
  add on a new line, at the end or underneath and the file exists. A model that sends the whole file again (the old lines plus the new one) is
  taken to mean a replacement, so the old lines are not doubled.
- When the "answer now" offer (stuck or out of steps) is spent on a question the user already answered, the model gets one more
  chance to answer or say what blocks it, instead of the task ending "blocked".
- `filesystem.manage apply_manifest` treated a folder as the destination of a move as a name collision and renamed around it: three
  files "moved" to `.` became `trash_2`, `trash_3` and `trash_4` outside their folder. A folder destination now means "into that
  folder" and moving a file onto itself is refused with a plain message.
- `schedule.manage list`, `memory.manage list` and the artifact list returned only a count to the model ("Found 35 schedule(s)."), so
  "what do I have scheduled?" was asked for again and again until the task was blocked. Each now lists its items in the text the model
  reads, and the schedule list leaves out deleted schedules, which it counted before.
- A coding CLI that rejects the model in the user's own config no longer pastes its JSON error into the chat. Codex with `model = "gpt-6.1-sol"` on a ChatGPT account answered with a 400 and the whole `turn.failed` payload; the message now names the model, says where it is set (`~/.codex/config.toml`) and offers Claude Code instead.
- An email address or web link in an answer that the request, the tool results and the remembered facts never held is sent back once
  with "do not make up values" (`operator.verify_numbers`). The local model answered a lookup with an address that was not in the file.
- An app is not called done until the launched page offers what the person said they would enter. Asked for "a web app that scales a recipe: I
  enter ingredient amounts and the number of servings", a live run launched a page with two servings fields and a fixed example recipe, and the
  person had nowhere to type an ingredient. After a launch YBM now fetches the page and lists its fields and buttons in the launch result ("Page
  check: 2 field(s) (Original Servings:, Target Servings) and 1 button(s). The request says the user enters ingredient amounts, but no field on
  the page is named for it"), and a new `page_matches_request` completion check compares every thing the request says the user enters with
  those labels, whatever the classifier declared. A page with a free-text area is never faulted, a request that states nothing to enter is not
  judged, and an app built another way (no page record) is not blocked.
- A model that keeps re-reading a file is offered the chance to answer in its own words before the raw result is given to the user:
  "what is the total on the receipt?" got the whole file back as its answer.
- A call that returns the same result three times no longer ends the task as "Blocked" at once. The model is told to answer with
  what it has or ask one question (and, with `operator.constrain_calls`, its next reply can only do that); a second repeat stops
  the task as before.
- `find_files` finds a file the Windows Search index does not cover. The index answers in milliseconds but only for what it has indexed; a hidden
  or just-created folder (every E2E fixture lives under `.agent_control`) came back as "No file matching ... was found" even though the file was
  there. When the index returns nothing readable, a bounded walk of the folders YBM may read looks for the name; a content search still needs the
  index.
- A request to submit a web form is not done until the form was submitted. "Sign me up" with the details you have on me ended with every
  field typed, a page summary as the answer, and nobody signed up: a new `form_submitted` check expects a successful click (or a fill
  that asked to submit) after the first fill when the request says submit, sign up, register or apply on a web page, and the model is told
  to click the button by its visible words; a model that repeats the fill instead is told the form is still unsubmitted, and if the run stops anyway the message says so in words ("I got stuck repeating a step that had already worked (filling in a form)... the form was filled in but never submitted") instead of `Stopped: browser.control:fill_form had already succeeded`. Requests that only ask about a form, or say "submit my report", are unaffected.
- A request that says to ask first ("Ask me before changing anything on disk") keeps its question: the guard that turns away
  optional questions judged "Rewrite this file" satisfied once the file was read and ended the task with a summary of the read.
- A form step with nothing to fill is refused before anyone is asked to approve it. `fill_form_step` had `fields` optional, so a model that
  sent an empty step (it sent eleven in one run) got an approval request for each, and the failure came from inside the browser adapter
  after the person had tapped Approve. `fields` is now required, one `field`/`value` pair is read as a one-entry `fields`, and the failure
  message no longer claims "Chrome is not running with remote debugging enabled" for every error that mentions the browser.
- `read_file` on a Word, Excel, PowerPoint or zip file returns its text or file list, not the archive's bytes; a binary file says
  there is nothing to read.
- A failed script shows the end of its traceback (the line that says why) and a missing package says what to do instead.
- A form fill that typed nothing is a failure. Run against a blank tab (the model skipped opening the page) it matched no field, came back as
  succeeded, and made the real attempt that followed look like a duplicate of it; the form was then submitted empty and the model reported
  "signed up" on the page's thank-you message. It now fails with "None of the fields ... matched anything on a blank tab" and the step to take
  (open the page first, use the labels as shown); a fill that matched only some fields succeeds and names the ones it missed.
- A calculated number in the answer to a calculation request (`operator.verify_numbers`, on by default) must appear in a tool
  result; otherwise the model is sent back once to calculate it with code.

### Security

- Updated opentelemetry-api 1.45.1 to 1.45.2 (CVE-2026-126360, fixed in 1.45.2). The `backend-quality` audit failed on every branch until then.
  `pip-audit` is clean on the updated lock; nothing else in the lock changed.

## [0.1.5] - 2026-10-22

### Security

- Updated pypdf 6.20.0 to 6.21.2 (three advisories published 2026-10-21, fixed in 6.20.1 and 6.21.1). The
  `backend-quality` audit failed on every branch until then. The backend tests pass on 6.21.2.
- Updated Pillow 12.3.0 to 13.0.0 (nine advisories, fixed in 13.0.0), certifi to 2026.10.19, and the WhatsApp
  bridge's `follow-redirects` 1.16.0 to a patched release (two high-severity advisories about credential headers
  on cross-origin redirects). These new advisories made the `backend-quality` and `whatsapp-bridge` jobs fail on
  every branch and would have failed the scheduled audit. `pip-audit` and `npm audit --audit-level=high` are
  clean in every lockfile again; the backend tests pass on Pillow 13.

### Fixed

- The macOS and Linux one-liner (`curl ... | bash`) had been failing since 2026-08-26: a line continuation was
  missing, and the release's `SHA256SUMS.txt` was written with Windows line endings, so checksum verification
  never matched. The script is fetched from `main`, so its fix was already live; this is the first release whose
  checksum file is written correctly, so `sha256sum -c SHA256SUMS.txt` also works on Linux.
- On Windows, `YBM.bat` / `scripts\ybm.ps1 run` started the Telegram poller on every launch, even on a fresh
  install with Telegram off and no token. It died at once (`Telegram token not found in TELEGRAM_BOT_TOKEN`), was
  restarted, and the status line still said `[OK]`. It now starts only when Telegram is enabled and has a token, the
  same rule the macOS/Linux launcher (the Python supervisor) already used.
- `ybm start` ended with "Stop with: ybm stop", but `ybm` is only on PATH while the virtualenv is active, so the
  macOS and Linux one-liner (and `ybm.sh`) left people with a command that does not exist. It now prints the
  console script of the install that started the services.
- The weekly dependency audit opened an issue when it found a vulnerability and kept commenting on it, but never
  closed it. The audit has passed since the fix, yet the issue stayed open and made the repository look broken. The
  workflow now closes the issue, with a comment, on the first clean run.
- The installers' closing message still told people to "pick a model" after first run started choosing one
  for them; it now says that YBM names the model it found and only asks when it found none.
- The usage guide told every install to stop YBM with `ybm stop`, which does not see services started by the
  Windows PowerShell wrapper (the MSI and `Install-YBM.bat`). It now gives the command for each way of
  starting it.

### Changed

- The admin console no longer depends on the `shadcn` npm package, which pulled in a `braces` version with an
  unpatched advisory and turned `npm audit` red. Its one stylesheet is vendored; the compiled CSS is
  byte-identical.
- Compatible dependency updates were consolidated and the security audits repaired.
- README: shorter, with a new "Is YBM for you?" section.

## [0.1.4] - 2026-10-02

### Fixed

- A model chosen or changed after the Telegram or WhatsApp intake started did not reach its first-line
  chat replies or task classification until everything was restarted. Both intake loops now follow
  `config.yaml` and `.env` the way the task worker does, and keep the previous setup if an edit is bad.
  Rotating a channel's own token still needs a restart.
- `ybm start` waited out its 45-second budget and reported the backend as failed when `server.port` was
  not 8765, because the readiness probe was hard-coded. The probe, `ybm doctor`'s port check, and the
  console address printed by `ui-build` and onboarding now follow the configured port. The launchers and
  tray icon still assume 8765 (see "Known limits" in the roadmap).
- The console showed a blank window until the first response from the backend, which on a cold launch
  looked like a crash. It now says "Starting YBM...".
- The per-role models and MCP servers toasts told people to restart long-running processes; neither is
  needed any more.
- Web chat, the default front door, acknowledged "remember that ..." and standing
  rules ("always answer in three bullets") and stored nothing, while Telegram and
  WhatsApp stored them. Web chat now uses the same runtime-level handling, so a rule
  stated in chat reaches the next task's context.
- Receipts understated model cost: the Concierge's own call (the first call of a task,
  and the only call of a chat-only reply) was never recorded. It now lands on the task
  it led to, in `token_usage.by_source.concierge` and the per-call trace, on web chat,
  Telegram and WhatsApp.
- YBM's built-in helpers (task status, skills, persona, notes search) no longer borrow
  the `telegram.receive` capability, so the Tools page stops describing a status check
  as "can read incoming Telegram messages" and turning Telegram's receiving off cannot
  remove them. They run under a new `agent.core` capability, enabled by default; a
  config written before it existed keeps following `telegram.receive`, so nothing
  changes on upgrade. The recorded scenario fixtures were migrated mechanically (the
  four catalog lines relabelled, every recorded response kept byte-for-byte).
- Switching **File system** access on in the console did not make the file tool
  available, because `filesystem.manage` also required `adapters.computer_use.enabled`,
  a desktop-control flag that setting never touched. "Organize my Downloads" stayed
  blocked with the access switched on. The file tools now follow the file-access
  setting alone.
- **Read-only** file access enabled `filesystem.read`, a capability no tool used, so
  the agent had no file tool at all. The read operations of `filesystem.manage` and
  `document.manage` now run under `filesystem.read` (`ToolDefinition.operation_capabilities`),
  so Read-only lets it look and never change anything. A config that enables only
  `filesystem.write` keeps working: writing implies reading, in memory only.
- A running worker never noticed changes to `config.yaml` or `.env`: access, folders
  and the chosen model only took effect after restarting the whole stack (the console
  even said "Restart long-running processes to pick it up"). The worker now rebuilds
  its policy, tools and model when either file changes, keeps the previous runtime if
  an edit is invalid, and reports it.
- A task blocked because access is off said "No available tool or capability can
  complete the request with the current configuration". It now names the access that
  is off (and records it in `metadata.blocked_access`), evidence-based and never from
  the wording of the request.
- The first-run wizard said "You're set" after skipping the model choice, and listed
  five "Not built yet" channels. It is honest about a missing model and shows only
  channels that exist. The Access page no longer says "Full autonomy" means no
  approval prompts: installing servers, promoting generated code, running unsandboxed
  code and sending secrets still ask.

- Windows first run: `run.bat` and `run.ps1` failed on every default start (an
  array was passed to a `[string]` parameter), and a successful start was
  reported as "startup failed" because the "is WhatsApp enabled" probe's exit
  code leaked through `$LASTEXITCODE`. Both fixed; `YBM.bat`, `Install-YBM.bat`
  and the installers share the fix.
- Windows PowerShell 5.1 started from a PowerShell 7 terminal inherited that
  terminal's module path and lost `Get-FileHash`, failing an install at its last
  step. The `.bat` entry points now clear `PSModulePath`, and the scripts drop the
  PowerShell 7 entries themselves.
- On a machine with no `uv`, the uv installer's console output was returned along
  with its path, so first run died with `The term 'Downloading uv ...' is not
  recognized`. The output is now captured and shown only if the install fails.
- `run.bat stop` and `logs` crashed with a raw Docker API error when Docker
  Desktop was installed but not running, and `uv sync` progress was printed as a
  red `NativeCommandError` stack trace in the middle of a successful install.
- `run.sh` aborted on macOS's stock bash 3.2 (an empty array under `set -u`).
- The default configuration no longer warns about itself ("Scheduler service is
  enabled, but schedule.manage capability is off"), and the Access page no longer
  shows a red "default LLM profile is not configured" on a fresh install.

- `frontend`: resolved the `fast-uri` (high) and `qs` (moderate) advisories
  flagged by the scheduled dependency audit (#32) via `npm audit fix`.
- The version a released archive reports through `ybm check-updates` now
  always matches the git tag it was built from. `scripts/package_release.py`
  previously copied `backend/pyproject.toml` into the payload unchanged, so
  the installed package's own metadata - what `check-updates` actually reads
  - stayed on whatever version was last committed there rather than the tag
  the release was named after; a release could ship in a way that reported
  itself as an *older* version than the download it came from. The packaging
  step now stamps the staged copy with the release version being built.
  Also caught up the committed value itself, which had drifted a full
  release behind (`0.1.2` while `v0.1.3` was already public).

### Added

- "Where may YBM work?": one step that grants folders and a file-access mode together
  (`GET /admin/api/setup/folders`, `POST /admin/api/setup/work-folders`), shown in the
  empty chat and offered from a blocked task with a one-click "Try again". Only real
  folders are accepted - never a whole drive, the home folder, or an operating-system
  directory - and only Read-only or Write-with-approval, never full autonomy.
- A blocked task that needs access shows what is off and a button to turn it on
  (`Turn on Browser`, `Choose folders for YBM`), then retries the same request.
- First run needs no input. A model is chosen from what is already on the machine
  (your own LocalDeploy via `YBM_LOCALDEPLOY_ROOT`, a running Ollama, then a
  provider API key already in your environment or `.env`), without calling a paid
  API, and the launcher and the chat header both say which one was picked. The
  shipped config no longer names a model that only existed on the author's machine
  or a silent paid fallback.
- The console signs in once and stays signed in. The admin token is generated
  and handed to the browser in the one-time `?token=` link every launcher, the
  tray icon and the new `ybm admin-url` open; the console exchanges it for an
  HttpOnly, SameSite=Strict session cookie (`POST /admin/api/session`), so a
  closed tab or a bookmark no longer lands on "paste your admin token". The token
  screen remains as a rare fallback and explains the easy way back in first.
- Docker needs no setup: the container generates its admin token and vault key on
  first start, keeps them (and keys saved in the console) in the `ybm-state`
  volume, and `./run.sh docker` / `.\run.ps1 docker` open a signed-in console.
  `docker compose up -d` works with no `.env` (Compose 2.24+).
- CI runs every launcher for real (Windows PowerShell 5.1, macOS bash 3.2, Linux)
  and starts the container with no configuration, instead of only parsing them.
- `ybm doctor --quiet` and `ybm setup --quiet`, which the launchers use to print
  only what needs attention.
- The Python version is pinned (`backend/.python-version`, 3.12), so a fresh
  install no longer runs whatever newest interpreter `uv` finds.

- `frontend`: a vitest + Testing Library unit-test setup, seeded with
  coverage for access-mode preset computation, task-status action gating,
  and the chat/task API response schemas - the console had TypeScript and a
  production build as its only automated gates until now.
- `frontend`: Playwright coverage for approving/denying a pending action,
  an expired approval disabling every decision button, and a failed task's
  trace highlighting the step that failed.
- Task receipts now report `execution` (calls attempted/succeeded/failed)
  and `verification` (items mechanically re-checked/verified/missing),
  surfaced in the chat receipt card and the plain-text export. Backed by a
  new `ToolDefinition.verify` hook (currently implemented for
  `filesystem.manage`'s `apply_manifest`, which re-reads the destination
  and source paths on disk after every move/copy/rename instead of trusting
  the adapter's own success claim) and a `ToolCallResult.verification`
  field `ToolExecutor` attaches automatically on a succeeded call.
- `ToolDefinition` gained a declarative `operation_egress` field:
  `ToolExecutor` now records an operation's outbound host itself by reading
  the call's own reported URL, so a tool needs no manual
  `egress.record_egress()` call site to be covered. `http.request` moved
  onto this from its old manual call; `browser.open` and `browser.control`
  are now covered for the first time - previously only `http.request`
  contacted egress, so browser traffic was invisible to receipts.
- `ToolDefinition.operation_content_trust` labels which operations return
  content this machine does not control - a web page, an HTTP response, an
  MCP server's own reply, a document someone else authored
  (`browser.open`, `browser.control`'s page-reading operations,
  `http.request`, `web.search`, `mcp.client`'s `call_tool`,
  `document.manage`). `ToolExecutor` stamps it onto
  `ToolCallResult.content_trust`, and it now shows as an "untrusted
  content" badge on the matching step in a task's trace. Does not yet
  reach the Operator prompt itself - see `docs/ROADMAP.md`.
- An automatically-retried TIMEOUT or transient failure on a risky write
  (filesystem/terminal/desktop/browser-control/VS Code, or high/critical
  risk) now records an explicit warning in the retry's history entry - it
  may have already taken effect before the connection was lost, so verify
  before repeating it - the same "silently retrying can do a thing twice"
  reasoning `reconcile_orphaned_tasks` already applies to a crashed worker,
  now reaching the ordinary in-process retry path the next `decide()` call
  actually reads. A clean rejection (rate-limited, quota exhausted) still
  gets no such warning - nothing ran yet in that case.
- Per-role LLM models: `concierge_profile`, `operator_profile`, and
  `auditor_profile` let Concierge, Operator, and Auditor use different
  profiles instead of always sharing `default_profile` - configurable from
  Settings' new "Per-role models" card (advanced mode) or directly in
  `config.yaml`. A new `fallback_chain` (ordered list of profile names)
  replaces single-fallback `fallback_profile` when set, and each entry gets
  its own cooldown after failing so a subsequent call skips straight to the
  next one instead of re-paying that entry's timeout. `_is_unavailability`
  now also treats HTTP 429/401/403 as failover-worthy, alongside the
  existing 5xx/timeout/connection-error handling (still not 400 - a request
  bug fails the same way against any profile). A receipt now says when a
  fallback model answered somewhere in the task.
- Scoped, revocable "Allow for this task" grants: a grant now inherits the
  exact path/target scope of the call it was approved from (so "allow this
  move in Downloads" no longer silently covers a later move anywhere else),
  carries a 200-operation cap so it's never unbounded for the rest of the
  task's TTL, and can be revoked early. The Access page's new "Active
  grants" card lists every currently-usable grant across every task -
  scope, usage, time remaining - with a one-click revoke, closing the gap
  `docs/archive/UI_UX_AUDIT.md` named: "there is still no way to see or revoke a
  live one."
- MCP servers can now be added, edited, tested, and removed from Settings
  instead of by hand-editing `config.yaml` - three new endpoints
  (`POST`/`DELETE /api/config/mcp/servers[/…]`, `POST …/test`, the last
  running a real stdio MCP handshake against the server). Env values are
  write-only: the response never echoes them back, and leaving the env
  field blank on an edit keeps the existing values instead of wiping them.
- A pending `adapter.factory` `promote_after_approval` approval now shows
  the actual generated adapter source and a real sandbox test result
  (`GET /api/adapters/review`), not just `adapter_dir`/`approved=true` -
  approving one used to be a decision made from the tool name alone.
- A new Insights page (`GET /api/dashboard`) reports cross-task reliability
  over a 7- or 30-day window: completion and *verified*-completion rate
  (the same mechanical `ToolVerification` data a task receipt already
  claims, not the model's word that it finished), failure/retry counts,
  token spend, a per-tool failure-rate table with a least-reliable-tool
  callout, and per-model/per-task-type breakdowns. Entirely computed on
  read from existing task/tool-invocation data - nothing new persisted.
- A completed task can now be replayed (`POST /api/tasks/{id}/replay`, a
  new "Replay" button on its trace page): a new task is created whose
  operator loop deterministically reissues that task's own succeeded tool
  calls in order, through the exact same approval, retry, and verification
  pipeline a live LLM-driven task uses - authority does not carry over, so
  a step that needed approval the first time needs it again. Delegated and
  parallel-batch calls are excluded from the replay plan for now, since
  reissuing those needs different handling than a single call; a step that
  fails or is denied on replay blocks the same way it would live, and a
  timeout or rate-limit is reissued up to three times before giving up
  rather than looping forever. Built as a thin scripted-decision layer over
  the existing operator loop rather than a second execution engine, so it
  needed no changes to `ToolExecutor`, `PolicyEngine`, or verification.
- A task receipt and its trace steps now say plainly when nothing was
  mechanically re-checked, instead of quietly showing nothing. Only one
  tool (`filesystem.manage`'s `apply_manifest`) has a `verify()` hook
  today, so a receipt whose calls all succeeded through some other tool
  previously rendered identically to one where every check came back
  clean - "checked, all good" and "never checked" looked the same because
  the verification line only appeared when `checked > 0`. The receipt's
  `verification` now also reports `not_checked` (succeeded calls whose
  tool had no hook) and `unverified_tools`, rendered as a neutral "Not
  verified - no automatic check yet for ..." line rather than the
  destructive/success styling used for an actual check result. Each trace
  step carries the same distinction (a new `verification` field, joined
  onto `operator_history` the same way `content_trust` already is), shown
  as a "not verified" badge next to a succeeded step whose tool has no
  hook. The reliability dashboard gained matching fields -
  `checked_completed`/`checked_completed_pct`/`verification_coverage_pct`
  and a per-tool `not_checked` count - so `verified_completed_pct` no
  longer has to be read without knowing whether its denominator was even
  checkable.
- Replaying a task no longer blindly reissues a timed-out consequential
  write. A `RATE_LIMITED` result is a clean pre-execution rejection -
  nothing ran yet, so reissuing is always safe - but a `TIMEOUT` on a
  filesystem/terminal/desktop/browser-control/VS Code write (the same
  "consequential" test `reconcile_orphaned_tasks` already applies to a
  crashed worker's in-flight call) means the call may have reached the far
  end before the connection was lost, and replay had no equivalent of the
  warning a live task's history entry gets in that situation. It now stops
  and asks instead of guessing: establishing that a reissue is safe would
  mean asking the tool's own `verify()` hook, but every hook today
  (`filesystem.manage`'s `apply_manifest` included) reads the completed
  call's reported output to decide what to re-check, and a timeout never
  produced one - so a consequential write's timeout now blocks the replay
  on the first occurrence, naming the step and why, rather than retrying a
  few times first. A tool no longer in the registry is treated the same
  fail-safe way. Reads and other low-risk calls are unaffected - a timeout
  there still reissues, bounded the same as before.
- Three more tools mechanically re-check their own effect on success
  instead of leaving `ToolCallResult.verification` as None
  (`filesystem.manage`'s `apply_manifest` was previously the only one):
  `code.interpreter` re-reads the workspace after `run_python`/
  `generate_and_run`/`solve_once`/`build_temp_helper`/`repair_script` to
  confirm every file the adapter's own before/after snapshot diff claimed
  as created or modified actually exists, and every file it claimed
  deleted does not; `document.manage` re-opens a `create_presentation`/
  `update_presentation`'s output `.pptx` (a zip) and counts its real
  `ppt/slides/slideN.xml` entries to confirm the claimed `slide_count`
  matches what is actually in the file, not just that a file with that
  name exists; `workspace.manage` re-reads the workspace after
  `prepare`/`write_files`/`materialize_static_app`/`launch_static`/
  `web_app_preview` the same way `code.interpreter` does. Chosen as the
  next-highest-frequency previously-unverified tools after the receipt's
  new `not_checked`/`unverified_tools` fields made that gap visible per
  call instead of only in the aggregate.
- A completion obligation can now be declared instead of only inferred.
  `MessageClassification` gained an optional `expected_postconditions`
  field the Concierge can populate at intake, before any tool has run;
  `orchestration/fulfillment.py`'s `expected_postconditions()` prefers a
  non-empty declaration over its own keyword-matched guess at the
  objective/original message text. Purely additive: the field is
  transmitted through the structured-output JSON schema, not embedded in
  prompt text, so it changed no `system_prompt`/`user_prompt` string and
  needed no scenario fixture re-record - every recorded fixture and every
  live classifier today simply omits it, which the fallback treats
  identically to "nothing declared". A hallucinated or unrecognized value
  is dropped rather than failing the whole classification, at both the
  schema boundary and the fulfillment lookup. Keyword inference is
  unchanged and remains the operative path until a classifier is actually
  updated to populate the field.
- Untrusted content is now fenced in the Operator prompt itself, not just
  labeled on the trace/evidence views. `ToolDefinition.operation_content_trust`
  (`browser.open`, `browser.control`'s page-reading operations,
  `http.request`, `web.search`, `mcp.client`'s `call_tool`,
  `document.manage`'s read operations) now reaches `worker.py`'s recorded
  history entry, and `_format_history` wraps such an entry's output in an
  explicit `[UNTRUSTED CONTENT ...]` / `[END UNTRUSTED CONTENT]` pair
  telling the model to read it as data and not follow anything it says.
  The wrapped content is neutralized against a payload that contains a
  literal copy of either marker first, so it cannot forge a fake boundary
  and make later content look like it's back outside the fence. This is a
  textual mitigation, not a guarantee - it does not prove a live model
  declines a given injection attempt, only that the boundary is explicit
  and cannot be trivially spoofed from inside the content itself.
  2 of 16 scenario fixtures needed new entries because this changed
  `_format_history`'s rendered text where untrusted content appeared
  earlier in a task's history; both were re-keyed by reversing the fence
  transformation on the newly-requested prompt to find the corresponding
  old fixture entry and copying its response forward, since the fence
  changes the prompt's presentation but not the correct decision - no live
  LLM time was needed.
- A completed task's replay plan can now be saved as a named, reusable
  "workflow" (new "Save as workflow" button on the trace page, alongside
  Replay) and run again later against different values, through the
  identical approval/retry/verification pipeline replay already provides.
  Saving lets specific literal values in the plan be named as parameters
  (e.g. a folder path becomes `{{folder}}`); running fills them back in.
  Not a workflow designer: the plan is always `build_replay_plan`'s real
  output from one actual run, and the only editing surface is naming
  values already present in it. Refuses to save (400, naming the exact
  steps) when the plan can't be fully captured - a delegated sub-task or a
  parallel-batch member `build_replay_plan` itself excludes - rather than
  silently saving a workflow missing part of what the source task did.
  New `workflows` table/repository, `TaskWorkflow` schema, and
  `POST /api/tasks/{id}/save_workflow`, `GET/DELETE /api/workflows[/{id}]`,
  `POST /api/workflows/{id}/run` endpoints. New Workflows page (reachable
  from the Agent hub) lists, runs, and deletes saved workflows - no visual
  workflow editor, matching this project's stated non-goal of a builder
  over a pipeline that isn't graph-shaped.
- A new Security review page (`GET /api/security-review`, linked from
  Access) reports this machine's actual exposure: whether the server is
  bound beyond loopback (`is_loopback_host`, the same test config.py's own
  auth gate uses) and whether an admin token is actually set; every
  currently-active "Allow for this task" grant across every task; which
  enabled capabilities require no approval at all; configured MCP servers;
  distinct external hosts contacted in the window (the same
  `EGRESS_CONTACTED` audit events receipts already read, aggregated
  across every task instead of one); and how many `code.interpreter` runs
  actually executed unsandboxed versus sandboxed. Every fact is read from
  configuration or a recorded row - nothing here probes the network or
  spawns Docker to check live, so the page loads as fast as any other and
  cannot hang waiting on either. A page, not a CLI flag, on the reasoning
  that a first-time user already has this console open and a terminal
  command they'd have to know exists reaches nobody who doesn't already
  know to look.

### Changed

- Documentation consolidated. The README is now a short front door (install, the first ten
  minutes, the five-stage promise stated without over-claiming). `docs/LOCAL_SETUP.md` became
  `docs/INSTALL.md`, a new `docs/USING.md` tours the console and absorbs the old end-to-end
  check, `docs/ROLES.md` folded into `docs/ARCHITECTURE.md`, and `docs/GAPS.md` into
  `docs/ROADMAP.md` ("Known limits"). The history, UI plan, UI audit, and E2E findings moved to
  `docs/archive/`, and every reference to them in code comments was repointed.
- Corrected statements that did not match the code: Architecture said parallel batches could not
  ask for approval (they ask once, for the whole list), counted the live E2E suite as 11 cases
  (there are 28), omitted `web.search` and `dependencies.install`, and linked a heading that did not
  exist; the console and README said WhatsApp could be connected from Settings, which has no
  WhatsApp card (it is configured in `config/config.yaml`, and the channel note now says so).
- `docs/THREAT_MODEL.md`'s pre-publication checklist is now a status table of the repository's
  actual protections.
- The Markdown link check also validates `#heading` anchors, which found and fixed six dead
  table-of-contents links in the archived history.
- Release archives now include the top-level docs, `SECURITY.md`, and `CONTRIBUTING.md`, so the
  README's links work in an installed copy.

## [0.1.3] - 2026-08-11

### Fixed

- The publish-only release job now targets `oney-erge/YBM` explicitly after
  downloading artifacts, so it does not require a local Git checkout to create
  the release page.
- Artifact upload/download actions use their current Node.js 24-based releases,
  removing the hosted-runner Node.js 20 deprecation warning.

## [0.1.2] - 2026-08-11

### Changed

- The one-command Windows and macOS/Linux installers now download the complete
  latest release instead of source archives, so the admin console works without
  Node.js. Re-running an installer refreshes application files while preserving
  local state.
- Release archives use stable `YBM-windows.zip` and `YBM-unix.tar.gz` names so
  installers and people can download the latest build directly.
- Installation is presented as two three-step Windows choices and one
  three-step macOS/Linux path; advanced and development routes moved out of
  the README's critical path.
- The MSI now has a visible minimal install UI and a final **Launch YBM now**
  action. Release CI installs the MSI, provisions its runtime, checks backend
  health, stops it, and uninstalls it instead of treating a successful build as
  proof that installation works.
- Windows releases include `Install-YBM.bat` for a no-MSI double-click path and
  `Install-YBM.ps1` for direct inspection or terminal use. Downloaded archives
  are checked against the release's SHA256 list before extraction.
- Tagged releases publish the tested headless image as
  `ghcr.io/oney-erge/ybm:<version>` and `ghcr.io/oney-erge/ybm:latest`.

### Removed

- Superseded planning archives, stale first-run screenshots, and unused logo
  explorations. Git history remains the record for those design iterations.

## [0.1.1] - 2026-08-11

### Added

- **Containerised headless profile.** `Dockerfile`, `docker-compose.yml` and
  `.dockerignore`. Telegram/WhatsApp intake, the operator loop, the code
  interpreter, MCP and the admin console all run in a container; desktop
  control, screenshots and the VS Code bridge cannot, and `ybm doctor` now
  reports them as unavailable rather than failing at call time.
- `ybm start --foreground`, which supervises until a service exits or a signal
  arrives. `start_all` spawns detached children and returns - correct for an
  interactive start, and an immediate exit to a container or systemd.
- **A signed-off install path on every platform.** `.github/workflows/release.yml`
  builds the admin console, stages a runtime payload
  (`scripts/package_release.py`), compiles a per-user MSI
  (`packaging/windows/ybm.wxs`, WiX v5), and publishes the installer plus a
  `.zip` and a `.tar.gz` with checksums and Sigstore build provenance on a tag.
  None of them needs administrator rights or Node.js, because the console ships
  prebuilt - previously a source install had no console at all until the user
  installed Node 22.22+ themselves.
- **`ybm.sh`**, the macOS/Linux counterpart to `YBM.bat`. One file that installs
  whatever is missing - `uv` included - then starts the stack and opens the
  console, idempotently. macOS and Linux previously had no equivalent: the
  documented path was `scripts/install.sh` followed by
  `./backend/.venv/bin/ybm start --open`, two commands where one only works
  after the other. The Python CLI cannot fill that gap because it lives inside
  the virtualenv it would have to create.
- winget manifest templates (`packaging/winget/`) plus
  `scripts/render_winget_manifests.ps1`, which hashes the published installer
  rather than trusting a number copied out of a build log.
- `--dry-run`, `--verify`, `--no-prompt` and `--install-dir` on both installers.
- Scheduled daily dependency audit (`.github/workflows/security-audit.yml`)
  covering the Python and all Node lockfiles; it opens an issue rather than a
  pull request.
- Optional `.pre-commit-config.yaml` running the same ruff and gitleaks checks
  CI runs.
- CI coverage for the frontend, WhatsApp sidecar, packaged container assets,
  and Node dependency audits.
- `error_text.describe_exception`, so an error a human reads is never empty.
- `harness.assert_rejected`, which refuses to let a replay miss pass as a
  policy refusal.

### Changed

- **One file to double-click, first run and every run.** `ybm setup` installs
  `uv` when it is missing (`Install-YbmUv` in `scripts/lib/common.ps1`) instead
  of refusing to continue, so `YBM.bat` handles a cold machine on its own. That
  refusal was the entire reason a separate first-run entry point had to exist.
  `scripts/install.ps1` no longer bootstraps uv either; it fetches the source
  and hands off, leaving one implementation rather than two that can drift.
- **`scripts/install.sh` hands off to `./ybm.sh`**, the way `install.ps1` hands
  off to `ybm.ps1 run`. Both installers now do the same small job - get the code
  onto the machine - and the launcher owns uv, the virtualenv, setup, and start.
  install.sh also stopped installing the developer extras (pytest, telethon,
  ruff) for people who only wanted to run YBM; that is the documented
  `uv sync --extra test --extra dev` line, as on Windows.
- **Installers require nothing preinstalled.** The Python 3.12+ gate is gone -
  `uv` is a standalone binary and provides the interpreter. git is optional,
  with an archive fallback. The uv installer URL is pinned to a version.
- `.mcp.json` launches the MCP server through `uv run` instead of a bare
  `python` with a relative `PYTHONPATH`, which only resolved on a machine that
  happened to have a system Python carrying the dependencies.
- The first-run wizard preselects a recommended Ollama model, and distinguishes
  "Ollama running with nothing pulled" from "no Ollama" - previously identical
  states, and the only point in onboarding that sent the user elsewhere.
- Scenario fixtures are rebuilt rather than merged when re-recording, dropping
  roughly 4,500 lines of unreachable keys.
- The headless image now packages the WhatsApp Node runtime, production
  dependencies, bundled starter skills, and project license.
- Container base images and the optional Ollama service are pinned to reviewed
  manifest digests.

### Removed

- `YBM-Setup.cmd`. `YBM.bat` now covers the first run too, so a second
  double-clickable file with a different name was one more thing to explain and
  one more way to pick the wrong one.

### Fixed

- **Credential redaction missed two shapes.** A quoted key (`"api_key": "…"`,
  i.e. any JSON config) was never matched, and an unquoted value stopped at the
  first space, so a passphrase redacted to `*** horse battery staple`. The
  scrubbed answer is now written to the task row as well as the audit sink and
  the outbound message.
- Two harness defects that made every negative scenario test pass vacuously: a
  pytest `tmp_path` counter that changed each run so recorded keys could never
  be hit again, and `sort_keys=True` reordering recorded payloads so replay fed
  tools a differently-ordered dict than recording did.
- Workspace recovery from a user's message only ever matched Windows drive
  letters, so it was dead code on Linux and macOS.
- A test set `os.name` on the real `os` module, flipping `pathlib` to the
  Windows flavour process-wide and breaking every later `Path()` on POSIX.
- The anti-fabrication guard was disabled task-wide by any earlier write; it now
  compares claimed filenames against recorded ones.
- `filesystem.manage`'s desktop alias enumerated a directory that was not an
  allowed root - the one operation bypassing `_safe_path`.
- Admin token comparison is constant-time; scope matching refuses a `..`
  segment; three type-narrowing `assert`s that `python -O` strips became real
  raises.
- `pypdf` 6.14.2 → 6.15.0 (CVE-2026-71852, CVE-2026-71870).
- Artifact downloads no longer put the long-lived admin token in a URL. They
  use short-lived artifact-scoped grants, and active HTML/SVG content is forced
  to download instead of executing on the admin origin.
- Browser responses now carry a restrictive content security policy and
  framing, referrer, permissions, opener, resource, and MIME-sniffing headers.
- Frontend `nanoid` was updated past GHSA-2v37-7h3g-55p8.
- Onboarding now supports backward navigation, describes the default capability
  policy accurately, and avoids duplicated setup/safety banners on small screens.

### Known issues

See `docs/ROADMAP.md`.
