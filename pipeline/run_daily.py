"""收盘每日任务。python -m pipeline.run_daily；--resume RUN_ID 恢复当日失败步骤。"""
import argparse
import json
from pathlib import Path

from core.daily_data import ROOT
from core.daily_runner import AlreadyRunning, DailyRunner


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT.parents[1] / "data" / "market.db"))
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--resume", help="当日运行编号；代码、持仓输入及运行模式必须相同")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--refresh-prices", action="store_true", help="重采全部双价格历史，跳过跨运行缓存")
    parser.add_argument("--holdings", help="JSON 文件，包含6位代码列表或 holdings 对象列表")
    parser.add_argument("--check", action="store_true", help="只检查交易日历和收盘时间，不采集或冻结")
    parser.add_argument("--shadow", action="store_true", help="仅研究观察，不新增冻结快照")
    parser.add_argument("--account-config", help="账户模拟预算 JSON；不同参数保留为独立账户")
    args = parser.parse_args(argv)
    holdings = None
    if args.holdings:
        values = json.loads(Path(args.holdings).read_text(encoding="utf-8"))
        values = values.get("holdings", []) if isinstance(values, dict) else values
        holdings = [str(value["code"] if isinstance(value, dict) else value) for value in values]
    try:
        result = DailyRunner(args.db, args.root, workers=args.workers, holdings=holdings,
                             shadow=args.shadow, refresh_prices=args.refresh_prices, account_config=json.loads(Path(args.account_config).read_text(encoding="utf-8"))
                             if args.account_config else None).run(args.resume, check_only=args.check)
    except (AlreadyRunning, ValueError, OSError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({key: result[key] for key in ("run_id", "day", "status", "coverage", "stability")},
                     ensure_ascii=False))
    return 1 if result["status"] in ("failed", "partial", "calendar_unavailable", "calendar_uncovered") else 0


if __name__ == "__main__":
    raise SystemExit(main())
