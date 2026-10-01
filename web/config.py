# -*- coding: utf-8 -*-
"""Web 层配置:根目录、数据/报告/缓存路径、板块类型常量。"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # C:\stock
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

DEFAULT_DB = os.path.join(BASE_DIR, "data", "market.db")
DEFAULT_TRADE_SIM_REPORT = os.path.join(BASE_DIR, "_analysis", "trade_sim.json")
SECTOR_TYPES = ("industry",)
DAILY_DIR = os.path.join(BASE_DIR, "_analysis", "daily")
SECTOR_MAP_PATH = os.path.join(BASE_DIR, "_analysis", "code2sector.json")
DAILY_PIPELINE_ROOT = os.path.join(BASE_DIR, "_analysis", "daily_pipeline")
REGIME_CACHE = os.path.join(DAILY_PIPELINE_ROOT, "regime_cache.json")
FORWARD_CALIB = os.path.join(BASE_DIR, "_analysis", "forward_calib.json")
THEME_VOL_REPORT = os.path.join(BASE_DIR, "_analysis", "theme_vol_strategy.json")
