"""The ladder: what a task has already proven, and what to try next when a step found nothing or failed.

A small local model deciding whether to search again, write a script, or give up is the weak link of a long task. It repeats a
search with a new spelling five times, or says "not found" after one failed read, or reports a file written to the folder it was
asked for when the tool wrote it somewhere else. None of that needs a better model; it needs something that reads the task's own
history and knows facts. Everything here is plain code over that history, shown to the model as a check entry (the channel the
stuck and audit checks already use), never as a change to its prompt.

The rungs, in order: the tool for the job, a wider built-in search (system.inventory walks every folder it may read and says how
much it covered), a short Python script run on this computer (code.interpreter), an offer to hand the task to Claude Code (only
with the user's yes), and an honest "I could not". Nothing in this module runs a tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Any

from agent_control.orchestration.request_facts import RequestFacts
from agent_control.tools.filesystem_manage import SEARCH_STOPPED_MARK
from agent_control.tools.system_inventory import NOT_ANYWHERE_MARK

CODE_TOOL = "code.interpreter"
FILESYSTEM_TOOL = "filesystem.manage"
INVENTORY_TOOL = "system.inventory"

NOT_FOUND_ERROR = re.compile(
    r"does not exist|doesn't exist|not found|no such file|could not find|couldn't find|cannot find|can't find|unable to find|"
    r"no deliverable artifact or file path was found",
    re.IGNORECASE,
)
_LOCKED = re.compile(r"access is denied|winerror 5|permissionerror|permission denied", re.IGNORECASE)
_EMPTY_RESULT = re.compile(r"^(?:No file matching|Found 0 )", re.IGNORECASE)
_DRIVE_PATH = re.compile(r"[A-Za-z]:[\\/]")

#: (tool, operation) pairs that look something up, so that coming back empty from several of them means the way of looking is wrong.
LOOKUPS = frozenset({
    (INVENTORY_TOOL, "find_files"), (FILESYSTEM_TOOL, "search"), (FILESYSTEM_TOOL, "find_by_description"),
})
_READS = frozenset({"read_file", "describe_file", "describe_folder", "inspect_folder"})


@dataclass(frozen=True)
class Hint:
    id: str
    text: str


def _operation(step: dict[str, Any]) -> str:
    tool_input = step.get("input")
    return str(tool_input.get("operation") or "") if isinstance(tool_input, dict) else ""


def _failed(step: dict[str, Any]) -> bool:
    return str(step.get("status") or "") in {"failed", "denied"}


def _found_nothing(step: dict[str, Any]) -> bool:
    if (step.get("tool_name"), _operation(step)) not in LOOKUPS or str(step.get("status") or "") != "succeeded":
        return False
    return bool(_EMPTY_RESULT.search(str(step.get("output_summary") or "").lstrip()))


def _norm(text: str) -> str:
    return " ".join(str(text).replace("/", "\\").casefold().split())


def _named_in(request: str, path: str) -> bool:
    """Whether the request's own words name this path (whole, or its file name when that is distinctive)."""
    if not path:
        return False
    wanted = _norm(path)
    asked = _norm(request)
    if wanted in asked:
        return True
    name = PureWindowsPath(path).name.casefold()
    return len(name) >= 5 and "." in name and name in asked


def _names_place(asked: str, place: str) -> bool:
    """Whether the (normalised) request names exactly this place, not merely the start of a longer path."""
    return re.search(re.escape(_norm(place)) + r"(?![\\\w-])", asked) is not None


def proven_absent(request: str, steps: list[dict[str, Any]]) -> str | None:
    """One sentence saying what is proven not to be there, or None.

    Two things prove it. A read of the very path the request names that failed because it does not exist: a path either exists or
    it does not, and no search elsewhere changes that. And a name search that went through every folder it may read and finished
    (system.inventory says so in the sentence it ends with), for a name made of words the request itself uses.
    """
    for step in steps:
        if step.get("tool_name") == FILESYSTEM_TOOL and _failed(step) and _operation(step) in _READS:
            path = str((step.get("input") or {}).get("path") or (step.get("input") or {}).get("root") or "")
            if NOT_FOUND_ERROR.search(str(step.get("error") or "")) and _named_in(request, path):
                return f"{path} does not exist."
    asked = _norm(request)
    for step in steps:
        summary = str(step.get("output_summary") or "")
        if _found_nothing(step) and NOT_ANYWHERE_MARK in summary:
            query_words = re.findall(r"[a-z0-9]+", str((step.get("input") or {}).get("query") or "").casefold())
            if query_words and all(word in asked for word in query_words):
                # Only what was covered: the "No file matching" part repeats what the answer already says.
                cut = summary.find(" I looked through")
                return summary[cut:].strip() if cut >= 0 else summary.strip()
    return None


