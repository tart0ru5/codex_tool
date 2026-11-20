"""The "rendered" page: one conversation entry as readable text, coloured.

Same idea as the log view: the text is exactly what render.py produced, this only
sets colours (field names, section headings, JSON) and a fixed-width font.
"""
from __future__ import annotations

import re

from .log_highlight import JsonHighlighter
from .qt import QFontDatabase, QPlainTextEdit, QWidget

META_RE = re.compile(r"([^:]+)(:)")
# Everything Qt starts a new text block on; folded to "\n" so that the line
# numbers counted here are the block numbers the highlighter sees.
LINE_BREAK_RE = re.compile("\r\n|\r|\u2029")


class RenderHighlighter(JsonHighlighter):
    def __init__(self, editor: QPlainTextEdit):
        super().__init__(editor)
        self.kinds: list[str] = []  # kind of each line, see render.render_segments

    def highlightBlock(self, text: str) -> None:  # noqa: N802 (Qt name)
        number = self.currentBlock().blockNumber()
        kind = self.kinds[number] if number < len(self.kinds) else "text"
        if kind == "json":
            super().highlightBlock(text)
        elif kind == "title":
            self.setFormat(0, len(text), self.formats["top_key"])
        elif kind == "meta":
            match = META_RE.match(text)
            if match:
                self.setFormat(0, match.end(1), self.formats["key"])
                self.setFormat(match.start(2), 1, self.formats["punct"])


class RenderView(QPlainTextEdit):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.highlighter = RenderHighlighter(self)

    def show_segments(self, segments: list[tuple[str, str]]) -> None:
        """Show (kind, text) pieces from render.render_segments, one after another."""
        kinds: list[str] = []
        texts: list[str] = []
        for kind, text in segments:
            text = LINE_BREAK_RE.sub("\n", text)
            texts.append(text)
            kinds.extend([kind] * (text.count("\n") + 1))
        self.highlighter.kinds = kinds  # before setPlainText: that is what triggers colouring
        super().setPlainText("\n".join(texts))
        if self.document().blockCount() != len(kinds):
            # Lines do not match what was counted: colours would land on the wrong
            # lines, so show the text uncoloured instead.
            self.highlighter.kinds = []
            self.highlighter.rehighlight()

    def setPlainText(self, text: str) -> None:  # noqa: N802
        self.show_segments([("text", text)])

    def clear(self) -> None:
        self.show_segments([])
