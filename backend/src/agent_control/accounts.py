"""Logins YBM makes for websites: the password is generated here, kept in the encrypted vault, and typed into the page by the browser
tool. The model never sees it, and it is not written to a file, a log, an approval or the chat unless the person asks for it.

An entry lives in the secret vault under service ``accounts`` and the site's host as the key, as JSON:
``{"username": ..., "password": ..., "task_id": ..., "created_at": ..., "url": ...}``. The browser tool resolves three placeholders in
a form's field values (tools/browser.py):

    {{new_password}}     a strong password made for this site by this task (the same one for "Password" and "Confirm password")
    {{saved_password}}   the password already saved for this site (to log in again)
    {{saved_username}}   the username or email saved for this site

The person reads an entry back only by asking: "show my <site> password" in chat, or the console's Accounts page.
"""

from __future__ import annotations

import json
import re
import secrets
import string
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from agent_control.config import SecretVaultConfig
from agent_control.storage.secrets import SecretVault, SecretVaultError

SERVICE = "accounts"
PLACEHOLDER = re.compile(r"\{\{\s*(new_password|saved_password|saved_username)\s*\}\}")
_USERNAME_LABEL = re.compile(r"e-?mail|user\s*name|login|account", re.IGNORECASE)
_SYMBOLS = "!@#$%^&*-_=+?"
# No look-alike characters (0/O, 1/l/I): the password may be read out of the console or a chat and typed by hand.
_LETTERS = "".join(c for c in string.ascii_letters if c not in "lIO")
_DIGITS = "".join(c for c in string.digits if c not in "01")


def new_password(length: int = 20) -> str:
    """A strong random password with every kind of character in it, from the operating system's random source."""
    length = max(length, 12)
    chars = [secrets.choice(string.ascii_lowercase.replace("l", "")), secrets.choice(string.ascii_uppercase.replace("I", "").replace("O", "")),
             secrets.choice(_DIGITS), secrets.choice(_SYMBOLS)]
    pool = _LETTERS + _DIGITS + _SYMBOLS
    chars += [secrets.choice(pool) for _ in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def host_of(url: str | None) -> str:
    """The site a page belongs to: the host without a leading www. (``https://www.example.com/signup`` -> ``example.com``)."""
    host = (urlparse(str(url or "")).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def uses_placeholder(fields: dict[str, Any]) -> bool:
    return any(isinstance(value, str) and PLACEHOLDER.search(value) for value in fields.values())


class AccountVault:
    """Entries for sites, over the encrypted secret vault."""

    def __init__(self, config: SecretVaultConfig) -> None:
        self.vault = SecretVault(config)

    # -- reading -----------------------------------------------------------------------------------------------------------------

    def get(self, host: str) -> dict[str, Any] | None:
        try:
            raw = self.vault.get_secret(SERVICE, host)
        except SecretVaultError:
            return None
        try:
            entry = json.loads(raw)
        except ValueError:
            return None
        return entry if isinstance(entry, dict) else None

    def list_entries(self) -> list[dict[str, Any]]:
        """Every saved login without its password: host, username, when it was made, and by which task."""
        entries = []
        for host in self.vault.list_secrets().get(SERVICE, []):
            entry = self.get(host)
            if entry is not None:
                entries.append({"host": host, "username": entry.get("username"), "created_at": entry.get("created_at"), "url": entry.get("url")})
        return entries

    def find(self, text: str) -> list[str]:
        """The saved hosts a person's words point at (``pinecone`` finds ``pinecone.local`` and ``notes.pinecone.io``)."""
        wanted = re.sub(r"[^a-z0-9.]+", "", text.lower())
        if not wanted:
            return []
        hosts = self.vault.list_secrets().get(SERVICE, [])
        exact = [host for host in hosts if host == wanted]
        return exact or [host for host in hosts if wanted in host or host.split(".")[0] in wanted.replace(".", " ").split()]

    # -- writing -----------------------------------------------------------------------------------------------------------------

    def password_for_new_account(self, host: str, *, task_id: str, url: str | None = None, username: str | None = None) -> str:
        """The password this task uses for a site: made once, then the same for every field that asks for it (confirm, repeat)."""
        existing = self.get(host)
        if existing and existing.get("task_id") == task_id and existing.get("password"):
            if username and not existing.get("username"):
                self._save(host, {**existing, "username": username})
            return str(existing["password"])
        entry = {
            "username": username or "", "password": new_password(), "task_id": task_id,
            "created_at": datetime.now(timezone.utc).isoformat(), "url": url or "",
        }
        self._save(host, entry)
        return entry["password"]

    def remember_username(self, host: str, username: str, *, task_id: str) -> None:
        """The email or username typed for a site, filed with the password the same task made (in either order)."""
        entry = self.get(host)
        if entry and entry.get("task_id") == task_id and entry.get("username") != username:
            self._save(host, {**entry, "username": username})

    def forget(self, host: str) -> bool:
        return self.vault.delete_secret(SERVICE, host)

    def _save(self, host: str, entry: dict[str, Any]) -> None:
        self.vault.set_secret(SERVICE, host, json.dumps(entry, ensure_ascii=False))

    # -- the browser tool's view -------------------------------------------------------------------------------------------------

    def resolve_fields(self, fields: dict[str, str], *, host: str, task_id: str, url: str | None = None) -> dict[str, str]:
        """The field values with the placeholders replaced. Raises ValueError, in words the model can act on, when one cannot be."""
        username = next((str(v) for k, v in fields.items() if _USERNAME_LABEL.search(str(k)) and not PLACEHOLDER.search(str(v))), None)
        if not uses_placeholder(fields):
            if username and host:
                self.remember_username(host, username, task_id=task_id)  # the email typed after the password was made is filed with it
            return fields
        if not host:
            raise ValueError("open the site's page first: the password is kept per site, and there is no page open to say which site")
        resolved: dict[str, str] = {}
        for key, value in fields.items():
            text = str(value)

            def replace(match: re.Match[str]) -> str:
                kind = match.group(1)
                if kind == "new_password":
                    return self.password_for_new_account(host, task_id=task_id, url=url, username=username)
                entry = self.get(host)
                if entry is None or not entry.get("password"):
                    raise ValueError(f"no login is saved for {host}; create the account first, or ask the user for the password")
                return str(entry.get("password" if kind == "saved_password" else "username") or "")

            resolved[key] = PLACEHOLDER.sub(replace, text)
        if username:
            self.remember_username(host, username, task_id=task_id)
        return resolved
