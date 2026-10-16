"""Runtime build identity: which version and which variant this program is.

The build scripts (win7/build_win7.py, win11/build_win11_webview.py and the
generated specs) write ``build_info.json`` next to the sources and bundle it at
the root of the frozen app; this module is the single place that reads it.

Version scheme is the build date ``YYYY.MM.DD`` so it sorts naturally and needs
no manual bumping. When the file is absent (running from a source checkout) the
version is ``0.0.0``, which is older than every published build, so a source run
will always consider itself out of date rather than mask an available update.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# PyInstaller onefile unpacks to sys._MEIPASS; in a checkout this file's
# parent's parent (common/ -> repo root) holds build_info.json.
BUNDLE_ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
BUILD_INFO_FILE = BUNDLE_ROOT / 'build_info.json'

DEV_VERSION = '0.0.0'
DEV_VARIANT = 'dev'


def _read_build_info():
    # type: () -> dict
    try:
        data = json.loads(BUILD_INFO_FILE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_BUILD_INFO = _read_build_info()

APP_VERSION = str(_BUILD_INFO.get('version') or DEV_VERSION)
BUILD_TIME = str(_BUILD_INFO.get('build_time') or '')
VARIANT = (str(_BUILD_INFO.get('variant') or '')
           or os.environ.get('STOCKCHART_VARIANT', '')
           or DEV_VARIANT)


def version_key(version):
    # type: (str) -> tuple
    """Turn ``2026.10.4`` into a comparable tuple, tolerating junk parts."""
    key = []
    for piece in str(version).strip().split('.'):
        digits = ''.join(ch for ch in piece if ch.isdigit())
        key.append(int(digits) if digits else 0)
    return tuple(key)


def is_newer(candidate, current):
    # type: (str, str) -> bool
    """True when *candidate* is a later release than *current*."""
    try:
        return version_key(candidate) > version_key(current)
    except TypeError:
        return False


def frozen_exe_path():
    # type: () -> Path | None
    """Absolute path of the running executable, or None in a source run."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable)
    return None
