"""Serve the assets shared by every theme and both desktop variants.

``assets/fonts`` (the HarmonyOS Sans SC set) and ``assets/js`` (the update badge)
live once in <repo root>/assets and ship to the same relative path inside the
frozen bundle, so no theme carries its own copy.

When the auto-updater has published a UI overlay, files from
``~/Documents/StockChart/ui/<variant>/assets/...`` win over the frozen ones, which
lets a shared JS or font change ship as a few hundred KB instead of a new EXE.
"""
from pathlib import Path
import sys

from flask import Blueprint, send_from_directory

assets_bp = Blueprint('assets', __name__, url_prefix='/assets')

# PyInstaller onefile unpacks to sys._MEIPASS; in development the repo root is
# two levels above this file (common/routes/ -> repo root).
_BUNDLE_ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))
ASSET_ROOT = _BUNDLE_ROOT / 'assets'

_ALLOWED_SUFFIXES = {'.ttf', '.otf', '.woff', '.woff2', '.js'}


def _search_dirs(kind):
    # type: (str) -> list
    """Overlay directory first, then the frozen one."""
    dirs = []
    try:
        from common import updater
        overlay = updater.overlay_dir() / 'assets' / kind
        if overlay.is_dir():
            dirs.append(overlay)
    except Exception:  # nosec B110 - 覆盖层不可用时退回内置资源
        pass
    dirs.append(ASSET_ROOT / kind)
    return dirs


def _serve(kind, filename):
    # type: (str, str) -> object
    if '..' in filename or filename.startswith(('/', '\\')):
        return ('Forbidden', 403)
    if Path(filename).suffix.lower() not in _ALLOWED_SUFFIXES:
        return ('Forbidden', 403)
    for directory in _search_dirs(kind):
        candidate = directory / filename
        if candidate.is_file():
            return send_from_directory(str(directory), filename)
    return ('Not found', 404)


@assets_bp.route('/fonts/<path:filename>')
def serve_font(filename):
    """Return a font file from the shared directory; extension-whitelisted."""
    return _serve('fonts', filename)


@assets_bp.route('/js/<path:filename>')
def serve_script(filename):
    """Return a shared script (update.js) from the shared directory."""
    return _serve('js', filename)
