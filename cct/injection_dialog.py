"""Dialog for switching the prompts Codex injects on and off.

Shows the catalog from injections.json as a tree. A checked entry is passed
on; an unchecked one is cut out of the requests by the proxy. Entries below an
unchecked one (and everything, while "block all" is ticked) are greyed out but
keep their own check state. Every change is saved at once through `save`.

The rest is display only and saved through `save_view`, never reaching the
proxy: the order of the entries (drag within one level), placeholder rows,
names given to top-level entries, which entries are expanded, the page size.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Any, Callable

from .qt import (
    QAbstractItemView,
    QBrush,
    QCheckBox,
    QColor,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    Qt,
    QTimer,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    USER_ROLE,
)
from .settings import PLACEHOLDER_PREFIX

CHECKED = Qt.CheckState.Checked
UNCHECKED = Qt.CheckState.Unchecked
BLOCKED_ROLE = USER_ROLE + 1  # column 0: a switch further up already blocks this entry
ORIGINAL_TITLE_ROLE = USER_ROLE + 2  # column 0: the entry's title in the catalog
DEFAULT_SIZE = (980, 680)
PLACEHOLDER_NAME = "占位条目"
# (background, text) of placeholder rows on a light / dark theme
PLACEHOLDER_COLORS = {False: ("#FFF1C9", "#7A5200"), True: ("#4D3D10", "#F2D98C")}


class _BlockedDelegate(QStyledItemDelegate):
    """Draws blocked entries greyed out and keeps their check boxes from toggling.

    The items stay enabled, so they can still be selected and dragged.
    """

    @staticmethod
    def _blocked(index) -> bool:
        return bool(index.sibling(index.row(), 0).data(BLOCKED_ROLE))

    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        if self._blocked(index):
            option.state = option.state & ~QStyle.StateFlag.State_Enabled

    def editorEvent(self, event, model, option, index) -> bool:
        if self._blocked(index):
            return False
        return super().editorEvent(event, model, option, index)


class _Tree(QTreeWidget):
    """Tree whose drops are handed to `on_drop` instead of Qt's own item move."""

    on_drop: Callable[[QTreeWidgetItem, QTreeWidgetItem | None, int], Any] | None = None

    def dropEvent(self, event) -> None:
        selected = self.selectedItems()
        if event.source() is not self or not selected or self.on_drop is None:
            event.ignore()
            return
        point = event.position().toPoint()
        self.on_drop(selected[0], self.itemAt(point), point.y())
        # The move (if any) is done; keep Qt from moving or removing rows itself.
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()


