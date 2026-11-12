"""Replay a real run's model responses, so the README demo shows a real run without a GPU.

YBM records every model call it makes (the llm_calls table: the prompt, the model's exact
response, and how long the call took). This turns one task's calls into a recording, and serves
them back as an OpenAI-compatible model server: point a YBM profile's base_url at it and run the
same request. Everything else is live - YBM, its tools, the files it moves, the approval, the
console - and each answer arrives after the time the real call took.

Each response is served only in the situation it was recorded in: the role (intake, operator,
auditor) and the steps taken so far (tool, operation, status) must match the recording, or the
request fails with the difference, so a replay can never quietly answer a run that went another
way.

    # 1. a recording, from a real run in a YBM database
    python scripts/replay_model_server.py export .agent_control/agent_control.db TASK_ID \\
        docs/screenshots/demo-run.json --replace "D:\\scratch\\demo-home=C:\\Users\\sam"
    # 2. serve it
    python scripts/replay_model_server.py serve docs/screenshots/demo-run.json --port 8781

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROLES = (
    ("concierge", "You are the Concierge"),
    ("operator", "You are the operator loop"),
    ("auditor", "You are the Auditor"),
)
# A history entry in an operator or auditor prompt: "2. filesystem.manage (succeeded)" and, on a
# following line, its input ("   input: {...}" or "Input: {...}").
_STEP = re.compile(r"^\s*\d+\. (\S+) \(([a-z_]+)\)\s*$", re.MULTILINE)
_INPUT = re.compile(r"^\s*(?:input|Input): (\{.*\})\s*$", re.MULTILINE)


def role_of(system_prompt: str) -> str | None:
    return next((role for role, start in ROLES if system_prompt.lstrip().startswith(start)), None)


def situation(user_prompt: str) -> list[list[str]]:
    """The steps taken so far, as [tool, operation, status], in order."""
    steps = []
    for match in _STEP.finditer(user_prompt):
        following = user_prompt[match.end(): match.end() + 4000]
        next_step = _STEP.search(following)
        window = following[: next_step.start()] if next_step else following
        found = _INPUT.search(window)
        operation = re.search(r'"operation":\s*"([^"]+)"', found.group(1)) if found else None
        steps.append([match.group(1), operation.group(1) if operation else "", match.group(2)])
    return steps


def _messages(raw: str) -> tuple[str, str]:
    messages = json.loads(raw)
    system = next((m["content"] for m in messages if m.get("role") == "system"), "")
    user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    return str(system), str(user)


def export(database: Path, task_id: str, out: Path, replace: list[str]) -> None:
    db = sqlite3.connect(database)
    objective = db.execute("select objective from tasks where id = ?", (task_id,)).fetchone()
    if objective is None:
        raise SystemExit(f"no task {task_id} in {database}")
    rows = db.execute(
        "select source, model, messages_json, response_text, prompt_tokens, completion_tokens, latency_ms "
        "from llm_calls where task_id = ? order by created_at",
        (task_id,),
    ).fetchall()
    pairs = [item.split("=", 1) for item in replace]

    def clean(text: str) -> str:
        for old, new in pairs:
            text = text.replace(old, new).replace(old.replace("\\", "\\\\"), new.replace("\\", "\\\\"))
        return text

    calls = []
    for source, model, messages, response, prompt_tokens, completion_tokens, latency_ms in rows:
        system, user = _messages(messages)
        role = role_of(system)
        if role is None:
            raise SystemExit(f"a {source} call has a system prompt this does not know: {system[:80]!r}")
        calls.append({
            "role": role,
            "situation": situation(user),
            "response": clean(str(response)),
            "latency_ms": round(latency_ms or 0),
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
            "prompt": clean(user),
        })
    recording = {"objective": objective[0], "model": rows[0][1] if rows else "", "calls": calls}
    out.write_text(json.dumps(recording, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{out}: {len(calls)} calls, {sum(c['latency_ms'] for c in calls) / 1000:.1f} s of model time")


class Replay:
    def __init__(self, recording: dict, speed: float) -> None:
        self.recording = recording
        self.speed = speed
        self.queues = {role: [c for c in recording["calls"] if c["role"] == role] for role, _ in ROLES}
        self.lock = threading.Lock()

    def answer(self, body: dict) -> tuple[int, dict]:
        messages = body.get("messages") or []
        system = next((str(m.get("content")) for m in messages if m.get("role") == "system"), "")
        user = next((str(m.get("content")) for m in reversed(messages) if m.get("role") == "user"), "")
        role = role_of(system)
        if role is None:
            return 409, {"error": {"message": "replay: no recorded call has this system prompt"}}
        with self.lock:
            queue = self.queues[role]
            if not queue:
                return 409, {"error": {"message": f"replay: every recorded {role} call has been served"}}
            call = queue[0]
            now = situation(user)
            if now != call["situation"]:
                return 409, {"error": {"message": f"replay: the {role} call is in a different situation: "
                                                  f"recorded {call['situation']}, now {now}"}}
            if role == "concierge" and self.recording["objective"] not in user:
                return 409, {"error": {"message": "replay: the request is not the recorded one"}}
            queue.pop(0)
        time.sleep(call["latency_ms"] / 1000 / self.speed)
        print(f"served {role} after {call['latency_ms'] / 1000 / self.speed:.1f} s", flush=True)
        return 200, {
            "id": "replay",
            "object": "chat.completion",
            "model": self.recording["model"],
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": call["response"]}}],
            "usage": {**call["usage"], "total_tokens": sum(v or 0 for v in call["usage"].values())},
        }


def serve(path: Path, port: int, speed: float) -> None:
    replay = Replay(json.loads(path.read_text(encoding="utf-8")), speed)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/").endswith("/models"):
                self._send(200, {"object": "list", "data": [{"id": replay.recording["model"], "object": "model"}]})
            else:
                self._send(200, {"status": "ok"})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            status, payload = replay.answer(body)
            if status != 200:
                print(payload["error"]["message"], file=sys.stderr, flush=True)
            self._send(status, payload)

        def log_message(self, *_: object) -> None:
            return

    print(f"replaying {len(replay.recording['calls'])} calls of {replay.recording['model']} on port {port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    out = sub.add_parser("export", help="turn one task's model calls into a recording")
    out.add_argument("database", type=Path)
    out.add_argument("task_id")
    out.add_argument("out", type=Path)
    out.add_argument("--replace", action="append", default=[], metavar="OLD=NEW",
                     help="replace a machine-specific path in prompts and responses")
    run = sub.add_parser("serve", help="serve a recording as an OpenAI-compatible model server")
    run.add_argument("recording", type=Path)
    run.add_argument("--port", type=int, default=8781)
    run.add_argument("--speed", type=float, default=1.0, help="play the recorded wait times this many times faster")
    args = parser.parse_args()
    if args.command == "export":
        export(args.database, args.task_id, args.out, args.replace)
    else:
        serve(args.recording, args.port, args.speed)


if __name__ == "__main__":
    main()
