import copy

import pandas as pd
import pytest

from core import account_ledger as ledger, daily_data, store
from pipeline import account_report

DAYS = pd.bdate_range("2026-09-01", periods=10).strftime("%Y-%m-%d").tolist()
CODES = ["600001", "600002"]


def settings(**changes):
    return {"initial_cash": 100000., "cash_reserve": .1, "max_stock_weight": .9,
            "strategy_budgets": {"breakout": .8}, "max_industry_weight": .9,
            "max_theme_weight": .9, "fee": 0., "slippage": 0., **changes}


def signal(sid=1, day=None, codes=None, strategy="breakout", horizon=2, version="test-v1"):
    day, codes = day or DAYS[0], codes or [CODES[0]]
    rows = [{"code": c, "signal_close": 10., "amount": 1e8} for c in codes]
    return {"id": sid, "signal_date": day, "strategy": strategy, "version": version,
            "frozen_at": day + "T16:00:00+08:00", "payload": {
                "horizon_sessions": horizon, "candidates": rows, "pool": rows}}


def inputs(signals=None):
    status, coverage, relations, rules = {}, {}, {}, {}
    for day in DAYS:
        known = {"verified": True, "source": "manual_fixture", "known_at": day + "T08:00:00+08:00"}
        status[day] = {c: {**known, "halted": False, "buy_open_allowed": True, "sell_open_allowed": True} for c in CODES}
        coverage[day] = {c: {**known, "complete": True} for c in CODES}
        rules[day] = {c: {**known, "buy_min": 100, "buy_step": 100, "sell_min": 100, "sell_step": 100,
            "odd_lot_sell_all": True, "commission_rate": 0., "minimum_commission": 0.,
            "sell_tax_rate": 0., "transfer_rate": 0.} for c in CODES}
        relations[day] = {c: {"status": "ok", "as_of": day, "categories_complete": ["industry", "theme"],
                              "relations": [{"id": "industry:A", "category": "industry"}]} for c in CODES}
    return {"snapshots": signals if signals is not None else [signal()], "calendar": DAYS,
            "bars": {c: {d: {"open": 10., "close": 10., "amount": 1e8} for d in DAYS} for c in CODES},
            "bases": {c: "verified_unadjusted_raw" for c in CODES}, "trade_status": status,
            "corporate_coverage": coverage, "corporate_actions": [], "relations": relations,
            "benchmark": {d: 100. for d in DAYS}, "sector_returns": {d: {"industry:A": 0.} for d in DAYS},
            "status_events": [], "execution_rules": rules}


def run(data=None, end=None, **changes):
    data = copy.deepcopy(data or inputs())
    for rows in data.get("execution_rules", {}).values():
        for row in rows.values():
            row["commission_rate"] = changes.get("fee", 0.)
    return ledger.replay(**data, as_of=end or DAYS[-1], settings=settings(**changes))


def fills(report, side):
    return [e for e in report["events"] if e["type"] == side + "_fill"]


def test_equal_weight_cash_nav_and_notional_turnover():
    data = inputs([signal(codes=CODES)])
    report = run(data)
    assert [e["shares"] for e in fills(report, "buy")] == [4000, 4000]
    assert report["daily"][1]["cash"] == 20000
    assert report["daily"][1]["position_value"] == 80000
    assert report["daily"][1]["buy_turnover"] == .8
    assert report["daily"][1]["one_way_turnover"] == .4
    assert report["daily"][3]["sell_turnover"] == .8
    assert report["cash"] == 100000
    assert all(r["cash_reconciliation_error"] == 0 for r in report["daily"])
    assert all(abs(r["pnl_reconciliation_error"]) < 1e-9 for r in report["daily"])
    assert report["metrics"]["annualized_return"] is None


def test_costs_reconcile_to_cash_and_strategy_pnl():
    data = inputs()
    data["bars"][CODES[0]][DAYS[3]]["open"] = 11.
    report = run(data, fee=.001, slippage=.001)
    entry, exit_ = fills(report, "buy")[0], fills(report, "sell")[0]
    q = entry["shares"]
    expected = 100000 - q * 10 * 1.001 * 1.001 + q * 11 * .999 * .999
    assert report["cash"] == pytest.approx(expected)
    assert report["daily"][-1]["strategy_cumulative_pnl"]["breakout"] == pytest.approx(expected - 100000)
    assert report["fees"] == pytest.approx(entry["fee"] + exit_["fee"])
    assert report["daily"][1]["buy_notional"] == pytest.approx(q * 10 * 1.001)
    assert report["daily"][3]["sell_notional"] == pytest.approx(q * 11 * .999)
    assert not report["daily"][1]["risk_breaches"]  # constraints include cost-induced NAV decrease


