"""Saved workflows from the chat: "save that as a workflow called weekly report", "run weekly report".

A workflow is a finished task's own successful steps, kept under a name so they can run again (see
`orchestration/workflows.py`). Until now they could only be saved and run from the web dashboard, which is the
last place someone texting an assistant from their phone is. These commands are deterministic text matches, so
saying one costs no model call, and a message that does not name a real workflow is left alone (so "run the
tests" is still an ordinary request, not a failed workflow lookup).
"""

from __future__ import annotations

import re

from agent_control.schemas import (
    AuditEventType, ChannelType, InboundMessage, OutboundMessage, TaskRecord, TaskStatus, TaskWorkflow,
)
from agent_control.storage.audit import AuditLogger
from agent_control.storage.repositories import Repositories

_NOUN = r"(?:workflow|routine|recipe|shortcut)"
_SAVE = re.compile(
    rf"^\s*(?:please\s+)?(?:save|keep|store|remember)\s+(?:this|that|it)(?:\s+task)?\s+as\s+(?:a\s+|my\s+)?{_NOUN}"
    r"(?:\s+(?:called|named))?\s*[:,]?\s*(?P<name>[^\n]{0,80}?)\s*[.!]*\s*$",
    re.IGNORECASE,
)
_LIST = re.compile(
    rf"^\s*(?:(?:list|show)\s+(?:me\s+)?(?:all\s+)?(?:my\s+)?(?:saved\s+)?{_NOUN}s?|what\s+{_NOUN}s?\s+do\s+i\s+have|my\s+{_NOUN}s?)\s*\??\s*$",
    re.IGNORECASE,
)
_RUN = re.compile(
    rf"^\s*(?:please\s+)?(?:run|start|do|repeat)\s+(?:my\s+|the\s+)?(?:{_NOUN}\s+)?(?P<name>.+?)(?:\s+{_NOUN})?"
    r"(?:\s+(?:with|using|for)\s+(?P<values>.+?))?\s*[.!]*\s*$",
    re.IGNORECASE,
)
_CADENCE = (
    r"(?P<cadence>daily|weekly|every\s+(?:day|week)|every\s+[1-9]\d{0,3}\s+(?:minutes?|hours?|days?|weeks?))"
)
_SCHEDULE = re.compile(
    rf"^\s*(?:please\s+)?(?:run|start|do|repeat)\s+(?:my\s+|the\s+)?(?:{_NOUN}\s+)?(?P<name>.+?)(?:\s+{_NOUN})?\s+{_CADENCE}"
    r"(?:\s+(?:with|using|for)\s+(?P<values>.+?))?\s*[.!]*\s*$",
    re.IGNORECASE,
)
#: Words that say when, which the scheduler cannot do yet (it repeats on an interval, with no time of day or weekday).
_WHEN_WORDS = re.compile(r"\b(?:every|each|daily|weekly|monthly|weekdays?|mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?|nightly|hourly)\b|\bat\s+\d", re.IGNORECASE)
DEFAULT_TIMEZONE = "America/Chicago"
_PAIR = re.compile(r"(\w+)\s*=\s*(\"[^\"]*\"|'[^']*'|[^,;]+)")


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.casefold()).split())


def _find(workflows: list[TaskWorkflow], spoken: str) -> TaskWorkflow | None:
    wanted = _norm(spoken)
    if not wanted:
        return None
    exact = [w for w in workflows if _norm(w.name) == wanted]
    if exact:
        return exact[0]
    # Looser only by whole words, and only for a name of two words or more: a one-word workflow called "x" must
    # not claim "run xylophone tests".
    spoken = set(wanted.split())
    close = [
        w for w in workflows
        if (len(_norm(w.name).split()) >= 2 and set(_norm(w.name).split()) <= spoken)
        or (len(spoken) >= 2 and spoken <= set(_norm(w.name).split()))
    ]
    return close[0] if len(close) == 1 else None


def _reply(inbound: InboundMessage, text: str) -> OutboundMessage:
    return OutboundMessage(channel=inbound.channel, chat_id=inbound.chat_id, text=text)


def _last_finished_task(repositories: Repositories, conversation_id: str | None) -> TaskRecord | None:
    for task in repositories.tasks.list_recent(30):
        if conversation_id and task.conversation_id != conversation_id:
            continue
        if task.status == TaskStatus.COMPLETED and not task.metadata.get("workflow_id") and task.metadata.get("operator_history"):
            return task
    return None


