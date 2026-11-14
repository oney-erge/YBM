"""Wake the local model while a message is still being routed.

After the model server has sat idle its model is gone from the GPU, and the first real call pays for loading it: 80 to 130 seconds on
the laptop this was built on. A message takes a few seconds to reach that call (routing on the CPU, the acknowledgement, the
memory update), so a one-token request sent the moment the message arrives loads the model in that time. It is sent at most once
every few minutes, only to a model on this machine or its network (a hosted model is never woken by an incoming message), and what
it returns is thrown away.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from agent_control import gaming
from agent_control.llm.cloud_gate import is_hosted
from agent_control.llm.providers import build_provider_for_profile

#: A model that was used this recently is still loaded.
MIN_INTERVAL_SECONDS = 240.0
_LAST: dict[str, float] = {}


def warm_profile_name(settings: Any) -> str | None:
    llm = settings.llm
    return llm.operator_profile or llm.major_profile or llm.default_profile or None


async def warm_up(settings: Any, *, clock: Callable[[], float] = time.monotonic, build: Callable[..., Any] = build_provider_for_profile) -> bool:
    """Send the wake-up request if it is due and the model is local. True when one was sent and answered."""
    if gaming.is_on():
        return False  # the person said they are gaming: no model is loaded until they say they are done
    name = warm_profile_name(settings)
    profile = settings.llm.profiles.get(name) if name else None
    if profile is None or is_hosted(profile.provider, profile.base_url):
        return False
    now = clock()
    last = _LAST.get(name)
    if last is not None and now - last < MIN_INTERVAL_SECONDS:
        return False
    _LAST[name] = now
    quick = profile.model_copy(update={"max_tokens": 1, "timeout_seconds": 170})
    try:
        await build(quick).generate_text("Reply with the single word OK.", "OK?")
    except Exception:  # noqa: BLE001 - waking the model is an optimisation; a failure here must never touch the message
        return False
    return True


def forget_warm_state() -> None:
    """For tests."""
    _LAST.clear()
