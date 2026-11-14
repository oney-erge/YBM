"""One record for everything the assistant can do, or could.

A tool, an MCP tool, a skill, a saved workflow, a script it kept, a coding agent it can hand work to, a local model it could load, and
a thing it cannot do at all (paying for an order) are all the same kind of fact to the code that has to choose between them: a
name, what it is good for, whether it is ready, and what it would take to make it ready. Before this module each was listed by its
own code in its own format (tools/spec.py `context`, tools/skills.py, tools/mcp_client.py, the workflows table), so "is there
anything that can do this?" had no single answer and a capability that was installed but switched off, or installable, or known
to be impossible, was invisible to the model that needed to know.

The record is data about a capability. It is not how the capability is called: `call` says that, in the terms of the existing
tools (a tool name and an input), so nothing here is a second way to run anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class AbilityKind(StrEnum):
    TOOL = "tool"  # a built-in tool (tools/registry.py)
    MCP_TOOL = "mcp_tool"  # a tool of a configured MCP server
    SKILL = "skill"  # a written procedure the model reads and follows (tools/skills.py)
    HELPER = "helper"  # a script that settled an earlier task, kept for the next one (skill_learning.py)
    WORKFLOW = "workflow"  # a saved sequence of tool calls (orchestration/workflows.py)
    AGENT = "agent"  # another agent the work can be handed to (Claude Code, Codex, Copilot)
    MODEL = "model"  # a local model that could be loaded to give the assistant a sense it lacks (vision)
    GAP = "gap"  # something that cannot be done by this assistant, and what to do instead


class AbilityState(StrEnum):
    READY = "ready"  # can be used now
    DISABLED = "disabled"  # exists and is switched off; the user can turn it on
    INSTALLABLE = "installable"  # not here yet; it can be fetched or loaded, with the user's approval
    NEEDS_ACCESS = "needs_access"  # works once the user supplies an account, a key or a login
    UNAVAILABLE = "unavailable"  # cannot be done by this assistant; the record says what to do instead


#: What it takes to move a capability from where it is to ready. The model is told these in words; the code uses them to decide
#: what to ask the user and which approval to raise.
NEED_ENABLE = "enable"  # switch it on in Access
NEED_APPROVAL = "approval"  # the user approves a load, an install or a swap
NEED_ACCOUNT = "account"  # the user gives an account, a key or a login
NEED_PAYMENT = "payment"  # money has to be spent, which the assistant never does on its own


@dataclass(frozen=True)
class Ability:
    #: Stable identity: "<kind>:<name>", e.g. "tool:filesystem.manage", "skill:invoice-totals", "gap:commerce.order".
    id: str
    kind: AbilityKind
    name: str
    #: One line: what it is for. This is what the model reads when choosing, so it says what the capability does, not what it is.
    summary: str
    #: The domain it belongs to ("files", "web", "code", "vision", "commerce"...). The decision model routes to a group before it
    #: routes to a tool, so adding a tool to a group needs no new route.
    group: str = ""
    state: AbilityState = AbilityState.READY
    #: Words people use for the things this does ("order", "buy", "resize", "photo"). Searched like the summary, weighted higher.
    provides: tuple[str, ...] = ()
    #: Operations, examples or steps: searched, and shown on `describe`.
    detail: str = ""
    #: How to use it now, in the terms of the tools that already exist: {"tool": ..., "input": {...}}. Empty when it cannot be used.
    call: dict = field(default_factory=dict)
    #: What it takes to make it ready (NEED_* above) and, in words, what that means for this capability.
    needs: tuple[str, ...] = ()
    #: For a capability that is not ready: what the assistant should say or do instead. Required for GAP.
    guidance: str = ""
    #: A yes/no question about a request, in the user's terms ("Does the user want the assistant to spend money ...?"). Words alone cannot
    #: tell "pay my electricity bill" from "remind me to pay the rent"; the decision model can be asked, and a record that has a question
    #: is shown for a request only when the answer is yes (`confirm_at`) or the words match very strongly.
    confirm: str = ""
    confirm_at: float = 0.3
    #: Where a request this ability is confirmed for goes: "plan" hands it to the planner (plans/stage.py) instead of the Operator loop.
    #: Set by a skill's `route:` line; empty for everything else.
    route: str = ""
    #: Where the record came from, for the console ("registry", "mcp catalog", "skills folder", "localdeploy").
    source: str = ""

    def line(self) -> str:
        """The record as one line for a model to read."""
        state = "" if self.state == AbilityState.READY else f" [{self.state.value}]"
        return f"{self.name}{state}: {self.summary}"