def _save(repositories: Repositories, audit: AuditLogger, inbound: InboundMessage, conversation_id: str | None, name: str) -> OutboundMessage:
    from agent_control.orchestration.replay import build_replay_plan
    from agent_control.orchestration.workflows import replay_plan_gaps

    source = _last_finished_task(repositories, conversation_id)
    if source is None:
        return _reply(inbound, "I have nothing finished to save yet. Ask me to do something first, then say \"save that as a workflow\".")
    history = [entry for entry in source.metadata.get("operator_history") or [] if isinstance(entry, dict)]
    plan = build_replay_plan(history)
    if not plan:
        return _reply(inbound, "That one did not use any steps I can repeat, so there is nothing to save.")
    gaps = replay_plan_gaps(history)
    if gaps:
        return _reply(inbound, "I can't save that one as a workflow: part of it cannot be repeated exactly (" + "; ".join(gaps) + ").")
    title = name.strip(" \"'") or " ".join(source.objective.split())[:50]
    if _find(repositories.workflows.list_all(), title) is not None:
        return _reply(inbound, f"You already have a workflow called \"{title}\". Pick another name, for example: save that as a workflow called {title} 2.")
    workflow = repositories.workflows.create(TaskWorkflow(
        name=title, source_task_id=source.id, objective_template=source.objective, plan=plan, parameters=[],
    ))
    audit.append(
        AuditEventType.TASK_CREATED, actor=f"{inbound.channel.value}:user:{inbound.sender_id}", task_id=source.id,
        payload={"workflow_saved": workflow.id, "name": workflow.name, "parameters": []},
    )
    return _reply(inbound, f"Saved \"{workflow.name}\" ({len(plan)} step{'s' if len(plan) != 1 else ''}). Say \"run {workflow.name}\" any time to do it again.")


def _listing(repositories: Repositories, inbound: InboundMessage) -> OutboundMessage:
    workflows = repositories.workflows.list_all()
    if not workflows:
        return _reply(inbound, "You have no saved workflows yet. Finish a task, then say \"save that as a workflow called <name>\".")
    lines = ["Your saved workflows:"]
    for workflow in workflows[:20]:
        needs = f" (needs: {', '.join(workflow.parameters)})" if workflow.parameters else ""
        lines.append(f"- {workflow.name}{needs}")
    lines.append("")
    lines.append("Say \"run <name>\" to use one.")
    return _reply(inbound, "\n".join(lines))


def _values(spoken: str | None, workflow: TaskWorkflow) -> dict[str, str]:
    if not spoken:
        return {}
    pairs = {key: value.strip().strip("\"'") for key, value in _PAIR.findall(spoken)}
    if pairs:
        return pairs
    # "run weekly report for Downloads": one word of value for a workflow that has exactly one parameter.
    return {workflow.parameters[0]: spoken.strip().strip("\"'")} if len(workflow.parameters) == 1 else {}


def _run(repositories: Repositories, audit: AuditLogger, inbound: InboundMessage, conversation_id: str | None,
         workflow: TaskWorkflow, spoken_values: str | None) -> OutboundMessage:
    from agent_control.orchestration.workflows import instantiate_plan

    values = _values(spoken_values, workflow)
    missing = [name for name in workflow.parameters if name not in values]
    if missing:
        example = ", ".join(f"{name}=<value>" for name in missing)
        return _reply(inbound, f"\"{workflow.name}\" needs {', '.join(missing)}. Say: run {workflow.name} with {example}")
    try:
        plan = instantiate_plan(workflow.plan, values)
    except ValueError as exc:
        return _reply(inbound, f"I can't run \"{workflow.name}\" yet: {exc}")
    task = repositories.tasks.create(
        f"{workflow.name} (workflow run)",
        conversation_id=conversation_id,
        metadata={
            "workflow_id": workflow.id, "workflow_run_values": values, "replay_plan": plan,
            "source_message_id": inbound.id, "source_channel": inbound.channel.value,
            "source_chat_id": inbound.chat_id, "source_sender_id": inbound.sender_id,
            "original_message_text": inbound.text,
        },
    )
    audit.append(
        AuditEventType.TASK_CREATED, actor=f"{inbound.channel.value}:user:{inbound.sender_id}", task_id=task.id,
        payload={"workflow_id": workflow.id, "values": values, "step_count": len(plan)},
    )
    return _reply(inbound, f"Running \"{workflow.name}\" now. I'll message you when it is done.")


