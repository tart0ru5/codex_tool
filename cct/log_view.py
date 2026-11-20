"""The log page's text view: stored JSON, coloured, with some fields foldable.

Folding only hides lines on screen. The text in the view (and what copy/select-all
gives) is always the complete log, and the file is never touched.
"""
from __future__ import annotations

from .log_highlight import JsonHighlighter
from .qt import QColor, QFontDatabase, QPainter, QPlainTextEdit, QPointF, QPolygonF, QSize, Qt, QWidget

GUTTER_WIDTH = 18


def _region(lines: list[str], start: int) -> tuple[int, int] | None:
    """(start, end) line numbers of the multi-line value opened on line `start`."""
    line = lines[start]
    if not line.endswith(("[", "{")):
        return None  # empty or scalar value: nothing to fold
    indent = len(line) - len(line.lstrip(" "))
    closer = " " * indent + ("]" if line.endswith("[") else "}")
    for end in range(start + 1, len(lines)):
        if lines[end] in (closer, closer + ","):
            return (start, end) if end > start + 1 else None
    return None


def _find_key(lines: list[str], key: str, indent: int, first: int, last: int) -> tuple[int, int] | None:
    prefix = " " * indent + f'"{key}": '
    for number in range(first, last):
        if lines[number].startswith(prefix):
            return _region(lines, number)
    return None


def default_folds(text: str) -> list[tuple[int, int]]:
    """Fields folded by default in an indent=2 log dump: `headers` and `body.tools`.

    With indent=2 every key sits on its own line and newlines inside strings are
    escaped, so indentation alone tells the nesting level.
    """
    lines = text.split("\n")
    regions = []
    headers = _find_key(lines, "headers", 2, 0, len(lines))
    if headers:
        regions.append(headers)
    body = _find_key(lines, "body", 2, 0, len(lines))
    if body:
        tools = _find_key(lines, "tools", 4, body[0] + 1, body[1])
        if tools:
            regions.append(tools)
    return sorted(regions)


class _Gutter(QWidget):
    def __init__(self, view: "LogView"):
        super().__init__(view)
        self.view = view
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt name)
        return QSize(GUTTER_WIDTH, 0)

    def paintEvent(self, event) -> None:  # noqa: N802
        self.view.paint_gutter(self, event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.view.gutter_clicked(event.position().y())


class LogView(QPlainTextEdit):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.highlighter = JsonHighlighter(self)
        self.folds: dict[int, tuple[int, bool]] = {}  # header line -> (closing line, folded)
        self.gutter = _Gutter(self)
        self.gutter.hide()
        self.updateRequest.connect(self._on_update_request)

    # ------------------------------------------------------------- content
    def setPlainText(self, text: str) -> None:  # noqa: N802
        """Show plain text with nothing foldable."""
        self.show_text(text, [])

    def show_text(self, text: str, folds: list[tuple[int, int]]) -> None:
        """Show `text`; each (start, end) line region starts out folded."""
        self.folds = {}
        super().setPlainText(text)
        for start, end in folds:
            self.folds[start] = (end, False)
            self.set_folded(start, True)
        self.gutter.setVisible(bool(self.folds))
        self.setViewportMargins(GUTTER_WIDTH if self.folds else 0, 0, 0, 0)
        self._place_gutter()

    # ------------------------------------------------------------- folding
    def set_folded(self, start: int, folded: bool) -> None:
        end, current = self.folds[start]
        if current == folded:
            return
        self.folds[start] = (end, folded)
        document = self.document()
        first = document.findBlockByNumber(start + 1)
        block = first
        last = first
        while block.isValid() and block.blockNumber() < end:
            block.setVisible(not folded)
            block.setLineCount(0 if folded else max(1, block.layout().lineCount()))
            last = block
            block = block.next()
        # Have the layout recompute the affected lines and the scroll range.
        document.markContentsDirty(first.position(), last.position() + last.length() - first.position())
        layout = document.documentLayout()
        layout.documentSizeChanged.emit(layout.documentSize())
        self.viewport().update()
        self.gutter.update()

    def toggle(self, start: int) -> None:
        self.set_folded(start, not self.folds[start][1])

    def _visible_headers(self):
        """(top, height, header line, folded) for fold headers currently on screen."""
        # Look only at the fold headers themselves. Walking the blocks from the top of
        # the screen is far too slow: hidden blocks have no height, so the walk never
        # gets past the bottom edge and measures every folded line on each repaint.
        offset = self.contentOffset()
        bottom = self.viewport().height()
        document = self.document()
        for number in sorted(self.folds):
            block = document.findBlockByNumber(number)
            if not block.isValid() or not block.isVisible():
                continue  # e.g. a header inside another folded field
            geometry = self.blockBoundingGeometry(block).translated(offset)
            if geometry.bottom() < 0 or geometry.top() > bottom:
                continue
            line_height = block.layout().lineAt(0).height() if block.layout().lineCount() else geometry.height()
            yield geometry.top(), line_height, number, self.folds[number][1]

    def paint_gutter(self, gutter: QWidget, event) -> None:
        painter = QPainter(gutter)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self.palette().text().color()))
        size = 4.0
        for top, height, _number, folded in self._visible_headers():
            cx, cy = GUTTER_WIDTH / 2, top + height / 2
            if folded:  # pointing right
                points = [QPointF(cx - size * 0.6, cy - size), QPointF(cx - size * 0.6, cy + size), QPointF(cx + size * 0.8, cy)]
            else:  # pointing down
                points = [QPointF(cx - size, cy - size * 0.6), QPointF(cx + size, cy - size * 0.6), QPointF(cx, cy + size * 0.8)]
            painter.drawPolygon(QPolygonF(points))
        painter.end()

    def gutter_clicked(self, y: float) -> None:
        for top, height, number, _folded in self._visible_headers():
            if top <= y < top + height:
                self.toggle(number)
                return

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if not any(folded for _end, folded in self.folds.values()):
            return
        # Mark folded fields so that hidden content is not mistaken for an empty value.
        painter = QPainter(self.viewport())
        painter.setPen(QColor(self.palette().placeholderText().color()))
        for top, _height, number, folded in self._visible_headers():
            if not folded:
                continue
            layout = self.document().findBlockByNumber(number).layout()
            if not layout.lineCount():
                continue
            line = layout.lineAt(layout.lineCount() - 1)
            x = self.contentOffset().x() + line.naturalTextWidth() + self.document().documentMargin() + 8
            baseline = top + line.y() + line.ascent()
            hidden = self.folds[number][0] - number - 1
            painter.drawText(QPointF(x, baseline), f"… 已折叠 {hidden} 行")
        painter.end()

    # -------------------------------------------------------------- gutter
    def _place_gutter(self) -> None:
        rect = self.contentsRect()
        self.gutter.setGeometry(rect.left(), rect.top(), GUTTER_WIDTH, rect.height())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place_gutter()

    def _on_update_request(self, _rect, _dy) -> None:
        if self.folds:
            self.gutter.update()
