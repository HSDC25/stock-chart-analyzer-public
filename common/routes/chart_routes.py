"""Chart export and save-dialog routes Blueprint.

Extracted from win11/app.py (chart/export and show-save-dialog endpoints).

Compatible with Python 3.8+.

简介：图表导出蓝图（chart_bp）——接收前端 Canvas 截图的 base64 数据，
保存为 PNG 文件（限制只能写到用户主目录内），以及唤起原生保存对话框。
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

logger = logging.getLogger("stockchart")

chart_bp = Blueprint('chart', __name__)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@chart_bp.route('/api/chart/export', methods=['POST'])
def api_chart_export():
    """Export a chart image and optionally save to a user-chosen path."""
    try:
        data = request.get_json() or {}
        img_data = data.get('image', '')
        filepath = data.get('filepath', '')

        if not img_data:
            return jsonify({'success': False, 'error': '没有图片数据'}), 400

        if ',' in img_data:
            img_data = img_data.split(',')[1]
        img_bytes = base64.b64decode(img_data)

        if filepath:
            save_path = Path(filepath)
            try:
                # 安全校验：解析真实路径并要求必须位于用户主目录内，
                # 防止任意路径写入（如覆盖系统文件）
                resolved = save_path.resolve()
                home = Path.home()
                resolved.relative_to(home)
            except (ValueError, RuntimeError):
                return jsonify({'error': 'Invalid save path'}), 403

            save_path.parent.mkdir(parents=True, exist_ok=True)
            if not str(save_path).lower().endswith('.png'):
                save_path = save_path.with_suffix('.png')

            with open(save_path, 'wb') as f:
                f.write(img_bytes)

            return jsonify({
                'success': True,
                'path': str(save_path),
                'message': '图表已保存到: %s' % save_path,
            })

        return jsonify({
            'success': True,
            'data': img_data,
            'message': '请在浏览器中下载',
        })
    except Exception as e:
        logger.exception("Failed to export chart image")
        return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500


@chart_bp.route('/api/show-save-dialog', methods=['GET'])
def api_show_save_dialog():
    """Open a native save-file dialog and return the chosen path.

    Uses an optional callback stored in ``current_app.config['SAVE_DIALOG_FN']``.
    When the callback is ``None`` (e.g. running without pywebview), returns
    an error response instead of crashing.
    """
    try:
        save_dialog_fn = current_app.config.get('SAVE_DIALOG_FN')
        if save_dialog_fn is None:
            return jsonify({'success': False, 'error': 'not supported'})

        result = save_dialog_fn()
        if result:
            path = result[0] if isinstance(result, tuple) else result
            return jsonify({'success': True, 'path': path})
        else:
            return jsonify({'success': False, 'error': '用户取消'})
    except Exception as e:
        logger.exception("Failed to show save dialog")
        return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500