def write_receipt_gap(request: str, changed_paths: list[str]) -> str | None:
    """A sentence when every file the task changed is outside every location the request names.

    "Write my cat's name into cat.txt in C:\\work" ended with the file in the repository root and an answer that named C:\\work,
    because the tool reported where it wrote and the answer was written from the request. The answer has to be built from the
    receipt. Only a request that names a drive path is checked, and only when none of the changed files is inside a named place.
    """
    if not changed_paths or not _DRIVE_PATH.search(request):
        return None
    asked = _norm(request)
    for changed in changed_paths:
        parents = PureWindowsPath(changed).parents
        if any(len(parent.parts) > 1 and _names_place(asked, str(parent)) for parent in parents) or _names_place(asked, changed):
            return None
    shown = ", ".join(changed_paths[:3])
    return (
        f"the request names a place on this computer, but what you changed is elsewhere: {shown}. If the user meant the place "
        "they named, write the file there; if you meant to put it somewhere else, say plainly where it is, and do not claim "
        "the place they named."
    )


_SCOPE = re.compile(r"\.([A-Za-z0-9]{2,5})\s+(?:files?|images?|documents?|pictures?|photos?)\b", re.IGNORECASE)


def move_scope_gap(request: str, undo_moves: dict[str, Any] | None) -> str | None:
    """A sentence when a move touched files of a kind the request did not name.

    "Move the .png files into pictures. Leave everything else where it is." was done by a plan that sorted the whole folder by type,
    which also moved the text file. When the request names the kinds of file it is about (".png files"), any moved file of another
    kind is a mistake, and the record of where it came from says how to put it back.
    """
    named = {match.group(1).casefold() for match in _SCOPE.finditer(request)}
    if not named or not isinstance(undo_moves, dict):
        return None
    strays = [
        move for move in undo_moves.get("moves") or []
        if isinstance(move, dict) and PureWindowsPath(str(move.get("from") or "")).suffix.lstrip(".").casefold() not in named
    ]
    if not strays:
        return None
    back = "; ".join(f"{move['from']} back to {move['to']}" for move in strays[:4])
    return (
        f"the request is about .{', .'.join(sorted(named))} files only, but you also moved other files. Move them back "
        f"(filesystem.manage apply_manifest, one move each): {back}. Then report what is where."
    )


CODE_SEARCH_TEXT = (
    "The built-in searches have not found it. Do not search again with another spelling. You can write and run a short Python "
    "program on this computer: call code.interpreter with operation run_python and put the Python in `code`. It can read every "
    "folder and needs no approval. Stop it after about two minutes and print how much it covered, so the answer can say how hard "
    "you looked. For example, to find a file by part of its name, other drives first and the big C: drive last:\n"
    "import os, string, time\n"
    "needle = 'part of the name'.lower()\n"
    "drives = [l + ':\\\\' for l in string.ascii_uppercase if os.path.exists(l + ':\\\\')]\n"
    "tops = [d for d in drives if d != 'C:\\\\'] + [os.path.expanduser('~'), 'C:\\\\']\n"
    "start, folders = time.time(), 0\n"
    "for top in tops:\n"
    "    for folder, dirs, files in os.walk(top, onerror=lambda error: None):\n"
    "        folders += 1\n"
    "        dirs[:] = [d for d in dirs if d not in ('node_modules', '$Recycle.Bin', 'Windows')]\n"
    "        for name in files:\n"
    "            if needle in name.lower():\n"
    "                print(os.path.join(folder, name))\n"
    "        if time.time() - start > 120:\n"
    "            raise SystemExit('stopped after 120 seconds and ' + str(folders) + ' folders')\n"
    "print('looked through', folders, 'folders')"
)

FIND_FILES_TEXT = (
    "That search ran out of time inside one big folder. Use system.inventory with operation find_files and the file name as the "
    "query: it looks at the top folders of every drive first and says how much it covered."
)

