import datetime as dt
from collections import Counter

import pandas as pd
import pytest

from core import daily_data, daily_runner, store, strategy_lifecycle as life
from core.stage1_data import _write_json
from pipeline.track_strategy_signals import run as track

NOW = dt.datetime(2026, 9, 30, 16, tzinfo=dt.timezone(dt.timedelta(hours=8)))


class Sources:
    def __init__(self):
        self.calls = Counter()
        self.fail = set()
        self.old = False

    def calendar(self):
        self.calls["calendar"] += 1
        return pd.DataFrame({"trade_date": pd.date_range("2025-01-01", "2027-01-01", freq="B")})

    def market(self):
        self.calls["market"] += 1
        if "market" in self.fail:
            raise ValueError("source unavailable")
        return pd.DataFrame([{"code": "600001", "name": "测试股", "price": 10.8,
            "change_pct": 8., "amount": 2e8, "volume": 200000}]), []

    def daily(self, code, start, end, adjust):
        self.calls[(code, adjust)] += 1
        if (code, adjust) in self.fail:
            raise ValueError("source unavailable")
        dates = pd.date_range(end=end, periods=66, freq="B")
        if self.old:
            dates = dates - pd.offsets.BDay(1)
        closes = [10.] * 65 + [10.8]
        return pd.DataFrame({"日期": dates, "开盘": closes, "最高": [10.1] * 65 + [10.9],
            "最低": [9.9] * 65 + [10.7], "收盘": closes,
            "成交量": [1000] * 61 + [2000] * 5, "成交额": [2e8] * 66, "换手率": [2.] * 66})

    def sectors(self):
        return pd.DataFrame([{"code": "885001", "name": "A"}]), False

    def membership(self, name):
        if "membership" in self.fail:
            return {"ok": False, "reason": "source_fail"}
        return {"ok": True, "codes": ["600001"], "source_name": "精确来源", "match_type": "exact", "stale": False}

    def sector_history(self, code):
        return pd.DataFrame({"date": pd.date_range(end=NOW.date(), periods=66, freq="B"),
                             "close": [100.] * 65 + [105.]}), False

    def benchmark(self):
        frame, stale = self.sector_history("sh000001")
        frame["close"] = 100.
        return frame, stale


def runner(tmp_path, sources=None, **kwargs):
    return daily_runner.DailyRunner(tmp_path / "db.sqlite", tmp_path / "daily", sources=sources or Sources(),
                                   now=kwargs.pop("now", NOW), sector_map={"600001": ["A"]}, **kwargs)


def test_daily_complete_repeat_and_readonly_outputs(tmp_path):
    job = runner(tmp_path)
    first = job.run()
    assert first["status"] == "success"
    assert first["coverage"]["research"]["raw"]["current"] == 1
    assert first["coverage"]["controls"]["expected"] == 1
    assert first["coverage"]["holdings"]["expected"] == 0
    assert first["stability"]["passed"] is False  # 注入数据不能冒充真实十日验收
    assert first["steps"]["account"]["status"] == "success"
    assert not (job.root / "accounts" / "latest.json").exists()  # 注入账户不冒充真实前向最新结果
    rows = store.list_strategy_signals(job.db)
    assert len(rows) == 3 and rows[0]["payload"]["price_basis"] == "verified_unadjusted_raw"
    assert rows[0]["payload"]["pool"][0]["sector_evidence"]["sector_rel20"] == 5.
    assert rows[0]["payload"]["sector_snapshot_ref"]["as_of"] == "2026-09-30"
    assert all(row["payload"]["status"] == "pending_entry" for row in store.list_strategy_observations(job.db))
    assert job.run()["status"] == "success"
    assert len(store.list_strategy_signals(job.db)) == 3
    assert len(store.list_strategy_observations(job.db)) == 3
    assert len(list((job.root / "runs").glob("*/report.json"))) == 2
    result = daily_data.read_json(job.root / "candidates" / "2026-09-30.json")
    assert result["groups"]["breakout"][0]["code"] == "sh600001"
    assert daily_data.read_json(job.root / "regime_cache.json")["label"] is None


