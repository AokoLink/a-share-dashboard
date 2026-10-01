import json
from pathlib import Path

import pandas as pd
import pytest
import requests

from core import daily_data as data, price_collection as pc


DATES = pd.bdate_range("2026-06-01", "2026-10-02").strftime("%Y-%m-%d").tolist()
START, DAY = DATES[0], "2026-09-30"


class Prices:
    def __init__(self):
        self.calls, self.rebase, self.fail, self.hole = [], False, None, None

    def daily(self, code, start, end, adjust):
        self.calls.append((code, start, end, adjust))
        if self.fail:
            exc, self.fail = self.fail, None
            raise exc
        days = [d for d in DATES if start <= d <= end and d != self.hole]
        close = [10 + DATES.index(d) / 100 for d in days]
        if self.rebase:
            close = [c * .9 if d < end else c for d,c in zip(days, close)]
        f = pd.DataFrame({"date": days, "open": close, "high": [c + .1 for c in close],
            "low": [c - .1 for c in close], "close": close, "volume": 1e5, "amount": 1e8, "turnover": .02})
        f.attrs["source"] = "checked_test_provider"
        return f


def collector(tmp_path, source, day=DAY, mode="injected", **kwargs):
    n = len(list((tmp_path / "runs").glob("*")))
    work = tmp_path / "runs" / str(n)
    work.mkdir(parents=True)
    return pc.PriceCollector(tmp_path, work, source, DATES, day, START, mode,
                             lambda: day + "T16:00:00+08:00", sleep=lambda _: None, **kwargs)


def test_cross_run_cache_increment_and_immutable_parents(tmp_path):
    source = Prices()
    first = collector(tmp_path, source).collect("600001", "technical")
    assert first["collection_mode"] == "history_backfill"
    same = collector(tmp_path, source).collect("600001", "technical")
    assert same["collection_mode"] == "cache_current" and len(source.calls) == 1
    next_day = collector(tmp_path, source, "2026-10-01").collect("600001", "technical")
    assert next_day["collection_mode"] == "incremental"
    assert source.calls[-1][1] > START
    assert next_day["quality"]["current"]
    assert data.intact(first["artifact"]) and data.intact(next_day["artifact"])
    assert len(pd.read_csv(next_day["artifact"]["path"])) == len(pd.read_csv(first["artifact"]["path"])) + 1
    # 注入缓存不能充当真实来源采集。
    live = collector(tmp_path, source, mode="live").collect("600001", "technical")
    assert live["collection_mode"] == "history_backfill"


def test_adjustment_rebase_forces_full_history_refresh(tmp_path):
    source = Prices()
    first = collector(tmp_path, source).collect("600001", "technical")
    source.rebase = True
    next_day = collector(tmp_path, source, "2026-10-01").collect("600001", "technical")
    assert next_day["collection_mode"] == "full_refresh"
    assert len(next_day["requests"]) == 2 and source.calls[-1][1] == START
    assert data.intact(first["artifact"])
    assert pd.read_csv(next_day["artifact"]["path"]).iloc[0]["close"] == 9.


def test_future_and_tampered_cache_are_not_used(tmp_path):
    source = Prices()
    first = collector(tmp_path, source).collect("600001", "raw")
    earlier = collector(tmp_path, source, "2026-09-29").collect("600001", "raw")
    assert earlier["collection_mode"] == "history_backfill"
    assert earlier["quality"]["last"] == "2026-09-29"
    Path(earlier["artifact"]["path"]).write_text("broken", encoding="utf-8")
    recollected = collector(tmp_path, source).collect("600001", "raw")
    assert recollected["status"] == "success" and recollected["collection_mode"] == "history_backfill"
    assert data.intact(first["artifact"])


def test_transport_retry_is_bounded_and_semantic_failure_is_not_retried(tmp_path):
    source = Prices()
    source.fail = requests.Timeout("transient")
    result = collector(tmp_path, source).collect("600001", "raw")
    assert result["status"] == "success" and len(result["requests"]) == 2
    source.fail = ValueError("bad_schema")
    result = collector(tmp_path, source, refresh=True).collect("600001", "raw")
    assert result["status"] == "failed" and len(result["requests"]) == 1
    cached = collector(tmp_path, source).collect("600001", "raw")
    assert cached["status"] == "success" and cached["collection_mode"] == "cache_current"


