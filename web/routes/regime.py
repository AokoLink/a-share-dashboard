# -*- coding: utf-8 -*-
"""市场情绪择时路由 /api/regime(regime gate)。"""
from core import environment as env
from core import freshness
from core import report_cache
from core.market_environment_inputs import market_history_readiness
from core.stage1_data import load_calendar
from datetime import date
from pathlib import Path
from web import config
from web import util


def register(app):
    @app.route("/api/market-environment")
    def api_market_environment():
        """Primary workflow reads the saved daily environment, without recomputing."""
        root = Path(app.config["DAILY_PIPELINE_ROOT"])
        now = util.now()
        try:
            available = []
            for path in (root / "runs").glob("*/regime.json"):
                candidate = report_cache.read(path)
                day = date.fromisoformat(candidate["as_of"])
                if day <= now.date():
                    available.append((day, path.stat().st_mtime_ns, path))
            for _, _, path in sorted(available, reverse=True):
                # Regime is small; the large manifest is cached as a reference only.
                ref = report_cache.read(path.parent / "run.json",
                    project=lambda run: (run.get("steps", {}).get("environment", {}).get("result", {}).get("regime_artifact")
                        or run.get("steps", {}).get("environment", {}).get("environment_checkpoint", {}).get("regime_artifact")
                        or run.get("steps", {}).get("features", {}).get("result", {}).get("regime_artifact")
                        or run.get("steps", {}).get("features", {}).get("environment_checkpoint", {}).get("regime_artifact")),
                    projection_key="saved-regime-ref-v3")
                if not ref or Path(ref["path"]).resolve() != path.resolve():
                    raise ValueError("invalid_regime_reference")
                saved = report_cache.read(path, ref["sha256"])
                saved = {**saved, "advice": env.regime_advice(saved.get("label")),
                         "source": "saved_daily_pipeline", "report_ref": ref, "read_only": True}
                if "readiness" not in saved:
                    # Older archives stay unchanged; show a separate read-only coverage diagnostic.
                    saved["readiness"], _ = market_history_readiness(root, saved["as_of"],
                                                                    load_calendar(root / "calendar.json"))
                observation = root / "market_daily" / (saved["as_of"] + ".json")
                if observation.exists():
                    daily = report_cache.read(observation)
                    if daily.get("date") == saved["as_of"]:
                        saved["saved_observation"] = {key: daily.get(key) for key in
                            ("date", "r1", "up_ratio", "limit_up", "limit_down", "turnover")}
                result = freshness.mark_regime(saved, now=now)
                return util.ok(result, stale=result["freshness"]["status"] != "current")
            return util.ok({"status": "not_run", "label": None, "metrics": {}, "read_only": True,
                            "source": "saved_daily_pipeline", "required_sessions": 61, "observed_sessions": 0})
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return util.err("INVALID_ENVIRONMENT_REPORT", "市场环境留档无法读取或校验，请检查每日任务记录", 500)

    @app.route("/api/regime")
    def api_regime():
        """读 pkl 合成指数,返回当日七态 + 择时建议。"""
        try:
            regime = env.latest_state(config.DAILY_DIR, config.SECTOR_MAP_PATH, cache_path=config.REGIME_CACHE + ".legacy.json")
        except (FileNotFoundError, RuntimeError) as e:
            return util.err("NO_DATA", f"regime 计算失败(缺 daily pkl 或 sector map): {e}", 500)
        except Exception as e:                      # 其它异常不拖垮服务,按错误码上报
            return util.err("REGIME_FAIL", str(e), 500)
        return util.ok(freshness.mark_regime(regime))
