"""HTTP surface for the self-updater: status polling plus manual check / install.

The updater owns the actual restart; these routes only drive
:mod:`common.updater` and report its progress so the page badge can show it.
"""
import sys

from flask import Blueprint, jsonify

from common import updater
from common.version import APP_VERSION, VARIANT

update_bp = Blueprint('update', __name__, url_prefix='/api/update')


@update_bp.route('/status', methods=['GET'])
def status():
    """Current version, variant and progress of any running update."""
    payload = updater.status()
    payload.update({
        'local_version': APP_VERSION,
        'variant': VARIANT,
        'overlay_active': updater.overlay_is_active(),
        'overlay_version': updater.overlay_version(),
        'frozen': bool(getattr(sys, 'frozen', False)),
    })
    return jsonify({'success': True, **payload})


@update_bp.route('/check', methods=['POST'])
def check():
    """Start an update check (and install, matching the startup behaviour)."""
    started = updater.start_manual_check()
    return jsonify({
        'success': True,
        'started': started is not None,
        'status': updater.status(),
    })


@update_bp.route('/install', methods=['POST'])
def install():
    """Install an EXE that was downloaded but not yet swapped in, then relaunch."""
    try:
        updater.apply_staged_update()   # only returns if it raised
    except updater.UpdateError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    return jsonify({'success': True, 'message': '正在重启以完成更新'})
