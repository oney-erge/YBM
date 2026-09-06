from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from agent_control.schemas import PlanPostcondition, PostconditionType, TaskRecord


@dataclass(frozen=True)
class FulfillmentValidation:
    expected: tuple[PlanPostcondition, ...]
    missing: tuple[PostconditionType, ...]

    @property
    def ok(self) -> bool:
        return not self.missing

    @property
    def first_gap(self) -> str | None:
        if not self.missing:
            return None
        return _gap_reason(self.missing[0])


# Mirrors every description string _postconditions_from_objective already
# uses per type, so a MessageClassification.expected_postconditions
# declaration (schemas.py) reads identically to the keyword-inferred
# fallback it can override - the two are meant to look the same to
# whatever consumes a PlanPostcondition, only their origin differs.
_POSTCONDITION_DESCRIPTIONS: dict[PostconditionType, str] = {
    PostconditionType.WORKSPACE_DIR: "A task workspace directory is reported.",
    PostconditionType.WORKSPACE_FILES: "One or more requested project files were actually produced.",
    PostconditionType.SOURCE_CONTENT: "Requested source-file contents were actually inspected.",
    PostconditionType.PREVIEW_URL: "A local preview URL is reported.",
    PostconditionType.ADAPTER_PROPOSAL: "A generated adapter proposal directory is reported.",
    PostconditionType.ARTIFACT_DELIVERED: "The requested file or artifact was delivered to the user.",
    PostconditionType.SCREENSHOT_DELIVERED: "The requested screenshot was delivered to the source channel.",
    PostconditionType.DOCUMENT_SUMMARY: "A document summary is reported.",
    PostconditionType.PRESENTATION_FILE: "A presentation file (.pptx) is reported.",
    PostconditionType.CODING_AGENT_STEP: (
        "The requested external coding provider reached a reported terminal or resumable state."
    ),
    PostconditionType.SCHEDULE_CREATED: "A schedule ID and next run timestamp are reported.",
    PostconditionType.BROWSER_STATE: "Browser state or page observation is reported.",
    PostconditionType.DESKTOP_OBSERVATION: "A desktop observation or screenshot is reported.",
    PostconditionType.FILE_ORGANIZATION: "Changed, moved, or organized file paths are reported.",
    PostconditionType.TASK_STATUS: "A task status summary is reported.",
    PostconditionType.GITHUB_PR: "A GitHub pull request URL or number is reported.",
    PostconditionType.EXTERNAL_COMMAND: "External command completion is reported.",
}


def _declared_postconditions(task: TaskRecord) -> list[PlanPostcondition]:
    """MessageClassification.expected_postconditions (schemas.py), persisted
    onto task.metadata at creation (channels/base.py) - a declaration made
    by the classifier before any tool ran, from the request alone, the same
    "independent of what actually happened" property _postconditions_from_
    objective's keyword guess has. Not a model self-report about its own
    work: this is set once, at intake, before the Operator loop starts.
    """
    declared = task.metadata.get("expected_postconditions") if isinstance(task.metadata, dict) else None
    if not isinstance(declared, list) or not declared:
        return []
    result: list[PlanPostcondition] = []
    for value in declared:
        try:
            postcondition_type = PostconditionType(value)
        except ValueError:
            continue
        result.append(
            PlanPostcondition(type=postcondition_type, description=_POSTCONDITION_DESCRIPTIONS[postcondition_type])
        )
    return result


