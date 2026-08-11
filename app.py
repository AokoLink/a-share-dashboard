# -*- coding: utf-8 -*-
"""A股三层分析看板 —— Flask 入口。完整实现在 Task 8。"""
from flask import Flask

def create_app(db_path=None):
    app = Flask(__name__)
    @app.route("/")
    def index():
        return "A股分析看板(实施中)"
    return app

if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=8000)
