"""Right-panel page for the prompt presets.

A preset is an ordered list of entries. Each entry has a role, a text and an
on/off check box; one fixed entry stands for everything Codex sent. Enabled
entries above it are put in front of Codex's conversation, those below it
after. Every change is saved at once through the `save` callback.
"""
from __future__ import annotations

import copy
from typing import Any, Callable

from .qt import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    Qt,
    QVBoxLayout,
    QWidget,
    USER_ROLE,
    YES,
)
from .settings import COMMON_ROLES, new_prompt_preset

CODEX_ENTRY_LABEL = "【Codex 发来的完整内容】（固定条目，可拖动调整位置）"
PREVIEW_CHARS = 80


class PromptEntryDialog(QDialog):
    def __init__(self, entry: dict[str, Any], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("编辑提示词条目")
        self.resize(640, 460)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.role = QComboBox()
        self.role.setEditable(True)  # common roles from the list, or type any other
        self.role.addItems(COMMON_ROLES)
        self.role.setCurrentText(entry["role"])
        form.addRow("role:", self.role)
        layout.addLayout(form)
        layout.addWidget(QLabel("内容:"))
        self.content = QPlainTextEdit()
        self.content.setPlainText(entry["content"])
        layout.addWidget(self.content, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self) -> None:
        if not self.role.currentText().strip():
            QMessageBox.warning(self, "编辑提示词条目", "role 不能为空。")
            return
        self.accept()

    def values(self) -> tuple[str, str]:
        return self.role.currentText().strip(), self.content.toPlainText()


class PromptPanel(QWidget):
    def __init__(
        self,
        *,
        load: Callable[[], dict[str, Any]],
        save: Callable[[dict[str, Any]], bool],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._save = save
        self._load = load
        self.prompts = load()
        self._filling = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        top = QHBoxLayout()
        self.combo = QComboBox()
        self.combo.activated.connect(lambda _index: self._preset_chosen())
        top.addWidget(self.combo, 1)
        for label, handler in (
            ("新建", self.new_preset),
            ("重命名", self.rename_preset),
            ("复制", self.copy_preset),
            ("删除", self.delete_preset),
        ):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, h=handler: h())
            top.addWidget(button)
        layout.addLayout(top)

        self.list = QListWidget()
        self.list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list.setWordWrap(False)
        self.list.itemDoubleClicked.connect(lambda item: self.edit_entry(item))
        self.list.itemChanged.connect(lambda _item: self._list_changed())
        self.list.model().rowsMoved.connect(lambda *_args: self._list_changed())
        layout.addWidget(self.list, 1)

        bottom = QHBoxLayout()
        for label, handler in (("新增条目", self.add_entry), ("编辑条目", self.edit_entry), ("删除条目", self.delete_entry)):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, h=handler: h())
            bottom.addWidget(button)
        bottom.addStretch(1)
        layout.addLayout(bottom)
        self._fill()

    # -- model <-> widgets ---------------------------------------------------

    def _names(self) -> list[str]:
        return [preset["name"] for preset in self.prompts["presets"]]

    def _preset(self) -> dict[str, Any]:
        for preset in self.prompts["presets"]:
            if preset["name"] == self.prompts["activePreset"]:
                return preset
        return self.prompts["presets"][0]

    @staticmethod
    def _label(entry: dict[str, Any]) -> str:
        if entry.get("codex"):
            return CODEX_ENTRY_LABEL
        text = " ".join(entry["content"].split())
        if len(text) > PREVIEW_CHARS:
            text = text[:PREVIEW_CHARS] + "…"
        return f"[{entry['role']}] {text or '（空）'}"

    def _make_item(self, entry: dict[str, Any]) -> QListWidgetItem:
        item = QListWidgetItem(self._label(entry))
        item.setData(USER_ROLE, copy.deepcopy(entry))
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
        if entry.get("codex"):
            item.setForeground(Qt.GlobalColor.darkGray)
        else:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
            item.setCheckState(Qt.CheckState.Checked if entry["enabled"] else Qt.CheckState.Unchecked)
            item.setToolTip(entry["content"][:2000])
        item.setFlags(flags)  # no ItemIsDropEnabled: dropping moves, never replaces
        return item

    def _fill(self) -> None:
        self._filling = True
        try:
            self.combo.clear()
            self.combo.addItems(self._names())
            self.combo.setCurrentText(self._preset()["name"])
            self.list.clear()
            for entry in self._preset()["entries"]:
                self.list.addItem(self._make_item(entry))
        finally:
            self._filling = False

    def _entries_from_list(self) -> list[dict[str, Any]]:
        entries = []
        for row in range(self.list.count()):
            item = self.list.item(row)
            entry = copy.deepcopy(item.data(USER_ROLE))
            if not entry.get("codex"):
                entry["enabled"] = item.checkState() == Qt.CheckState.Checked
            entries.append(entry)
        return entries

    def _commit(self, prompts: dict[str, Any]) -> bool:
        """Save `prompts`; on failure keep showing what is really stored."""
        ok = self._save(prompts)
        self.prompts = self._load()
        self._fill()
        return ok

    def _list_changed(self) -> None:
        """A check box was toggled or an entry was dragged to a new place."""
        if self._filling:
            return
        entries = self._entries_from_list()
        if entries == self._preset()["entries"]:
            return
        row = self.list.currentRow()
        prompts = copy.deepcopy(self.prompts)
        for preset in prompts["presets"]:
            if preset["name"] == prompts["activePreset"]:
                preset["entries"] = entries
        self._commit(prompts)
        self.list.setCurrentRow(row)

    def _set_entries(self, entries: list[dict[str, Any]], select: int) -> None:
        prompts = copy.deepcopy(self.prompts)
        for preset in prompts["presets"]:
            if preset["name"] == prompts["activePreset"]:
                preset["entries"] = entries
        self._commit(prompts)
        self.list.setCurrentRow(min(select, self.list.count() - 1))

    # -- presets -------------------------------------------------------------

    def _preset_chosen(self) -> None:
        name = self.combo.currentText()
        if name == self.prompts["activePreset"]:
            return
        prompts = copy.deepcopy(self.prompts)
        prompts["activePreset"] = name
        self._commit(prompts)

    def _ask_name(self, title: str, initial: str = "") -> str | None:
        name, ok = QInputDialog.getText(self, title, "预设名称:", text=initial)
        name = name.strip()
        if not ok or not name:
            return None
        if name in self._names() and name != initial:
            QMessageBox.warning(self, title, f"已有名为「{name}」的预设。")
            return None
        return name

    def add_preset(self, name: str, entries: list[dict[str, Any]] | None = None) -> None:
        prompts = copy.deepcopy(self.prompts)
        preset = new_prompt_preset(name)
        if entries is not None:
            preset["entries"] = copy.deepcopy(entries)
        prompts["presets"].append(preset)
        prompts["activePreset"] = name
        self._commit(prompts)

    def new_preset(self) -> None:
        name = self._ask_name("新建提示词预设")
        if name:
            self.add_preset(name)

    def copy_preset(self) -> None:
        base = self._preset()["name"] + " copy"
        initial, number = base, 2
        while initial in self._names():
            initial, number = f"{base} {number}", number + 1
        name = self._ask_name("复制提示词预设", initial)
        if name and name not in self._names():
            self.add_preset(name, self._preset()["entries"])

    def rename_to(self, name: str) -> None:
        prompts = copy.deepcopy(self.prompts)
        for preset in prompts["presets"]:
            if preset["name"] == prompts["activePreset"]:
                preset["name"] = name
        prompts["activePreset"] = name
        self._commit(prompts)

    def rename_preset(self) -> None:
        current = self._preset()["name"]
        name = self._ask_name("重命名提示词预设", current)
        if name and name != current:
            self.rename_to(name)

    def remove_preset(self) -> None:
        prompts = copy.deepcopy(self.prompts)
        prompts["presets"] = [p for p in prompts["presets"] if p["name"] != prompts["activePreset"]]
        prompts["activePreset"] = prompts["presets"][0]["name"]
        self._commit(prompts)

    def delete_preset(self) -> None:
        if len(self.prompts["presets"]) <= 1:
            QMessageBox.information(self, "删除提示词预设", "至少要保留一个预设。")
            return
        name = self._preset()["name"]
        if QMessageBox.question(self, "删除提示词预设", f"删除预设「{name}」？") == YES:
            self.remove_preset()

    # -- entries -------------------------------------------------------------

    def _edit_dialog(self, entry: dict[str, Any]) -> tuple[str, str] | None:
        dialog = PromptEntryDialog(entry, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.values()

    def insert_entry(self, role: str, content: str, enabled: bool = True) -> None:
        entries = self._entries_from_list()
        row = self.list.currentRow()
        row = len(entries) if row < 0 else row + 1
        entries.insert(row, {"role": role, "content": content, "enabled": enabled})
        self._set_entries(entries, row)

    def add_entry(self) -> None:
        values = self._edit_dialog({"role": COMMON_ROLES[0], "content": ""})
        if values:
            self.insert_entry(*values)

    def update_entry(self, row: int, role: str, content: str) -> None:
        entries = self._entries_from_list()
        entries[row].update(role=role, content=content)
        self._set_entries(entries, row)

    def edit_entry(self, item: QListWidgetItem | None = None) -> None:
        row = self.list.row(item) if item is not None else self.list.currentRow()
        if row < 0:
            return
        entry = self.list.item(row).data(USER_ROLE)
        if entry.get("codex"):
            QMessageBox.information(self, "编辑提示词条目", "这一条代表 Codex 发来的完整内容，目前不能编辑。")
            return
        values = self._edit_dialog(entry)
        if values:
            self.update_entry(row, *values)

    def remove_entry(self, row: int) -> bool:
        entries = self._entries_from_list()
        if not 0 <= row < len(entries) or entries[row].get("codex"):
            return False
        del entries[row]
        self._set_entries(entries, row)
        return True

    def delete_entry(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        if self.list.item(row).data(USER_ROLE).get("codex"):
            QMessageBox.information(self, "删除提示词条目", "这一条代表 Codex 发来的完整内容，不能删除。")
            return
        self.remove_entry(row)
