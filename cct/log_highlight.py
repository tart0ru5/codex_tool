"""Colouring for the log view.

The log view shows a log file's JSON unchanged; this only sets colours so that
field names, values and escape sequences are easy to tell apart.
"""
from __future__ import annotations

import re

from .qt import QColor, QFont, QPlainTextEdit, QSyntaxHighlighter, QTextCharFormat

# With indent=2 every JSON token sits on one line, so lines are coloured independently.
KEY_RE = re.compile(r'^( *)("(?:[^"\\]|\\.)*")\s*:')
STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
ESCAPE_RE = re.compile(r'\\(?:u[0-9a-fA-F]{4}|.)')
LITERAL_RE = re.compile(r'-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\btrue\b|\bfalse\b|\bnull\b')
PUNCT_RE = re.compile(r'[{}\[\],:]')

# name: (colour on light background, colour on dark background)
COLORS = {
    "top_key": ("#b3261e", "#ff8a80"),
    "key": ("#0b57d0", "#8ab4f8"),
    "string": ("#1b6e2c", "#a5d6a7"),
    "escape": ("#9a6700", "#e3b341"),
    "number": ("#8250df", "#d2a8ff"),
    "keyword": ("#bc4c00", "#ffab70"),
    "punct": ("#7a7a7a", "#8b949e"),
}


class JsonHighlighter(QSyntaxHighlighter):
    def __init__(self, editor: QPlainTextEdit):
        super().__init__(editor.document())
        dark = editor.palette().base().color().lightness() < 128
        self.formats: dict[str, QTextCharFormat] = {}
        for name, pair in COLORS.items():
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(pair[1 if dark else 0]))
            if name == "top_key":
                fmt.setFontWeight(QFont.Weight.Bold)
            self.formats[name] = fmt

    def highlightBlock(self, text: str) -> None:  # noqa: N802 (Qt name)
        if not text.lstrip().startswith(('"', "{", "}", "[", "]", "-", "t", "f", "n") + tuple("0123456789")):
            return  # not JSON (a plain message such as "no logs yet")
        formats = self.formats
        start = 0
        key = KEY_RE.match(text)
        if key:
            # Fields of the log record itself (indent 2) stand out from nested ones.
            name = "top_key" if len(key.group(1)) == 2 else "key"
            self.setFormat(key.start(2), key.end(2) - key.start(2), formats[name])
            self.setFormat(key.end(2), key.end() - key.end(2), formats["punct"])
            start = key.end()
        position = start
        for string in STRING_RE.finditer(text, start):
            self._plain(text, position, string.start())
            self.setFormat(string.start(), string.end() - string.start(), formats["string"])
            for escape in ESCAPE_RE.finditer(text, string.start() + 1, string.end() - 1):
                self.setFormat(escape.start(), escape.end() - escape.start(), formats["escape"])
            position = string.end()
        self._plain(text, position, len(text))

    def _plain(self, text: str, start: int, end: int) -> None:
        """Colour the part of a line that is outside any string."""
        if start >= end:
            return
        for match in PUNCT_RE.finditer(text, start, end):
            self.setFormat(match.start(), 1, self.formats["punct"])
        for match in LITERAL_RE.finditer(text, start, end):
            name = "number" if match.group()[0] in "-0123456789" else "keyword"
            self.setFormat(match.start(), match.end() - match.start(), self.formats[name])