def test_partial_entry_remainder_is_cash_not_reallocated():
    data = inputs([signal(codes=CODES)])
    data["trade_status"][DAYS[1]][CODES[0]]["max_buy_shares"] = 1500
    report = run(data)
    assert [e["shares"] for e in fills(report, "buy")] == [1500, 4000]
    assert report["daily"][1]["cash"] == 45000
    assert report["orders"][0]["status"] == "partial_fill"
    assert any(e["type"] == "entry_remainder_cancelled" for e in report["events"])


def test_gap_cancelled_slot_stays_cash():
    data = inputs([signal(codes=CODES)])
    data["bars"][CODES[0]][DAYS[1]]["open"] = 11.
    report = run(data)
    assert len(fills(report, "buy")) == 1
    assert fills(report, "buy")[0]["shares"] == 4000
    assert report["daily"][1]["cash"] == 60000
    assert report["orders"][0]["reason"] == "entry_gap_above_limit"


def test_cross_strategy_stock_budget_merges_shares():
    data = inputs([signal(), signal(2, strategy="pullback")])
    report = run(data, max_stock_weight=.6, strategy_budgets={"breakout": .4, "pullback": .4})
    holding = report["daily"][1]["positions"][0]
    assert holding["shares"] == 6000 and holding["weight"] == .6
    assert holding["strategies"] == ["breakout", "pullback"]
    assert report["daily"][1]["cash"] == 40000


def test_overlap_does_not_reuse_cash_or_redistribute_cancelled_slot():
    data = inputs([signal(codes=CODES, horizon=5), signal(2, day=DAYS[1], horizon=5)])
    data["bars"][CODES[1]][DAYS[1]]["open"] = 13.
    report = run(data, end=DAYS[2])
    assert report["daily"][-1]["positions"][0]["shares"] == 8000
    assert report["cash"] == 20000
    assert len(report["lots"]) == 2


def test_first_frozen_revision_and_version_only_once():
    later = signal(2, version="test-v2")
    later["frozen_at"] = DAYS[0] + "T17:00:00+08:00"
    report = run(inputs([later, signal()]))
    assert len(fills(report, "buy")) == 1
    assert report["rejected_snapshots"][0]["snapshot_id"] == 2
    later["frozen_at"] = DAYS[0] + "T15:00:00+00:00"  # 23:00 Shanghai, lexical order would be wrong
    assert fills(run(inputs([later, signal()])), "buy")[0]["order_id"].startswith("1:")


@pytest.mark.parametrize("problem", ["missing", "halt", "limit"])
def test_failed_exit_remains_exposed_and_retries(problem):
    data = inputs()
    if problem == "missing":
        del data["bars"][CODES[0]][DAYS[3]]["open"]
    elif problem == "halt":
        data["trade_status"][DAYS[3]][CODES[0]]["halted"] = True
    else:
        data["trade_status"][DAYS[3]][CODES[0]]["sell_open_allowed"] = False
    report = run(data)
    row = report["daily"][3]
    assert row["positions"][0]["pending_exit_shares"] == 8000
    assert row["sell_notional"] == 0 and row["nav"] == 100000
    assert fills(report, "sell")[0]["date"] == DAYS[4]


def test_partial_exit_capacity_is_shared_across_same_code_lots():
    data = inputs([signal(), signal(2, strategy="pullback")])
    data["trade_status"][DAYS[3]][CODES[0]]["max_sell_shares"] = 1500
    report = run(data, strategy_budgets={"breakout": .4, "pullback": .4})
    assert sum(e["shares"] for e in fills(report, "sell") if e["date"] == DAYS[3]) == 1500
    assert report["daily"][3]["positions"][0]["shares"] == 6500
    assert report["daily"][4]["positions"] == []


def test_missing_mark_is_null_nav_never_forward_filled():
    data = inputs()
    del data["bars"][CODES[0]][DAYS[2]]["close"]
    report = run(data)
    assert report["daily"][2]["nav"] is None
    assert report["daily"][2]["positions"][0]["market_value"] is None
    assert report["daily"][3]["daily_return"] is None
    assert report["metrics"]["total_return"] is None
    assert report["metrics"]["max_drawdown"] is None
    assert report["status"] == "valuation_incomplete"


def test_dividend_receivable_split_and_payment_after_exit():
    data = inputs()
    data["corporate_actions"] = [{"id": "split-div", "code": CODES[0], "ex_date": DAYS[2],
        "pay_date": DAYS[4], "share_factor": 2., "net_cash_per_share": 1.,
        "known_at": DAYS[0] + "T08:00:00+08:00", "verified": True, "source": "manual_fixture"}]
    for day in DAYS[2:]:
        data["bars"][CODES[0]][day].update(open=4.5, close=4.5)
    report = run(data)
    assert report["daily"][2]["positions"][0]["shares"] == 16000
    assert report["daily"][2]["receivables"] == 8000
    assert report["daily"][3]["cash"] == 92000
    assert report["daily"][3]["positions"] == []
    assert report["daily"][4]["cash"] == 100000
    assert all(r["nav"] == 100000 for r in report["daily"])
    assert report["metrics"]["total_return"] == 0


