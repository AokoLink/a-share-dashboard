# -*- coding: utf-8 -*-
"""人工记录策略版本状态变更；正式使用必须通过证据门槛。"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import store, strategy_lifecycle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "market.db"))
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--status", required=True, choices=strategy_lifecycle.ALLOWED_STATES)
    parser.add_argument("--reason", required=True, help="审查依据及证据文件路径")
    args = parser.parse_args()
    store.init_db(args.db)
    store.add_strategy_status_event(args.db, args.strategy, args.version,
                                    args.status, args.reason,
                                    datetime.now().astimezone().isoformat())
    print(f"{args.strategy} {args.version}: {args.status}")


if __name__ == "__main__":
    main()