def test_resume_retries_only_failed_price_input(tmp_path):
    source = Sources()
    source.fail.add(("600001", ""))
    job = runner(tmp_path, source)
    first = job.run()
    assert first["status"] == "partial"
    assert not store.list_strategy_signals(job.db)
    assert first["coverage"]["research"]["raw"]["missing_rate"] == 1
    source.fail.clear()
    second = job.run(resume=first["run_id"])
    assert second["status"] == "success"
    assert source.calls["market"] == 1
    assert source.calls[("600001", "qfq")] == 1
    assert source.calls[("600001", "")] == 2
    assert len(store.list_strategy_signals(job.db)) == 3
    job.run(resume=first["run_id"])
    assert len(store.list_strategy_signals(job.db)) == 3


def test_resume_detects_tampering_and_recollects(tmp_path):
    source = Sources()
    job = runner(tmp_path, source)
    first = job.run()
    path = job.work / "spot.csv"
    path.write_text("tampered", encoding="utf-8")
    second = job.run(first["run_id"])
    assert second["status"] == "success"
    assert source.calls["market"] == 2
    assert len(store.list_strategy_signals(job.db)) == 3


@pytest.mark.parametrize("now,reason", [(NOW.replace(hour=14), "waiting_for_close"),
    (NOW.replace(day=27), "market_closed"), (NOW.replace(year=2028), "calendar_uncovered")])
def test_calendar_time_gate_prevents_collection(tmp_path, now, reason):
    source = Sources()
    result = runner(tmp_path, source, now=now).run()
    assert result["status"] == reason
    assert not source.calls["market"]
    assert result["steps"]["freeze"]["status"] == "skipped"


def test_unconfirmed_price_day_never_freezes(tmp_path):
    source = Sources()
    source.old = True
    job = runner(tmp_path, source)
    result = job.run()
    assert result["status"] == "partial"
    assert not store.list_strategy_signals(job.db)
    assert result["steps"]["quality"]["result"]["freeze_ready"] is False
    assert result["steps"]["track"]["status"] == "success"
    assert result["steps"]["prices"]["result"]["stale_inputs"] == 2
    source.old = False
    resumed = job.run(result["run_id"])
    assert resumed["status"] == "success"
    # 此测试来源把整段历史平移一天，重叠窗口发生修订，必须再做全量重建。
    assert source.calls[("600001", "")] == source.calls[("600001", "qfq")] == 3
    assert resumed["steps"]["prices"]["result"]["entries"]["600001"]["technical"]["collection_mode"] == "full_refresh"


def test_resume_restores_execution_cache_from_checked_archive(tmp_path):
    source = Sources()
    job = runner(tmp_path, source)
    result = job.run()
    path = job.root / "execution" / "raw_daily" / "600001.csv"
    path.write_text("corrupted", encoding="utf-8")
    assert job.run(result["run_id"])["status"] == "success"
    assert source.calls[("600001", "")] == 1
    assert pd.read_csv(path).iloc[-1]["close"] == 10.8


def test_source_failure_is_distinct_from_no_candidates(tmp_path):
    source = Sources()
    source.fail.add("market")
    result = runner(tmp_path, source).run()
    assert result["status"] == "failed"
    assert result["steps"]["inputs"]["status"] == "failed"
    assert result["steps"]["freeze"]["status"] == "skipped"
    assert result["steps"]["track"]["status"] == "success"


def test_holdings_are_requested_even_outside_research_pool(tmp_path):
    source = Sources()
    result = runner(tmp_path, source, holdings=["600002"]).run()
    assert source.calls[("600002", "")] == source.calls[("600002", "qfq")] == 1
    assert result["coverage"]["holdings"]["codes"] == ["600002"]


def test_single_instance_and_lock_releases(tmp_path):
    with daily_runner.RunLock(tmp_path):
        with pytest.raises(daily_runner.AlreadyRunning):
            with daily_runner.RunLock(tmp_path):
                pass
    with daily_runner.RunLock(tmp_path):
        pass