def test_unresolved_corporate_action_blocks_subsequent_fake_exit():
    data = inputs()
    data["corporate_actions"] = [{"id": "unknown", "code": CODES[0], "ex_date": DAYS[2],
                                 "share_factor": 2, "verified": False}]
    report = run(data)
    assert all(r["nav"] is None for r in report["daily"][2:])
    assert fills(report, "sell") == []
    assert report["lots"][0]["shares"] == 8000


def test_verified_inputs_require_status_company_and_sector_coverage():
    data = inputs()
    assert len(fills(run(data, mode="verified_inputs"), "buy")) == 1
    data["corporate_coverage"] = {}
    assert fills(run(data, mode="verified_inputs"), "buy") == []
    proxy = run(data)
    assert len(fills(proxy, "buy")) == 1 and not proxy["formal"]
    assert any("corporate_action_coverage_unknown" == reason for r in proxy["daily"] for _, reason in r["evidence_limits"])
    data = inputs()
    data["relations"][DAYS[0]][CODES[0]]["categories_complete"] = ["industry"]
    assert fills(run(data, mode="verified_inputs"), "buy") == []


def test_previous_liquidity_only_and_future_invariance():
    data = inputs()
    original = run(data, end=DAYS[2])
    data["bars"][CODES[0]][DAYS[1]]["amount"] = 1e30  # final entry-day amount cannot size entry
    assert fills(run(data, end=DAYS[2]), "buy") == fills(original, "buy")
    data["bars"][CODES[0]][DAYS[1]]["amount"] = 1e8  # known closing liquidity exposure may legitimately change
    for day in DAYS[3:]:
        data["bars"][CODES[0]][day].update(open=1000000., close=1000000., amount=1e30)
    future = signal(20, day=DAYS[5])
    data["snapshots"].append(future)
    changed = run(data, end=DAYS[2])
    assert original["daily"] == changed["daily"]
    assert original["events"] == changed["events"]
    assert original["orders"] == changed["orders"]


def test_low_prior_liquidity_partial_and_missing_entry_is_not_reported_as_cash_alpha():
    data = inputs()
    data["bars"][CODES[0]][DAYS[0]]["amount"] = 1e6
    data["bars"][CODES[0]][DAYS[1]]["amount"] = 1e30
    assert fills(run(data, end=DAYS[1]), "buy")[0]["shares"] == 1000
    del data["bars"][CODES[0]][DAYS[1]]["open"]
    missing = run(data)
    assert missing["cash"] == 100000
    assert missing["metrics"]["total_return"] is None
    assert missing["status"] == "execution_inputs_incomplete"
    paired = account_report.build(data, as_of=DAYS[-1], settings=settings())["comparisons"]["pool_equal"]["daily"]
    assert all(r["daily_net_excess"] is None for r in paired[1:])


def test_status_before_open_blocks_entry_but_not_existing_exits():
    data = inputs()
    data["status_events"] = [{"strategy": "breakout", "version": "test-v1", "status": "paused",
                              "changed_at": DAYS[2] + "T08:00:00+08:00"}]
    assert len(fills(run(data), "sell")) == 1
    data["status_events"][0]["changed_at"] = DAYS[1] + "T08:00:00+08:00"
    assert fills(run(data), "buy") == []


def test_industry_budget_and_attribution_reconcile():
    data = inputs([signal(codes=CODES)])
    report = run(data, max_industry_weight=.5)
    assert report["daily"][1]["sector_exposure"]["industry"]["industry:A"] == .5
    assert report["daily"][1]["cash"] == 50000
    for row in report["daily"][1:]:
        assert sum(row["attribution"][k] for k in ("capital_market", "cash_and_receivable_drag",
            "covered_sector_increment", "stock_and_unexplained_sector_residual", "execution_cost")) == pytest.approx(row["pnl"])
    data["sector_returns"] = {}
    incomplete = run(data)
    assert incomplete["daily"][2]["attribution"]["sector_complete"] is False


def test_matched_account_control_is_zero_net_excess():
    same = inputs([signal(codes=CODES)])
    same["snapshots"][0]["payload"]["pool"].reverse()
    report = account_report.build(same, as_of=DAYS[-1], settings=settings(fee=.001, slippage=.001, max_industry_weight=.5))
    for key in ("pool_equal", "sector_equal"):
        assert report["comparisons"][key]["total_net_excess"] == 0
        assert all(r["daily_net_excess"] == 0 for r in report["comparisons"][key]["daily"])
    data = inputs()
    data["relations"] = {}
    blocked = account_report.build(data, as_of=DAYS[-1], settings=settings())
    assert blocked["comparisons"]["sector_equal"]["status"] == "blocked_membership_coverage"
    assert blocked["comparisons"]["sector_equal"]["total_net_excess"] is None


