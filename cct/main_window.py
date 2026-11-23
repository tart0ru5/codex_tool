"""Main window: wires the panels to Codex's session files and config.toml."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import CodexConfig
from .documents import Entry, SessionDoc
from . import injections
from .exchange_log import ExchangeLog, MissingLog
from .injection_dialog import InjectionDialog
from .log_view import default_folds
from .panels import CenterPanel, LeftPanel, RightPanel
from .paths import CODEX_HOME, SESSION_INDEX_FILE, SESSION_ROOTS
from . import model_catalog
from .proxy import ProxyServer
from .proxy_control import ProxyController, ProxyError
from .proxy_dialog import ProxyDialog
from .qt import (
    EXTENDED_SELECTION,
    QColor,
    QDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    Qt,
    QVBoxLayout,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QTimer,
    QTreeWidgetItem,
    SCROLLBAR_WIDTH,
    SINGLE_SELECTION,
    SPLITTER_HANDLE_WIDTH,
    USER_ROLE,
    YES,
)
from .prompt_panel import PromptPanel
from .settings import Settings, display_path
from .render import entry_kind, entry_preview, is_tool_record, json_dump, render_segments

ROLE_COLORS = {"user": "#1f6feb", "assistant": "#8957e5"}
COMMON_RECORD_KINDS = {
    "session_meta", "turn_context", "developer", "reasoning",
    "function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output",
    "event:user_message", "event:agent_message", "event:agent_reasoning",
    "event:task_started", "event:task_complete", "event:token_count",
    "event:item_started", "event:item_completed",
}
SESSION_ID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")


class MainWindow(QMainWindow):
    def __init__(self, initial_path: Path | None = None):
        super().__init__()
        self.setWindowTitle("Codex Context Tool - local only (GitHub @tart0ru5)")
        self.resize(1400, 850)
        self.setStyleSheet(self._scrollbar_style())

        self.doc: SessionDoc | None = None
        self.config = CodexConfig()
        self.settings = Settings()
        self.settings.load()
        stored = self.settings.data.get("hiddenEntryKinds", [])
        self.hidden_kinds = {k for k in stored if isinstance(k, str)} if isinstance(stored, list) else set()
        self.exchange_log = ExchangeLog(self.settings.log_dir, self.settings.log_keep)
        self.proxy_server = ProxyServer(self.exchange_log)
        self.shown_log: tuple | None = None
        self.proxy_control = ProxyController(self.config, self.settings, self.proxy_server)
        self.dirty = False
        self.shown_row: int | None = None
        self.anchor_row: int | None = None

        self._tree_state: tuple | None = None
        self._build_ui()
        self.refresh_tree()
        self.load_model_config()
        if self.settings.load_error:
            self.status.setText(f"无法读取工具配置，使用默认值：{self.settings.load_error}")
        elif self.settings.proxy["enabled"]:
            self.apply_proxy(interactive=False)
        if initial_path is not None and initial_path.exists():
            self.open_file(initial_path)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check_external_change)
        self.timer.start(3000)

    @staticmethod
    def _scrollbar_style() -> str:
        return f"""
            QScrollBar:vertical {{ width: {SCROLLBAR_WIDTH}px; margin: 0px; }}
            QScrollBar::handle:vertical {{ min-height: 32px; border-radius: 4px; }}
            QScrollBar:horizontal {{ height: {SCROLLBAR_WIDTH}px; margin: 0px; }}
            QScrollBar::handle:horizontal {{ min-width: 32px; border-radius: 4px; }}
        """

    def _build_ui(self) -> None:
        self.root_splitter = QSplitter()
        self.root_splitter.setHandleWidth(SPLITTER_HANDLE_WIDTH)
        self.root_splitter.setStyleSheet(
            f"QSplitter::handle:horizontal {{ min-width: {SPLITTER_HANDLE_WIDTH}px; }}"
        )
        self.setCentralWidget(self.root_splitter)

        self.left_panel = LeftPanel(on_tree_open=self.tree_open, on_refresh=self.refresh_tree, parent=self)
        self.center_panel = CenterPanel(
            on_provider_activated=self.provider_activated,
            on_model_finished=self.model_finished,
            on_proxy_config=self.open_proxy_config,
            on_injections=self.open_injections,
            on_find_next=self.find_next,
            on_toggle_edit_selection_mode=self.toggle_edit_selection_mode,
            on_item_clicked=self.item_clicked,
            on_delete_selected=self.delete_selected,
            on_clear_tool_records=self.clear_tool_records,
            parent=self,
        )
        self.prompt_panel = PromptPanel(load=lambda: self.settings.prompts, save=self.save_prompts)
        filter_action = self.center_panel.more_menu.addAction("聊天记录显示项目…")
        filter_action.triggered.connect(self.open_record_filter)
        self.right_panel = RightPanel(
            on_save=self.save_current,
            on_page_changed=self.right_page_changed,
            on_log_selected=self.show_log,
            prompt_page=self.prompt_panel,
            parent=self,
        )

        self.tree = self.left_panel.tree
        self.provider_combo = self.center_panel.provider_combo
        self.model_input = self.center_panel.model_input
        self.search = self.center_panel.search
        self.edit_mode = self.center_panel.edit_mode
        self.list = self.center_panel.list
        self.rendered = self.right_panel.rendered
        self.raw = self.right_panel.raw
        self.status = self.right_panel.status

        self.root_splitter.addWidget(self.left_panel)
        self.root_splitter.addWidget(self.center_panel)
        self.root_splitter.addWidget(self.right_panel)
        self.root_splitter.setSizes(self.settings.splitter_sizes or [280, 600, 520])
        # Persist column widths while the user drags a splitter handle.
        self.root_splitter.splitterMoved.connect(lambda _pos, _index: self.save_splitter_sizes())

    def save_splitter_sizes(self) -> None:
        if self.settings.load_error:
            return  # never overwrite a settings file we could not read
        try:
            self.settings.set_splitter_sizes(self.root_splitter.sizes())
        except OSError as exc:
            self.status.setText(f"无法保存栏宽到 {display_path(self.settings.path)}: {exc}")

    # ------------------------------------------------------------------ tree

    @staticmethod
    def _thread_names() -> dict[str, str]:
        """Session id -> thread name, from Codex's session_index.jsonl."""
        names: dict[str, str] = {}
        if not SESSION_INDEX_FILE.exists():
            return names
        for line in SESSION_INDEX_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except Exception:
                continue
            if isinstance(record, dict) and record.get("id") and record.get("thread_name"):
                names[str(record["id"])] = str(record["thread_name"])
        return names

    @staticmethod
    def _tree_signature() -> tuple:
        """What the tree shows: every conversation file plus the title index."""
        files: list[str] = []
        for _label, root in SESSION_ROOTS:
            if root.is_dir():
                files.extend(str(path) for path in root.rglob("*.jsonl"))
        try:
            index = SESSION_INDEX_FILE.stat()
            index_state = (index.st_mtime_ns, index.st_size)
        except OSError:
            index_state = None
        return (tuple(sorted(files)), index_state)

    def auto_refresh_tree(self) -> None:
        """Rebuild the tree when conversations appear, move or get a title."""
        try:
            signature = self._tree_signature()
        except OSError:
            return  # a directory vanished mid-scan; try again on the next tick
        if signature == self._tree_state:
            return
        current = self.tree.currentItem()
        selected = current.data(0, USER_ROLE) if current is not None else None
        scroll = self.tree.verticalScrollBar().value()
        status = self.status.text()
        try:
            self.refresh_tree()
        except OSError:
            return
        self.status.setText(status)  # a background refresh must not replace the message
        items: list[QTreeWidgetItem] = []
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            items.append(item)
            stack.extend(item.child(i) for i in range(item.childCount()))
        # Qt expands every ancestor of the item it makes current, so put the
        # folders the user had collapsed back afterwards.
        collapsed = [item for item in items if item.childCount() and not item.isExpanded()]
        for item in items:
            if selected is not None and item.data(0, USER_ROLE) == selected:
                self.tree.setCurrentItem(item)
                break
        for item in collapsed:
            item.setExpanded(False)
        self.tree.verticalScrollBar().setValue(scroll)

    def refresh_tree(self) -> None:
        self._tree_state = self._tree_signature()
        collapsed = set()

        def remember(item: QTreeWidgetItem) -> None:
            if item.childCount() and not item.isExpanded():
                collapsed.add(item.data(0, USER_ROLE))
            for index in range(item.childCount()):
                remember(item.child(index))

        for index in range(self.tree.topLevelItemCount()):
            remember(self.tree.topLevelItem(index))

        names = self._thread_names()
        self.tree.clear()
        total = 0

        def add_dir(parent: QTreeWidgetItem, directory: Path) -> int:
            count = 0
            children = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            for child in children:
                if child.is_dir():
                    node = QTreeWidgetItem([child.name])
                    node.setData(0, USER_ROLE, str(child))
                    added = add_dir(node, child)
                    if added:
                        parent.addChild(node)
                        count += added
                elif child.suffix == ".jsonl":
                    match = SESSION_ID_RE.search(child.name)
                    name = names.get(match.group(1)) if match else None
                    node = QTreeWidgetItem([f"{child.name}  「{name}」" if name else child.name])
                    node.setData(0, USER_ROLE, str(child))
                    node.setToolTip(0, str(child))
                    parent.addChild(node)
                    count += 1
            return count

        for label, root in SESSION_ROOTS:
            top = QTreeWidgetItem([label])
            top.setData(0, USER_ROLE, str(root))
            top.setToolTip(0, str(root))
            self.tree.addTopLevelItem(top)
            if root.is_dir():
                total += add_dir(top, root)
            else:
                top.setText(0, f"{label} (目录不存在)")

        def restore(item: QTreeWidgetItem) -> None:
            item.setExpanded(item.data(0, USER_ROLE) not in collapsed)
            for index in range(item.childCount()):
                restore(item.child(index))

        for index in range(self.tree.topLevelItemCount()):
            restore(self.tree.topLevelItem(index))
        self.status.setText(f"{CODEX_HOME}: {total} conversation files")

    def tree_open(self, item: QTreeWidgetItem) -> None:
        path = Path(item.data(0, USER_ROLE) or "")
        if path.is_file():
            self.open_file(path)

    # ---------------------------------------------------------------- config

    def load_model_config(self) -> None:
        try:
            self.config.load()
        except Exception as exc:
            self.status.setText(f"Failed to read config.toml: {exc}")
        self.provider_combo.clear()
        self.provider_combo.addItems(self.config.providers())
        self.provider_combo.setCurrentText(self.config.provider)
        self.model_input.setText(self.config.model)

    def _write_config(self, key: str, value: str) -> None:
        try:
            self.config.set_value(key, value)
            self.status.setText(f"Saved {key} = {value!r} to {self.config.path} (new Codex sessions use it)")
        except Exception as exc:
            QMessageBox.critical(self, "config.toml", f"Failed to save {key}:\n{exc}")
            self.load_model_config()

    def provider_activated(self) -> None:
        provider = self.provider_combo.currentText().strip()
        if provider and provider != self.config.provider:
            self._write_config("model_provider", provider)
            if self.settings.proxy["enabled"]:
                self.apply_proxy()  # move the redirect to the new provider

    def model_finished(self) -> None:
        model = self.model_input.text().strip()
        if not model:
            self.model_input.setText(self.config.model)
            return
        if model != self.config.model:
            self._write_config("model", model)

    # ----------------------------------------------------------------- proxy

    def update_proxy_button(self) -> None:
        running = self.proxy_server.running
        self.center_panel.proxy_button.setText(f"代理配置（已开启 :{self.proxy_server.port}）" if running else "代理配置")

    def apply_proxy(self, *, interactive: bool = True) -> bool:
        """Make listener + Codex config match the saved proxy settings."""
        try:
            message = self.proxy_control.apply()
            ok = True
        except Exception as exc:
            ok = False
            message = f"代理未能开启：{exc}"
            try:
                self.proxy_control.release()
            except Exception as release_exc:
                message += f"\n恢复 Codex 配置时出错：{release_exc}"
            if interactive:
                QMessageBox.critical(self, "代理配置", message)
        self.load_model_config()
        self.update_proxy_button()
        self.status.setText(message.replace("\n", " "))
        return ok

    def proxy_state_text(self) -> str:
        if self.proxy_server.running:
            target = self.proxy_control.upstream or "（当前预设未填写 baseURL，请求会报错）"
            return f"当前状态：正在监听 127.0.0.1:{self.proxy_server.port}，转发到 {target}"
        return "当前状态：未监听"

    def model_adaptation_text(self) -> str:
        """Tooltip of the "enable proxy" checkbox: what the model adaptation does."""
        catalog = self.proxy_control.catalog_setting()
        return (
            "模型适配：Codex 只给它认识的模型发 apply_patch（改文件）工具。\n"
            f"开启代理时，工具把所有 API 预设里的模型名登记到 {catalog}（只增不减），\n"
            f"并在 {self.proxy_control.config_display()} 顶层加一行 {model_catalog.CONFIG_KEY} = \"{catalog}\"。\n"
            "关闭代理或退出工具时去掉这一行，文件保留。"
        )

    def open_proxy_config(self) -> None:
        if self.settings.load_error:
            QMessageBox.critical(self, "代理配置", f"工具配置文件无法读取，请先修复或删除它：\n{self.settings.load_error}")
            return
        dialog = ProxyDialog(
            self.settings.proxy,
            status_text=self.proxy_state_text(),
            log_lines=self.proxy_server.log_lines,
            enable_tooltip=self.model_adaptation_text(),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.result_proxy is None:
            return
        proxy = dialog.result_proxy
        applied = self.settings.proxy.get("applied")  # owned by ProxyController, not the dialog
        proxy.pop("applied", None)
        if applied:
            proxy["applied"] = applied
        try:
            self.settings.set_proxy(proxy)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "代理配置", f"无法保存代理配置（{display_path(self.settings.path)} / {display_path(self.settings.env.path)}）:\n{exc}")
            return
        self.apply_proxy()

    # ------------------------------------------------------------------ logs

    def right_page_changed(self) -> None:
        if self.right_panel.current_page() == RightPanel.PAGE_LOG:
            self.refresh_logs()

    def refresh_logs(self) -> None:
        """Re-read the log directory; keep the selected entry if it still exists."""
        combo = self.right_panel.log_combo
        try:
            names = self.exchange_log.entries()
        except OSError as exc:
            self.status.setText(f"无法读取日志目录 {display_path(self.exchange_log.root)}: {exc}")
            return
        if names != [combo.itemText(index) for index in range(combo.count())]:
            selected = combo.currentText()
            combo.clear()
            combo.addItems(names)
            if selected in names:
                combo.setCurrentText(selected)
        self.show_log()

    def show_log(self) -> None:
        panel = self.right_panel
        name = panel.log_combo.currentText()
        kind = panel.log_kind()
        log_dir = display_path(self.exchange_log.root)
        if not name:
            self.shown_log = None
            panel.log_view.setPlainText(f"还没有日志。开启代理后，每个请求会存到工具目录下的 {log_dir}/ 里。")
            return
        path = self.exchange_log.source_file(name, kind)
        try:
            stamp = path.stat().st_mtime_ns
        except FileNotFoundError:
            stamp = None
        except OSError as exc:
            stamp = str(exc)
        key = (name, kind, stamp)
        if key == self.shown_log:
            return  # unchanged: keep the scroll position
        self.shown_log = key
        folds = []
        try:
            # The log exactly as stored; the view only colours it (see log_highlight).
            text = json_dump(self.exchange_log.read(name, kind), indent=2)
            if kind == "response_merged":
                folds = default_folds(text)  # headers and body.tools start out folded
        except MissingLog:
            if kind == "request_forwarded":
                text = "这个请求没有被转发到上游（工具直接返回了错误，见「原始回复」），或请求仍在处理中。"
            elif kind.startswith("response"):
                text = "这份日志还没有回复：请求仍在进行中，或工具在收到回复前已退出。"
            else:
                text = f"日志文件不存在：{log_dir}/{name}/{path.name}"
        except Exception as exc:
            text = f"无法读取日志 {log_dir}/{name}/{path.name}:\n{type(exc).__name__}: {exc}"
        panel.log_view.show_text(text, folds)

    # ----------------------------------------------------------- prompt presets

    def save_prompts(self, prompts: dict) -> bool:
        """Store the prompt presets and let a running proxy use them at once."""
        if self.settings.load_error:
            self.status.setText(f"工具配置文件无法读取，未保存提示词预设：{self.settings.load_error}")
            return False
        try:
            self.settings.set_prompts(prompts)
        except OSError as exc:
            self.status.setText(f"无法保存提示词预设到 {display_path(self.settings.path)}: {exc}")
            return False
        self.status.setText(f"提示词预设已保存（当前预设：{self.settings.prompts['activePreset']}）")
        if self.proxy_server.running:
            self.apply_proxy(interactive=False)
        return True

    # ------------------------------------------------------------- documents

    # ------------------------------------------------------ prompt injections

    def scan_injections(self, previous: dict | None = None) -> dict | None:
        """Re-read the logs and Codex's models cache into cct/injections.json."""
        try:
            catalog = injections.scan(self.settings.log_dir, CODEX_HOME, previous)
            injections.save_catalog(catalog)
        except Exception as exc:
            QMessageBox.critical(self, "提示词注入", f"识别注入提示词失败：\n{type(exc).__name__}: {exc}")
            return None
        count = sum(1 for _ in injections.iter_nodes(catalog["items"]))
        self.status.setText(f"已识别 {count} 个注入提示词条目，保存到 cct/{injections.CATALOG_FILE.name}")
        if self.proxy_server.running:  # the filter is built from the catalog
            self.apply_proxy(interactive=False)
        return catalog

    def save_injections(self, state: dict) -> bool:
        """Store the injection switches and let a running proxy use them at once."""
        try:
            self.settings.set_injections(state)
        except OSError as exc:
            self.status.setText(f"无法保存提示词注入设置到 {display_path(self.settings.path)}: {exc}")
            return False
        saved = self.settings.injections
        if saved["blockAll"]:
            text = "提示词注入：拦截所有已识别的条目"
        elif saved["off"]:
            text = f"提示词注入：已取消勾选 {len(saved['off'])} 个条目"
        else:
            text = "提示词注入：不拦截任何条目"
        if self.proxy_server.running:
            if not self.apply_proxy(interactive=False):
                return False
        else:
            text += "（代理未开启，暂不起作用）"
        self.status.setText(text)
        return True

    def save_injection_view(self, view: dict) -> bool:
        """Store how the injection page looks; display only, the proxy is not touched."""
        try:
            self.settings.set_injection_view(view)
        except OSError as exc:
            self.status.setText(f"无法保存提示词注入页面的布局到 {display_path(self.settings.path)}: {exc}")
            return False
        return True

    def open_injections(self) -> None:
        if self.settings.load_error:
            QMessageBox.critical(self, "提示词注入", f"工具配置文件无法读取，请先修复或删除它：\n{self.settings.load_error}")
            return
        try:
            catalog = injections.load_catalog()
        except FileNotFoundError:
            catalog = self.scan_injections()
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "提示词注入", f"无法读取 cct/{injections.CATALOG_FILE.name}：\n{exc}")
            return
        if catalog is None:
            return
        InjectionDialog(
            catalog,
            self.settings.injections,
            save=self.save_injections,
            view=self.settings.injection_view,
            save_view=self.save_injection_view,
            rescan=lambda: self.scan_injections(self._current_catalog()),
            proxy_running=self.proxy_server.running,
            parent=self,
        ).exec()

    @staticmethod
    def _current_catalog() -> dict | None:
        try:
            return injections.load_catalog()
        except (OSError, ValueError):
            return None

    def confirm_discard(self) -> bool:
        """Ask before throwing away unsaved in-memory changes."""
        if not self.doc:
            return True
        if not self.apply_raw_edit(quiet=True) or self.dirty:
            return QMessageBox.question(
                self, "Unsaved changes", f"Discard unsaved changes to\n{self.doc.path}?"
            ) == YES
        return True

    def open_file(self, path: Path, *, force: bool = False) -> None:
        if not force and not self.confirm_discard():
            return
        try:
            doc = SessionDoc(path)
            doc.load()
        except Exception as exc:
            QMessageBox.critical(self, "Open failed", str(exc))
            return
        self.doc = doc
        self.dirty = False
        self.anchor_row = None
        self.reload_list()
        self.status.setText(f"Loaded {path} ({len(doc.entries)} entries)")

    def open_record_filter(self) -> None:
        if not self.apply_raw_edit():
            return
        kinds = COMMON_RECORD_KINDS | self.hidden_kinds
        if self.doc:
            kinds.update(entry_kind(entry.obj) for entry in self.doc.entries)
        kinds.difference_update(ROLE_COLORS)
        dialog = QDialog(self)
        dialog.setWindowTitle("聊天记录显示项目")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("勾选要显示的条目类型"))
        choices = QListWidget()
        layout.addWidget(choices)
        for kind in sorted(kinds):
            item = QListWidgetItem(kind)
            item.setCheckState(Qt.CheckState.Unchecked if kind in self.hidden_kinds else Qt.CheckState.Checked)
            choices.addItem(item)

        def toggle(item: QListWidgetItem) -> None:
            if item.checkState() == Qt.CheckState.Checked:
                self.hidden_kinds.discard(item.text())
            else:
                self.hidden_kinds.add(item.text())
            self.reload_list(self.shown_row or 0)
            if not self.settings.load_error:
                self.settings.data["hiddenEntryKinds"] = sorted(self.hidden_kinds)
                try:
                    self.settings.save()
                except OSError as exc:
                    self.status.setText(f"无法保存聊天记录显示设置：{exc}")

        choices.itemChanged.connect(toggle)
        close = QPushButton("关闭")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.resize(380, 420)
        dialog.exec()

    def reload_list(self, select_row: int = 0) -> None:
        self.anchor_row = None
        self.list.clear()
        self.shown_row = None
        self.rendered.clear()
        self.raw.clear()
        if not self.doc:
            return
        self.doc.renumber_lines()
        for index, entry in enumerate(self.doc.entries):
            item = QListWidgetItem(self.list_label(index, entry))
            item.setData(USER_ROLE, index)
            color = ROLE_COLORS.get(entry_kind(entry.obj))
            if color:
                item.setForeground(QColor(color))
            self.list.addItem(item)
            item.setHidden(entry_kind(entry.obj) in self.hidden_kinds)
        self.select_visible_row(select_row)

    def select_visible_row(self, preferred: int) -> None:
        visible = (i for i in range(self.list.count()) if not self.list.item(i).isHidden())
        row = None
        for row in visible:
            if row >= preferred:
                break
        if row is not None:
            self.list.setCurrentRow(row)
            self.show_entry(row)
        else:
            self.list.setCurrentRow(-1)
            self.shown_row = None
            self.rendered.clear()
            self.raw.clear()

    @staticmethod
    def list_label(index: int, entry: Entry) -> str:
        obj = entry.obj
        return f"{index + 1:04d}  [{entry_kind(obj)}] {obj.get('timestamp', '')}  {entry_preview(obj)}"

    def show_entry(self, row: int) -> None:
        if not self.doc or row < 0 or row >= len(self.doc.entries):
            return
        entry = self.doc.entries[row]
        self.shown_row = row
        self.rendered.show_segments(render_segments(row, entry.line, entry.obj))
        self.raw.setPlainText(json_dump(entry.obj, indent=2))

    def apply_raw_edit(self, *, quiet: bool = False) -> bool:
        """Apply edits made in the raw tab to the entry being shown.

        Returns False if the raw text is not a valid JSON object.
        """
        if not self.doc or self.shown_row is None or self.shown_row >= len(self.doc.entries):
            return True
        entry = self.doc.entries[self.shown_row]
        text = self.raw.toPlainText()
        if text == json_dump(entry.obj, indent=2):
            return True
        try:
            obj = json.loads(text)
            if not isinstance(obj, dict):
                raise ValueError("top-level value must be a JSON object")
        except Exception as exc:
            if not quiet:
                QMessageBox.warning(self, "Invalid JSON", f"原始内容不是合法的 JSON 对象，未应用：\n{exc}")
            return False
        if obj != entry.obj:
            entry.obj = obj
            entry.raw = None
            self.dirty = True
            self.list.item(self.shown_row).setText(self.list_label(self.shown_row, entry))
            self.rendered.show_segments(render_segments(self.shown_row, entry.line, obj))
            if entry_kind(obj) in self.hidden_kinds:
                self.list.item(self.shown_row).setHidden(True)
                self.select_visible_row(self.shown_row)
        return True

    def item_clicked(self, item: QListWidgetItem) -> None:
        row = item.data(USER_ROLE)
        if row is None:
            return
        if row != self.shown_row and not self.apply_raw_edit():
            # Keep the user on the entry whose raw edit is invalid.
            if self.shown_row is not None and not self.edit_mode.isChecked():
                self.list.setCurrentRow(self.shown_row)
            return
        if self.edit_mode.isChecked():
            self.select_range_by_click(row, item)
        self.show_entry(row)
        if self.dirty:
            self.status.setText("有未保存的修改，点击“保存到文件”写入")

    def toggle_edit_selection_mode(self) -> None:
        enabled = self.edit_mode.isChecked()
        self.anchor_row = None
        self.list.clearSelection()
        self.list.setSelectionMode(EXTENDED_SELECTION if enabled else SINGLE_SELECTION)
        if not enabled and self.shown_row is not None:
            self.list.setCurrentRow(self.shown_row)
        self.status.setText(
            "Selection mode enabled: click start, then end to select a range"
            if enabled
            else "Selection mode disabled"
        )

    def select_range_by_click(self, row: int, item: QListWidgetItem) -> None:
        if self.anchor_row is None:
            self.anchor_row = row
            self.list.clearSelection()
            item.setSelected(True)
            self.status.setText(f"Anchor set: {row + 1}")
            return
        start, end = sorted([self.anchor_row, row])
        self.list.clearSelection()
        for index in range(start, end + 1):
            if not self.list.item(index).isHidden():
                self.list.item(index).setSelected(True)
        self.anchor_row = None
        self.status.setText(f"Selected {start + 1}..{end + 1}")

    def selected_rows(self) -> list[int]:
        rows = {self.list.row(item) for item in self.list.selectedItems() if not item.isHidden()}
        if not rows and self.list.currentItem() and not self.list.currentItem().isHidden():
            rows = {self.list.currentRow()}
        return sorted(rows)

    def _remove_rows(self, rows: list[int]) -> None:
        assert self.doc is not None
        for row in reversed(rows):
            del self.doc.entries[row]
        self.dirty = True
        self.anchor_row = None
        self.edit_mode.setChecked(False)
        self.reload_list(select_row=rows[0])
        self.status.setText(f"已从内存中移除 {len(rows)} 条记录；尚未写入文件，点击“保存到文件”生效")

    def delete_selected(self) -> None:
        if not self.doc or not self.apply_raw_edit():
            return
        rows = self.selected_rows()
        if not rows:
            return
        if QMessageBox.question(self, "Delete", f"Delete {len(rows)} entries from memory?") != YES:
            return
        self._remove_rows(rows)

    def clear_tool_records(self) -> None:
        if not self.doc or not self.apply_raw_edit():
            return
        rows = [index for index, entry in enumerate(self.doc.entries) if is_tool_record(entry.obj)]
        if not rows:
            QMessageBox.information(self, "Clear tool records", "No tool call / tool output records found")
            return
        if QMessageBox.question(
            self,
            "Clear tool records",
            f"Remove {len(rows)} tool call / tool output entries from memory?\n{self.doc.path}",
        ) != YES:
            return
        self._remove_rows(rows)

    def save_current(self) -> None:
        if not self.doc:
            self.status.setText("No file open")
            return
        if not self.apply_raw_edit():
            return
        if self.doc.changed_on_disk():
            if QMessageBox.question(
                self,
                "File changed on disk",
                f"{self.doc.path}\n\n该文件在打开后已被其他程序修改（可能是 Codex 正在写入）。\n"
                "继续保存会覆盖磁盘上的新内容，是否继续？",
            ) != YES:
                return
        try:
            self.doc.save()
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.dirty = False
        self.status.setText(f"Saved {self.doc.path} ({len(self.doc.entries)} entries)")

    def find_next(self) -> None:
        query = self.search.text()
        count = self.list.count()
        if not query or not count or not self.doc:
            return
        if not self.apply_raw_edit():
            return
        start = max(self.list.currentRow() + 1, 0)
        needle = query.lower()
        for offset in range(count):
            row = (start + offset) % count
            if self.list.item(row).isHidden():
                continue
            haystack = self.list.item(row).text() + "\n" + json_dump(self.doc.entries[row].obj)
            if needle in haystack.lower():
                self.list.setCurrentRow(row)
                self.show_entry(row)
                self.status.setText(f"Match at {row + 1}")
                return
        self.status.setText("No match")

    def check_external_change(self) -> None:
        self.auto_refresh_tree()
        if self.right_panel.current_page() == RightPanel.PAGE_LOG:
            self.refresh_logs()
        if self.config.changed_on_disk() and not self.model_input.hasFocus():
            self.load_model_config()
            self.status.setText("config.toml changed on disk; reloaded provider/model")

        if not self.doc or not self.doc.changed_on_disk():
            return
        raw_edited = self.shown_row is not None and self.shown_row < len(self.doc.entries) and (
            self.raw.toPlainText() != json_dump(self.doc.entries[self.shown_row].obj, indent=2)
        )
        if self.dirty or raw_edited:
            self.status.setText(f"磁盘上的文件已变化，但有未保存的修改，未自动重新加载：{self.doc.path}")
            return
        path, row = self.doc.path, self.list.currentRow()
        self.open_file(path, force=True)
        if 0 <= row < self.list.count():
            self.select_visible_row(row)
        self.status.setText(f"Reloaded external changes: {path}")

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        if self.confirm_discard():
            # Codex must not be left pointing at a port nobody listens on.
            try:
                self.proxy_control.release()
            except Exception as exc:
                QMessageBox.warning(
                    self, "代理配置", f"退出时未能还原 Codex 的配置（base URL / {model_catalog.CONFIG_KEY}），请手动检查 {self.config.path}:\n{exc}"
                )
            event.accept()
        else:
            event.ignore()