def _schedule(repositories: Repositories, audit: AuditLogger, inbound: InboundMessage, workflow: TaskWorkflow,
              cadence: str, spoken_values: str | None) -> OutboundMessage:
    """Run a saved workflow on a repeat, through the same replay pipeline and approvals as a run made by hand."""
    from agent_control.orchestration.workflows import instantiate_plan
    from agent_control.schemas import ScheduleRecord, utc_now
    from agent_control.scheduler import next_run_after, validate_schedule

    cadence = " ".join(cadence.lower().split())
    values = _values(spoken_values, workflow)
    missing = [name for name in workflow.parameters if name not in values]
    if missing:
        example = ", ".join(f"{name}=<value>" for name in missing)
        return _reply(inbound, f"\"{workflow.name}\" needs {', '.join(missing)}. Say: run {workflow.name} {cadence} with {example}")
    try:
        validate_schedule(cadence, DEFAULT_TIMEZONE)
        plan = instantiate_plan(workflow.plan, values)
    except ValueError as exc:
        return _reply(inbound, f"I can't schedule \"{workflow.name}\" yet: {exc}")
    schedule = repositories.schedules.create(ScheduleRecord(
        source_channel=inbound.channel, source_chat_id=inbound.chat_id, objective=f"{workflow.name} (scheduled workflow)",
        cadence=cadence, timezone=DEFAULT_TIMEZONE, next_run_at=next_run_after(cadence, utc_now(), DEFAULT_TIMEZONE),
        metadata={"workflow_id": workflow.id, "workflow_run_values": values, "replay_plan": plan},
    ))
    audit.append(
        AuditEventType.TASK_CREATED, actor=f"{inbound.channel.value}:user:{inbound.sender_id}",
        payload={"workflow_id": workflow.id, "schedule_id": schedule.id, "cadence": cadence},
    )
    from zoneinfo import ZoneInfo

    first = schedule.next_run_at.astimezone(ZoneInfo(DEFAULT_TIMEZONE)).strftime("%A %B %d at %H:%M")
    return _reply(inbound, f"Scheduled \"{workflow.name}\" to run {cadence}. The first run is {first} ({DEFAULT_TIMEZONE} time). "
                           "It goes through the same approvals as a run by hand.")


def workflow_command_reply(
    repositories: Repositories, audit: AuditLogger, inbound: InboundMessage, conversation_id: str | None
) -> OutboundMessage | None:
    """The reply to a workflow command, or None when the message is not one (it carries on to normal intake)."""
    text = (inbound.text or "").strip()
    if not text or len(text) > 200 or inbound.channel != ChannelType.TELEGRAM:
        return None
    if (match := _SAVE.match(text)) is not None:
        return _save(repositories, audit, inbound, conversation_id, match.group("name") or "")
    if _LIST.match(text):
        return _listing(repositories, inbound)
    if (match := _SCHEDULE.match(text)) is not None:
        workflow = _find(repositories.workflows.list_all(), match.group("name"))
        if workflow is not None:
            return _schedule(repositories, audit, inbound, workflow, match.group("cadence"), match.group("values"))
    if (match := _RUN.match(text)) is not None:
        workflows = repositories.workflows.list_all()
        workflow = _find(workflows, match.group("name"))
        # Words beyond the workflow's own name ("weekly report" is a name, "every Monday" is a when).
        own = set(_norm(workflow.name).split()) if workflow is not None else set()
        rest = " ".join(w for w in (match.group("name") or "").split() if _norm(w) not in own)
        if workflow is not None and _WHEN_WORDS.search(rest):
            # "run steam count every Monday at 9": the scheduler repeats on an interval, with no weekday or time of day, and
            # running it now would be the wrong reading of a request to run it later.
            return _reply(inbound, "I can repeat a workflow daily, weekly, or every N minutes, hours, days or weeks, but not on a particular "
                                   f"weekday or at a time of day yet. For example: run {workflow.name} every 2 days.")
        if workflow is not None:
            return _run(repositories, audit, inbound, conversation_id, workflow, match.group("values"))
    return None
