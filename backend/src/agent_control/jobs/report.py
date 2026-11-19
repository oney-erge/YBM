"""Create and check a Word copy of a research report."""

from pathlib import Path
from zipfile import BadZipFile, ZipFile

from agent_control.tools.document_manage import _document_paragraphs, _write_minimal_docx


def write_docx(markdown: str, path: Path) -> None:
    lines = markdown.splitlines()
    title = "Research report"
    if lines and lines[0].startswith("# "):
        title, lines = lines[0][2:].strip() or title, lines[1:]
    _write_minimal_docx(path, _document_paragraphs(title, "\n".join(lines)))
    try:
        with ZipFile(path) as archive:
            if archive.testzip() is not None or "word/document.xml" not in archive.namelist():
                raise ValueError("Word report failed validation")
    except BadZipFile as exc:
        raise ValueError("Word report is not a valid document") from exc
