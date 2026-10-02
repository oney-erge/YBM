"""The catalog of ways to reach YBM.

Today that is web chat, Telegram, and WhatsApp. Tomorrow it is whatever else
gets built, and the console should not need changing to show it - so the list
of channels is data, exactly like llm/catalog.py.

`status` is the honest state of each entry:

- "ready"     - implemented and connectable from the console right now
- "manual"    - implemented, but connecting it needs steps outside the console
- "planned"   - not built yet, listed so the shape of the product is visible

Listing planned channels is deliberate. A single hardcoded "Also enable
Telegram" toggle told a new user that Telegram was the only thing that would
ever exist; a catalog that shows what is coming sets the right expectation and
costs one row per entry.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelSpec:
    key: str
    label: str
    status: str
    #: One line, written for someone who has not read the docs.
    blurb: str
    #: Set for "planned" entries so the UI can explain rather than just grey out.
    note: str = ""
    #: Whether the console has a guided flow for it (see the Telegram step).
    guided: bool = False
    #: Whether it works with no setup at all.
    zero_setup: bool = False


CHANNELS: tuple[ChannelSpec, ...] = (
    ChannelSpec(
        key="web",
        label="Web chat",
        status="ready",
        blurb="This console. Works with no setup - you are using it right now.",
        zero_setup=True,
    ),
    ChannelSpec(
        key="telegram",
        label="Telegram",
        status="ready",
        blurb="Message YBM from your phone. Takes about a minute to connect.",
        guided=True,
    ),
    ChannelSpec(
        key="whatsapp",
        label="WhatsApp",
        status="manual",
        blurb="Message YBM from WhatsApp.",
        note="Turn it on in config/config.yaml, then scan a QR code. Steps are in docs/INSTALL.md.",
    ),
    ChannelSpec(
        key="discord",
        label="Discord",
        status="planned",
        blurb="Talk to YBM from a Discord server or DM.",
        note="Not built yet.",
    ),
    ChannelSpec(
        key="slack",
        label="Slack",
        status="planned",
        blurb="Talk to YBM from a Slack workspace.",
        note="Not built yet.",
    ),
    ChannelSpec(
        key="signal",
        label="Signal",
        status="planned",
        blurb="Message YBM over Signal.",
        note="Not built yet.",
    ),
    ChannelSpec(
        key="matrix",
        label="Matrix",
        status="planned",
        blurb="Message YBM from any Matrix client.",
        note="Not built yet.",
    ),
    ChannelSpec(
        key="email",
        label="Email",
        status="planned",
        blurb="Send YBM a task by email and get the result back.",
        note="Not built yet.",
    ),
)

BY_KEY: dict[str, ChannelSpec] = {spec.key: spec for spec in CHANNELS}


def get(key: str) -> ChannelSpec | None:
    return BY_KEY.get(key)
