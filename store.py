# -*- coding: utf-8 -*-
"""持久化层:SQLite 每日快照。db 参数为数据库文件路径。"""
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_daily (
  date TEXT PRIMARY KEY,
  up_count INTEGER, down_count INTEGER, flat_count INTEGER,
  limit_up INTEGER, limit_down INTEGER,
  total_turnover REAL, index_close REAL, snapshot_time TEXT
);
CREATE TABLE IF NOT EXISTS sector_daily (
  date TEXT, type TEXT, code TEXT, name TEXT,
  change_pct REAL, up_ratio REAL, turnover REAL, rank INTEGER,
  PRIMARY KEY(date, type, code)
);
"""


def init_db(db):
    os.makedirs(os.path.dirname(os.path.abspath(db)), exist_ok=True)
    conn = _connect(db)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def _connect(db):
    conn = sqlite3.connect(db, timeout=30)
    conn.row_factory = sqlite3.Row  # 使 dict(row) 按列名映射
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _row_to_dict(row):
    return dict(row) if row else None


def upsert_market_daily(db, date, up_count, down_count, flat_count, limit_up, limit_down,
                        total_turnover, index_close, snapshot_time):
    conn = _connect(db)
    conn.execute(
        """INSERT INTO market_daily(date, up_count, down_count, flat_count, limit_up, limit_down,
             total_turnover, index_close, snapshot_time)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(date) DO UPDATE SET
             up_count=excluded.up_count, down_count=excluded.down_count,
             flat_count=excluded.flat_count, limit_up=excluded.limit_up,
             limit_down=excluded.limit_down, total_turnover=excluded.total_turnover,
             index_close=excluded.index_close, snapshot_time=excluded.snapshot_time""",
        (date, up_count, down_count, flat_count, limit_up, limit_down,
         total_turnover, index_close, snapshot_time))
    conn.commit()
    conn.close()


def get_market_daily_prev(db, date):
    conn = _connect(db)
    cur = conn.execute(
        "SELECT * FROM market_daily WHERE date < ? ORDER BY date DESC LIMIT 1", (date,))
    row = cur.fetchone()
    conn.close()
    return _row_to_dict(row)


def upsert_sector_daily(db, date, type, code, name, change_pct, up_ratio, turnover, rank):
    conn = _connect(db)
    conn.execute(
        """INSERT INTO sector_daily(date, type, code, name, change_pct, up_ratio, turnover, rank)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(date, type, code) DO UPDATE SET
             name=excluded.name, change_pct=excluded.change_pct, up_ratio=excluded.up_ratio,
             turnover=excluded.turnover, rank=excluded.rank""",
        (date, type, code, name, change_pct, up_ratio, turnover, rank))
    conn.commit()
    conn.close()


def get_sector_turnover_avg(db, type, code, date, days=5):
    conn = _connect(db)
    cur = conn.execute(
        """SELECT AVG(turnover) FROM (
             SELECT turnover FROM sector_daily
             WHERE type=? AND code=? AND date < ?
             ORDER BY date DESC LIMIT ?)""",
        (type, code, date, days))
    val = cur.fetchone()[0]
    conn.close()
    return val if val is not None else None


def get_sector_prev_change(db, type, code, date):
    conn = _connect(db)
    cur = conn.execute(
        "SELECT change_pct FROM sector_daily WHERE type=? AND code=? AND date < ? "
        "ORDER BY date DESC LIMIT 1", (type, code, date))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def get_sector_change_3d(db, type, code, date):
    conn = _connect(db)
    cur = conn.execute(
        "SELECT change_pct FROM sector_daily WHERE type=? AND code=? AND date < ? "
        "ORDER BY date DESC LIMIT 3", (type, code, date))
    rows = cur.fetchall()
    conn.close()
    if len(rows) < 2:
        return None
    prod = 1.0
    for (c,) in rows:
        prod *= (1 + c / 100.0)
    return (prod - 1) * 100.0


def get_consecutive_days(db, type, code, date, top_n=20):
    conn = _connect(db)
    cur = conn.execute(
        "SELECT rank FROM sector_daily WHERE type=? AND code=? AND date <= ? "
        "ORDER BY date DESC", (type, code, date))
    days = 0
    for (r,) in cur:
        if r is not None and r <= top_n:
            days += 1
        else:
            break
    conn.close()
    return days
