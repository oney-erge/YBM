"""Small things a person says to an assistant about itself: what apps it started, what it knows about them, and forgetting.

    "what apps are running?"       the apps YBM started that still answer, by the name of the page and its address
    "stop the recipe app"          stops one it started (only after checking the process is the server it started)
    "what do you know about me?"   the remembered facts and rules, in words
    "forget my t-shirt size"       forgets the one fact that matches

Deterministic text matches (no model call, instant). A message is treated as a command only when it names something that exists, so
"stop the music" or "forget about the meeting" go on to ordinary intake untouched.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from typing import Any
from urllib.parse import urlparse

from agent_control.schemas import ChannelType, InboundMessage, OutboundMessage, TaskRecord
from agent_control.storage.repositories import Repositories
from agent_control.tools.page_check import fetch_page

_LIST_APPS = re.compile(
    r"^\s*(?:(?:what|which)\s+(?:apps?|servers?|sites?|pages?|web\s*apps?)\s+(?:are|is)\s+(?:running|up|live|open|started)"
    r"|(?:what|which)\s+(?:apps?|web\s*apps?)\s+(?:did|have)\s+you\s+(?:start|started|launch|launched|made|built)"
    r"|(?:list|show)\s+(?:me\s+)?(?:the\s+|my\s+)?(?:running\s+)?(?:apps?|web\s*apps?))\b[^\n]{0,30}$",
    re.IGNORECASE,
)
_STOP_APP = re.compile(
    r"^\s*(?:please\s+)?(?:stop|close|shut\s*down|shutdown|kill|turn\s+off)\s+(?:the\s+|that\s+|my\s+|this\s+)?"
    r"(?P<what>[^\n]{1,80}?)\s*[.!]*\s*$",
    re.IGNORECASE,
)
_SHOW_MEMORY = re.compile(
    r"^\s*(?:(?:what|tell\s+me\s+what)\s+do\s+you\s+(?:know|remember)(?:\s+about\s+me)?"
    r"|what\s+have\s+you\s+(?:remembered|learned)(?:\s+about\s+me)?"
    r"|(?:show|list)\s+(?:me\s+)?(?:my\s+)?(?:profile|memories|memory|facts|remembered\s+facts))\s*\??\s*$",
    re.IGNORECASE,
)
_FORGET = re.compile(
    r"^\s*(?:please\s+)?forget\s+(?:about\s+)?(?:that\s+)?(?P<what>(?:my|the|that)\s+[^\n]{1,70}?|everything[^\n]{0,30})\s*[.!]*\s*$",
    re.IGNORECASE,
)
_ANY_APP = {"that", "it", "this", "app", "the app", "that app", "the last one", "last one", "the last app", "everything", "all", "all apps", "them"}
_FILLER = {"the", "a", "an", "app", "apps", "server", "site", "page", "web", "my", "that", "this", "last", "one"}


def _reply(inbound: InboundMessage, text: str) -> OutboundMessage:
    return OutboundMessage(channel=inbound.channel, chat_id=inbound.chat_id, text=text)


# ---- apps YBM started --------------------------------------------------------------------------

def _mine(task: TaskRecord, inbound: InboundMessage, conversation_id: str | None) -> bool:
    return bool(
        (conversation_id and task.conversation_id == conversation_id)
        or str(task.metadata.get("source_chat_id") or "") == str(inbound.chat_id)
    )


def running_apps(repositories: Repositories, inbound: InboundMessage, conversation_id: str | None) -> list[dict[str, Any]]:
    """The apps this chat's tasks started that still answer, newest first."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for task in repositories.tasks.list_recent(80):
        server = task.metadata.get("preview_server")
        if not isinstance(server, dict) or not _mine(task, inbound, conversation_id):
            continue
        url = str(server.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        if fetch_page(url, timeout=0.8) is None:
            continue
        controls = task.metadata.get("page_controls")
        title = str(controls.get("title") or "") if isinstance(controls, dict) else ""
        found.append({
            "name": title or " ".join(task.objective.split())[:60], "url": url, "pid": int(server.get("pid") or 0),
            "task_id": task.id, "words": " ".join([title, task.objective]).lower(),
        })
    return found


def _command_line(pid: int) -> str:
    try:
        if os.name == "nt":
            done = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                capture_output=True, text=True, timeout=15,
            )
        else:
            done = subprocess.run(["ps", "-p", str(int(pid)), "-o", "args="], capture_output=True, text=True, timeout=10)
        return done.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _stop_server(app: dict[str, Any]) -> bool:
    """Stop the static server for an app, only if the process with that id is still the one that was started.

    A process id can be reused after a restart; killing whatever now holds it would be worse than leaving the app running.
    """
    pid, port = int(app["pid"]), urlparse(app["url"]).port
    if not pid or not port:
        return False
    cmdline = _command_line(pid)
    if "http.server" not in cmdline or str(port) not in cmdline:
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=20, check=False)
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        return False
    return fetch_page(app["url"], timeout=0.5) is None


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _FILLER]


