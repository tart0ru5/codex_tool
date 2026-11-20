"""Command-line entrypoint for Codex Context Tool."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local GUI for Codex conversation records.")
    parser.add_argument("path", nargs="?", type=Path, default=None, help="rollout .jsonl file to open initially")
    parser.add_argument("--self-test", action="store_true", help="instantiate the UI and quit immediately")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from .main_window import MainWindow
    from .qt import QApplication, QTimer

    app = QApplication(sys.argv[:1])
    window = MainWindow(args.path)
    window.show()
    if args.self_test:
        QTimer.singleShot(100, app.quit)
    return app.exec()