def test_quality_rejects_duplicate_reverse_and_invalid_prices():
    source = Sources()
    frame = daily_data.normalize_source_daily(source.daily("600001", "2025-01-01", "2026-09-30", ""), "600001")
    sessions = [d.strftime("%Y-%m-%d") for d in pd.date_range("2025-01-01", "2027-01-01", freq="B")]
    for bad in (frame.iloc[::-1], pd.concat([frame, frame.tail(1)]), frame.assign(high=1.)):
        with pytest.raises(ValueError):
            daily_data.daily_quality(bad, "2026-09-30", sessions)
    gapped = frame.drop(index=10)
    _, quality = daily_data.daily_quality(gapped, "2026-09-30", sessions)
    assert quality["missing_sessions"] == 1
    assert quality["missing_interpretation"] == "unknown_suspension_or_source_gap"


def test_ten_session_gate_does_not_accept_injected_or_missing_runs(tmp_path):
    dates = pd.bdate_range("2026-09-01", periods=10).strftime("%Y-%m-%d").tolist()
    calendar = {"dates": dates}
    for i, day in enumerate(dates):
        _write_json(tmp_path / "runs" / str(i) / "run.json", {
            "run_id": str(i), "day": day, "started_at": day, "status": "success", "source_mode": "live"})
    assert daily_runner.continuity(tmp_path, calendar)["passed"] is True
    path = tmp_path / "runs" / "7" / "run.json"
    row = daily_data.read_json(path)
    row["source_mode"] = "injected"
    _write_json(path, row)
    stability = daily_runner.continuity(tmp_path, calendar)
    assert stability["consecutive_complete_sessions"] == 2
    assert stability["passed"] is False


def test_incremental_tracker_ignores_future_bars_and_records_pending(tmp_path):
    from tests.test_strategy_lifecycle import _snapshot
    snapshot = _snapshot()
    days = ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"]
    bars = {"600001": {day: {"open": 10., "close": 10.} for day in days},
            "600002": {day: {"open": 20., "close": 20.} for day in days},
            "600003": {day: {"open": 30., "close": 30.} for day in days}}
    early = life.evaluate_snapshot(snapshot, days, bars, as_of="2025-01-02")
    assert early["status"] == "pending_entry"
    entered = life.evaluate_snapshot(snapshot, days, bars, as_of="2025-01-03")
    assert entered["status"] == "holding" and entered["entered"] == 2
    assert entered["mean_net_return"] is None
    bars["600001"]["2025-01-07"] = {"open": 10000., "close": 10000.}
    later = life.evaluate_snapshot(snapshot, days, bars, as_of="2025-01-03")
    assert later == entered
    before_open = dt.datetime(2025, 1, 7, 9, 15)
    waiting = life.evaluate_snapshot(snapshot, days, bars, as_of=before_open)
    assert waiting["status"] == "waiting_exit"
    missing = {code: {day: bar for day, bar in rows.items() if day != "2025-01-03"}
               for code, rows in bars.items()}
    bad = life.evaluate_snapshot(snapshot, days, missing, as_of="2025-01-03")
    assert bad["status"] == "partial_data"
    assert bad["selected"][0]["reason"] == "missing_entry_or_halt_unverified"
    # 即使日历尚未覆盖退出日，也可记录入场，不必等待完整持有期。
    short = life.evaluate_snapshot(snapshot, days[:2], bars, as_of="2025-01-03")
    assert short["entered"] == 2 and short["exit_day"] is None


