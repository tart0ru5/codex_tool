#!/usr/bin/env python3
"""Codex Context Tool - local GUI for browsing/editing Codex conversation records."""
import sys


def _report_without_console(text: str) -> None:
    """run.bat starts us with pythonw.exe: no console, so show errors in a box."""
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, text[-3000:], "Codex Context Tool", 0x10)


if __name__ == "__main__":
    no_console = sys.platform == "win32" and sys.stderr is None
    try:
        from cct.cli import main

        code = main()
    except SystemExit as exc:
        if no_console and isinstance(exc.code, str):
            _report_without_console(exc.code)
        raise
    except Exception:
        if not no_console:
            raise
        import traceback

        _report_without_console(traceback.format_exc())
        code = 1
    raise SystemExit(code)
