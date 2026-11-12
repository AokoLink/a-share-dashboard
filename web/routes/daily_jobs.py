"""Explicit JSON mutation launches background work; polling never launches it."""
from urllib.parse import urlsplit
from flask import request
from core.daily_jobs import controller
from core.daily_runner import AlreadyRunning
from web import util


def register(app):
    @app.route("/api/daily-job", methods=["GET", "POST"])
    def daily_job():
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).netloc != request.host:
                return util.err("BAD_ORIGIN", "请从本项目页面启动任务", 403)
            if not request.is_json or request.get_json(silent=True) != {}:
                return util.err("BAD_PARAM", "启动任务需提交空 JSON 对象", 400)
        try:
            manager = controller(app)
            result = manager.request() if request.method == "POST" else manager.status()
            result.setdefault("automation", manager.gate())
            result["automation"]["enabled"] = bool(app.config.get("DAILY_AUTO_RUN_ENABLED"))
            return util.ok(result)
        except AlreadyRunning:
            return util.err("JOB_BUSY", "任务状态正在更新，请稍后重试", 409)
        except (OSError, ValueError, KeyError, TypeError):
            app.logger.exception("daily job state unavailable")
            return util.err("INVALID_JOB_STATE", "任务状态无法读取，请检查 automation 留档及服务日志", 500)
