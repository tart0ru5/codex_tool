"""The three panels of the main window. Layout only; behaviour lives in MainWindow."""
from __future__ import annotations

from typing import Callable

from .log_view import LogView
from .render_view import RenderView
from .qt import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFontDatabase,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QStackedWidget,
    QTextEdit,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
    SINGLE_SELECTION,
    TREE_INDENTATION,
)


class LeftPanel(QWidget):
    """Conversation record tree plus the bottom button row."""

    def __init__(self, *, on_tree_open: Callable, on_refresh: Callable, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Codex 对话记录"])
        # sessions/year/month/day/file is five levels deep; Qt's default 20px
        # per level would push file names half-way across the panel.
        self.tree.setIndentation(TREE_INDENTATION)
        self.tree.itemClicked.connect(on_tree_open)
        layout.addWidget(self.tree)

        # Bottom button row. Add future left-side buttons here.
        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(lambda _checked=False: on_refresh())
        buttons.addWidget(self.refresh_button)
        layout.addLayout(buttons)


class CenterPanel(QWidget):
    """Provider/model controls on top, message list below."""

    def __init__(
        self,
        *,
        on_provider_activated: Callable,
        on_model_finished: Callable,
        on_proxy_config: Callable,
        on_injections: Callable,
        on_find_next: Callable,
        on_toggle_edit_selection_mode: Callable,
        on_item_clicked: Callable,
        on_delete_selected: Callable,
        on_clear_tool_records: Callable,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addWidget(self._build_model_panel(on_provider_activated, on_model_finished, on_proxy_config, on_injections)
        )
        layout.addWidget(
            self._build_message_panel(
                on_find_next,
                on_toggle_edit_selection_mode,
                on_item_clicked,
                on_delete_selected,
                on_clear_tool_records,
            ),
            1,
        )

    def _build_model_panel(self, on_provider_activated, on_model_finished, on_proxy_config, on_injections) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)

        # Row 1: provider + model.
        row = QHBoxLayout()
        row.addWidget(QLabel("Provider:"))
        self.provider_combo = QComboBox()
        # `activated` fires only for an explicit user choice, never while the
        # list is being rebuilt from config.toml.
        self.provider_combo.activated.connect(lambda _index: on_provider_activated())
        row.addWidget(self.provider_combo, 1)
        row.addWidget(QLabel("Model:"))
        self.model_input = QLineEdit()
        self.model_input.setPlaceholderText("例如 gpt-5.5")
        self.model_input.editingFinished.connect(on_model_finished)
        row.addWidget(self.model_input, 2)
        layout.addLayout(row)

        # Row 2: feature buttons. Add future buttons to this row, or as
        # actions in `self.more_menu`.
        buttons = QHBoxLayout()
        self.proxy_button = QPushButton("代理配置")
        self.proxy_button.clicked.connect(lambda _checked=False: on_proxy_config())
        self.placeholder_button = QPushButton("占位")
        self.placeholder_button.setEnabled(False)
        self.more_button = QPushButton("更多选项 ▾")
        self.more_menu = QMenu(self.more_button)
        self.more_button.setMenu(self.more_menu)
        self.injection_action = self.more_menu.addAction("提示词注入…")
        self.injection_action.triggered.connect(lambda _checked=False: on_injections())
        for button in (self.proxy_button, self.placeholder_button, self.more_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        return panel

    def _build_message_panel(
        self,
        on_find_next,
        on_toggle_edit_selection_mode,
        on_item_clicked,
        on_delete_selected,
        on_clear_tool_records,
    ) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)

        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("关键词搜索")
        self.search.returnPressed.connect(on_find_next)
        search_button = QPushButton("Search/Next")
        search_button.clicked.connect(lambda _checked=False: on_find_next())
        self.edit_mode = QCheckBox("编辑选择模式：点A再点B选范围")
        self.edit_mode.stateChanged.connect(lambda _state: on_toggle_edit_selection_mode())
        top.addWidget(self.search)
        top.addWidget(search_button)
        top.addWidget(self.edit_mode)
        layout.addLayout(top)

        self.list = QListWidget()
        self.list.setSelectionMode(SINGLE_SELECTION)
        self.list.itemClicked.connect(on_item_clicked)
        layout.addWidget(self.list)

        # Bottom operation row. Add future message operations here.
        operations = QHBoxLayout()
        for label, handler in [
            ("Delete selected", on_delete_selected),
            ("Clear tool records", on_clear_tool_records),
        ]:
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, h=handler: h())
            operations.addWidget(button)
        layout.addLayout(operations)
        return panel


