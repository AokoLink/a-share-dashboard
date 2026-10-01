"""实验运行前登记并按内容冻结，历史已看过的区间不称为独立验证。"""
import hashlib
import json
from pathlib import Path

from core import strategies, screening_engine, research_evaluation, sector_relations
from core import strategy_lifecycle as life
from core.stage1_data import _write_json, sha256

ROOT = Path(__file__).resolve().parents[1]


def register(root, start, end, step, validation_start="2026-10-01"):
    if step < 1 or start > end:
        raise ValueError("invalid_experiment_window")
    removed = {"breakout_no_sector": "remove sector condition and score bonus",
               "breakout_no_buffer": "breakout buffer 0.1% to 0%, retain magnitude ranking",
               "breakout_no_volume": "remove volume condition and volume score",
               "pullback_no_depth": "remove drawdown range and depth score",
               "pullback_no_shrink": "remove shrinking volume condition and score",
               "rebound_no_drop": "remove 5-day drop threshold and score",
               "rebound_no_position": "remove 60-day position threshold and score"}
    protocol = {"protocol_version": "strategy-experiment-v1", "status": "registered_exploratory",
        "development": {"start": start, "end": end, "regular_step_sessions": step,
                        "include_all_panic_sessions": True, "previously_examined": True},
        "validation": {"start": validation_start, "status": "future_forward_evidence_pending",
                       "purge_by": "label_end", "embargo_sessions": 10,
                       "rolling_windows": "expanding_development_then_monthly_validation_no_optimization"},
        "parameters": {"pool_n": screening_engine.POOL_N, "top_n": screening_engine.TOP_N,
            "random_draws": 50, "seed": 17, "entry": "T+1_open", "exit": "T+h+1_open"},
        "hypotheses": {"breakout": "sector, buffer and volume conditions may improve cost-adjusted same-day selection",
            "pullback": "depth and shrinking volume conditions may improve trend pullback selection",
            "rebound": "stock selection may outperform same-day basket across independent panic events",
            "allocation": "blocked until publication-date fundamentals and valuation history exist"},
        "variants": {"base": "current declared conditions", **removed},
        "primary_metric": "event_cluster_equal_weight_fixed_slot_net_excess_vs_same_day_pool",
        "secondary_metrics": ["conditional_fill_net_excess", "same_sector_equal", "same_sector_random",
                              "environment_and_event_groups", "style_exposures", "cost_sensitivity"],
        "cost": {"version": life.COST_MODEL_VERSION, "fee_per_side": life.FEE_PER_SIDE,
                 "slippage_per_side": life.SLIPPAGE_PER_SIDE, "sensitivity_multipliers": [0., .5, 1., 1.5, 2.]},
        "missing_policy": "retain selected codes and events; unknown execution is not zero or reallocated",
        "randomization": "draw at signal date before checking execution or label availability",
        "benchmarks": ["same_pool_equal", "same_sector_equal", "same_sector_random",
                       "sh000001_reference_proxy_not_all_A", "same_environment_breakout_for_pullback"],
        "stopping_rules": ["do_not_promote_on_previously_examined_history", "do_not_tune_to_validation",
            "no_formal_upgrade_without_source_and_execution_verification",
            "retain_negative_and_missing_results", "no_claim_from_day_or_cluster_count_alone"],
        "unresolved_exposures": ["historical_market_cap", "historical_delisted_universe", "historical_ST_halts_limits"],
        "code_sha256": {"strategies": sha256(strategies.__file__), "engine": sha256(screening_engine.__file__),
            "evaluation": sha256(research_evaluation.__file__), "relations": sha256(sector_relations.__file__)}}
    protocol["code_sha256"]["experiment_runner"] = sha256(ROOT / "pipeline" / "strategy_experiments.py")
    protocol["code_sha256"]["protocol_code"] = sha256(__file__)
    digest = hashlib.sha256(json.dumps(protocol, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    path = Path(root) / "registry" / f"{digest}.json"
    if not path.exists():
        _write_json(path, {**protocol, "registered_at": life.observation_clock().isoformat(), "protocol_sha256": digest})
    return json.loads(path.read_text(encoding="utf-8")), {"path": str(path.resolve()), "sha256": sha256(path)}
