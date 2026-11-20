"""Keep the computer awake while a long job runs or a task is being worked on, and let it sleep again when neither is.

A campaign (a survey for a night, a research for hours) stops being a background job the moment Windows goes to sleep with it half done. The owner said a campaign may keep the PC awake
(11/19), so while a job of any kind but a watch is running, the process asks Windows not to sleep (`SetThreadExecutionState`, the call a video player makes); when the last such job ends,
pauses or fails, the request is withdrawn. It keeps the system awake, not the screen: the display may turn off. It is a no-op away from Windows.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable

logger = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _windows_setter(flags: int) -> None:
    import ctypes

    ctypes.windll.kernel32.SetThreadExecutionState(flags)  # type: ignore[attr-defined]


class KeepAwake:
    """`want(True)` while there is long work; `want(False)` when there is none. Only a change calls the system."""

    def __init__(self, setter: Callable[[int], None] | None = None) -> None:
        self._setter = setter if setter is not None else (_windows_setter if sys.platform == "win32" else None)
        self.holding = False

    def want(self, awake: bool) -> None:
        if awake == self.holding or self._setter is None:
            return
        try:
            self._setter(ES_CONTINUOUS | ES_SYSTEM_REQUIRED if awake else ES_CONTINUOUS)
            self.holding = awake
            logger.info("keep_awake %s", "on" if awake else "off")
        except Exception:  # noqa: BLE001 - sleeping on a long job is a loss of time, never a failed job
            logger.warning("keep_awake_failed", exc_info=True)
