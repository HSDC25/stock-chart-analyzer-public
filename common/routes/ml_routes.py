"""Machine-learning prediction routes Blueprint.

Extracted from win11/app.py (ml_* endpoints).
All ML logic delegates to ``ml_module`` which is imported lazily.

Compatible with Python 3.8+.

简介：机器学习预测蓝图（ml_bp）——单只股票预测、ML 选股、随机森林
训练与预测。ml_module 在各路由内延迟导入，避免应用启动时加载
sklearn 等重型依赖拖慢启动。
"""
from __future__ import annotations

import logging
import re
import threading

from flask import Blueprint, jsonify, request

logger = logging.getLogger("stockchart")

ml_bp = Blueprint('ml', __name__)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@ml_bp.route('/api/ml/predict/<code>')
def api_ml_predict(code):
    """Single-stock ML prediction."""
    if not re.match(r'^[036]\d{5}$', code):
        return jsonify({'error': 'Invalid stock code format'}), 400
    try:
        from ml_module import predict_stock
        result = predict_stock(code)
        return jsonify({'success': 'error' not in result, **result})
    except Exception as e:
        logger.exception("Failed to predict stock %s", code)
        return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500


@ml_bp.route('/api/ml/screen', methods=['GET'])
def api_ml_screen():
    """ML-based stock screening (同步接口，保留兼容）。

    全市场筛选耗时约 30~90 秒，交互界面请改用
    POST /api/ml/screen/start + GET /api/ml/screen/status 的异步接口。
    """
    try:
        top_n = request.args.get('top_n', 20, type=int)
        from ml_module import screen_stocks
        results = screen_stocks(top_n=top_n)
        return jsonify({'success': True, 'data': results, 'total': len(results)})
    except Exception as e:
        logger.exception("Failed to screen stocks via ML")
        return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500


# ---------------------------------------------------------------------------
# ML 筛选异步作业：全市场扫描耗时分钟级，改后台线程 + 进度轮询，
# 避免请求线程被长时间占用、用户界面长时间无反馈。
# ---------------------------------------------------------------------------
_ml_screen_jobs = {}          # job_id -> {'status','progress','total','result','error'}
_ml_screen_jobs_lock = threading.Lock()
_ml_screen_next_id = [0]


@ml_bp.route('/api/ml/screen/start', methods=['POST'])
def api_ml_screen_start():
    """启动一次后台 ML 筛选作业。同一时间只允许一个作业。"""
    top_n = request.args.get('top_n', 20, type=int)

    with _ml_screen_jobs_lock:
        if any(j['status'] == 'running' for j in _ml_screen_jobs.values()):
            return jsonify({'success': False, 'error': '筛选已在运行中'}), 409
        _ml_screen_next_id[0] += 1
        job_id = str(_ml_screen_next_id[0])
        _ml_screen_jobs[job_id] = {
            'status': 'running', 'progress': 0, 'total': 0,
            'result': None, 'error': None,
        }

    def _job():
        job = _ml_screen_jobs[job_id]
        try:
            from ml_module import screen_stocks as _screen

            def _progress(done, total):
                job['progress'] = done
                job['total'] = total

            results = _screen(top_n=top_n, progress_callback=_progress)
            job['result'] = results
            job['status'] = 'done'
        except Exception as e:
            logger.exception("ML screen job %s failed", job_id)
            job['error'] = str(e)
            job['status'] = 'error'

    threading.Thread(target=_job, daemon=True).start()
    return jsonify({'success': True, 'job_id': job_id})


@ml_bp.route('/api/ml/screen/status')
def api_ml_screen_status():
    """查询 ML 筛选作业进度；不带 job_id 时返回最近一次作业。"""
    job_id = request.args.get('job_id')
    with _ml_screen_jobs_lock:
        if job_id:
            job = _ml_screen_jobs.get(job_id)
        else:
            job = None
            for j in _ml_screen_jobs.values():
                job = j
        if job is None:
            return jsonify({'success': False, 'error': '作业不存在'}), 404
        payload = {
            'success': True,
            'status': job['status'],
            'progress': job['progress'],
            'total': job['total'],
        }
        if job['status'] == 'done':
            payload['data'] = job['result']
            payload['total'] = len(job['result'] or [])
        if job['status'] == 'error':
            payload['error'] = job['error']
        return jsonify(payload)


@ml_bp.route('/api/ml/train', methods=['POST'])
def api_ml_train():
    """Train the random-forest model."""
    try:
        from ml_module import train_rf_model
        result = train_rf_model()
        if isinstance(result, dict):
            return jsonify(result)
        else:
            return jsonify({'success': False, 'error': '训练失败，请检查数据是否充足'})
    except Exception as e:
        logger.exception("Failed to train RF model")
        return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500


@ml_bp.route('/api/ml/rf_predict/<code>')
def api_ml_rf_predict(code):
    """Random-forest prediction for a single stock."""
    if not re.match(r'^[036]\d{5}$', code):
        return jsonify({'error': 'Invalid stock code format'}), 400
    try:
        from ml_module import rf_predict
        result = rf_predict(code)
        return jsonify({'success': 'error' not in result, **result})
    except Exception as e:
        logger.exception("Failed to run RF prediction for stock %s", code)
        return jsonify({'success': False, 'error': '服务器内部错误，请稍后重试'}), 500