TRY_CODE_TEXT = (
    "Several steps in a row did not work. If the tools above cannot do this, you can write a short Python program and run it "
    "with code.interpreter (operation run_python, the program in `code`). It runs on this computer, can read and write any "
    "folder, and needs no approval. Print what it did, so the answer comes from what it printed."
)


# --- web forms: passwords, boxes, CAPTCHAs, confirmation emails ----------------------------------------------------------------------

BROWSER_TOOLS = frozenset({"browser.open", "browser.control"})
_PASSWORD_FIELD = re.compile(r"\bpassword\b", re.IGNORECASE)
_TERMS_BOX = re.compile(r"\b(?:terms|privacy policy|agree to|i accept|accept the)\b", re.IGNORECASE)
_CAPTCHA = re.compile(r"captcha|not a robot|verify (?:that )?you(?:'re| are) (?:a )?(?:human|person)|are you (?:a )?human|security check", re.IGNORECASE)
_EMAIL_CONFIRMATION = re.compile(
    r"(?:confirm|verify)\w*\s+(?:your\s+)?(?:e-?mail|address|account)|check your (?:e-?mail|inbox)|(?:sent|emailed)\b[^.\n]{0,60}(?:link|e-?mail)|"
    r"activation (?:link|e-?mail)|confirmation (?:link|e-?mail)",
    re.IGNORECASE,
)

PASSWORD_TEXT = (
    "This page asks for a password. Do not make one up, and do not write it in a file or in your answer. Fill every password field "
    "(the one that asks for it and the one that asks it again) with the value {{new_password}}: YBM makes a long random password, keeps "
    "it in its encrypted vault for this site, types it in, and never shows it to you. When you answer, say the account was made and "
    "that the password is saved in YBM's vault; the user can say 'show my <site> password' to see it. Fill the whole form in ONE browser.control "
    "call (operation fill_form, every field in `fields`, the terms box too if there is one, and \"submit\": true), so it is sent in that step: "
    "filling it piece by piece loses track of what is done and never sends it."
)
LOGIN_TEXT = (
    "This page asks for a login. If the user has an account here that YBM made, fill the username field with {{saved_username}} and "
    "the password field with {{saved_password}}: the vault types the saved values and you never see the password. If you get 'no login "
    "is saved', say so and ask the user for the login (action ask_user)."
)
TERMS_TEXT = (
    "The form has a box about agreeing to terms. The user asked for this account, so tick it: use the box's label as the key and true as "
    "the value (for example \"I agree to the terms of service\": \"true\"), and say in your answer that you did."
)
CAPTCHA_TEXT = (
    "This page asks to prove that you are a person. Do not try to get around it. Send the user a screenshot of the page (artifact.deliver, "
    "operation send_screenshot), then ask them one short question: that they solve it in the browser window that is open, and tell you when "
    "it is done (action ask_user). Carry on from there when they answer."
)
EMAIL_CONFIRMATION_TEXT = (
    "The site says it sent an email to confirm the address. You cannot read the user's mail. Finish now (action done): say the account is made "
    "but not active until the user opens that email and clicks the link, and that the password is saved in YBM's vault."
)


def _browser_page(step: dict[str, Any]) -> str | None:
    """What the page said, when the last step was the browser and it worked."""
    if step.get("tool_name") not in BROWSER_TOOLS or str(step.get("status") or "") != "succeeded":
        return None
    return str(step.get("output_summary") or "")


def _typed_a_placeholder(steps: list[dict[str, Any]]) -> bool:
    for step in steps:
        tool_input = step.get("input")
        fields = tool_input.get("fields") if isinstance(tool_input, dict) else None
        if isinstance(fields, dict) and any("{{" in str(value) and "password" in str(value) for value in fields.values()):
            return True
    return False


RENAME_TEXT = (
    "organize_plan only sorts files into folders (by type); it does not rename anything, so asking for it again will not help. To give "
    "files new names call filesystem.manage with operation apply_manifest, the folder as root, and a manifest with one entry per file: "
    "{\"source\": <the file's full path>, \"destination\": <the new full path in the same folder>, \"operation\": \"rename\"}. Take the file "
    "names from the listing you already have, build every new name the way the request says, and apply them all in one call."
)