def expected_postconditions(task: TaskRecord) -> tuple[PlanPostcondition, ...]:
    """What this task needs to show to count as done, preferring a
    declaration the classifier made at intake (docs/ROADMAP.md "Finish the
    Proof") over inferring it from keywords - explicit beats guessed, when
    something explicit exists.

    Falls back to keyword inference (unchanged) when nothing was declared,
    which today is every task: no live classifier prompt has been changed
    to encourage populating the field yet (see schemas.py's own comment on
    why adding it didn't require one), and no scenario fixture predates it
    either. This is the whole point of an additive, defaulted field - the
    fallback path is not a stub, it is what every existing task still runs.

    There used to be a plan-derived path here that took priority (the LLM's
    own declared `plan.postconditions`, then tool-name-derived rules). Both
    are gone with the plan-once execution path (docs/HISTORY.md P3) -
    nothing creates a PlanModel anymore, so `plan` was always None and those
    branches were unreachable. See docs/HISTORY.md §1.1.

    `task.objective` is the classifier's *paraphrase* of the request, so
    relying on it alone made the keyword fallback depend on the wording a
    model happened to choose: "Create the real files" yields a
    WORKSPACE_DIR obligation, the paraphrase "creating package.json"
    yielded none, and a run that wrote nothing while claiming otherwise
    completed unchallenged (docs/E2E_FINDINGS.md P0-2). The user's own
    message is the stable source of intent, so both are read and the
    results unioned - a paraphrase can add an obligation it makes explicit,
    but can no longer drop one the request already established.
    """
    declared = _declared_postconditions(task)
    if declared:
        return tuple(_dedupe(declared))
    expected: list[PlanPostcondition] = list(_postconditions_from_objective(task.objective))
    original_message = task.metadata.get("original_message_text") if isinstance(task.metadata, dict) else None
    if isinstance(original_message, str) and original_message.strip():
        expected.extend(_postconditions_from_objective(original_message))
    return tuple(_dedupe(expected))


def validate_fulfillment(task: TaskRecord) -> FulfillmentValidation:
    expected = expected_postconditions(task)
    missing = tuple(
        item.type
        for item in expected
        if item.required and not _postcondition_satisfied(task, item.type)
    )
    return FulfillmentValidation(expected=expected, missing=missing)


_EMBEDDED_PATH = re.compile(
    r"[a-z]:\\\S+|(?<![:/])/\S+|\\\\\S+",
    re.IGNORECASE,
)


def _strip_embedded_paths(text: str) -> str:
    """Objectives routinely embed a literal filesystem path ("look in the
    folder C:\\Users\\...\\search_results" or
    "/tmp/.../search_results"). Left in, a path segment that
    happens to contain a trigger word (a folder named "...search", "...app",
    "...schedule") produces a false-positive expected postcondition that
    nothing in the actual task run ever satisfies - this is the sole
    fulfillment signal the Operator loop has, so a false positive means a task
    that genuinely finished loops on a gap it can never close. Strip anything
    path-shaped before keyword matching, not just one trigger word - the
    failure mode is the pattern (words embedded in a path getting matched as
    intent), not any single keyword.
    """
    return _EMBEDDED_PATH.sub(" ", text)


# Verbs whose past/participle form is not a suffix of the base, so the
# generated-inflection rule below cannot reach them.
_IRREGULAR_FORMS: dict[str, tuple[str, ...]] = {
    "build": ("built",),
    "make": ("made",),
    "write": ("wrote", "written"),
    "find": ("found",),
    "run": ("ran",),
    "send": ("sent",),
    "set": (),
    "sort": (),
}


def _inflections(word: str) -> set[str]:
    """Ordinary English inflections of one trigger word.

    Forms are generated from the known trigger vocabulary rather than by
    stemming arbitrary input, so an unrelated word can never collapse onto a
    trigger. That direction matters: a false positive here invents a
    postcondition nothing in the run can satisfy, which makes a genuinely
    finished task loop on a gap it can never close (see `_strip_embedded_paths`).
    """
    forms = {word, f"{word}s", f"{word}ed", f"{word}ing", f"{word}es"}
    if word.endswith("e"):
        stem = word[:-1]
        forms.update({f"{stem}ing", f"{stem}ed", f"{stem}es"})
    if word.endswith("y"):
        stem = word[:-1]
        forms.update({f"{stem}ies", f"{stem}ied"})
    forms.update(_IRREGULAR_FORMS.get(word, ()))
    return forms


def _expand(*words: str) -> set[str]:
    expanded: set[str] = set()
    for word in words:
        expanded |= _inflections(word)
    return expanded


