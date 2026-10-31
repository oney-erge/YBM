"""Keep hosted models out of the loop unless the user said yes.

The local model does the work. When it cannot answer, a configured fallback that lives on someone else's server
(OpenAI, Anthropic, any non-local endpoint) is not called on its own: sending the request and the file contents in
it off the machine is the user's decision, not a retry policy. The provider raises CloudFallbackNeeded instead, the
worker asks the user, and only a "yes" for that task lets the hosted profile answer.

(Codex and Claude Code are a different thing: tools the local model starts when the user asks for them by name.)
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlparse

_LOCAL_NAMES = {"localhost", "host.docker.internal"}
_ALLOWED: ContextVar[frozenset[str]] = ContextVar("hosted_llm_profiles_allowed", default=frozenset())


class CloudFallbackNeeded(Exception):
    """The local model failed and the only fallback is a hosted model that has not been approved.

    Deliberately not a ValueError: the Operator retries those, and a retry must not reach a hosted model.
    """

    def __init__(self, profile: str, model: str, reason: str) -> None:
        super().__init__(f"hosted fallback {profile!r} ({model}) needs the user's approval: {reason}")
        self.profile = profile
        self.model = model
        self.reason = reason


def is_hosted(provider: str, base_url: str | None) -> bool:
    """Whether a profile talks to a server outside this machine and its private network."""
    if provider != "openai_compatible":
        return True  # Anthropic and any other native provider is a hosted API
    host = (urlparse(base_url or "").hostname or "").lower()
    if not host:
        return True  # an unknown endpoint is treated as hosted, the safe side
    if host in _LOCAL_NAMES:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True  # a public DNS name
    return not (address.is_loopback or address.is_private or address.is_link_local)


def is_allowed(profile: str) -> bool:
    return profile in _ALLOWED.get()


@contextmanager
def hosted_allowed(profiles: Iterable[str]) -> Iterator[None]:
    """Let the named hosted profiles answer inside this block (the task the user approved)."""
    token = _ALLOWED.set(frozenset(profiles))
    try:
        yield
    finally:
        _ALLOWED.reset(token)


AFFIRMATIVE = {
    "yes", "y", "yeah", "yep", "yup", "ok", "okay", "sure", "send it", "go ahead", "do it",
    "yes please", "please do", "yes send it", "yes please send it", "yes go ahead",
}


def is_yes(answer: object) -> bool:
    """A plain yes, matched whole. Anything else is a no and nothing is sent.

    Whole-reply matching is deliberate: "Yes, but keep my files off it" and the E2E harness's canned "Yes, please go
    ahead with what you proposed..." must not count as approval to leave the machine.
    """
    text = " ".join(str(answer or "").casefold().replace("!", " ").replace(".", " ").replace(",", " ").split())
    return text in AFFIRMATIVE