class InjectionDialog(QDialog):
    def __init__(
        self,
        catalog: dict[str, Any],
        state: dict[str, Any],
        *,
        save: Callable[[dict[str, Any]], bool],
        rescan: Callable[[], dict[str, Any] | None],
        proxy_running: bool,
        view: dict[str, Any] | None = None,
        save_view: Callable[[dict[str, Any]], bool] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("提示词注入")
        self.save = save
        self.rescan = rescan
        self.save_view = save_view or (lambda _view: True)
        self.state = {"blockAll": bool(state.get("blockAll")), "off": list(state.get("off") or [])}
        view = view or {}
        self.view: dict[str, Any] = {
            "order": {key: list(ids) for key, ids in (view.get("order") or {}).items()},
            "placeholders": dict(view.get("placeholders") or {}),
            "titles": dict(view.get("titles") or {}),
            "expanded": list(view["expanded"]) if isinstance(view.get("expanded"), list) else None,
            "size": list(view["size"]) if view.get("size") else None,
        }
        self.expanded: set[str] = set(self.view["expanded"] or [])
        self.catalog_order: dict[str, list[str]] = {}
        self._busy = 0  # > 0 while the tree is changed by code, not by the user
        self.resize(*(self.view["size"] or DEFAULT_SIZE))

        layout = QVBoxLayout(self)
        self.block_all = QCheckBox("一键取消所有已识别的注入提示词（勾选后，下面的条目全部被拦截；各条目自己的勾选状态保留）")
        self.block_all.setChecked(self.state["blockAll"])
        self.block_all.stateChanged.connect(lambda _state: self._changed())
        layout.addWidget(self.block_all)

        hint = QLabel(
            "勾选 = 照常传给 AI；取消勾选 = 代理转发时拦截该条目。上级条目被取消勾选时，它的下级条目变灰。\n"
            "同级条目可以拖动调整顺序（只影响本页的显示，不影响实际发送）。右键条目可以在它下方添加占位条目、编辑名称。"
            + ("" if proxy_running else "\n代理当前未开启：这里的设置要在代理开启后才起作用。")
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.tree = _Tree()
        self.tree.setHeaderLabels(["条目", "类型", "字符数", "内容开头"])
        self.tree.setUniformRowHeights(True)
        self.tree.setItemDelegate(_BlockedDelegate(self.tree))
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.tree.setDropIndicatorShown(True)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.on_drop = self.move_item
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_menu)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.itemExpanded.connect(lambda item: self._expansion_changed(item, True))
        self.tree.itemCollapsed.connect(lambda item: self._expansion_changed(item, False))
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        for column, width in enumerate([460, 150, 60]):
            self.tree.setColumnWidth(column, width)
        layout.addWidget(self.tree, 1)

        self.source_label = QLabel()
        self.source_label.setWordWrap(True)
        layout.addWidget(self.source_label)

        row = QHBoxLayout()
        self.rescan_button = QPushButton("重新识别")
        self.rescan_button.setToolTip("重新读取代理日志和 Codex 的 models_cache，更新条目列表（已有条目保留）")
        self.rescan_button.clicked.connect(lambda _checked=False: self._rescan())
        row.addWidget(self.rescan_button)
        for label, expand in [("全部展开", True), ("全部收起", False)]:
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, e=expand: self.set_all_expanded(e))
            row.addWidget(button)
        row.addStretch(1)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(lambda _checked=False: self.accept())
        row.addWidget(close_button)
        layout.addLayout(row)

        self._size_timer = QTimer(self)
        self._size_timer.setSingleShot(True)
        self._size_timer.setInterval(400)
        self._size_timer.timeout.connect(self._save_size)

        self._fill(catalog)

    # --------------------------------------------------------------- helpers

    @contextmanager
    def _quiet(self):
        """Changes made inside are the code's own: not saved as user actions."""
        self._busy += 1
        try:
            yield
        finally:
            self._busy -= 1

    @staticmethod
    def item_id(item: QTreeWidgetItem | None) -> str:
        return "" if item is None else str(item.data(0, USER_ROLE) or "")

    @classmethod
    def is_placeholder(cls, item: QTreeWidgetItem | None) -> bool:
        return cls.item_id(item).startswith(PLACEHOLDER_PREFIX)

    @staticmethod
    def is_blocked(item: QTreeWidgetItem) -> bool:
        return bool(item.data(0, BLOCKED_ROLE))

    def _container(self, item: QTreeWidgetItem) -> QTreeWidgetItem:
        """The item holding `item`: its parent, or the invisible root."""
        return item.parent() or self.tree.invisibleRootItem()

    def _all_items(self, start: QTreeWidgetItem | None = None):
        """Every row below `start` (default: the whole tree), placeholders included."""
        start = start or self.tree.invisibleRootItem()
        for index in range(start.childCount()):
            child = start.child(index)
            yield child
            yield from self._all_items(child)

    def _items(self):
        """The catalog entries (rows with a check box), without the placeholders."""
        return (item for item in self._all_items() if not self.is_placeholder(item))

    # ------------------------------------------------------------------ tree

    def _new_placeholder(self, placeholder_id: str, name: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem([name])
        item.setData(0, USER_ROLE, placeholder_id)
        dark = self.tree.palette().base().color().lightness() < 128
        background, text = PLACEHOLDER_COLORS[dark]
        item.setBackground(0, QBrush(QColor(background)))
        item.setForeground(0, QBrush(QColor(text)))
        font = item.font(0)
        font.setItalic(True)
        item.setFont(0, font)
        item.setToolTip(0, "占位条目：只用于在本页分隔和标注，不起实际作用。双击或右键可改名。")
        return item

    def _fill(self, catalog: dict[str, Any]) -> None:
        off = set(self.state["off"])
        order = self.view["order"]
        placeholders = self.view["placeholders"]
        titles = self.view["titles"]
        self.catalog_order = {}

        def add_children(parent: QTreeWidgetItem, parent_id: str, nodes: list[dict[str, Any]]) -> None:
            by_id = {str(node.get("id")): node for node in nodes}
            self.catalog_order[parent_id] = list(by_id)
            sequence = [i for i in dict.fromkeys(order.get(parent_id) or []) if i in by_id or i in placeholders]
            sequence += [i for i in by_id if i not in sequence]  # entries the saved order does not know
            for item_id in sequence:
                node = by_id.get(item_id)
                if node is None:
                    parent.addChild(self._new_placeholder(item_id, placeholders[item_id]))
                    continue
                chars = node.get("chars") or 0
                title = str(node.get("title", ""))
                item = QTreeWidgetItem(
                    parent,
                    [title, str(node.get("note", "")), str(chars) if chars else "", str(node.get("preview", ""))],
                )
                item.setData(0, USER_ROLE, item_id)
                item.setData(0, ORIGINAL_TITLE_ROLE, title)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, UNCHECKED if item_id in off else CHECKED)
                if parent_id == "" and item_id in titles:
                    self._show_title(item, titles[item_id])
                if node.get("preview"):
                    item.setToolTip(3, str(node["preview"]))
                add_children(item, item_id, node.get("children") or [])

        with self._quiet():
            self.tree.clear()
            root = self.tree.invisibleRootItem()
            add_children(root, "", catalog.get("items") or [])
            if self.view["expanded"] is None:  # never saved: top level open
                self.expanded = {self.item_id(root.child(i)) for i in range(root.childCount()) if root.child(i).childCount()}
            self._decorate(root)
        self._update_enabled()
        sources = "、".join(catalog.get("sources") or []) or "无"
        self.source_label.setText(
            f"条目来源：{sources}；识别时间：{catalog.get('generated') or '未知'}。结构保存在工具的 cct/injections.json。"
        )

    def _decorate(self, start: QTreeWidgetItem) -> None:
        """Re-apply what the view forgets when rows are (re)inserted: spans, expansion."""
        with self._quiet():
            items = list(self._all_items(start))
            if start is not self.tree.invisibleRootItem():
                items.insert(0, start)
            for item in items:
                if self.is_placeholder(item):
                    item.setFirstColumnSpanned(True)
                elif item.childCount():
                    item.setExpanded(self.item_id(item) in self.expanded)

    def _show_title(self, item: QTreeWidgetItem, title: str | None) -> None:
        original = str(item.data(0, ORIGINAL_TITLE_ROLE) or "")
        item.setText(0, title or original)
        item.setToolTip(0, f"原名称：{original}" if title else "")

    def _update_enabled(self) -> None:
        """Grey out what a switch further up already blocks."""

        def walk(item: QTreeWidgetItem, blocked: bool) -> None:
            for index in range(item.childCount()):
                child = item.child(index)
                if self.is_placeholder(child):
                    continue
                if self.is_blocked(child) != blocked:
                    child.setData(0, BLOCKED_ROLE, blocked)
                walk(child, blocked or child.checkState(0) == UNCHECKED)

        with self._quiet():
            walk(self.tree.invisibleRootItem(), self.block_all.isChecked())
        self.tree.viewport().update()

    # ------------------------------------------------------- switches (proxy)

    def _item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column == 0 and not self._busy and not self.is_placeholder(item):
            self._changed()

    def _changed(self) -> None:
        shown = {self.item_id(item) for item in self._items()}
        off = {self.item_id(item) for item in self._items() if item.checkState(0) == UNCHECKED}
        # Switches of entries that are not in the list right now are kept as they are.
        kept = {item for item in self.state["off"] if item not in shown}
        self.state = {"blockAll": self.block_all.isChecked(), "off": sorted(off | kept)}
        self._update_enabled()
        self.save(self.state)

    def _rescan(self) -> None:
        catalog = self.rescan()
        if catalog is not None:
            self._fill(catalog)

    # ------------------------------------------------------ layout (display)

    def collect_view(self) -> dict[str, Any]:
        """The page layout as it is now. Data about entries not shown is kept."""
        root = self.tree.invisibleRootItem()
        parents = [("", root)] + [(self.item_id(item), item) for item in self._items()]
        shown_parents = {parent_id for parent_id, _item in parents}
        order = {key: ids for key, ids in self.view["order"].items() if key not in shown_parents}
        placeholders = {
            key: name
            for key, name in self.view["placeholders"].items()
            if any(key in ids for ids in order.values())
        }
        for parent_id, parent in parents:
            ids = [self.item_id(parent.child(index)) for index in range(parent.childCount())]
            if ids != self.catalog_order.get(parent_id, []):
                order[parent_id] = ids
        for item in self._all_items():
            if self.is_placeholder(item):
                placeholders[self.item_id(item)] = item.text(0)
        return {
            "order": order,
            "placeholders": placeholders,
            "titles": dict(self.view["titles"]),
            "expanded": sorted(self.expanded),
            "size": [self.width(), self.height()],
        }

    def _view_changed(self) -> None:
        self.view = self.collect_view()
        self.save_view(self.view)

    def _expansion_changed(self, item: QTreeWidgetItem, expanded: bool) -> None:
        if self._busy:
            return
        (self.expanded.add if expanded else self.expanded.discard)(self.item_id(item))
        self._view_changed()

    def set_all_expanded(self, expanded: bool) -> None:
        shown = {self.item_id(item) for item in self._items()}
        parents = {self.item_id(item) for item in self._items() if item.childCount()}
        # Ids of entries that are not in the list right now are kept as they are.
        self.expanded = (self.expanded | parents) if expanded else (self.expanded - shown)
        self._decorate(self.tree.invisibleRootItem())
        self._view_changed()

    def move_item(self, dragged: QTreeWidgetItem, target: QTreeWidgetItem | None, y: int | None = None) -> bool:
        """Move `dragged` next to `target` within its own level; False if not possible.

        `target` may be any row: one inside another entry of the same level
        counts as that entry. With `y` (viewport coordinate) the upper half of
        the target row means "before it"; otherwise the entry goes after it.
        `target` None moves it to the end of its level.
        """
        parent = self._container(dragged)
        level = self.item_id(dragged.parent())
        if target is None:
            sibling, after = None, True
        else:
            sibling = target
            while sibling is not None and self.item_id(sibling.parent()) != level:
                sibling = sibling.parent()
            if sibling is None or self.item_id(sibling) == self.item_id(dragged):
                return False  # another level, or dropped onto itself / its own children
            after = True
            if sibling is target and y is not None:
                after = y >= self.tree.visualItemRect(target).center().y()
        with self._quiet():
            parent.takeChild(parent.indexOfChild(dragged))
            index = parent.childCount() if sibling is None else parent.indexOfChild(sibling) + (1 if after else 0)
            parent.insertChild(index, dragged)
            self._decorate(dragged)
            self.tree.setCurrentItem(dragged)
        self._view_changed()
        return True

    def add_placeholder(self, below: QTreeWidgetItem | None = None, name: str = PLACEHOLDER_NAME) -> QTreeWidgetItem:
        """Add a placeholder row right below `below` (same level), or at the very end."""
        item = self._new_placeholder(f"{PLACEHOLDER_PREFIX}{uuid.uuid4().hex[:12]}", name)
        with self._quiet():
            if below is None:
                self.tree.invisibleRootItem().addChild(item)
            else:
                parent = self._container(below)
                parent.insertChild(parent.indexOfChild(below) + 1, item)
            self._decorate(item)
            self.tree.setCurrentItem(item)
        self._view_changed()
        return item

    def remove_placeholder(self, item: QTreeWidgetItem) -> None:
        if not self.is_placeholder(item):
            return
        with self._quiet():
            parent = self._container(item)
            parent.takeChild(parent.indexOfChild(item))
        self._view_changed()

    def can_rename(self, item: QTreeWidgetItem | None) -> bool:
        """Placeholders, and the top-level entries (whose titles are just given names)."""
        return item is not None and (self.is_placeholder(item) or item.parent() is None)

    def rename(self, item: QTreeWidgetItem, name: str) -> bool:
        """Rename a placeholder or a top-level entry. An empty name restores an entry's own title."""
        if not self.can_rename(item):
            return False
        name = name.strip()
        with self._quiet():
            if self.is_placeholder(item):
                if not name:
                    return False
                item.setText(0, name)
            else:
                original = str(item.data(0, ORIGINAL_TITLE_ROLE) or "")
                if name and name != original:
                    self.view["titles"][self.item_id(item)] = name
                else:
                    self.view["titles"].pop(self.item_id(item), None)
                    name = ""
                self._show_title(item, name or None)
        self._view_changed()
        return True

    def _ask_rename(self, item: QTreeWidgetItem) -> None:
        placeholder = self.is_placeholder(item)
        label = "占位条目的名称：" if placeholder else "条目名称（留空则恢复原名称）："
        name, ok = QInputDialog.getText(self, "编辑名称", label, QLineEdit.EchoMode.Normal, item.text(0))
        if ok:
            self.rename(item, name)

    def _double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if self.is_placeholder(item):
            self._ask_rename(item)

    def menu_actions(self, item: QTreeWidgetItem | None) -> list[tuple[str, Callable[[], Any]]]:
        """Context menu entries for `item` (None: the empty area below the rows)."""
        if item is None:
            return [("在末尾添加占位条目", lambda: self.add_placeholder(None))]
        actions: list[tuple[str, Callable[[], Any]]] = [("在下方添加占位条目", lambda: self.add_placeholder(item))]
        if self.can_rename(item):
            actions.append(("编辑名称…", lambda: self._ask_rename(item)))
        if self.is_placeholder(item):
            actions.append(("删除占位条目", lambda: self.remove_placeholder(item)))
        elif self.item_id(item) in self.view["titles"]:
            actions.append(("恢复原名称", lambda: self.rename(item, "")))
        return actions

    def _show_menu(self, point) -> None:
        item = self.tree.itemAt(point)
        menu = QMenu(self)
        for label, handler in self.menu_actions(item):
            menu.addAction(label).triggered.connect(lambda _checked=False, h=handler: h())
        menu.exec(self.tree.viewport().mapToGlobal(point))

    # ------------------------------------------------------------- page size

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.isVisible():
            self._size_timer.start()

    def _save_size(self) -> None:
        self._size_timer.stop()
        if [self.width(), self.height()] != self.view["size"]:
            self._view_changed()

    def done(self, result: int) -> None:
        if self.isVisible():
            self._save_size()
        super().done(result)
