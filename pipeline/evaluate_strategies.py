# -*- coding: utf-8 -*-
"""阶段二同日、同池、同成本的探索性对照；静态板块和复权状态未核实。"""
import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from core import backtest, strategies, strategy_lifecycle as lifecycle, sector_relations, screening_engine, research_evaluation
from core.stage1_data import sha256

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "_analysis" / "daily"
SECTOR_MAP = ROOT / "_analysis" / "code2sector.json"
REGIME = ROOT / "_analysis" / "environment_report.json"
SIDE_COST = lifecycle.FEE_PER_SIDE + lifecycle.SLIPPAGE_PER_SIDE
POOL_N = 200
TOP_N = 20


def _hash_inputs(paths):
    digest = hashlib.sha256()
    for path in sorted(map(Path, paths)):
        digest.update(path.name.encode("utf-8"))
        digest.update(sha256(path).encode("ascii"))
    return digest.hexdigest()


def _mean(values):
    return float(np.mean(values)) if values else None


def evaluate_day(universe, pos_of, days, index, sector_map, market_state=None,
                 pool_n=POOL_N, top_n=TOP_N, random_draws=50, seed=17, sector_snapshot=None,
                 rows=None, variant="base", execution=None):
    """同一共享引擎产生 T 日决定；未来执行数据只传给独立核算模块。"""
    day = str(pd.Timestamp(days[index]).date())
    prefixes = {}
    for code, frame in universe.items():
        bar = pos_of[code].get(days[index])
        if bar is not None:
            prefixes[code] = frame.iloc[max(0, bar - 64):bar + 1]
    records = rows if rows is not None else [{"code": c, "name": None} for c in sorted(universe)]
    selection = screening_engine.select(day, prefixes, records, regime={"as_of": day, "label": market_state},
        sector_snapshot=sector_snapshot, pool_n=pool_n, top_n=top_n, variant=variant)
    results = research_evaluation.evaluate_selection(selection, universe if execution is None else execution, days,
        random_draws=random_draws, seed=seed)
    for record in results.values():
        record["screening_decision"] = selection
        record["selected_amount"] = record["exposure"]["selected"]["amount"]
        record["pool_amount"] = record["exposure"]["pool"]["amount"]
        record["selected_vol20"] = None
        record["pool_vol20"] = None
    return results


def run(start="2022-01-01", end="2026-08-14", step=20, sector_root=None):
    from pipeline.strategy_experiments import run as run_registered
    return run_registered(start, end, step, sector_root=sector_root)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2022-01-01")
    parser.add_argument("--end", default="2026-08-14")
    parser.add_argument("--step", type=int, default=20)
    parser.add_argument("--out", default=str(ROOT / "_analysis" / "stage2_strategy_eval_v4.json"))
    parser.add_argument("--sector-root", default=str(ROOT / "_analysis" / "daily_pipeline"))
    args = parser.parse_args()
    if args.step < 1:
        parser.error("--step must be positive")
    result = run(args.start, args.end, args.step, args.sector_root)
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "summary": result["summary"],
                      "sampled_days": result["sampled_sessions"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
