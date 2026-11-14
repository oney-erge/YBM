"""Gaming mode from the chat: "I'm about to play a game", "free up the GPU", "gaming mode off", "I'm done gaming".

The assistant's model lives in the video memory of the machine the person also plays on. Saying so frees it at once and keeps it free until
they say they are done (gaming.py). These are exact phrases, not a guess from the mood of a message, and they are answered without a model
call: asking a language model to free the GPU would load it first.
"""

from __future__ import annotations

import re

from agent_control import gaming
from agent_control.schemas import ChannelType, InboundMessage, OutboundMessage, TaskStatus
from agent_control.storage.repositories import Repositories

_INTENT = r"(?:(?:i'?m|i am|i'?ll|about to|going to|gonna|let me|time to|gotta|i want to|i'?m going to)\s+)+"
_ON = re.compile(
    r"^\s*(?:please\s+)?(?:"
    r"(?:(?:turn|switch)\s+on\s+|enable\s+|start\s+)?gaming\s+mode(?:\s+on)?"
    r"|(?:free|release|clear)(?:\s+up)?\s+(?:the\s+|my\s+)?(?:gpu|vram|graphics\s+card)"
    rf"|{_INTENT}(?:be\s+)?(?:play(?:ing)?\s+(?:a\s+|some\s+)?(?:game|games|steam)|gam(?:e|ing))"
    r"|gaming\s+time"
    r")\b.{0,80}$",
    re.IGNORECASE,
)
_OFF = re.compile(
    r"^\s*(?:please\s+)?(?:"
    r"(?:(?:turn|switch)\s+off\s+|disable\s+|stop\s+|end\s+|exit\s+)gaming\s+mode|gaming\s+mode\s+off"
    r"|(?:i'?m|i am)\s+(?:done|finished|through)\s+(?:with\s+)?(?:gaming|playing|(?:the|my)\s+games?)"
    r"|(?:done|finished)\s+(?:gaming|playing)"
    r"|(?:you\s+can|go\s+ahead\s+and)\s+(?:have|take)\s+(?:the\s+)?gpu\s+back"
    r"|(?:the\s+)?gpu\s+(?:is\s+)?(?:yours|back)(?:\s+now)?"
    r")\b.{0,80}$",
    re.IGNORECASE,
)
_BUSY = (TaskStatus.RUNNING, TaskStatus.INTERPRETING)


def _reply(inbound: InboundMessage, text: str) -> OutboundMessage:
    return OutboundMessage(channel=inbound.channel, chat_id=inbound.chat_id, text=text)


def gaming_command_reply(
    repositories: Repositories, inbound: InboundMessage, *, localdeploy_url: str | None, flag=gaming.FLAG_PATH
) -> OutboundMessage | None:
    """The reply to a gaming-mode phrase, or None when the message is not one (it carries on to normal intake)."""
    text = (inbound.text or "").strip()
    if not text or len(text) > 120 or inbound.channel != ChannelType.TELEGRAM:
        return None
    if _OFF.match(text):
        if not gaming.is_on(flag):
            return _reply(inbound, "Gaming mode was not on.")
        held = gaming.turn_off(flag)
        said = "Gaming mode is off. I'll load the model again when the next message needs it."
        if held:
            lines = "\n".join(f"- {item}" for item in held[-5:])
            said += f"\n\nYou sent {len(held)} message{'s' if len(held) != 1 else ''} while I was off the GPU. Send {'them' if len(held) != 1 else 'it'} again if you still want that:\n{lines}"
        return _reply(inbound, said)
    if _ON.match(text):
        already = not gaming.turn_on(flag)
        busy = any(task.status in _BUSY for task in repositories.tasks.list_recent(50))
        if busy:
            freed = "I'm finishing the task that is running, and I'll free the GPU as soon as it is done."
        else:
            freed = gaming.free_gpu(gaming.localdeploy_root(localdeploy_url))
            gaming.mark_gpu_freed(flag)
        lead = "Gaming mode is already on. " if already else "Gaming mode is on. "
        return _reply(inbound, f"{lead}{freed} I'll stay off the GPU until you tell me you're done gaming; anything you send meanwhile has to wait.")
    return None


def while_gaming_reply(inbound: InboundMessage, *, flag=gaming.FLAG_PATH) -> OutboundMessage | None:
    """What to say to a message that needs the model while gaming mode is on: that it waits, and that it is kept in mind. None when it is off."""
    if not gaming.is_on(flag):
        return None
    gaming.hold(inbound.text or "[a file]", flag)
    return _reply(inbound, "I'm in gaming mode, so I'm keeping off the GPU and can't work on this yet. Tell me when you're done gaming and send it again.")
