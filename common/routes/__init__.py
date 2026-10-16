"""Flask Blueprint route modules.

简介：按功能拆分的 Flask 路由蓝图包。包含四个蓝图：
- api_bp（api_routes）：首页、预加载控制、K 线、市场状态、榜单等核心 API；
- chart_bp（chart_routes）：图表导出与原生保存对话框；
- ml_bp（ml_routes）：机器学习预测/选股/训练；
- screen_bp（screen_routes）：涨停/放量/连阳等选股策略筛选。
由应用入口统一 register_blueprint 挂载。
"""
