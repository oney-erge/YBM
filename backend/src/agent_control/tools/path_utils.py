from __future__ import annotations

import re


def safe_path_segment(value: str, *, fallback: str) -> str:
    """Sanitize a model/user-derived value before using it as one path part."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned or fallback


# Windows marks a OneDrive "Files On-Demand" file that has not been downloaded with these attributes.
_RECALL_ON_DATA_ACCESS = 0x400000
_RECALL_ON_OPEN = 0x40000
_OFFLINE = 0x1000


def is_online_only(path: object) -> bool:
    """True for a cloud file (OneDrive Files On-Demand) whose content is not on this computer.

    Reading one fails with a misleading "not a zip file" or a permission error, and a model told only that
    reports the file as corrupted. The file is fine; it just has to be kept on the device first.
    """
    import os

    try:
        attributes = getattr(os.stat(str(path)), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & (_RECALL_ON_DATA_ACCESS | _RECALL_ON_OPEN | _OFFLINE))


ONLINE_ONLY_MESSAGE = (
    "{name} is stored only in the cloud (OneDrive) and has not been downloaded to this computer, so it cannot be read. "
    "In File Explorer, right-click it and choose \"Always keep on this device\", then ask again."
)