def test_before_open_and_intraday_never_use_current_close():
    data = inputs()
    early = run(data, end=DAYS[1] + "T09:00:00+08:00")
    assert fills(early, "buy") == []
    noon = run(data, end=DAYS[1] + "T12:00:00+08:00")
    data["bars"][CODES[0]][DAYS[1]]["close"] = 100000
    assert noon["daily"] == run(data, end=DAYS[1] + "T12:00:00+08:00")["daily"]
    assert noon["status"] == "intraday_pending_close" and noon["valuation_as_of"] == DAYS[0]


def test_immutable_idempotent_archives_research_separate_from_forward(tmp_path):
    data = inputs()
    for i in range(2):
        report = account_report.run(None, tmp_path, as_of=DAYS[-1], settings=settings(), supplied_inputs=data,
                                    source="manual_verification_fixture")
    assert len(list((tmp_path / "accounts" / "blobs").glob("*.json"))) == 1
    assert not (tmp_path / "accounts" / "latest.json").exists()
    assert daily_data.intact(report["report_ref"])
    assert daily_data.intact(report["inputs_ref"])
    with pytest.raises(ValueError, match="explicit_research_source"):
        account_report.run(None, tmp_path, supplied_inputs=data, settings=settings())


def test_account_api_reads_only_hash_checked_forward_report(tmp_path):
    from web import create_app
    from core.stage1_data import save_calendar
    app = create_app(str(tmp_path / "api.sqlite"))
    app.config["DAILY_PIPELINE_ROOT"] = str(tmp_path)
    client = app.test_client()
    assert client.get("/api/account-ledger").get_json()["data"]["status"] == "not_run"
    save_calendar(pd.DataFrame({"trade_date": pd.to_datetime(DAYS)}), tmp_path / "calendar.json", DAYS[0])
    report = account_report.run(app.config["DB"], tmp_path, as_of=DAYS[-1], settings=settings())
    response = client.get("/api/account-ledger")
    assert response.status_code == 200
    assert response.get_json()["data"]["status"] == "no_signals"
    assert "events" not in response.get_json()["data"]
    from pathlib import Path
    Path(report["report_ref"]["path"]).write_text("{}", encoding="utf-8")
    assert client.get("/api/account-ledger").status_code == 500


def test_raw_account_loader_rejects_duplicate_dates_and_adjusted_files(tmp_path):
    code = CODES[0]
    path = tmp_path / "execution" / "raw_daily" / (code + ".csv")
    frame = pd.DataFrame({"date": [DAYS[0], DAYS[0]], "open": [10., 10.], "close": [10., 10.], "amount": [1e8, 1e8]})
    daily_data.archive_frame(path, frame, "fixture", DAYS[0], adjust="none")
    with pytest.raises(ValueError, match="duplicate"):
        account_report._raw(code, tmp_path)
    daily_data.archive_frame(path, frame.iloc[:1], "fixture", DAYS[0], adjust="qfq")
    with pytest.raises(ValueError, match="adjustment"):
        account_report._raw(code, tmp_path)


def test_forward_no_signals_has_no_performance_and_missing_prices_dont_fallback(tmp_path):
    db = tmp_path / "market.sqlite"
    store.init_db(db)
    daily_data.archive_json(tmp_path / "calendar.json", {"dates": DAYS, "source": "manual_fixture"}, "fixture", DAYS[0])
    # load_calendar needs the standard enveloped metadata produced by stage1_data.
    from core.stage1_data import save_calendar
    save_calendar(pd.DataFrame({"trade_date": pd.to_datetime(DAYS)}), tmp_path / "calendar.json", DAYS[0])
    report = account_report.run(db, tmp_path, as_of=DAYS[-1], settings=settings())
    assert report["status"] == "no_signals" and report["metrics"]["total_return"] is None
    assert (tmp_path / "accounts" / "latest.json").exists()
    snap = signal()
    store.freeze_strategy_signal(db, snap["signal_date"], snap["strategy"], snap["version"], snap["frozen_at"], snap["payload"])
    report = account_report.run(db, tmp_path, as_of=DAYS[-1], settings=settings())
    assert fills(report, "buy") == [] and report["input_manifest"]["source_errors"]


@pytest.mark.parametrize("change", [{"initial_cash": -1}, {"fee": float("nan")}, {"lot_size": 0},
    {"strategy_budgets": {"breakout": 1}}, {"mode": "real"}, {"unknown": 1}])
def test_invalid_account_budget(change):
    with pytest.raises(ValueError):
        ledger.config(settings(**change))
