# -*- coding: utf-8 -*-
"""A股三层分析看板 —— Flask 入口 + API 路由。"""
import os
from datetime import datetime

from flask import Flask, jsonify, render_template, request

import analysis as an
import data_source as ds
import store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "data", "market.db")
SECTOR_TYPES = ("industry",)


def ok(data, stale=False):
    return jsonify({"ok": True,
                    "meta": {"stale": stale, "updated_at": ds.last_updated_at},
                    "data": data})


def err(code, message, http):
    return jsonify({"ok": False, "error": {"code": code, "message": message}}), http


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _num(v):
    """None/NaN/非数值 → None,其余 → float。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _sort_key(v):
    """数值排序键:None/非数值排最后。"""
    n = _num(v)
    return float("-inf") if n is None else n


def _parse_sector_code(raw):
    if not raw or ":" not in raw:
        return None
    t, c = raw.split(":", 1)
    if t not in SECTOR_TYPES or not (c.isdigit() and len(c) == 6):
        return None
    return t, c


def _parse_stock_code(raw):
    if not raw:
        return None
    c = ds.normalize_code(raw)
    if not (c.isdigit() and len(c) == 6):
        return None
    return c


# ---------- API ----------

def register_routes(app):
    db_path = app.config["DB"]

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/market")
    def api_market():
        try:
            spot, spot_stale = ds.get_market_spot()
            indices, idx_stale = ds.get_index_realtime()
        except ds.DataSourceError as e:
            return err("SOURCE_FAIL", str(e), 500)
        now = datetime.now()
        today = _today()
        exclude = ds.get_new_stocks()
        breadth = an.compute_breadth(spot, exclude)
        volume_pct = None
        if an.is_after_close(now):
            prev = store.get_market_daily_prev(db_path, today)
            if prev and prev["total_turnover"]:
                volume_pct = round(
                    (breadth["total_turnover"] - prev["total_turnover"]) / prev["total_turnover"] * 100, 2)
        if an.is_trading_time(now):
            index_close = indices[0]["price"] if indices else None
            store.upsert_market_daily(
                db_path, today, breadth["up"], breadth["down"], breadth["flat"],
                breadth["limit_up"], breadth["limit_down"], breadth["total_turnover"],
                index_close, now.strftime("%Y-%m-%d %H:%M:%S"))
        return ok({"indices": indices, "breadth": breadth,
                   "volume_vs_yesterday": {"pct": volume_pct}}, stale=spot_stale or idx_stale)

    @app.route("/api/sectors")
    def api_sectors():
        type_key = request.args.get("type", "industry")
        if type_key not in SECTOR_TYPES:
            return err("BAD_PARAM", "type 必须为 industry", 400)
        try:
            top = int(request.args.get("top", "60"))
        except ValueError:
            return err("BAD_PARAM", "top 必须为整数", 400)
        top = max(1, min(200, top))
        search = (request.args.get("search") or "").strip()
        try:
            summary, stale = ds.get_sector_summary(type_key)
            spot, spot_stale = ds.get_market_spot()
        except ds.DataSourceError as e:
            return err("SOURCE_FAIL", str(e), 500)
        now = datetime.now()
        today = _today()
        market_turnover = float(spot["amount"].sum()) if len(spot) else 0.0

        # 计算当日 rank 并持久化(仅交易日);change_pct 可能为 None → _sort_key 排序
        if an.is_trading_time(now):
            ranked = summary.iloc[summary["change_pct"].map(_sort_key).sort_values(ascending=False).index]
            for i, r in ranked.iterrows():
                store.upsert_sector_daily(
                    db_path, today, type_key, r["code"], r["name"], _num(r["change_pct"]),
                    None, _num(r["turnover"]), i + 1)

        rows = []
        for _, r in summary.iterrows():
            code = str(r["code"])
            chg = _num(r["change_pct"])
            turn = _num(r["turnover"])
            # 降级口径(无成分股聚合):上涨家数占比取摘要上涨/(上涨+下跌);涨停占比无成分股数据恒 None
            # (情绪子项走 _weighted 归一化);领涨强度取摘要领涨股-涨跌幅
            up_count = _num(r["up_count"])
            down_count = _num(r["down_count"])
            up_ratio = None
            if up_count is not None and down_count is not None and (up_count + down_count) > 0:
                up_ratio = up_count / (up_count + down_count)
            limit_ratio = None
            leader_change = _num(r["leader_change_pct"])
            data_complete = True
            turnover_ratio = None
            if an.is_after_close(now) and turn is not None:
                avg5 = store.get_sector_turnover_avg(db_path, type_key, code, today, 5)
                if avg5 and avg5 > 0:
                    turnover_ratio = turn / float(avg5)
            prev_change = store.get_sector_prev_change(db_path, type_key, code, today)
            change_3d = store.get_sector_change_3d(db_path, type_key, code, today)
            activity = (turn / market_turnover) if (turn is not None and market_turnover) else None
            consecutive = store.get_consecutive_days(db_path, type_key, code, today, 20)

            emotion = an.sector_emotion(up_ratio, limit_ratio, turnover_ratio, leader_change)
            strength = an.sector_strength(chg, consecutive, activity)
            risk = an.sector_risk(chg, change_3d, turnover_ratio, prev_change, up_ratio)
            composite = an.composite_score(strength, emotion, risk)
            verdict = an.sector_verdict(emotion, strength, risk, consecutive, data_complete)
            rows.append({
                "code": "%s:%s" % (type_key, code), "name": str(r["name"]),
                "index_change_pct": chg,
                "emotion_score": emotion, "strength_score": strength, "risk_score": risk,
                "composite_score": composite, "verdict": verdict,
                "consecutive_days": consecutive, "data_complete": data_complete,
            })

        if search:
            kw = search.lower()
            matched = [x for x in rows if kw in str(x["name"]).lower()]
            total = len(matched)
            rows = matched
        else:
            total = len(rows)
            rows = sorted(rows, key=lambda x: (x["composite_score"] is None, -(x["composite_score"] or 0)))
        return ok({"type": type_key, "total": total, "sectors": rows[:top]}, stale=stale or spot_stale)

    @app.route("/api/sector")
    def api_sector():
        parsed = _parse_sector_code(request.args.get("code"))
        if parsed is None:
            return err("BAD_PARAM", "code 必须形如 industry:885887", 400)
        type_key, code = parsed
        try:
            summary, _ = ds.get_sector_summary(type_key)
            hist, _ = ds.get_sector_index_history(code, type_key)
            spot, _ = ds.get_market_spot()
        except ds.DataSourceError as e:
            return err("SOURCE_FAIL", str(e), 500)
        row = summary[summary["code"] == code]
        if row.empty:
            return err("BAD_PARAM", "未找到该板块", 400)
        r = row.iloc[0]
        chg = _num(r["change_pct"])
        turn = _num(r["turnover"])
        up_count = _num(r["up_count"])
        down_count = _num(r["down_count"])
        up_ratio = None
        if up_count is not None and down_count is not None and (up_count + down_count) > 0:
            up_ratio = up_count / (up_count + down_count)
        limit_ratio = None
        leader_change = _num(r["leader_change_pct"])
        now = datetime.now()
        today = _today()
        turnover_ratio = None
        if an.is_after_close(now) and turn is not None:
            avg5 = store.get_sector_turnover_avg(app.config["DB"], type_key, code, today, 5)
            if avg5 and avg5 > 0:
                turnover_ratio = turn / float(avg5)
        prev_change = store.get_sector_prev_change(app.config["DB"], type_key, code, today)
        change_3d = store.get_sector_change_3d(app.config["DB"], type_key, code, today)
        consecutive = store.get_consecutive_days(app.config["DB"], type_key, code, today, 20)
        market_turnover = float(spot["amount"].sum()) if len(spot) else 0.0
        activity = (turn / market_turnover) if (turn is not None and market_turnover) else None
        emotion = an.sector_emotion(up_ratio, limit_ratio, turnover_ratio, leader_change)
        strength = an.sector_strength(chg, consecutive, activity)
        risk = an.sector_risk(chg, change_3d, turnover_ratio, prev_change, up_ratio)
        composite = an.composite_score(strength, emotion, risk)
        verdict = an.sector_verdict(emotion, strength, risk, consecutive, True)
        hist_rows = [{"date": str(x["date"]), "open": float(x["open"]), "high": float(x["high"]),
                      "low": float(x["low"]), "close": float(x["close"]), "volume": float(x["volume"])}
                     for x in hist.to_dict("records")]
        return ok({"code": "%s:%s" % (type_key, code), "name": str(r["name"]),
                   "scores": {"emotion": emotion, "strength": strength, "risk": risk,
                              "composite": composite},
                   "verdict": verdict, "index_history": hist_rows})

    @app.route("/api/stock")
    def api_stock():
        raw = request.args.get("code")
        code6 = _parse_stock_code(raw)
        if code6 is None:
            return err("BAD_PARAM", "code 必须为 6 位股票代码", 400)
        try:
            quote, _ = ds.get_stock_quote(code6)
            daily, _ = ds.get_stock_daily(code6)
            minute, _ = ds.get_stock_minute(code6)
        except ds.DataSourceError as e:
            return err("SOURCE_FAIL", str(e), 500)
        now = datetime.now()
        scores = an.score_stock(daily, quote, now)
        kline = [{"date": str(x["date"]), "open": float(x["open"]), "high": float(x["high"]),
                  "low": float(x["low"]), "close": float(x["close"]), "volume": float(x["volume"])}
                 for x in daily.tail(250).to_dict("records")]
        intraday = [{"time": str(x["time"]), "price": float(x["price"]),
                     "avg": float(x["avg"]), "volume": float(x["volume"])}
                    for x in minute.to_dict("records")]
        return ok({"code": ds.with_prefix(code6), "name": quote["name"], "quote": quote,
                   "scores": scores, "verdict": scores["verdict"], "kline": kline,
                   "intraday": intraday})


def create_app(db_path=None):
    app = Flask(__name__)
    app.config["DB"] = db_path or DEFAULT_DB
    os.makedirs(os.path.dirname(os.path.abspath(app.config["DB"])), exist_ok=True)
    store.init_db(app.config["DB"])
    register_routes(app)
    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=8000)