def _app_command(repositories: Repositories, inbound: InboundMessage, conversation_id: str | None, text: str) -> OutboundMessage | None:
    if _LIST_APPS.match(text):
        apps = running_apps(repositories, inbound, conversation_id)
        if not apps:
            return _reply(inbound, "I have no apps running right now.")
        return _reply(inbound, "Apps I started that are running:\n" + "\n".join(f"- {a['name']}: {a['url']}" for a in apps)
                      + "\n\nSay \"stop <name>\" to close one.")
    match = _STOP_APP.match(text)
    if match is None:
        return None
    what = match.group("what").strip().lower()
    apps = running_apps(repositories, inbound, conversation_id)
    if not apps:
        return None  # nothing of mine is running, so "stop the music" is somebody else's request
    if what in _ANY_APP or not _words(what):
        chosen = apps if what in {"everything", "all", "all apps", "them"} else apps[:1]
    else:
        wanted = _words(what)
        chosen = [a for a in apps if all(w in a["words"] for w in wanted)]
        if not chosen:
            return None
    lines = []
    for app in chosen:
        lines.append(f"Stopped {app['name']} ({app['url']})." if _stop_server(app)
                     else f"I could not stop {app['name']} ({app['url']}): the process is not the one I started, so I left it alone.")
    return _reply(inbound, "\n".join(lines))


# ---- what it knows about the person --------------------------------------------------------------

def _fact_words(text: str) -> list[str]:
    return [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in _words(text) if w not in {"about", "is", "are", "was"}]


def _memory_command(repositories: Repositories, inbound: InboundMessage, text: str) -> OutboundMessage | None:
    if _SHOW_MEMORY.match(text):
        facts = repositories.memory_facts.list_all()
        if not facts:
            return _reply(inbound, "I don't know anything about you yet. Tell me, for example: remember that my t-shirt size is L.")
        profile = [f for f in facts if f.category == "profile"]
        rules = [f for f in facts if f.category != "profile"]
        lines = ["Here is what I know about you:"] + [f"- {f.content}" for f in profile]
        if rules:
            lines += ["", "Things you asked me to remember:"] + [f"- {f.content}" for f in rules[:20]]
        lines += ["", "Say \"forget my <thing>\" to remove one."]
        return _reply(inbound, "\n".join(lines))
    match = _FORGET.match(text)
    if match is None:
        return None
    what = match.group("what").strip()
    if what.lower().startswith("everything"):
        return _reply(inbound, "I won't erase everything in one go. Tell me what to drop one at a time, for example: forget my t-shirt size.")
    wanted = _fact_words(re.sub(r"^(?:my|the|that)\s+", "", what, flags=re.IGNORECASE))
    if not wanted:
        return None
    facts = repositories.memory_facts.list_all()
    hits = [f for f in facts if all(any(w in token for token in _fact_words(f.content)) for w in wanted)]
    if len(hits) == 1:
        repositories.memory_facts.delete(hits[0].id)
        return _reply(inbound, f"Forgot: {hits[0].content}")
    if len(hits) > 1:
        return _reply(inbound, "Which one?\n" + "\n".join(f"- {f.content}" for f in hits[:8]) + "\nSay \"forget\" with more of the words.")
    return _reply(inbound, f"I don't have anything about \"{what}\" to forget.") if what.lower().startswith("my ") else None


def chat_command_reply(
    repositories: Repositories, inbound: InboundMessage, conversation_id: str | None
) -> OutboundMessage | None:
    """The reply to one of these commands, or None when the message is not one (it carries on to normal intake)."""
    text = (inbound.text or "").strip()
    if not text or len(text) > 120 or inbound.channel != ChannelType.TELEGRAM:
        return None
    return _memory_command(repositories, inbound, text) or _app_command(repositories, inbound, conversation_id, text)
