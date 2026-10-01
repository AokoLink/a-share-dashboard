# -*- coding: utf-8 -*-
"""Web 层组合根:创建 Flask 应用,注入配置与路由。

模板/静态目录显式指回根目录(Flask(__name__) 默认按包目录定位,拆到 web/ 后会错位)。
"""
import logging
import os

from flask import Flask

from core import data_source as ds
from core import store
from web import config
from web.routes import register_routes


def create_app(db_path=None):
    app = Flask(__name__, template_folder=config.TEMPLATE_DIR, static_folder=config.STATIC_DIR)
    app.config["DB"] = db_path or config.DEFAULT_DB
    app.config["DAILY_PIPELINE_ROOT"] = config.DAILY_PIPELINE_ROOT
    os.makedirs(os.path.dirname(os.path.abspath(app.config["DB"])), exist_ok=True)
    store.init_db(app.config["DB"])
    register_routes(app)
    health = ds.validate_sector_map()
    app.config["SECTOR_MAP_HEALTH"] = health
    if not health.get("ok") or health.get("stale") or health.get("renamed"):
        logging.getLogger(__name__).warning("sector map health: %s", health)
    return app