def test_tracker_persists_waiting_and_is_idempotent(tmp_path):
    from tests.test_strategy_lifecycle import _snapshot
    db = str(tmp_path / "db.sqlite")
    store.init_db(db)
    row = _snapshot()
    store.freeze_strategy_signal(db, row["signal_date"], row["strategy"], row["version"], "2025-01-02T16:00", row["payload"])
    calendar = tmp_path / "calendar.json"
    _write_json(calendar, {"source": "test", "dates": ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"]})
    for minute in (10, 20):
        result = track(db, tmp_path / "raw", tmp_path / "legacy", calendar, as_of=dt.datetime(2025, 1, 2, 16, minute))
        assert result["pending"] == 1
    assert len(store.list_strategy_observations(db)) == 1


def test_completed_observation_is_preserved_when_source_files_are_missing(tmp_path):
    from tests.test_strategy_lifecycle import _snapshot
    db = str(tmp_path / "db.sqlite")
    store.init_db(db)
    row = _snapshot()
    frozen = store.freeze_strategy_signal(db, row["signal_date"], row["strategy"], row["version"], "2025-01-02T16:00", row["payload"])
    old = {"snapshot_id": frozen["id"], "status": "observed", "cost_model_version": life.COST_MODEL_VERSION,
           "filled": 1, "mean_net_return": .05, "pool_control_net": .04, "sector_control_net": .04}
    store.save_strategy_observation(db, frozen["id"], "forward-observation-v2", "old", "2025-01-07", old)
    calendar = tmp_path / "calendar.json"
    _write_json(calendar, {"source": "test", "dates": ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"]})
    result = track(db, tmp_path / "raw", tmp_path / "legacy", calendar, as_of="2025-01-08")
    assert result["pending"] == 0
    assert len(store.list_strategy_observations(db)) == 1
    assert store.list_strategy_observations(db)[0]["payload"] == old


def test_partial_membership_is_reported_without_promoting_coverage(tmp_path):
    source = Sources()
    source.fail.add("membership")
    result = runner(tmp_path, source).run()
    assert result["status"] == "partial"
    assert result["coverage"]["research"]["membership"]["missing_rate"] == 1
    assert result["coverage"]["research"]["trade_status"]["verified_halt_and_execution_limits"] == 0


def test_resume_refuses_another_day_or_changed_holdings(tmp_path):
    job = runner(tmp_path)
    result = job.run()
    with pytest.raises(ValueError, match="same_day_code_inputs"):
        runner(tmp_path, now=NOW + dt.timedelta(days=1)).run(result["run_id"])
    with pytest.raises(ValueError, match="same_day_code_inputs"):
        runner(tmp_path, holdings=["600002"]).run(result["run_id"])


@pytest.mark.parametrize("code,volume", [("000002", 100000.), ("600001", 100000.), ("688001", 1000.)])
def test_tencent_adapter_price_basis_and_units(code, volume):
    from core.data_source import with_prefix
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"data": {with_prefix(code): {"day": [["2026-09-30", "10", "10", "11", "9", "1000", {}, "2", "100"]]}}}
    result = daily_data.tencent_daily(code, "2026-09-01", "2026-09-30", "", get=lambda *a, **k: Response())
    assert result.iloc[0]["volume"] == volume
    assert result.iloc[0]["amount"] == 1e6
    assert result.iloc[0]["turnover"] == .02
    assert result.attrs["response_price_keys"] == ["day"]
    class OnlyAdjusted(Response):
        def json(self):
            return {"data": {with_prefix(code): {"qfqday": [["2026-09-30", "10", "10", "11", "9", "1000", {}, "2", "100"]]}}}
    with pytest.raises(ValueError, match="price_basis"):
        daily_data.tencent_daily(code, "2026-09-01", "2026-09-30", "", get=lambda *a, **k: OnlyAdjusted())
    with pytest.raises(ValueError, match="price_basis"):
        daily_data.tencent_daily(code, "2026-09-01", "2026-09-30", "qfq", get=lambda *a, **k: Response())


def test_primary_source_failure_switches_provider_once(monkeypatch):
    import akshare
    calls = Counter()
    def primary(**kwargs):
        calls["primary"] += 1
        raise ConnectionError("endpoint down")
    def secondary(*args, **kwargs):
        calls["secondary"] += 1
        return pd.DataFrame()
    monkeypatch.setattr(akshare, "stock_zh_a_hist", primary)
    monkeypatch.setattr(daily_data, "tencent_daily", secondary)
    source = daily_data.LiveSources()
    for code in ("600001", "600002"):
        source.daily(code, "2026-01-01", "2026-09-30", "")
    assert calls == {"primary": 1, "secondary": 2}
    assert source.price_provider == "tencent"
