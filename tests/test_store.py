# tests/test_store.py
# -*- coding: utf-8 -*-
import os
import pytest
from core import store


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "t.db")
    store.init_db(path)
    return path


def test_init_creates_tables(db):
    # 无报错即表已建;再次 init 幂等
    store.init_db(db)


def test_market_daily_upsert_idempotent(db):
    store.upsert_market_daily(db, "2026-08-10", 100, 50, 5, 3, 1, 1.0e9, 3400.0, "15:00:00")
    store.upsert_market_daily(db, "2026-08-10", 120, 60, 6, 5, 2, 1.2e9, 3420.0, "15:05:00")  # 覆盖
    rows = store.get_market_daily_prev(db, "2026-08-11")
    assert rows == {"date": "2026-08-10", "up_count": 120, "down_count": 60, "flat_count": 6,
                    "limit_up": 5, "limit_down": 2, "total_turnover": 1.2e9,
                    "index_close": 3420.0, "snapshot_time": "15:05:00"}


def test_market_daily_prev_skips_self(db):
    store.upsert_market_daily(db, "2026-08-10", 1, 0, 0, 0, 0, 100.0, 1.0, "15:00:00")
    assert store.get_market_daily_prev(db, "2026-08-10") is None  # 不含自身
    assert store.get_market_daily_prev(db, "2026-08-11")["date"] == "2026-08-10"


def test_sector_turnover_avg_excludes_today(db):
    for i, d in enumerate(["2026-08-05", "2026-08-06", "2026-08-07", "2026-08-08", "2026-08-11"]):
        store.upsert_sector_daily(db, d, "industry", "885887", "半导体", 1.0, 0.5, 100 + i, i + 1)
    avg = store.get_sector_turnover_avg(db, "industry", "885887", "2026-08-11", days=5)
    # 排除 2026-08-11,取前 4 行 100..103 均值 = 101.5
    assert avg == pytest.approx(101.5)
    assert store.get_sector_turnover_avg(db, "industry", "999999", "2026-08-11") is None


def test_sector_prev_change_and_3d(db, tmp_path):
    store.upsert_sector_daily(db, "2026-08-06", "industry", "885887", "半导体", 1.0, 0.5, 100.0, 1)
    store.upsert_sector_daily(db, "2026-08-07", "industry", "885887", "半导体", 2.0, 0.6, 110.0, 1)
    store.upsert_sector_daily(db, "2026-08-08", "industry", "885887", "半导体", 3.0, 0.7, 120.0, 1)
    assert store.get_sector_prev_change(db, "industry", "885887", "2026-08-11") == pytest.approx(3.0)
    # 3日累计 = (1.01*1.02*1.03-1) ≈ 6.11%
    assert store.get_sector_change_3d(db, "industry", "885887", "2026-08-11") == pytest.approx(6.11, abs=0.01)
    # 仅 1 行历史 → 无法判断 3 日 → None
    store2 = str(tmp_path / "t2.db")
    store.init_db(store2)
    store.upsert_sector_daily(store2, "2026-08-08", "industry", "885887", "半导体", 1.0, 0.5, 100.0, 1)
    assert store.get_sector_change_3d(store2, "industry", "885887", "2026-08-11") is None


def test_consecutive_days_counts_rank_streak(db):
    rows = [("2026-08-03", 5), ("2026-08-04", 30), ("2026-08-05", 2), ("2026-08-06", 3)]
    for i, (d, r) in enumerate(rows):
        store.upsert_sector_daily(db, d, "industry", "885887", "半导体", 1.0, 0.5, 100.0 + i, r)
    assert store.get_consecutive_days(db, "industry", "885887", "2026-08-06") == 2  # 8-06,8-05 连续,8-04 断开


def test_recommend_snapshot_upsert_and_get_before(tmp_path):
    db = str(tmp_path / "reco_snap.db")
    store.init_db(db)
    stocks = [{"code": "sh600050", "name": "联通", "signal_close": 5.01}]
    store.upsert_recommend_snapshot(db, "2026-08-12", "2026-08-12 17:40:00",
                                    "2026-08-12", "2026-08-11", stocks)
    # 同日重建覆盖(PK 幂等)
    store.upsert_recommend_snapshot(db, "2026-08-12", "2026-08-12 18:00:00",
                                    "2026-08-12", "2026-08-11",
                                    [{"code": "sh600050", "name": "联通", "signal_close": 5.02}])
    # 盘中 08-13 生成 → get_before(08-13) 命中 08-12
    store.upsert_recommend_snapshot(db, "2026-08-13", "2026-08-13 09:30:00",
                                    "2026-08-12", "2026-08-12", [])
    row = store.get_recommend_snapshot_before(db, "2026-08-13")
    assert row is not None and row["signal_date"] == "2026-08-12"
    assert row["close_date"] == "2026-08-12"
    assert row["stocks"][0]["signal_close"] == 5.02     # 同日重建覆盖生效
    assert store.get_recommend_snapshot_before(db, "2026-08-12") is None   # 无更早


def test_connection_reuse_within_thread(tmp_path):
    db = str(tmp_path / "reuse.db")
    store.init_db(db)
    c1 = store._connect(db)
    c2 = store._connect(db)
    assert c1 is c2                      # 同线程同路径复用同一连接


def test_write_visible_without_close(tmp_path):
    db = str(tmp_path / "vis.db")
    store.init_db(db)
    store.upsert_market_daily(db, "2026-08-10", 100, 50, 5, 3, 1, 1e9, 3400.0, "15:00")
    row = store.get_market_daily_prev(db, "2026-08-11")
    assert row["date"] == "2026-08-10"   # 无显式 close 仍已 commit 且可读


def test_close_all_reopens(tmp_path):
    db = str(tmp_path / "close.db")
    store.init_db(db)
    c1 = store._connect(db)
    store.close_all()
    c2 = store._connect(db)
    assert c1 is not c2