def _postconditions_from_objective(objective: str) -> list[PlanPostcondition]:
    lowered = _strip_embedded_paths(objective).lower()
    words = set(re.findall(r"[a-z0-9]+", lowered))
    # "Start a small app" ordinarily means begin/build the project, especially
    # when delegated to a coding agent. It does not promise a running local
    # server. Reserve PREVIEW_URL for explicit visible-runtime language.
    visible_action = bool(words & _expand("launch", "serve", "open", "preview")) or "show me" in lowered or "url" in words
    app_request = bool(words & _expand("app", "application", "website", "webpage", "html")) or "web page" in lowered
    # "scaffold" belongs here with the other construction verbs: it is the word
    # models reach for when asked to lay down a project skeleton, and without it
    # a scaffolding request carried no completion obligation at all.
    create_action = bool(
        words
        & _expand(
            "create", "build", "write", "make", "add", "implement",
            "generate", "update", "edit", "scaffold", "start",
        )
    )
    # A named output file is fulfilled by its changed/artifact path; it does
    # not imply that a task workspace must also exist. Reserve WORKSPACE_DIR
    # for code/project/app construction where a workspace is itself useful
    # completion evidence.
    workspace_subject = bool(
        words
        & _expand(
            "code", "script", "app", "application", "project", "website",
            "webpage", "html", "plugin", "extension", "addon",
        )
    )
    has_adapter_word = bool(words & _expand("adapter", "tool", "capability", "connector"))
    expected: list[PlanPostcondition] = []
    source_actions = r"read|review|analy(?:ze|se)|summari(?:ze|se)"
    source_subjects = r"files?|documents?|evidence|reports?|resumes?|notes?"
    # Require the content verb and its source to live in the same local
    # phrase. A global word-set cross product made "career evidence ... tell
    # me what review is still required" look like a request to read evidence,
    # leaving a completed adapter scaffold with an impossible SOURCE_CONTENT
    # obligation. Bidirectional proximity still covers "read the file" and
    # "the file ... read it" without joining unrelated clauses.
    source_content_request = bool(
        re.search(rf"\b(?:{source_actions})\w*\b[^.\n]{{0,80}}\b(?:{source_subjects})\b", lowered)
        or re.search(rf"\b(?:{source_subjects})\b[^.\n]{{0,80}}\b(?:{source_actions})\w*\b", lowered)
    )
    inspect_every_file = bool(re.search(r"\b(?:inspect|review|analyze)\s+(?:all|every)\b", lowered)) and bool(
        words & _expand("file", "document", "evidence")
    )
    if source_content_request or inspect_every_file:
        expected.append(
            PlanPostcondition(
                type=PostconditionType.SOURCE_CONTENT,
                description="Requested source-file contents were actually inspected.",
            )
        )
    if visible_action and app_request:
        expected.extend(
            [
                PlanPostcondition(
                    type=PostconditionType.PREVIEW_URL,
                    description="A local preview URL is reported.",
                ),
                PlanPostcondition(
                    type=PostconditionType.WORKSPACE_DIR,
                    description="A task workspace directory is reported.",
                ),
            ]
        )
    elif create_action and workspace_subject and not has_adapter_word:
        expected.extend(
            [
                PlanPostcondition(
                    type=PostconditionType.WORKSPACE_DIR,
                    description="A task workspace directory is reported.",
                ),
                PlanPostcondition(
                    type=PostconditionType.WORKSPACE_FILES,
                    description="One or more requested project files were actually produced.",
                ),
            ]
        )

    coding_provider = bool(words & {"codex", "claude", "copilot"})
    coding_action = bool(
        words
        & _expand(
            "use", "ask", "tell", "run", "start", "build", "create", "write",
            "make", "implement", "scaffold", "fix",
        )
    )
    if coding_provider and coding_action:
        expected.append(
            PlanPostcondition(
                type=PostconditionType.CODING_AGENT_STEP,
                description="The requested external coding provider reached a reported terminal or resumable state.",
            )
        )

    if has_adapter_word and create_action:
        expected.append(
            PlanPostcondition(
                type=PostconditionType.ADAPTER_PROPOSAL,
                description="A generated adapter proposal directory is reported.",
            )
        )
    delivery_action = bool(words & _expand("send", "share", "deliver", "upload", "email"))
    delivery_subject = bool(
        words & _expand("file", "artifact", "document", "report", "brief", "attachment")
    )
    # Keep chat replies distinct from file delivery. "Send me an update" does
    # not need an artifact, while "send me that exact file" does. The latter
    # previously had no durable obligation, so the operator could write a
    # report, move on to scheduling, and finish without attaching it.
    if delivery_action and delivery_subject:
        expected.append(
            PlanPostcondition(
                type=PostconditionType.ARTIFACT_DELIVERED,
                description="The requested file or artifact was delivered to the user.",
            )
        )
    # Search/find/look are transport-neutral actions. Requiring browser state
    # merely because a local-file request says "search" traps a successfully
    # completed filesystem task in an impossible recovery loop. Demand an
    # explicit web surface as well as an action.
    browser_surface = bool(words & _expand("browser", "website", "webpage", "site")) or bool(
        words & {"http", "https", "url", "online", "internet"}
    ) or "web page" in lowered
    browser_action = bool(words & _expand("open", "search", "visit", "look", "find", "browse"))
    if browser_surface and browser_action:
        expected.append(
            PlanPostcondition(
                type=PostconditionType.BROWSER_STATE,
                description="Browser state or page observation is reported.",
            )
        )
    if (bool(words & {"screenshot", "screen", "desktop"}) or "what do you see" in lowered) and not _desktop_file_listing_request(
        lowered
    ):
        expected.append(
            PlanPostcondition(
                type=PostconditionType.DESKTOP_OBSERVATION,
                description="A desktop observation or screenshot is reported.",
            )
        )
    delivery_action = bool(words & _expand("send", "share", "deliver", "upload", "email"))
    delivery_subject = bool(
        words & _expand("artifact", "document", "file", "image", "photo", "pdf", "report", "screenshot")
    )
    if delivery_action and delivery_subject:
        expected.append(
            PlanPostcondition(
                type=(
                    PostconditionType.SCREENSHOT_DELIVERED
                    if "screenshot" in words
                    else PostconditionType.ARTIFACT_DELIVERED
                ),
                description="The requested file or screenshot is delivered to the source channel.",
            )
        )
    if bool(words & _expand("organize", "move", "rename", "sort")) and bool(
        words & _expand("file", "folder", "directory")
    ):
        expected.append(
            PlanPostcondition(
                type=PostconditionType.FILE_ORGANIZATION,
                description="Changed, moved, or organized file paths are reported.",
            )
        )
    if ("pull request" in lowered or " pr " in f" {lowered} " or "github" in words) and bool(
        words & _expand("create", "open", "make", "submit")
    ):
        expected.append(
            PlanPostcondition(
                type=PostconditionType.GITHUB_PR,
                description="A GitHub pull request URL or number is reported.",
            )
        )
    if bool(words & _expand("run", "execute", "launch")) and bool(words & _expand("command", "terminal", "script")):
        expected.append(
            PlanPostcondition(
                type=PostconditionType.EXTERNAL_COMMAND,
                description="External command completion is reported.",
            )
        )
    # Precise on purpose: a bare "daily" or "weekly" anywhere in an unrelated
    # sentence must not trigger this on its own (see _inflections' docstring
    # on why a false-positive postcondition is worse than a missed one) - it
    # has to sit next to a schedule-ish noun. The cadence regex accepts
    # spelled-out one/two/three alongside a bare digit, and an optional count
    # so "every day" matches without requiring "every 1 day".
    schedule_subject = bool(words & _expand("schedule", "recurring")) or bool(
        re.search(r"\b(?:daily|weekly)\s+(?:schedule|job|task|check)\b", lowered)
    )
    cadence = bool(
        re.search(
            r"\bevery\s+(?:(?:\d+|one|two|three)\s+)?(?:minute|minutes|hour|hours|day|days|week|weeks)\b",
            lowered,
        )
    )
    if (schedule_subject or cadence) and bool(words & _expand("create", "add", "set", "run", "check", "search")):
        expected.append(
            PlanPostcondition(
                type=PostconditionType.SCHEDULE_CREATED,
                description="A schedule ID and next run timestamp are reported.",
            )
        )
    return _dedupe(expected)


