# -*- coding: utf-8 -*-
"""持久化层:SQLite 每日快照。db 参数为数据库文件路径。"""
import json
import hashlib
import os
import sqlite3
import threading

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
CREATE TABLE IF NOT EXISTS recommend_snapshot (
  signal_date TEXT PRIMARY KEY,
  generated_at TEXT, close_date TEXT, prev_trading_date TEXT, payload TEXT
);
CREATE TABLE IF NOT EXISTS actionable_snapshot (
  signal_date TEXT PRIMARY KEY,
  generated_at TEXT, close_date TEXT, total INTEGER, payload TEXT
);
CREATE TABLE IF NOT EXISTS strategy_signal_snapshot (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_date TEXT NOT NULL,
  strategy TEXT NOT NULL,
  version TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  frozen_at TEXT NOT NULL,
  payload TEXT NOT NULL,
  UNIQUE(signal_date, strategy, version, content_sha256)
);
CREATE INDEX IF NOT EXISTS idx_strategy_signal_day
  ON strategy_signal_snapshot(signal_date, strategy, version, id);
CREATE TABLE IF NOT EXISTS strategy_observation (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_id INTEGER NOT NULL REFERENCES strategy_signal_snapshot(id),
  tracker_version TEXT NOT NULL,
  input_sha256 TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  payload TEXT NOT NULL,
  UNIQUE(snapshot_id, tracker_version, input_sha256)
);
CREATE TABLE IF NOT EXISTS strategy_status_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy TEXT NOT NULL,
  version TEXT NOT NULL,
  status TEXT NOT NULL,
  reason TEXT NOT NULL,
  changed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observation_snapshot_id ON strategy_observation(snapshot_id,id);
CREATE INDEX IF NOT EXISTS idx_status_strategy_version ON strategy_status_event(strategy,version,id);
"""

_conns = threading.local()


def _conn_cache():
    cache = getattr(_conns, "cache", None)
    if cache is None:
        cache = {}
        _conns.cache = cache
    return cache


def init_db(db):
    os.makedirs(os.path.dirname(os.path.abspath(db)), exist_ok=True)
    conn = _connect(db)
    conn.executescript(SCHEMA)
    conn.commit()
    # 连接留在线程本地缓存中复用(由 close_all() 统一关闭)


def _connect(db):
    key = os.path.abspath(db)
    cache = _conn_cache()
    conn = cache.get(key)
    if conn is None:
        conn = sqlite3.connect(db, timeout=30)
        conn.row_factory = sqlite3.Row  # 使 dict(row) 按列名映射
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        cache[key] = conn
    return conn


def close_all():
    """关闭线程本地缓存中的所有连接(应用关闭 / 测试 teardown)。"""
    cache = getattr(_conns, "cache", None)
    if cache:
        for conn in cache.values():
            try:
                conn.close()
            except Exception:
                pass
        _conns.cache = {}


def _row_to_dict(row):
    return dict(row) if row else None


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def freeze_strategy_signal(db, signal_date, strategy, version, frozen_at, payload):
    """内容相同幂等；内容变化另存修订，首次快照绝不覆盖。"""
    body = _canonical(payload)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    conn = _connect(db)
    with conn:
        conn.execute("""INSERT OR IGNORE INTO strategy_signal_snapshot
            (signal_date, strategy, version, content_sha256, frozen_at, payload)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (signal_date, strategy, version, digest, frozen_at, body))
        row = conn.execute("""SELECT id FROM strategy_signal_snapshot
            WHERE signal_date=? AND strategy=? AND version=? AND content_sha256=?""",
            (signal_date, strategy, version, digest)).fetchone()
    return {"id": row[0], "sha256": digest}


def list_strategy_signals(db, strategy=None, version=None, signal_date=None, limit=500):
    clauses, args = [], []
    if strategy:
        clauses.append("strategy=?")
        args.append(strategy)
    if version:
        clauses.append("version=?")
        args.append(version)
    if signal_date:
        clauses.append("signal_date=?")
        args.append(signal_date)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = _connect(db).execute("SELECT * FROM strategy_signal_snapshot" + where
        + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
    return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]


def get_strategy_signal(db, snapshot_id):
    row = _connect(db).execute("SELECT * FROM strategy_signal_snapshot WHERE id=?",
                                    (snapshot_id,)).fetchone()
    return {**dict(row), "payload": json.loads(row["payload"])} if row else None


def save_strategy_observation(db, snapshot_id, tracker_version, input_sha256,
                              observed_at, payload):
    body = _canonical(payload)
    conn = _connect(db)
    with conn:
        conn.execute("""INSERT OR IGNORE INTO strategy_observation
            (snapshot_id, tracker_version, input_sha256, observed_at, payload)
            VALUES (?, ?, ?, ?, ?)""",
            (snapshot_id, tracker_version, input_sha256, observed_at, body))