TAB_STYLE = """
    QPushButton { padding: 4px 12px; border: 1px solid palette(mid); border-bottom: none;
                  border-top-left-radius: 4px; border-top-right-radius: 4px; }
    QPushButton:checked { background: palette(base); font-weight: bold; }
"""


class RightPanel(QWidget):
    """Detail view with a selector row on top, save button and status line.

    The selector row has two ends: content views are added from the left
    (`LEFT_TABS`), tool views from the right (`RIGHT_TABS`, listed right to left).
    """

    PAGE_RENDERED, PAGE_RAW, PAGE_LOG, PAGE_PROMPTS = range(4)
    LEFT_TABS = [(PAGE_RENDERED, "渲染内容"), (PAGE_RAW, "原始内容")]
    RIGHT_TABS = [(PAGE_PROMPTS, "提示词预设"), (PAGE_LOG, "日志")]  # rightmost first

    def __init__(
        self,
        *,
        on_save: Callable,
        on_page_changed: Callable,
        on_log_selected: Callable,
        prompt_page: QWidget,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        layout = QVBoxLayout(self)

        self.rendered = RenderView()
        self.raw = QTextEdit()
        self.raw.setAcceptRichText(False)
        self.stack = QStackedWidget()
        pages = {
            self.PAGE_RENDERED: self.rendered,
            self.PAGE_RAW: self.raw,
            self.PAGE_LOG: self._build_log_page(on_log_selected),
            self.PAGE_PROMPTS: prompt_page,
        }
        for index in sorted(pages):
            self.stack.addWidget(pages[index])

        selector = QHBoxLayout()
        selector.setSpacing(2)
        self.tab_group = QButtonGroup(self)
        self.tab_group.setExclusive(True)

        def tab(index: int, label: str) -> QPushButton:
            button = QPushButton(label)
            button.setCheckable(True)
            button.setStyleSheet(TAB_STYLE)
            self.tab_group.addButton(button, index)
            return button

        for index, label in self.LEFT_TABS:
            selector.addWidget(tab(index, label))
        selector.addStretch(1)
        for index, label in reversed(self.RIGHT_TABS):
            selector.addWidget(tab(index, label))
        self.tab_group.button(self.PAGE_RENDERED).setChecked(True)

        def switch(index: int) -> None:
            self.stack.setCurrentIndex(index)
            on_page_changed()

        self.tab_group.idClicked.connect(switch)
        layout.addLayout(selector)
        layout.addWidget(self.stack, 1)

        buttons = QHBoxLayout()
        self.save_button = QPushButton("保存到文件")
        self.save_button.clicked.connect(lambda _checked=False: on_save())
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

        self.status = QLabel("Ready")
        self.status.setWordWrap(True)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.status)

    def current_page(self) -> int:
        return self.stack.currentIndex()

    # Which part of an exchange the log page shows: (kind, label, tooltip).
    LOG_KINDS = [
        ("request_original", "原始请求", "Codex 发给本工具的请求，未经改动"),
        ("request_forwarded", "转发请求", "经本工具处理后，真正发往上游的请求"),
        ("response_raw", "原始回复", "上游的回复，保持收到时的样子（流式回复按 chunk 原样）"),
        ("response_merged", "整理回复", "同一份回复，流式 chunk 已拼成一个完整对象"),
    ]

    def _build_log_page(self, on_log_selected) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        self.log_combo = QComboBox()
        self.log_combo.setToolTip("每一份对应代理收到的一个请求，最新的在最上面")
        # `activated` = explicit user choice only, not list rebuilds.
        self.log_combo.activated.connect(lambda _index: on_log_selected())
        row.addWidget(self.log_combo, 1)
        self.log_kind_buttons: dict[str, QRadioButton] = {}
        for kind, label, tip in self.LOG_KINDS:
            button = QRadioButton(label)
            button.setToolTip(tip)
            button.clicked.connect(lambda _checked=False: on_log_selected())
            row.addWidget(button)
            self.log_kind_buttons[kind] = button
        self.log_kind_buttons["request_original"].setChecked(True)
        layout.addLayout(row)

        self.log_view = LogView()
        self.log_highlighter = self.log_view.highlighter
        layout.addWidget(self.log_view, 1)
        return page

    def log_kind(self) -> str:
        return next(kind for kind, button in self.log_kind_buttons.items() if button.isChecked())
