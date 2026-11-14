"""The live index: what is known right now, rebuilt when it is old enough to have changed.

Skills are dropped into a folder, MCP catalogs are rewritten by `list_tools`, workflows are saved from the chat, a tool is switched on in
Access. The index is a snapshot of all of that, so it is rebuilt after a few seconds and on demand (`invalidate`) instead of being kept
in step by every place that changes one of them.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable

from agent_control.abilities.index import AbilityIndex
from agent_control.abilities.model import Ability, AbilityState
from agent_control.abilities.sources import build_index
from agent_control.config import AppSettings
from agent_control.tools.spec import ToolRegistry

#: Seconds a snapshot is trusted. Short: reading the sources is a few file reads, and a skill dropped in should be found at once.
INDEX_TTL_SECONDS = 5.0


class AbilityCatalog:
    def __init__(
        self,
        registry: ToolRegistry,
        settings: AppSettings,
        *,
        workflows: Callable[[], Iterable[object]] | None = None,
        extra: Callable[[], Iterable[Ability]] | None = None,
        ttl_seconds: float = INDEX_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry = registry
        self._settings = settings
        self._workflows = workflows
        self._extra = extra
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._built_at: float | None = None
        self._index: AbilityIndex | None = None

    def index(self) -> AbilityIndex:
        with self._lock:
            now = self._clock()
            if self._index is None or self._built_at is None or now - self._built_at >= self._ttl:
                self._index = build_index(
                    self._registry,
                    self._settings,
                    workflows=self._workflows,
                    extra=tuple(self._extra()) if self._extra is not None else (),
                )
                self._built_at = now
            return self._index

    def invalidate(self) -> None:
        with self._lock:
            self._index = None

    def search(
        self, text: str, limit: int = 8, *, states: Iterable[AbilityState] | None = None
    ) -> list[tuple[Ability, float]]:
        return self.index().search(text, limit, states=states)
