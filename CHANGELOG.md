# Changelog

Notable changes to YBM. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

### Changed

- The operator's rules about the model's habits are three ordered lists in their own modules, not branches in the worker's tick (the worker lost about 780 lines, no behavior changed):
  `orchestration/request_facts.py` reads what a request says once, each fact from the text it always came from;
  `orchestration/call_policy.py` is what happens to a tool call before it runs (refusals, canonical form, fourteen labeled rewrites, in order);
  `orchestration/answer_checks.py` is what a script checks in a `done` answer (a value no tool produced, a sum that does not add up,
  a quotation nothing contains, a receipt that does not match), with how often each may speak. The moved rules keep their names in the
  worker, so existing imports work. `docs/ARCHITECTURE.md` has a table of which module decides what.
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

### Fixed

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

### Changed

- `operator.constrain_calls` is on by default. Measured over the 60-case held-out suite on the local Qwen3.5-9B: calls a tool refused
  fell from 52 of 266 steps (19.5%) to 5 of 150 (3.3%), made-up tool names from 30 to 0, and the held-out pass rate went from 43 to 54 of
  60 together with the other fixes in this release. It applies to local servers only and falls back to the plain schema when a server
  refuses it; set `operator.constrain_calls: false` to turn it off.

### Added

- `operator.constrain_calls` holds the Operator's reply to a JSON schema built from the tool definitions, so a
  tool call with a wrong argument name, an unknown tool or no operation cannot be written. It needs a local server that enforces
  a response schema (llama.cpp, Ollama) and LocalDeploy with its response-schema fix; otherwise the plain schema is used.

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
