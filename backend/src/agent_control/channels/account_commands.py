"""Reading back the logins YBM made for websites: "show my pinecone password", "which accounts did you make?", "forget my pinecone login".

The password is made, kept and typed by the tools (accounts.py) and never reaches the model, so it is the person who asks for it, here, in
words that mean it. Deterministic text matches, no model call; a message is a command only when it names a site that has a saved login,
so "show my bank password" or "what is a good password" go on to ordinary intake untouched. Showing one is recorded in the audit trail
(the site, never the value).
"""

from __future__ import annotations

import re

from agent_control.accounts import AccountVault
from agent_control.config import SecretVaultConfig
from agent_control.schemas import AuditEventType, ChannelType, InboundMessage, OutboundMessage
from agent_control.storage.audit import AuditLogger
from agent_control.storage.secrets import SecretVaultError

_SHOW = re.compile(
    r"^\s*(?:please\s+)?(?:show|send|tell|give|read|what(?:'s|\s+is|\s+was))\s+(?:me\s+)?(?:my\s+|the\s+)?(?P<what>[^\n]{1,60}?)\s+"
    r"(?:password|login|credentials)\s*\??\s*[.!]*\s*$",
    re.IGNORECASE,
)
_LIST = re.compile(
    r"^\s*(?:what|which|list|show)\s+(?:me\s+)?(?:my\s+|the\s+)?(?:accounts?|logins?)(?:\s+(?:did\s+you|have\s+you)\s+(?:make|made|create|created|open|opened))?\s*\??\s*[.!]*\s*$",
    re.IGNORECASE,
)
_FORGET = re.compile(
    r"^\s*(?:please\s+)?(?:forget|delete|remove)\s+(?:my\s+|the\s+)?(?P<what>[^\n]{1,60}?)\s+(?:password|login|account\s+login)\s*[.!]*\s*$",
    re.IGNORECASE,
)


def _reply(inbound: InboundMessage, text: str) -> OutboundMessage:
    return OutboundMessage(channel=inbound.channel, chat_id=inbound.chat_id, text=text)


def _day(entry: dict) -> str:
    return str(entry.get("created_at") or "")[:10]


def account_command_reply(
    secrets: SecretVaultConfig | None, audit: AuditLogger, inbound: InboundMessage
) -> OutboundMessage | None:
    text = (inbound.text or "").strip()
    if secrets is None or not text or len(text) > 120 or inbound.channel != ChannelType.TELEGRAM:
        return None
    show, listing, forget = _SHOW.match(text), _LIST.match(text), _FORGET.match(text)
    if not (show or listing or forget):
        return None
    accounts = AccountVault(secrets)
    try:
        if listing:
            entries = accounts.list_entries()
            if not entries:
                return _reply(inbound, "I have not made any logins for you yet.")
            lines = [f"- {e['host']}: {e.get('username') or 'no username saved'} (made {_day(e)})" for e in entries]
            return _reply(inbound, "Logins I made for you:\n" + "\n".join(lines) + "\n\nSay 'show my <site> password' to see one.")
        wanted = (show or forget).group("what")
        hosts = accounts.find(wanted)
        if not hosts:
            return None
        if forget:
            for host in hosts:
                accounts.forget(host)
            audit.append(AuditEventType.CONFIG_UPDATED, actor=f"{inbound.channel.value}:user:{inbound.sender_id}",
                         payload={"what": "account_login_forgotten", "hosts": hosts})
            return _reply(inbound, "Forgot my copy of the login for " + ", ".join(hosts) + ". The account itself still exists on the site.")
        lines = []
        for host in hosts[:3]:
            entry = accounts.get(host) or {}
            lines.append(f"{host}\nUsername: {entry.get('username') or '(none saved)'}\nPassword: {entry.get('password') or '(none saved)'}")
        audit.append(AuditEventType.CONFIG_UPDATED, actor=f"{inbound.channel.value}:user:{inbound.sender_id}",
                     payload={"what": "account_password_shown", "hosts": hosts[:3]})
        return _reply(inbound, "\n\n".join(lines) + "\n\nDelete this message once you have copied it.")
    except SecretVaultError:
        return _reply(inbound, "I cannot open my vault right now (its key is missing), so I cannot read or list logins.")