def deliverable_evidence(task: TaskRecord) -> str:
    """What this task has demonstrably produced, as facts for the Auditor.

    Deliberately free of intent inference. `_postconditions_from_objective`
    guesses what the user *meant* from word-set intersections and then vetoes
    the Operator when the guess isn't met - a semantic judgment made in Python
    and enforced over two LLMs that already agreed. This reports only what is
    observably true of the task record and lets the Auditor, which can read
    the actual request, decide which of these the objective needed.

    Every line is derived from the same `_postcondition_satisfied` checks the
    old gate used, so nothing is weakened - only the "which ones matter here"
    decision moves.
    """
    produced: list[str] = []
    missing: list[str] = []
    for postcondition in PostconditionType:
        label = postcondition.value
        if _postcondition_satisfied(task, postcondition):
            produced.append(label)
        else:
            missing.append(label)
    lines = ["Produced: " + (", ".join(produced) if produced else "(nothing recorded)")]
    lines.append("Not produced: " + (", ".join(missing) if missing else "(none)"))
    return "\n".join(lines)


def _postcondition_satisfied(task: TaskRecord, expected: PostconditionType) -> bool:
    if expected == PostconditionType.WORKSPACE_DIR:
        # Direct filesystem writes to an explicit project folder may not emit
        # a separate workspace_dir field. Concrete produced paths prove both
        # that project files exist and that they have a parent directory; do
        # not force the operator to manufacture a second, unrelated YBM task
        # workspace just to satisfy metadata.
        return bool(_value(task, "workspace_dir", "workspace_dir") or _reported_project_files(task))
    if expected == PostconditionType.WORKSPACE_FILES:
        return bool(_reported_project_files(task))
    if expected == PostconditionType.SOURCE_CONTENT:
        return _source_content_satisfied(task)
    if expected == PostconditionType.PREVIEW_URL:
        value = _value(task, "preview_url", "url")
        return isinstance(value, str) and value.startswith(("http://", "https://"))
    if expected == PostconditionType.ADAPTER_PROPOSAL:
        return bool(_value(task, "adapter_dir", "adapter_dir"))
    if expected == PostconditionType.ARTIFACT_DELIVERED:
        delivery = task.metadata.get("artifact_delivery")
        output = _last_output_dict(task)
        candidate = delivery if isinstance(delivery, dict) else output
        return candidate.get("delivered") is True
    if expected == PostconditionType.SCREENSHOT_DELIVERED:
        delivery = task.metadata.get("artifact_delivery")
        output = _last_output_dict(task)
        candidate = delivery if isinstance(delivery, dict) else output
        path = str(candidate.get("path") or "").lower()
        return candidate.get("delivered") is True and (
            candidate.get("operation") == "send_screenshot"
            or candidate.get("delivery_method") == "telegram.sendPhoto"
            or path.endswith((".png", ".jpg", ".jpeg", ".webp"))
        )
    if expected == PostconditionType.DOCUMENT_SUMMARY:
        return bool(_any_value(task, metadata_keys=("document_summary",), output_keys=("summary", "text")))
    if expected == PostconditionType.PRESENTATION_FILE:
        value = _any_value(task, metadata_keys=("document_path",), output_keys=("path",))
        return isinstance(value, str) and value.lower().endswith(".pptx")
    if expected == PostconditionType.CODING_AGENT_STEP:
        output = _last_output_dict(task)
        session = task.metadata.get("coding_agent_session")
        session_status = session.get("status") if isinstance(session, dict) else None
        provider = output.get("provider") or (session.get("provider") if isinstance(session, dict) else None)
        if provider not in {"codex", "github_copilot", "claude_code"}:
            return False
        return (
            output.get("returncode") == 0
            or output.get("status") == "completed"
            or session_status in {"completed", "failed", "stopped"}
            or bool((output.get("limit_state") or {}).get("limited"))
        )
    if expected == PostconditionType.SCHEDULE_CREATED:
        return bool(_any_value(task, metadata_keys=("schedule_id",), output_keys=("schedule_id", "task_id")))
    if expected == PostconditionType.BROWSER_STATE:
        return bool(
            _any_value(
                task,
                metadata_keys=("browser_state", "browser_url", "page_title", "screenshot_uri", "screenshot_path"),
                output_keys=("browser_state", "browser_url", "url", "page_title", "screenshot_uri", "screenshot_path"),
            )
        )
    if expected == PostconditionType.DESKTOP_OBSERVATION:
        output = _last_output_dict(task)
        if output.get("operation") == "run_goal":
            # A run_goal step is a multi-step action loop with an actual objective;
            # a screenshot existing is not evidence the goal was reached. Only an
            # explicit completed=True counts (max_steps exhaustion reports False).
            return output.get("completed") is True
        return bool(
            _any_value(
                task,
                metadata_keys=("desktop_observation", "screenshot_uri", "screenshot_path", "computer_use_actions"),
                output_keys=("desktop_observation", "observation", "screenshot_uri", "screenshot_path", "artifact_uri", "final_summary"),
            )
        )
    if expected == PostconditionType.FILE_ORGANIZATION:
        return bool(
            _any_value(
                task,
                metadata_keys=("organized_paths", "moved_files", "changed_files", "files", "file_manifest"),
                output_keys=("organized_paths", "changed_paths", "moved_files", "changed_files", "files", "manifest", "entries"),
            )
        )
    if expected == PostconditionType.TASK_STATUS:
        return bool(_any_value(task, metadata_keys=("task_status",), output_keys=("task_status", "summary")))
    if expected == PostconditionType.GITHUB_PR:
        return bool(
            _any_value(
                task,
                metadata_keys=("pull_request_url", "pr_url", "pr_number"),
                output_keys=("pull_request_url", "pr_url", "html_url", "url", "pr_number", "number"),
            )
        )
    if expected == PostconditionType.EXTERNAL_COMMAND:
        return _external_command_succeeded(task)
    return False


