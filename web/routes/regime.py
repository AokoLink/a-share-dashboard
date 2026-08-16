# -*- coding: utf-8 -*-
"""市场情绪择时路由 /api/regime(regime gate)。"""
from core import environment as env
from web import config
from web import util


def register(app):
    @app.route("/api/regime")
    def api_regime():
        """读 pkl 合成指数,返回当日七态 + 择时建议。"""
        try:
            regime = env.latest_state(config.DAILY_DIR, config.SECTOR_MAP_PATH, cache_path=config.REGIME_CACHE)
        except (FileNotFoundError, RuntimeError) as e:
            return util.err("NO_DATA", f"regime 计算失败(缺 daily pkl 或 sector map): {e}", 500)
        except Exception as e:                      # 其它异常不拖垮服务,按错误码上报
            return util.err("REGIME_FAIL", str(e), 500)
        return util.ok(regime)