MOVE_TEXT = (
    "The user named the folder and the files to move, so do not use organize_plan: it sorts every file in the folder into folders it names "
    "itself, by type. Call filesystem.manage with operation apply_manifest, the folder as root, and a manifest with one entry for each "
    "file the request names and no other: {\"source\": <the file's name>, \"destination\": <the folder the user named>}. Files the "
    "request does not name stay where they are."
)


MONEY_TEXT = (
    "You cannot buy or order anything for the user: you have no payment method and no shop accounts set up, and you must not enter payment "
    "details or press a buy or order button. Do not go on with this and do not ask a question first. Finish now (action done) with one or "
    "two plain sentences: you cannot place orders or pay, and you can look up the place's website, menu or phone number, or open its page "
    "so they can finish it themselves, if they tell you which place."
)


def money_hint(request: str, steps: list[dict[str, Any]], given: set[str]) -> Hint | None:
    """A request to buy, order or book something, said once when the model has started to browse for it: a model that can browse will try."""
    if "money" in given or not any(step.get("tool_name") in BROWSER_TOOLS for step in steps) or not RequestFacts.from_text(request).spends_money:
        return None
    return Hint("money", MONEY_TEXT)


def move_hint(request: str, steps: list[dict[str, Any]], given: set[str]) -> Hint | None:
    """A request to move some files into a folder it names, once the model has looked at files and before it sorts them all by type."""
    if "move" in given or not any(step.get("tool_name") == FILESYSTEM_TOOL for step in steps):
        return None
    if any(_operation(step) == "apply_manifest" for step in steps) or not RequestFacts.from_text(request).moves_into_a_named_folder:
        return None
    return Hint("move", MOVE_TEXT)


def rename_hint(request: str, steps: list[dict[str, Any]], given: set[str]) -> Hint | None:
    """A request to rename files, after a plan that only sorts them into folders."""
    last = steps[-1]
    if "rename" in given or (last.get("tool_name"), _operation(last)) != (FILESYSTEM_TOOL, "organize_plan"):
        return None
    if str(last.get("status") or "") != "succeeded" or not RequestFacts.from_text(request).renames:
        return None
    return Hint("rename", RENAME_TEXT)


def form_hint(request: str, steps: list[dict[str, Any]], given: set[str]) -> Hint | None:
    """What the page in front of the model asks for, when the request is to make an account or to log in. Each is said once."""
    page = _browser_page(steps[-1])
    if page is None:
        return None
    facts = RequestFacts.from_text(request)
    if _CAPTCHA.search(page) and "captcha" not in given:
        return Hint("captcha", CAPTCHA_TEXT)
    if facts.wants_a_new_account and _EMAIL_CONFIRMATION.search(page) and _typed_a_placeholder(steps) and "email_confirmation" not in given:
        return Hint("email_confirmation", EMAIL_CONFIRMATION_TEXT)
    if _PASSWORD_FIELD.search(page) and not _typed_a_placeholder(steps):
        if facts.wants_a_new_account and "password" not in given:
            return Hint("password", PASSWORD_TEXT)
        if facts.wants_to_log_in and not facts.wants_a_new_account and "login" not in given:
            return Hint("login", LOGIN_TEXT)
    if facts.wants_a_new_account and _TERMS_BOX.search(page) and "terms" not in given:
        return Hint("terms", TERMS_TEXT)
    return None


TALLY_TEXT = (
    "You are adding up what several files hold, and a model that adds in its head gets it wrong (4 + 5 + 6 came out as 12). Do not add or "
    "count by yourself. Run code.interpreter once: a short Python program that reads the files (os.walk covers every subfolder), works the "
    "total out and prints it, and answer with the number it printed."
)
_CONTENT_READS = frozenset({"read_file", "extract_text", "inspect_document", "describe_file"})


def tally_hint_due(request: str, steps: list[dict[str, Any]]) -> bool:
    """A request to add up or average, and two or more files already read: the sum is arithmetic for code, not for the model."""
    reads = sum(1 for step in steps if step.get("status") == "succeeded" and _operation(step) in _CONTENT_READS)
    return reads >= 2 and RequestFacts.from_text(request).tallies