def _value(task: TaskRecord, metadata_key: str, output_key: str) -> Any:
    if task.metadata.get(metadata_key):
        return task.metadata[metadata_key]
    result = task.metadata.get("last_tool_result")
    if not isinstance(result, dict):
        return None
    output = result.get("output")
    if not isinstance(output, dict):
        return None
    return output.get(output_key)


def _any_value(task: TaskRecord, metadata_keys: tuple[str, ...], output_keys: tuple[str, ...]) -> Any:
    for key in metadata_keys:
        if task.metadata.get(key):
            return task.metadata[key]
    output = _last_output_dict(task)
    for key in output_keys:
        if output.get(key):
            return output[key]
    return None


def _external_command_succeeded(task: TaskRecord) -> bool:
    output = _last_output_dict(task)
    for key in ("returncode", "exit_code"):
        if output.get(key) == 0:
            return True
    terminal_output = output.get("terminal_output")
    if isinstance(terminal_output, list):
        for item in terminal_output:
            if isinstance(item, dict) and item.get("is_final") and item.get("exit_code") == 0:
                return True
    return False


def _last_output_dict(task: TaskRecord) -> dict[str, Any]:
    result = task.metadata.get("last_tool_result")
    if not isinstance(result, dict):
        return {}
    output = result.get("output")
    return output if isinstance(output, dict) else {}


