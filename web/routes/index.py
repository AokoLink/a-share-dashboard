# -*- coding: utf-8 -*-
"""首页路由。"""
from flask import render_template


def register(app):
    @app.route("/")
    def index():
        return render_template("index.html")
