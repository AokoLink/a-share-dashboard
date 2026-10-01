# -*- coding: utf-8 -*-
import numpy as np
import pandas as pd
import pytest

from core import portfolio_insights, sector_insights, store


def _daily(closes, amount=2e8):
    days = pd.date_range("2026-01-02", periods=len(closes), freq="B")
    return pd.DataFrame({"date": days, "close": closes, "amount": [amount] * len(closes)})


def test_sector_relative_strength_and_divergence_are_descriptive():
    days = pd.date_range("2026-01-02", periods=70, freq="B")
    sector = pd.DataFrame({"date": days, "close": np.linspace(100, 130, 70)})
    market = pd.DataFrame({"date": days, "close": np.linspace(100, 105, 70)})
    rel = sector_insights.relative_strength(sector, market)
    assert rel["status"] == "ok" and rel["returns"]["20"]["excess_pct"] > 0
    assert len(rel["trajectory"]) == 61
    members = [{"code": "600001", "name": "甲", "price": 10, "volume": 100,
                "change_pct": 5, "amount": 80},
               {"code": "600002", "name": "乙", "price": 10, "volume": 100,
                "change_pct": -1, "amount": 10},
               {"code": "600003", "name": "丙", "price": 10, "volume": 100,
                "change_pct": -2, "amount": 5},
               {"code": "600004", "name": "丁", "price": 10, "volume": 100,
                "change_pct": -3, "amount": 5}]
    breadth = sector_insights.member_breadth(members, 5)
    assert breadth["up_ratio"] == 0.25 and breadth["coverage_ratio"] == 0.8
    assert breadth["top3_amount_share"] == 0.95
    assert sector_insights.descriptive_state(rel, breadth) == "明显分化"


def test_leader_roles_separate_trading_price_and_missing_fundamentals():
    members = [{"code": "600001", "name": "成交核心", "price": 10, "amount": 80},
               {"code": "600002", "name": "价格领涨", "price": 10, "amount": 20}]
    daily = {"600001": _daily(np.linspace(10, 11, 30)),
             "600002": _daily(np.linspace(10, 15, 30))}
    roles = sector_insights.leader_roles(members, daily, sector_return20=5.0)
    assert roles["trading_core"][0]["code"] == "600001"
    assert roles["price_leaders"][0]["code"] == "600002"
    assert roles["industrial_core"] == []
    assert roles["industrial_core_status"] == "fundamentals_unavailable"


def test_portfolio_exposure_cash_correlation_and_risk_contribution():
    n = 65
    moves = np.array([0.005 + 0.01 * np.sin(i) for i in range(n)])
    a = 100 * np.cumprod(1 + moves)
    b = 20 * np.cumprod(1 + moves * 0.8)
    c = 30 * np.cumprod(1 - moves * 0.5)
    holdings = [{"code": "600001", "weight": 0.4}, {"code": "600002", "weight": 0.3},
                {"code": "600003", "weight": 0.1}]
    frames = {"600001": _daily(a, 5e7), "600002": _daily(b), "600003": _daily(c)}
    sectors = {"600001": ["通信", "AI"], "600002": ["通信"], "600003": ["半导体"]}
    result = portfolio_insights.analyze(holdings, frames, sectors)
    assert result["cash_weight"] == pytest.approx(0.2)
    assert result["sector_exposure_nonexclusive"]["通信"] == pytest.approx(0.7)
    assert result["low_liquidity_weight"] == pytest.approx(0.4)
    assert result["risk_status"] == "descriptive_only"
    assert ["600001", "600002"] in result["correlation_clusters"]
    assert sum(result["risk_contribution"].values()) == pytest.approx(1, abs=1e-4)


def test_rank_history_recomputes_legacy_wrong_rank(tmp_path):
    db = str(tmp_path / "ranks.db")
    store.init_db(db)
    store.upsert_sector_daily(db, "2026-09-29", "industry", "A", "甲", 5, 0.7, 100, 9)
    store.upsert_sector_daily(db, "2026-09-29", "industry", "B", "乙", 1, 0.5, 50, 1)
    observations = store.get_sector_observations(db, "industry", "A")
    assert observations[0]["rank"] == 1
    assert store.get_consecutive_days(db, "industry", "A", "2026-09-29", top_n=1) == 1
