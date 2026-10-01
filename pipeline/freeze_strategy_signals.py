# -*- coding: utf-8 -*-
"""兼容旧入口：统一执行每日采集、质量检查、冻结与跟踪。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.run_daily import main


if __name__ == "__main__":
    raise SystemExit(main())