def test_gap_repair_is_targeted_and_preserves_unknown_semantics(tmp_path):
    source = Prices()
    source.hole = "2026-07-01"
    first = collector(tmp_path, source).collect("600001", "raw")
    assert first["quality"]["missing_dates"] == [source.hole]
    source.hole = None
    second = collector(tmp_path, source).collect("600001", "raw", first)
    assert source.calls[-1][1] == "2026-07-01"
    assert second["collection_mode"] == "incremental" and second["quality"]["missing_sessions"] == 0
    assert data.intact(first["artifact"])


def test_factor_evidence_is_required_and_price_basis_is_explicit(tmp_path):
    raw = Prices().daily("600001", START, DAY, "")
    payload = {"total": 2, "data": [{"d": "1900-01-01", "f": "2"}, {"d": "2026-09-01", "f": "1"}]}
    class Response:
        def raise_for_status(self):
            pass
        @property
        def text(self):
            return "var sh600001qfq=" + json.dumps(payload) + "; throw Error('must not execute');"
    result = data.sina_factor_adjusted(raw, "600001", DAY, get=lambda *a, **k: Response())
    assert result.iloc[0]["close"] == raw.iloc[0]["close"] / 2
    assert result.iloc[-1]["close"] == raw.iloc[-1]["close"]
    assert result["volume"].equals(raw["volume"])
    class Adjusted(Prices):
        def daily(self, *args):
            return result
    archived = collector(tmp_path, Adjusted()).collect("600001", "technical")
    ref = archived["response"]["dependencies"][0]
    assert data.intact(ref) and data.read_json(ref["path"])["payload"] == payload
    Path(ref["path"]).write_text("tampered", encoding="utf-8")
    assert not data.intact(archived["artifact"])
    payload["total"] = 3
    with pytest.raises(ValueError, match="incomplete"):
        data.sina_factor_adjusted(raw, "600001", DAY, get=lambda *a, **k: Response())


def test_price_stability_is_separate_from_whole_pipeline_and_excludes_shadow(tmp_path):
    from core.daily_runner import continuity
    from core.stage1_data import _write_json
    days = DATES[-10:]
    for i, day in enumerate(days):
        _write_json(tmp_path / "runs" / str(i) / "run.json", {
            "run_id": str(i), "day": day, "started_at": day, "source_mode": "live", "status": "partial",
            "steps": {key: {"status": "success"} for key in ("inputs", "universe", "prices", "quality")}})
    report = continuity(tmp_path, {"dates": DATES})
    assert not report["passed"] and report["price_coverage"]["passed"]
    path = tmp_path / "runs" / "9" / "run.json"
    row = data.read_json(path)
    row["observe_only"] = True
    _write_json(path, row)
    report = continuity(tmp_path, {"dates": DATES})
    assert report["price_coverage"]["consecutive_complete_sessions"] == 9


def test_live_source_only_constructs_qfq_after_explicit_factor_evidence(monkeypatch):
    source = data.LiveSources()
    source.price_provider = "tencent"
    calls = []
    def tencent(code, start, end, adjust, **kwargs):
        calls.append(adjust)
        if adjust:
            raise ValueError("requested_price_basis_not_available")
        return Prices().daily(code, start, end, "")
    class Response:
        def raise_for_status(self):
            pass
        text = 'var sh600001qfq={"total":1,"data":[{"d":"1900-01-01","f":"1"}]}'
    monkeypatch.setattr(data, "tencent_daily", tencent)
    monkeypatch.setattr(source, "get", lambda *a, **k: Response())
    result = source.daily("600001", START, DAY, "qfq")
    assert calls == ["qfq", ""]
    assert result.attrs["source"] == "tencent.raw+sina.qfq_factors"
    assert result.attrs["factor_evidence"]["payload"]["total"] == 1


def test_920_prices_use_beijing_and_b_share_prefix_remains_shanghai():
    from core.data_source import with_prefix
    from core.analysis import limit_threshold
    assert with_prefix("920002") == with_prefix("bj920002") == "bj920002"
    assert with_prefix("900901") == "sh900901"
    assert limit_threshold("920002") == 29.9


def test_resume_does_not_mix_injected_and_live_sources(tmp_path):
    from tests.test_daily_pipeline import runner
    job = runner(tmp_path)
    result = job.run()
    job.sources = data.LiveSources()
    with pytest.raises(ValueError, match="same_day_code_inputs"):
        job.run(result["run_id"])