def list_strategy_observations(db, limit=500):
    rows = _connect(db).execute("SELECT * FROM strategy_observation ORDER BY id DESC LIMIT ?",
                                     (limit,)).fetchall()
    return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]


def monitor_watermark(db):
    return {table: dict(_connect(db).execute(f"SELECT COUNT(*) AS count,COALESCE(MAX(id),0) AS last_id FROM {table}").fetchone())
            for table in ("strategy_signal_snapshot", "strategy_observation", "strategy_status_event")}


def monitor_page(db, *, day=None, strategy=None, version=None, before=None, limit=30):
    clauses, args = [], []
    for field, value in (("signal_date", day), ("strategy", strategy), ("version", version)):
        if value:
            clauses.append("s." + field + "=?")
            args.append(value)
    if before:
        clauses.append("s.id<?")
        args.append(before)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = _connect(db).execute("SELECT s.* FROM strategy_signal_snapshot s" + where + " ORDER BY s.id DESC LIMIT ?",
                               (*args, limit + 1)).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    snapshots = [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]
    ids = [r["id"] for r in snapshots]
    observations = []
    if ids:
        marks = ",".join("?" for _ in ids)
        observations = _connect(db).execute(f"SELECT o.* FROM strategy_observation o WHERE o.snapshot_id IN ({marks}) "
            "AND o.id=(SELECT MAX(n.id) FROM strategy_observation n WHERE n.snapshot_id=o.snapshot_id) ORDER BY o.id DESC", ids).fetchall()
    events = _connect(db).execute("SELECT * FROM strategy_status_event ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"snapshots": snapshots, "observations": [{**dict(r), "payload": json.loads(r["payload"])} for r in observations],
            "status_events": [dict(r) for r in events], "next_cursor": rows[-1]["id"] if more else None}


def add_strategy_status_event(db, strategy, version, status, reason, changed_at):
    from core.strategy_lifecycle import ALLOWED_STATES, review_versions
    if status not in ALLOWED_STATES:
        raise ValueError("unknown strategy status")
    if not reason.strip():
        raise ValueError("status transition requires reason")
    events = list_strategy_status_events(db)
    prior = next((row["status"] for row in reversed(events)
                  if row["strategy"] == strategy and row["version"] == version), "exploratory")
    transitions = {
        "exploratory": {"historical_validated", "paused", "retired"},
        "historical_validated": {"forward_observation", "paused", "retired"},
        "forward_observation": {"formal", "paused", "retired"},
        "formal": {"paused", "retired"},
        "paused": {"exploratory", "historical_validated", "forward_observation", "retired"},
        "retired": set(),
    }
    if status not in transitions[prior]:
        raise ValueError(f"invalid status transition: {prior} -> {status}")
    matching = list_strategy_signals(db, strategy=strategy, version=version, limit=1)
    if not matching:
        raise ValueError("strategy/version has no frozen signal")
    if status == "formal":
        review = review_versions(list_strategy_signals(db, strategy=strategy, version=version,
                                                      limit=100000),
                                 list_strategy_observations(db, limit=100000), events)
        if not review or review[0]["blockers_to_formal"]:
            raise ValueError("formal status blocked by evidence gates")
    with _connect(db) as conn:
        conn.execute("""INSERT INTO strategy_status_event
            (strategy, version, status, reason, changed_at) VALUES (?, ?, ?, ?, ?)""",
            (strategy, version, status, reason, changed_at))


def list_strategy_status_events(db):
    return [dict(row) for row in _connect(db).execute(
        "SELECT * FROM strategy_status_event ORDER BY id")]


def get_strategy_statuses(db, version, strategies):
    """只读取指定版本的最终状态，未出现过的策略处于探索研究。"""
    keys = tuple(strategies)
    statuses = {key: "exploratory" for key in keys}
    for row in _connect(db).execute(
            "SELECT strategy, status FROM strategy_status_event WHERE version=? ORDER BY id",
            (version,)):
        if row["strategy"] in statuses:
            statuses[row["strategy"]] = row["status"]
    return statuses


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


def get_market_daily_prev(db, date):
    conn = _connect(db)
    cur = conn.execute(
        "SELECT * FROM market_daily WHERE date < ? ORDER BY date DESC LIMIT 1", (date,))
    row = cur.fetchone()
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


