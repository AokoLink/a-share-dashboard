# -*- coding: utf-8 -*-
"""A股三层分析看板 —— 入口(组合根)。

路由与配置已按功能模块拆到 web/ 包;本文件仅保留启动入口。
保留 `from datetime import datetime` 作为时钟间接层锚点:web/util.now() 在调用时读取
`app.datetime`,使测试对 app_mod.datetime 的 monkeypatch 能穿透到路由(见 _freeze_now_weekday)。
"""
from datetime import datetime  # noqa: F401  (时钟锚点,web/util.now() 依赖)

from web import create_app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=8000)
