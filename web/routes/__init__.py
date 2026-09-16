# -*- coding: utf-8 -*-
"""路由聚合:每个功能模块暴露 register(app),在此统一挂载。"""
from web.routes import index, market, sectors, stock, recommend, regime, actionable, swing, theme_vol, report, diagnose, low_position


def register_routes(app):
    index.register(app)
    market.register(app)
    sectors.register(app)
    stock.register(app)
    recommend.register(app)
    regime.register(app)
    actionable.register(app)
    swing.register(app)
    low_position.register(app)
    theme_vol.register(app)
    report.register(app)
    diagnose.register(app)
