"""Plain text out of Word, Excel and PowerPoint files and zip archives, for document.manage.

These files are zip containers of XML. They are read here with a few bounded regular expressions rather than an XML
parser, so a hostile file cannot use entity expansion ("billion laughs") against the worker, and nothing is extracted to
disk: members are read in memory with a size limit, and an archive is only listed.
"""

from __future__ import annotations

import html
import re
import zipfile
from pathlib import Path

from agent_control.tools.path_utils import ONLINE_ONLY_MESSAGE, is_online_only

MAX_MEMBER_BYTES = 8 * 1024 * 1024  # a larger member is skipped, so a zip bomb cannot fill memory
MAX_MEMBERS = 500
MAX_ROWS = 2000
SUFFIXES = {".docx", ".xlsx", ".pptx", ".zip"}

_TAG = re.compile(r"<[^>]+>")


def _read(archive: zipfile.ZipFile, name: str) -> str:
    try:
        info = archive.getinfo(name)
    except KeyError:
        return ""
    if info.file_size > MAX_MEMBER_BYTES:
        return ""
    return archive.read(info).decode("utf-8", errors="replace")


def _text(fragment: str) -> str:
    return html.unescape(_TAG.sub("", fragment))


def _docx(archive: zipfile.ZipFile) -> str:
    body = _read(archive, "word/document.xml")
    paragraphs = []
    for paragraph in re.findall(r"<w:p[ >].*?</w:p>", body, flags=re.S):
        runs = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", paragraph, flags=re.S)
        line = "".join(html.unescape(run) for run in runs).strip()
        if line:
            paragraphs.append(line)
    return "\n".join(paragraphs)


def _pptx(archive: zipfile.ZipFile) -> str:
    slides = sorted(
        (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
        key=lambda name: int(re.findall(r"\d+", name)[-1]),
    )
    out = []
    for number, name in enumerate(slides, start=1):
        texts = [html.unescape(run).strip() for run in re.findall(r"<a:t(?:\s[^>]*)?>(.*?)</a:t>", _read(archive, name), flags=re.S)]
        out.append(f"Slide {number}: " + " | ".join(text for text in texts if text))
    return "\n".join(out)


def _column(reference: str) -> str:
    return re.sub(r"\d+", "", reference)


def _xlsx(archive: zipfile.ZipFile) -> str:
    shared = [
        _text("".join(re.findall(r"<t(?:\s[^>]*)?>(.*?)</t>", item, flags=re.S)))
        for item in re.findall(r"<si[ >].*?</si>", _read(archive, "xl/sharedStrings.xml"), flags=re.S)
    ]
    workbook = _read(archive, "xl/workbook.xml")
    names = [html.unescape(name) for name in re.findall(r'<sheet\s[^>]*name="([^"]*)"', workbook)]
    sheets = sorted(
        (name for name in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)),
        key=lambda name: int(re.findall(r"\d+", name)[-1]),
    )
    out = []
    for position, sheet in enumerate(sheets):
        out.append(f"Sheet {names[position] if position < len(names) else position + 1}:")
        for row in re.findall(r"<row[ >].*?</row>", _read(archive, sheet), flags=re.S)[:MAX_ROWS]:
            cells = []
            for attributes, inner in re.findall(r"<c\s([^>]*?)(?:/>|>(.*?)</c>)", row, flags=re.S):
                reference = re.search(r'r="([A-Z]+\d+)"', attributes)
                kind = re.search(r't="(\w+)"', attributes)
                value = re.search(r"<v>(.*?)</v>", inner or "", flags=re.S)
                if kind and kind.group(1) == "s" and value and value.group(1).strip().isdigit():
                    index = int(value.group(1))
                    text = shared[index] if index < len(shared) else ""
                elif kind and kind.group(1) == "inlineStr":
                    text = _text("".join(re.findall(r"<t(?:\s[^>]*)?>(.*?)</t>", inner or "", flags=re.S)))
                else:
                    text = html.unescape(value.group(1)) if value else ""
                if text != "":
                    cells.append(f"{reference.group(1) if reference else '?'}={text}")
            if cells:
                out.append("  " + "  ".join(cells))
    return "\n".join(out)


def _listing(archive: zipfile.ZipFile) -> str:
    infos = archive.infolist()[:MAX_MEMBERS]
    lines = [f"{info.filename} ({info.file_size} bytes)" for info in infos if not info.is_dir()]
    more = len(archive.infolist()) - len(infos)
    header = f"Archive with {len(archive.infolist())} entries (not extracted):"
    return "\n".join([header, *lines] + ([f"... and {more} more"] if more > 0 else []))


def office_text(path: Path) -> str | None:
    """The text of a .docx, .xlsx or .pptx, or the file list of a .zip. None for any other suffix."""
    suffix = path.suffix.lower()
    if suffix not in SUFFIXES:
        return None
    if is_online_only(path):
        raise ValueError(ONLINE_ONLY_MESSAGE.format(name=path.name))
    try:
        with zipfile.ZipFile(path) as archive:
            if suffix == ".docx":
                return _docx(archive)
            if suffix == ".xlsx":
                return _xlsx(archive)
            if suffix == ".pptx":
                return _pptx(archive)
            return _listing(archive)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{path.name} is not a valid {suffix} file: {exc}") from exc
