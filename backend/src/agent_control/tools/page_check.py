"""Read a launched page back and compare it with what the person said they would enter.

A live run was asked for "a web app that scales a recipe: I enter ingredient amounts and the number of servings". It launched a page with
two servings fields and a fixed example recipe, called that done, and the person had nowhere to type an ingredient. The model's own
report of what it built is the weakest possible evidence, so after an app is launched YBM fetches the page, lists its controls
(fields, selects, buttons) and checks every thing the request says the user enters against the labels of those controls.

Deterministic and conservative: it only judges a request that states what the user enters ("I enter ...", "the user types ..."), a
page with a free-text area is never faulted for a missing field, and it reports what it could not find, not a verdict on quality.
"""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

_SKIPPED_INPUT_TYPES = {"hidden", "submit", "button", "reset", "image"}
_STOPWORDS = {
    "the", "a", "an", "my", "own", "some", "any", "of", "in", "into", "to", "for", "number", "amount", "amounts", "value", "values",
    "data", "info", "information", "details", "text", "new", "your", "their", "its", "all", "each", "every", "and", "or",
}


@dataclass
class Control:
    kind: str  # "field", "textarea", "select"
    label: str
    input_type: str = ""


@dataclass
class PageControls:
    title: str = ""
    headings: list[str] = field(default_factory=list)
    controls: list[Control] = field(default_factory=list)
    buttons: list[str] = field(default_factory=list)
    has_script: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PageControls":
        return cls(
            title=str(data.get("title") or ""), headings=list(data.get("headings") or []), buttons=list(data.get("buttons") or []),
            has_script=bool(data.get("has_script")),
            controls=[
                Control(str(c.get("kind") or "field"), str(c.get("label") or ""), str(c.get("type") or ""))
                for c in data.get("controls") or [] if isinstance(c, dict)
            ],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title, "headings": self.headings, "buttons": self.buttons, "has_script": self.has_script,
            "controls": [{"kind": c.kind, "label": c.label, "type": c.input_type} for c in self.controls],
        }