def next_hint(
    request: str,
    steps: list[dict[str, Any]],
    given: set[str],
    *,
    code_enabled: bool,
    inventory_enabled: bool = False,
) -> Hint | None:
    """The one thing worth telling the model now, or None. Each hint is given once per task."""
    if not steps:
        return None
    last = steps[-1]
    form = (
        money_hint(request, steps, given) or form_hint(request, steps, given) or rename_hint(request, steps, given)
        or move_hint(request, steps, given)
    )
    if form is not None:
        return form
    absent = proven_absent(request, steps)
    if absent and "absent" not in given:
        return Hint(
            "absent",
            f"That is settled: {absent} Do not search for it again. Tell the user plainly that it is not there (action done), "
            "saying what you looked at; you may add the closest names one look at the parent folder shows.",
        )
    empty = [step for step in steps if _found_nothing(step)]
    if empty and NOT_ANYWHERE_MARK in str(empty[-1].get("output_summary") or "") and "complete" not in given and not absent:
        return Hint(
            "complete",
            "That search went through every folder you can read and finished, so what it looked for is not there. Do not run "
            "it again with another spelling. Say so plainly (action done), and offer one next step: ask whether it could be on "
            "another drive or in the cloud, or whether the name is spelled differently.",
        )
    if _failed(last) and _LOCKED.search(str(last.get("error") or "")) and "locked" not in given:
        return Hint(
            "locked",
            "Windows would not let that step open a folder. Do not try that folder again; leave it out and carry on with the "
            "rest. A Python program run with code.interpreter can skip folders it cannot open.",
        )
    used_code = any(step.get("tool_name") == CODE_TOOL for step in steps)
    if code_enabled and not used_code and "tally" not in given and tally_hint_due(request, steps):
        return Hint("tally", TALLY_TEXT)
    incomplete_walk = any(
        _found_nothing(step)
        and ("does not prove" in str(step.get("output_summary") or "") or SEARCH_STOPPED_MARK in str(step.get("output_summary") or ""))
        for step in steps
    )
    used_find_files = any((step.get("tool_name"), _operation(step)) == (INVENTORY_TOOL, "find_files") for step in steps)
    if inventory_enabled and not used_find_files and "find_files" not in given and incomplete_walk:
        return Hint("find_files", FIND_FILES_TEXT)
    if code_enabled and not used_code and "code_search" not in given and (len(empty) >= 2 or incomplete_walk):
        return Hint("code_search", CODE_SEARCH_TEXT)
    recent_failures = sum(1 for step in steps[-5:] if _failed(step))
    if code_enabled and not used_code and "try_code" not in given and recent_failures >= 3:
        return Hint("try_code", TRY_CODE_TEXT)
    return None


def exhausted(steps: list[dict[str, Any]]) -> bool:
    """The local model has used the rungs it has: a script was tried and did not settle it, or six steps came to nothing."""
    code_steps = [step for step in steps if step.get("tool_name") == CODE_TOOL]
    code_failed = sum(1 for step in code_steps if _failed(step))
    if code_failed >= 2:
        return True
    if code_steps and _failed(code_steps[-1]) and sum(1 for step in steps if _failed(step) or _found_nothing(step)) >= 5:
        return True
    return sum(1 for step in steps if _failed(step) or _found_nothing(step)) >= 8


FEASIBILITY_TEXT = (
    "Before you ask the user a question, check it is the right one. If it is about which of several things you found, or a detail "
    "that blocks work you can really do, ask it. If you are asking because you cannot actually do what was asked (ordering food, "
    "phoning someone, buying something, anything that needs an account or a service you cannot reach), do not ask for details: say "
    "plainly in one or two sentences that you cannot do that, and offer the nearest thing you can do (action done). If you can do "
    "it with your tools, or with a short Python program run with code.interpreter, call one now."
)


def needs_feasibility_check(given: set[str], *, route: object, questions_asked: int) -> bool:
    """The model is about to ask the user its first question, and the classifier found no tool for this request.

    "Order me a pizza from the place on the corner" was answered with questions (which restaurant, what size) about something YBM
    cannot do at all, once straight away and once after a web search that found nothing it could use. A request no tool matches
    is the one most likely to be asked about because it cannot be done, so the model is asked once to check that before the user is.
    """
    return not questions_asked and "feasibility" not in given and str(route or "") in {"unknown", "None", ""}


# --- a path that came from an earlier turn, not from this request ----------------------------------------------------------------------------