def _reported_project_files(task: TaskRecord) -> list[str]:
    """Return produced project paths, excluding YBM's own workspace marker.

    A prepared workspace always contains ``TASK.md``. Counting that control
    file as project output let an empty directory satisfy a request to build
    an extension. Only paths reported by a successful write/coding operation
    are considered, and the marker is explicitly excluded.
    """
    candidates: list[Any] = []
    for key in ("changed_paths", "changed_files", "files", "files_created", "files_modified"):
        value = task.metadata.get(key)
        if isinstance(value, list):
            candidates.extend(value)
    output = _last_output_dict(task)
    for key in ("changed_paths", "changed_files", "files", "files_created", "files_modified"):
        value = output.get(key)
        if isinstance(value, list):
            candidates.extend(value)
    return [
        str(value)
        for value in candidates
        if str(value).strip()
        and str(value).replace("\\", "/").rsplit("/", 1)[-1].lower() != "task.md"
    ]


def _source_content_satisfied(task: TaskRecord) -> bool:
    history = task.metadata.get("operator_history")
    if not isinstance(history, list):
        return False
    read_paths: set[str] = set()
    listed_file_count = 0
    for entry in history:
        if not isinstance(entry, dict) or entry.get("status") != "succeeded":
            continue
        tool_name = entry.get("tool_name")
        tool_input = entry.get("input") if isinstance(entry.get("input"), dict) else {}
        operation = str(tool_input.get("operation") or "")
        if tool_name == "filesystem.manage" and operation == "describe_folder":
            return True
        if tool_name == "filesystem.manage" and operation == "read_file":
            path = str(tool_input.get("path") or "").strip()
            if path:
                read_paths.add(path.casefold())
        if tool_name == "document.manage" and operation in {
            "inspect_document",
            "extract_text",
            "summarize_pdf",
        }:
            return True
        if tool_name == "filesystem.manage" and operation in {
            "inspect_folder",
            "collect_folder_snapshot",
        }:
            listed_file_count = max(
                listed_file_count,
                str(entry.get("output_summary") or "").count("- [file]"),
            )

    request_text = " ".join(
        str(value or "")
        for value in (task.metadata.get("original_message_text"), task.objective)
    ).lower()
    requires_every_file = bool(
        re.search(r"\b(?:all|every)\b[^.\n]{0,40}\b(?:file|files|document|documents|evidence)\b", request_text)
    )
    if requires_every_file and listed_file_count:
        return len(read_paths) >= listed_file_count
    return bool(read_paths)


