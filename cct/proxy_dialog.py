"""Proxy configuration dialog."""
from __future__ import annotations

import copy
import json
from typing import Any, Callable

from .qt import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
    YES,
)
from .settings import MAX_PORT, MIN_PORT, new_preset

REASONING_EFFORTS = ["", "none", "minimal", "low", "medium", "high", "xhigh"]


class ProxyDialog(QDialog):
    """Edits a copy of the proxy settings; read `result_proxy` after accept()."""

    def __init__(
        self,
        proxy: dict[str, Any],
        *,
        status_text: str,
        log_lines: Callable[[], list[str]],
        enable_tooltip: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("代理配置")
        self.resize(640, 720)
        self.proxy = copy.deepcopy(proxy)
        self.result_proxy: dict[str, Any] | None = None
        self._log_lines = log_lines
        self._current_name = self.proxy["activePreset"]

        layout = QVBoxLayout(self)

        self.enabled = QCheckBox("开启代理（开启后自动把 Codex 的 base URL 指向本机监听端口）")
        self.enabled.setChecked(self.proxy["enabled"])
        self.enabled.setToolTip(enable_tooltip)
        layout.addWidget(self.enabled)

        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("监听端口:"))
        self.port = QSpinBox()
        self.port.setRange(MIN_PORT, MAX_PORT)
        self.port.setValue(self.proxy["port"])
        port_row.addWidget(self.port)
        port_row.addWidget(QLabel("（127.0.0.1；端口被占用时无法开启）"))
        port_row.addStretch(1)
        layout.addLayout(port_row)

        self.state_label = QLabel(status_text)
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)

        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("API 预设:"))
        self.preset_combo = QComboBox()
        self.preset_combo.activated.connect(lambda _index: self._preset_chosen())
        preset_row.addWidget(self.preset_combo, 1)
        for label, handler in [("新建", self._new_preset), ("重命名", self._rename_preset), ("删除", self._delete_preset)]:
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, h=handler: h())
            preset_row.addWidget(button)
        layout.addLayout(preset_row)

        form = QFormLayout()
        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText("必填，例如 https://openrouter.ai/api/v1；不填时代理对所有请求报错")
        self.model = QLineEdit()
        self.model.setPlaceholderText("不填则透传 Codex 请求中的模型名")
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("不填则透传 Codex 自己的 Authorization；保存在工具目录的 .env 文件中")
        self.show_key = QCheckBox("显示")
        self.show_key.stateChanged.connect(
            lambda _state: self.api_key.setEchoMode(
                QLineEdit.EchoMode.Normal if self.show_key.isChecked() else QLineEdit.EchoMode.Password
            )
        )
        key_row = QHBoxLayout()
        key_row.addWidget(self.api_key, 1)
        key_row.addWidget(self.show_key)
        self.extra_headers = QPlainTextEdit()
        self.extra_headers.setPlaceholderText('JSON 对象，例如 {"X-Title": "codex"}；值为 null 表示删除该请求头')
        self.extra_body = QPlainTextEdit()
        self.extra_body.setPlaceholderText('JSON 对象，深度合并进请求体，例如 {"provider": {"sort": "price"}}')
        self.reasoning = QComboBox()
        self.reasoning.setEditable(True)
        self.reasoning.addItems(REASONING_EFFORTS)
        self.reasoning.lineEdit().setPlaceholderText("不填则透传 Codex 的配置")
        form.addRow("baseURL:", self.base_url)
        form.addRow("模型名:", self.model)
        form.addRow("API key:", key_row)
        form.addRow("额外请求头:", self.extra_headers)
        form.addRow("额外请求体:", self.extra_body)
        form.addRow("推理强度:", self.reasoning)
        layout.addLayout(form)

        log_row = QHBoxLayout()
        log_row.addWidget(QLabel("最近的代理请求:"))
        log_row.addStretch(1)
        refresh = QPushButton("刷新日志")
        refresh.clicked.connect(lambda _checked=False: self._refresh_log())
        log_row.addWidget(refresh)
        layout.addLayout(log_row)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存并应用")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._fill_combo()
        self._load_fields()
        self._refresh_log()

    # -- presets -------------------------------------------------------------

    def _names(self) -> list[str]:
        return [preset["name"] for preset in self.proxy["presets"]]

    def _preset(self, name: str) -> dict[str, Any]:
        return next(preset for preset in self.proxy["presets"] if preset["name"] == name)

    def _fill_combo(self) -> None:
        self.preset_combo.clear()
        self.preset_combo.addItems(self._names())
        self.preset_combo.setCurrentText(self._current_name)

    def _load_fields(self) -> None:
        preset = self._preset(self._current_name)
        self.base_url.setText(preset["baseUrl"])
        self.model.setText(preset["model"])
        self.api_key.setText(preset["apiKey"])
        for widget, value in ((self.extra_headers, preset["extraHeaders"]), (self.extra_body, preset["extraBody"])):
            widget.setPlainText(json.dumps(value, ensure_ascii=False, indent=2) if value else "")
        self.reasoning.setCurrentText(preset["reasoningEffort"])

    def _parse_object(self, label: str, text: str) -> dict[str, Any]:
        if not text.strip():
            return {}
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label} 不是合法的 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{label} 必须是 JSON 对象（{{...}}）")
        return value

    def _store_fields(self) -> bool:
        """Copy the form into the current preset. False (with a message) if invalid."""
        try:
            headers = self._parse_object("额外请求头", self.extra_headers.toPlainText())
            body = self._parse_object("额外请求体", self.extra_body.toPlainText())
            for name, value in headers.items():
                if isinstance(value, (dict, list)):
                    raise ValueError(f"额外请求头 {name!r} 的值必须是字符串、数字或 null")
            base_url = self.base_url.text().strip()
            if base_url and not base_url.lower().startswith(("http://", "https://")):
                raise ValueError("baseURL 必须以 http:// 或 https:// 开头")
        except ValueError as exc:
            QMessageBox.warning(self, "代理配置", str(exc))
            return False
        preset = self._preset(self._current_name)
        preset.update(
            baseUrl=base_url,
            model=self.model.text().strip(),
            apiKey=self.api_key.text().strip(),
            extraHeaders=headers,
            extraBody=body,
            reasoningEffort=self.reasoning.currentText().strip(),
        )
        return True

    def _preset_chosen(self) -> None:
        chosen = self.preset_combo.currentText()
        if chosen == self._current_name:
            return
        if not self._store_fields():
            self.preset_combo.setCurrentText(self._current_name)
            return
        self._current_name = chosen
        self._load_fields()

    def _ask_name(self, title: str, initial: str = "") -> str | None:
        name, ok = QInputDialog.getText(self, title, "预设名称:", text=initial)
        name = name.strip()
        if not ok or not name or name == initial:
            return None
        if name in self._names():
            QMessageBox.warning(self, title, f"已存在名为“{name}”的预设")
            return None
        return name

    def _new_preset(self) -> None:
        if not self._store_fields():
            return
        name = self._ask_name("新建预设")
        if name is None:
            return
        self.proxy["presets"].append(new_preset(name))
        self._current_name = name
        self._fill_combo()
        self._load_fields()

    def _rename_preset(self) -> None:
        name = self._ask_name("重命名预设", self._current_name)
        if name is None:
            return
        self._preset(self._current_name)["name"] = name
        self._current_name = name
        self._fill_combo()

    def _delete_preset(self) -> None:
        if len(self.proxy["presets"]) <= 1:
            QMessageBox.information(self, "删除预设", "至少需要保留一个预设")
            return
        if QMessageBox.question(self, "删除预设", f"删除预设“{self._current_name}”？") != YES:
            return
        self.proxy["presets"] = [p for p in self.proxy["presets"] if p["name"] != self._current_name]
        self._current_name = self.proxy["presets"][0]["name"]
        self._fill_combo()
        self._load_fields()

    # -- misc ----------------------------------------------------------------

    def _refresh_log(self) -> None:
        self.log.setPlainText("\n".join(self._log_lines()))
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _save(self) -> None:
        if not self._store_fields():
            return
        self.proxy["enabled"] = self.enabled.isChecked()
        self.proxy["port"] = self.port.value()
        self.proxy["activePreset"] = self._current_name
        self.result_proxy = self.proxy
        self.accept()