class _Reader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.page = PageControls()
        self._labels_for: dict[str, str] = {}
        self._open_label: list[str] | None = None
        self._label_text: list[str] = []
        self._stack: list[str] = []
        self._button_text: list[str] | None = None
        self._pending: list[tuple[Control, dict[str, str]]] = []
        self._in_title = False
        self._heading: list[str] | None = None

    # -- helpers
    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {key.lower(): (value or "") for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = self._attrs(attrs)
        if tag == "title":
            self._in_title = True
        elif tag in {"h1", "h2", "h3"}:
            self._heading = []
        elif tag == "script":
            self.page.has_script = True
        elif tag == "label":
            self._open_label = [a.get("for", "")]
            self._label_text = []
        elif tag == "button":
            self._button_text = []
        elif tag in {"input", "textarea", "select"}:
            input_type = a.get("type", "text").lower() if tag == "input" else ""
            if tag == "input" and input_type in {"submit", "button", "reset", "image"}:
                label = a.get("value") or a.get("aria-label") or ""
                if label:
                    self.page.buttons.append(label.strip())
                return
            if tag == "input" and input_type in _SKIPPED_INPUT_TYPES:
                return
            kind = "textarea" if tag == "textarea" else "select" if tag == "select" else "field"
            control = Control(kind, "", input_type)
            self._pending.append((control, a))
            self.page.controls.append(control)
            if self._open_label is not None:
                # A control inside a label is named by that label's text, filled in when the label closes.
                self._open_label.append(str(len(self.page.controls) - 1))

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in {"h1", "h2", "h3"} and self._heading is not None:
            text = " ".join("".join(self._heading).split())
            if text:
                self.page.headings.append(text)
            self._heading = None
        elif tag == "button" and self._button_text is not None:
            text = " ".join("".join(self._button_text).split())
            if text:
                self.page.buttons.append(text)
            self._button_text = None
        elif tag == "label" and self._open_label is not None:
            text = " ".join("".join(self._label_text).split())
            target = self._open_label[0]
            if target:
                self._labels_for[target] = text
            for index in self._open_label[1:]:
                self._labels_for[f"#{index}"] = text
            self._open_label = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.page.title += data
        if self._heading is not None:
            self._heading.append(data)
        if self._button_text is not None:
            self._button_text.append(data)
        if self._open_label is not None:
            self._label_text.append(data)

    def finish(self) -> PageControls:
        for index, (control, attrs) in enumerate(self._pending):
            named = [
                self._labels_for.get(attrs.get("id", ""), ""), self._labels_for.get(f"#{index}", ""),
                attrs.get("aria-label", ""), attrs.get("placeholder", ""),
            ]
            # What a person reads first; the name or id (often "full_name") only when the page offers no words.
            parts = [part for part in named if part] or [attrs.get("name", "") or attrs.get("id", "")]
            control.label = " ".join(" ".join(part.replace("_", " ").replace("-", " ").split()) for part in parts if part).strip()
        self.page.title = " ".join(self.page.title.split())
        return self.page


def inspect_html(html: str) -> PageControls:
    reader = _Reader()
    try:
        reader.feed(html)
        reader.close()
    except Exception:  # noqa: BLE001 - a half-parsed page is still evidence
        pass
    return reader.finish()


def fetch_page(url: str, *, timeout: float = 4.0) -> str | None:
    """The page's HTML, retrying briefly because the server was only just started."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 - a local URL this process just started
                return response.read(400_000).decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, ValueError):
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.4)


# "I enter ingredient amounts and the number of servings, and it shows ..." -> "ingredient amounts and the number of servings".
_ENTER = re.compile(
    r"\b(?:i|you|users?|people|someone|the user)\s+(?:can\s+|could\s+|should\s+|will\s+|want\s+to\s+|need\s+to\s+)?"
    r"(?:enter|type|input|paste|fill\s+in|put\s+in|write)\s+(?:in\s+)?(?P<what>[^.;:!?\n]+)",
    re.IGNORECASE,
)
_END_OF_WHAT = re.compile(
    r",?\s+(?:and\s+)?(?:then|so\s+that|so|it|the\s+(?:app|page|tool|site)|show|shows|display|displays|calculate|calculates|give|gives"
    r"|see|view|get|read|look|click|press|run|launch)\b",
    re.IGNORECASE,
)


def requirement_items(request_text: str) -> list[str]:
    """What the request says the user enters, one phrase per thing ("ingredient amounts", "the number of servings")."""
    items: list[str] = []
    for match in _ENTER.finditer(request_text or ""):
        what = match.group("what")
        cut = _END_OF_WHAT.search(what)
        if cut is not None:
            what = what[: cut.start()]
        for part in re.split(r",|\band\b|\bplus\b|&", what, flags=re.IGNORECASE):
            phrase = " ".join(part.split()).strip(" '\"")
            if phrase and phrase.lower() not in items:
                items.append(phrase.lower())
    return items[:6]


def _keywords(phrase: str) -> list[str]:
    words = [re.sub(r"[^a-z0-9]", "", w) for w in phrase.lower().split()]
    return [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words if w and w not in _STOPWORDS]


def unmet_items(request_text: str, page: PageControls) -> list[str]:
    """Things the request says the user enters that no control on the page is named for. Empty when it cannot judge."""
    items = requirement_items(request_text)
    if not items or not page.controls:
        # A page with no controls at all is its own, different problem; this check only compares names.
        return [] if not items else list(items)
    if any(control.kind == "textarea" for control in page.controls):
        return []  # free text can hold anything the person was going to type
    labels = " ".join(control.label.lower() for control in page.controls)
    missing = []
    for item in items:
        words = _keywords(item)
        if words and not any(word in labels for word in words):
            missing.append(item)
    return missing


def describe(page: PageControls, unmet: list[str]) -> str:
    fields = [c.label or c.kind for c in page.controls]
    lines = [f"Page check: {len(fields)} field(s)" + (f" ({', '.join(fields[:8])})" if fields else "")
             + f" and {len(page.buttons)} button(s)" + (f" ({', '.join(page.buttons[:5])})" if page.buttons else "") + "."]
    if not page.controls:
        lines.append("There is no field to type into.")
    if unmet:
        lines.append(
            "The request says the user enters " + ", ".join(unmet) + ", but no field on the page is named for "
            + ("it" if len(unmet) == 1 else "them") + ". Add the field(s) so the person can enter them."
        )
    return " ".join(lines)