def _dedupe(values: list[PlanPostcondition]) -> tuple[PlanPostcondition, ...]:
    result: list[PlanPostcondition] = []
    seen: set[PostconditionType] = set()
    for item in values:
        if item.type in seen:
            continue
        seen.add(item.type)
        result.append(item)
    return tuple(result)


def _gap_reason(value: PostconditionType) -> str:
    if value == PostconditionType.WORKSPACE_DIR:
        return "expected_workspace_dir_missing"
    if value == PostconditionType.WORKSPACE_FILES:
        return "expected_workspace_files_missing"
    if value == PostconditionType.SOURCE_CONTENT:
        return "expected_source_content_missing"
    if value == PostconditionType.PREVIEW_URL:
        return "expected_preview_url_missing"
    if value == PostconditionType.ADAPTER_PROPOSAL:
        return "expected_adapter_proposal_missing"
    if value == PostconditionType.ARTIFACT_DELIVERED:
        return "expected_artifact_delivery_missing"
    if value == PostconditionType.SCHEDULE_CREATED:
        return "expected_schedule_created_missing"
    return f"expected_{value.value}_missing"


def fulfillment_guidance(gap: str) -> str:
    """Turn an internal postcondition code into an actionable next step.

    The code remains stable for traces/tests, while the Operator gets the tool
    and operation that can actually satisfy it instead of being asked to infer
    that mapping from an enum-shaped string.
    """
    guidance = {
        "expected_workspace_dir_missing": "Use workspace.manage or code.interpreter and retain its workspace_dir.",
        "expected_preview_url_missing": "Use workspace.manage launch_static/web_app_preview and retain preview_url.",
        "expected_adapter_proposal_missing": "Call adapter.factory with operation=scaffold and retain adapter_dir.",
        "expected_schedule_created_missing": "Call schedule.manage with operation=create and retain schedule_id.",
        "expected_artifact_delivered_missing": (
            "Call artifact.deliver with operation=send_screenshot for a requested screenshot; "
            "for other files use send_file with the exact requested path."
        ),
        "expected_screenshot_delivered_missing": (
            "Call artifact.deliver with operation=send_screenshot so the screenshot image itself is sent."
        ),
        "expected_browser_state_missing": "Call browser.open to inspect the requested page and retain its URL/title.",
        "expected_desktop_observation_missing": "Call computer.use with tool_input {\"operation\": \"observe\"}.",
        "expected_file_organization_missing": (
            "Call filesystem.manage organize_plan, then pass its returned manifest unchanged to apply_manifest."
        ),
        "expected_external_command_missing": "Use code.interpreter or the configured terminal tool and verify exit code 0.",
    }
    return guidance.get(gap, "Call the available tool that produces this missing result, or explain why it is blocked.")
def _desktop_file_listing_request(lowered: str) -> bool:
    """True when the message uses 'desktop' as a folder path, not as a screen surface.

    This exempts the objective from requiring a DESKTOP_OBSERVATION postcondition.
    Covers two cases:
    - Listing the contents of the desktop folder ("list files on desktop")
    - Acting on a file located on the desktop ("find/read/open X on my desktop")
    """
    if "desktop" not in lowered:
        return False
    listing_markers = (
        "list all", "list the", "show all", "show me all",
        "what files", "which files",
        "files on", "files at", "files in",
        "folders on", "folders at", "folders in",
        "desktop files",
    )
    file_action_markers = (
        "find ", "search ", "look for ", "locate ",
        "read ", "open the ", "open my ", "open a ",
        "delete ", "remove ", "rename ", "move ", "copy ",
        "send me ", "share ", "email ", "upload ", "get ",
        ".pdf", ".docx", ".txt", ".png", ".jpg", ".jpeg", ".xlsx", ".csv",
    )
    return any(marker in lowered for marker in listing_markers + file_action_markers)