_PATH_KEYS = ("path", "root", "folder_path", "file_path", "source", "destination")
_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


def facts_block(memory_context: str | None) -> str:
    """The "Remembered facts" lines of a memory context: things the person asked to be remembered, not the conversation summary."""
    lines: list[str] = []
    collecting = False
    for line in str(memory_context or "").splitlines():
        if line.startswith("Remembered facts:"):
            collecting = True
            continue
        if collecting:
            if line.startswith("- ["):
                lines.append(line)
            else:
                break
    return "\n".join(lines)


def _call_paths(tool_input: object) -> list[str]:
    if not isinstance(tool_input, dict):
        return []
    found = [str(tool_input[key]).strip() for key in _PATH_KEYS if isinstance(tool_input.get(key), str)]
    manifest = tool_input.get("manifest")
    if isinstance(manifest, list):
        for item in manifest[:50]:
            if isinstance(item, dict):
                found += [str(item[key]).strip() for key in ("source", "destination") if isinstance(item.get(key), str)]
    return [path for path in found if _ABSOLUTE.match(path)]


def _grounded_in(corpus: str, path: str) -> bool:
    """The path, a folder above it, or something inside it appears in the text."""
    wanted = _norm(path)
    if wanted in corpus:
        return True
    parents = PureWindowsPath(path).parents
    if any(len(parent.parts) > 1 and _names_place(corpus, str(parent)) for parent in parents):
        return True
    return re.search(re.escape(wanted) + r"[\\]", corpus) is not None


def ungrounded_paths(request: str, tool_input: object, history: list[dict[str, Any]], memory_context: str | None) -> list[str]:
    """Paths a call names that nothing in this task supports: not the request, not any result so far, not a remembered fact.

    A "remember these about me" request was answered by reading a file that an earlier turn of the conversation had named; the
    conversation summary is in the prompt, so the model took it for the task. A person's own words, what this task found, and
    what they asked to be remembered are grounds for a path. An earlier turn is not.
    """
    paths = _call_paths(tool_input)
    if not paths:
        return []
    parts = [request, facts_block(memory_context)]
    for step in history:
        if isinstance(step, dict):
            parts += [str(step.get("output_summary") or ""), str(step.get("error") or ""), str(step.get("input") or "")]
    corpus = _norm(" ".join(parts))
    return [path for path in paths if not _grounded_in(corpus, path)]


UNGROUNDED_TEXT = (
    "Not run: {paths} is not in the user's request, nothing in this task found it, and it is not something they asked to be "
    "remembered. It only appears in earlier conversation. Act on what the user asked just now. If the request really does refer "
    "back to that, say which file you mean and call again."
)


# --- a quoted value no tool returned -----------------------------------------------------------------------------------------------------------

_QUOTED = re.compile(r"[\"\u201c]([^\"\u201c\u201d\n]{4,120})[\"\u201d]")


def ungrounded_quotes(answer: str | None, request: str, steps: list[dict[str, Any]], memory_context: str | None = None) -> list[str]:
    """Quoted strings in an answer that appear in nothing the task saw: not the request, not a remembered fact, not any tool result.

    Asked for "the exact page title" of four sites, the local model answered with "Mozilla Firefox" for a page whose title is
    "Internet for people, not profit - Mozilla Global", and "Python.org" for "Welcome to Python.org". A string put in quotation marks
    is presented as what something said, so it has to be what a tool returned.
    """
    quoted = [" ".join(match.group(1).split()) for match in _QUOTED.finditer(str(answer or ""))]
    if not quoted:
        return []
    parts = [request, facts_block(memory_context)]
    for step in steps:
        if isinstance(step, dict):
            parts += [str(step.get("output_summary") or ""), str(step.get("error") or ""), str(step.get("input") or "")]
    corpus = " ".join(" ".join(parts).split()).casefold()
    seen: set[str] = set()
    missing = []
    for item in quoted:
        key = item.casefold()
        if key not in corpus and key not in seen:
            seen.add(key)
            missing.append(item)
    return missing


UNGROUNDED_QUOTES_TEXT = (
    "Declared done, but {quotes} is in quotation marks in your answer and no tool result contains it. A quoted title, name or "
    "phrase must be copied exactly from a tool result. Look again at what the tool returned and quote that, or say plainly that "
    "you could not find it."
)