def get_sector_turnover_avg(db, type, code, date, days=5):
    conn = _connect(db)
    cur = conn.execute(
        """SELECT AVG(turnover) FROM (
             SELECT turnover FROM sector_daily
             WHERE type=? AND code=? AND date < ?
             ORDER BY date DESC LIMIT ?)""",
        (type, code, date, days))
    val = cur.fetchone()[0]
    return val if val is not None else None


def get_sector_prev_change(db, type, code, date):
    conn = _connect(db)
    cur = conn.execute(
        "SELECT change_pct FROM sector_daily WHERE type=? AND code=? AND date < ? "
        "ORDER BY date DESC LIMIT 1", (type, code, date))
    row = cur.fetchone()
    return row[0] if row else None


def get_sector_change_3d(db, type, code, date):
    conn = _connect(db)
    cur = conn.execute(
        "SELECT change_pct FROM sector_daily WHERE type=? AND code=? AND date < ? "
        "ORDER BY date DESC LIMIT 3", (type, code, date))
    rows = cur.fetchall()
    if len(rows) < 2:
        return None
    prod = 1.0
    for (c,) in rows:
        prod *= (1 + c / 100.0)
    return (prod - 1) * 100.0


def get_consecutive_days(db, type, code, date, top_n=20):
    conn = _connect(db)
    cur = conn.execute(
        """WITH ranked AS (
             SELECT date, code,
                    CASE WHEN COUNT(*) OVER (PARTITION BY date, type) > 1
                         THEN ROW_NUMBER() OVER (PARTITION BY date, type
                                                 ORDER BY change_pct DESC, code)
                         ELSE rank END AS actual_rank
             FROM sector_daily WHERE type=? AND date<=?)
           SELECT actual_rank FROM ranked WHERE code=? ORDER BY date DESC""",
        (type, date, code))
    days = 0
    for (r,) in cur:
        if r is not None and r <= top_n:
            days += 1
        else:
            break
    return days


def get_sector_observations(db, type, code, limit=60):
    """仅返回应用实际保存的板块排名/成交快照，不填补缺失交易日。"""
    conn = _connect(db)
    rows = conn.execute(
        """WITH ranked AS (
             SELECT date, type, code, up_ratio, turnover,
                    ROW_NUMBER() OVER (PARTITION BY date, type
                                       ORDER BY change_pct DESC, code) AS actual_rank
             FROM sector_daily WHERE type=?)
           SELECT s.date, s.actual_rank AS rank, s.up_ratio, s.turnover,
                  CASE WHEN m.total_turnover > 0 THEN s.turnover / m.total_turnover END AS amount_share
           FROM ranked s LEFT JOIN market_daily m ON m.date=s.date
           WHERE s.code=? ORDER BY s.date DESC LIMIT ?""",
        (type, code, limit)).fetchall()
    return [dict(row) for row in reversed(rows)]


def upsert_recommend_snapshot(db, signal_date, generated_at, close_date, prev_trading_date, stocks):
    conn = _connect(db)
    conn.execute(
        """INSERT INTO recommend_snapshot(signal_date, generated_at, close_date, prev_trading_date, payload)
           VALUES(?,?,?,?,?)
           ON CONFLICT(signal_date) DO UPDATE SET
             generated_at=excluded.generated_at, close_date=excluded.close_date,
             prev_trading_date=excluded.prev_trading_date, payload=excluded.payload""",
        (signal_date, generated_at, close_date, prev_trading_date,
         json.dumps(stocks, ensure_ascii=False)))
    conn.commit()


def get_recommend_snapshot_before(db, signal_date):
    conn = _connect(db)
    cur = conn.execute(
        "SELECT * FROM recommend_snapshot WHERE signal_date < ? ORDER BY signal_date DESC LIMIT 1",
        (signal_date,))
    row = cur.fetchone()
    d = _row_to_dict(row)
    if d and d.get("payload"):
        d["stocks"] = json.loads(d["payload"])
    return d


def upsert_actionable_snapshot(db, signal_date, generated_at, close_date, total, items):
    """可介入龙头的每信号日快照(完整 items)。signal_date 为主键 → 同日重建覆盖,天然去重。

    items 为完整 item 列表(含当时算出的 position/composite/verdict/tier 等)。
    这是「生成值存档」:记录当时算出了什么,不是「推荐后表现」—— 后者需另配回填脚本。
    """
    conn = _connect(db)
    conn.execute(
        """INSERT INTO actionable_snapshot(signal_date, generated_at, close_date, total, payload)
           VALUES(?,?,?,?,?)
           ON CONFLICT(signal_date) DO UPDATE SET
             generated_at=excluded.generated_at, close_date=excluded.close_date,
             total=excluded.total, payload=excluded.payload""",
        (signal_date, generated_at, close_date, total,
         json.dumps(items, ensure_ascii=False)))
    conn.commit()
