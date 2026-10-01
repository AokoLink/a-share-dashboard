# -*- coding: utf-8 -*-
"""板块描述性证据：相对强度、内部广度、分化和龙头角色。"""
import math

import pandas as pd

from core.analysis import limit_threshold

VERSION = "dated-sector-features-v2"
MIN_COVERAGE = 0.8


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def relative_strength(sector_history, market_history, periods=(5, 20, 60), *, as_of=None, calendar=None):
    """只比较两条指数共同交易日；基准为上证综指，不能称全A市场。"""
    if sector_history is None or market_history is None or not len(sector_history) or not len(market_history):
        return {"status": "benchmark_unavailable", "benchmark": "sh000001", "returns": {}, "as_of": None}
    sector = sector_history[["date", "close"]].copy()
    market = market_history[["date", "close"]].copy()
    sector["date"] = pd.to_datetime(sector["date"]).dt.strftime("%Y-%m-%d")
    market["date"] = pd.to_datetime(market["date"]).dt.strftime("%Y-%m-%d")
    sector["close"] = pd.to_numeric(sector["close"], errors="coerce")
    market["close"] = pd.to_numeric(market["close"], errors="coerce")
    if sector["date"].duplicated().any() or market["date"].duplicated().any():
        return {"status": "duplicate_dates", "benchmark": "sh000001", "returns": {}, "as_of": None}
    if as_of:
        sector = sector[sector["date"] <= as_of]
        market = market[market["date"] <= as_of]
    sector = sector[sector["close"] > 0]
    market = market[market["close"] > 0]
    both = sector.merge(market, on="date", suffixes=("_sector", "_market"))
    both = both.sort_values("date").drop_duplicates("date")
    if both.empty:
        return {"status": "no_common_dates", "benchmark": "sh000001", "returns": {}, "as_of": None}
    result = {}
    for period in periods:
        expected = [d for d in (calendar or market["date"].tolist()) if d <= (as_of or both["date"].iloc[-1])][-period - 1:]
        if (len(both) <= period or len(expected) <= period
                or both["date"].tail(period + 1).tolist() != expected
                or as_of and both["date"].iloc[-1] != as_of):
            result[str(period)] = None
            continue
        s0, s1 = _number(both["close_sector"].iloc[-period - 1]), _number(both["close_sector"].iloc[-1])
        m0, m1 = _number(both["close_market"].iloc[-period - 1]), _number(both["close_market"].iloc[-1])
        if not all((s0, s1, m0, m1)) or min(s0, s1, m0, m1) <= 0:
            result[str(period)] = None
            continue
        sector_pct = (s1 / s0 - 1) * 100
        market_pct = (m1 / m0 - 1) * 100
        result[str(period)] = {"sector_pct": round(sector_pct, 2),
                               "benchmark_pct": round(market_pct, 2),
                               "excess_pct": round(sector_pct - market_pct, 2)}
    recent = both.tail(61)
    sbase, mbase = _number(recent["close_sector"].iloc[0]), _number(recent["close_market"].iloc[0])
    trajectory = []
    if sbase and mbase and sbase > 0 and mbase > 0:
        trajectory = [{"date": str(row.date),
                       "sector_pct": round((float(row.close_sector) / sbase - 1) * 100, 2),
                       "benchmark_pct": round((float(row.close_market) / mbase - 1) * 100, 2)}
                      for row in recent.itertuples(index=False)]
    return {"status": "stale_or_unmatched_dates" if as_of and both["date"].iloc[-1] != as_of else
            "ok" if any(v is not None for v in result.values()) else "short_history",
            "benchmark": "sh000001", "returns": result, "as_of": str(both["date"].iloc[-1]),
            "common_sessions": len(both), "trajectory": trajectory}


def member_breadth(member_rows, total_members):
    """当前成分快照的当日截面，成交额占比不等于资金净流入。"""
    active = [r for r in member_rows if (_number(r.get("price")) or 0) > 0
              and (_number(r.get("volume")) or 0) > 0 and _number(r.get("change_pct")) is not None]
    amount_rows = sorted((r for r in active if (_number(r.get("amount")) or 0) > 0),
                         key=lambda r: -_number(r["amount"]))
    total_amount = sum(_number(r["amount"]) for r in amount_rows)
    top3_share = (sum(_number(r["amount"]) for r in amount_rows[:3]) / total_amount
                  if total_amount else None)
    hhi = (sum((_number(r["amount"]) / total_amount) ** 2 for r in amount_rows)
           if total_amount else None)
    changes = sorted(_number(r["change_pct"]) for r in active)
    top3_changes = sorted((_number(r["change_pct"]) for r in amount_rows[:3]))
    n = len(changes)
    return {"total_members": total_members, "spot_matched": len(member_rows), "active_members": n,
            "coverage_ratio": round(len(member_rows) / total_members, 4) if total_members else None,
            "up_ratio": round(sum(v > 0 for v in changes) / n, 4) if n else None,
            "limit_up_ratio": round(sum(_number(r["change_pct"]) >= limit_threshold(str(r["code"]))
                                        for r in active) / n, 4) if n else None,
            "median_change_pct": round(float(pd.Series(changes).median()), 2) if n else None,
            "top3_amount_change_pct": round(float(pd.Series(top3_changes).median()), 2)
                                      if top3_changes else None,
            "top3_amount_share": round(top3_share, 4) if top3_share is not None else None,
            "amount_hhi": round(hhi, 4) if hhi is not None else None,
            "amount_total": total_amount, "basis": "current_snapshot_cross_source"}


def descriptive_state(relative, breadth):
    """仅描述当前截面；不是预测或交易信号。"""
    r5 = (relative.get("returns") or {}).get("5")
    r20 = (relative.get("returns") or {}).get("20")
    up = breadth.get("up_ratio")
    concentration = breadth.get("top3_amount_share")
    if not r5 or not r20 or up is None or breadth.get("status") == "coverage_insufficient":
        return "证据不足"
    if r20["excess_pct"] > 0 and concentration is not None and concentration >= 0.5 and up < 0.5:
        return "明显分化"
    if r20["excess_pct"] > 0 and up >= 0.6:
        return "内部扩散"
    if r5["excess_pct"] > 0 and r20["excess_pct"] <= 0:
        return "启动观察"
    if r5["excess_pct"] < 0 and r20["excess_pct"] > 0:
        return "热度退潮"
    return "持续走强" if r20["excess_pct"] > 0 else "观察"


def leader_roles(member_rows, daily_by_code, sector_return20=None, *, as_of=None, calendar=None,
                 total_members=None):
    """交易核心与价格领涨分开；产业核心没有基本面证据就留空。"""
    active = [r for r in member_rows if (_number(r.get("price")) or 0) > 0]
    by_amount = sorted((r for r in active if (_number(r.get("amount")) or 0) > 0),
                       key=lambda r: -_number(r["amount"]))
    total = sum(_number(r["amount"]) for r in by_amount)
    trading = [{"code": str(r["code"]), "name": str(r.get("name") or ""),
                "amount_share": round(_number(r["amount"]) / total, 4)}
               for r in by_amount[:3]] if total else []
    price = []
    for code, frame in daily_by_code.items():
        if str(code) not in {str(r["code"]) for r in active}:
            continue
        if frame is not None and as_of:
            frame = frame[pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d") <= as_of].copy()
            expected = [d for d in (calendar or []) if d <= as_of][-21:]
            if frame.empty or str(frame["date"].iloc[-1])[:10] != as_of or (expected and
                    pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d").tail(21).tolist() != expected):
                continue
        if frame is None or len(frame) < 21:
            continue
        close = pd.to_numeric(frame["close"], errors="coerce")
        now, old20, old5 = (_number(close.iloc[-1]), _number(close.iloc[-21]),
                            _number(close.iloc[-6]))
        if not all((now, old20, old5)) or min(now, old20, old5) <= 0:
            continue
        ret20 = (now / old20 - 1) * 100
        ret5 = (now / old5 - 1) * 100
        row = next((r for r in active if str(r["code"]) == code), None)
        price.append({"code": code, "name": str(row.get("name") or "") if row else "",
                      "return20_pct": round(ret20, 2), "return5_pct": round(ret5, 2),
                      "excess20_pct": round(ret20 - sector_return20, 2)
                      if sector_return20 is not None else None,
                      "as_of": str(frame["date"].iloc[-1])[:10]})
    price.sort(key=lambda r: (-(r["excess20_pct"] if r["excess20_pct"] is not None
                               else r["return20_pct"]), r["code"]))
    followers = [r["return20_pct"] for r in price[3:]]
    leader_median = float(pd.Series([r["return20_pct"] for r in price[:3]]).median()) if price else None
    follower_median = float(pd.Series(followers).median()) if followers else None
    return {"trading_core": trading, "price_leaders": price[:3],
            "trading_universe_checked": len(by_amount), "trading_universe_expected": total_members or len(member_rows),
            "trading_status": "complete" if len(by_amount) == (total_members or len(member_rows)) else "partial",
            "amount_share_basis": "matched_members_with_available_positive_amount",
            "price_universe_checked": len(daily_by_code),
            "price_universe_valid": len(price), "price_universe_expected": total_members or len(member_rows),
            "price_coverage": len(price) / (total_members or len(member_rows)) if (total_members or len(member_rows)) else None,
            "price_status": "complete" if len(price) == (total_members or len(member_rows)) else "partial",
            "ordinary_member_return20_median": follower_median,
            "leader_ordinary_gap20_pct": leader_median - follower_median
                if leader_median is not None and follower_median is not None else None,
            "industrial_core": [], "industrial_core_status": "fundamentals_unavailable",
            "basis": "observed_members_and_all_available_daily"}


def complete_breadth(member_rows, total_members, daily_by_code, as_of, calendar, verified=True):
    result = member_breadth(member_rows, total_members)
    active_coverage = result["active_members"] / total_members if total_members else 0
    result.update({"minimum_coverage": MIN_COVERAGE, "active_coverage": active_coverage,
                   "status": "ok" if verified and active_coverage >= MIN_COVERAGE else "coverage_insufficient"})
    if result["status"] != "ok":
        for key in ("up_ratio", "limit_up_ratio", "median_change_pct", "top3_amount_change_pct"):
            result[key] = None
    amount_count = sum((_number(r.get("amount")) or 0) > 0 for r in member_rows)
    result["amount_coverage"] = amount_count / total_members if total_members else None
    if not verified or not total_members or amount_count / total_members < MIN_COVERAGE:
        for key in ("top3_amount_share", "amount_hhi", "amount_total"):
            result[key] = None
    counts = {"above_ma20_ratio": 0, "above_ma60_ratio": 0, "new_high60_ratio": 0}
    valid = 0
    expected = [d for d in calendar if d <= as_of][-61:]
    for row in member_rows:
        frame = daily_by_code.get(str(row["code"]))
        if frame is None or len(expected) < 61:
            continue
        frame = frame.copy()
        frame["date"] = pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d")
        frame = frame[frame["date"] <= as_of].tail(61)
        close = pd.to_numeric(frame["close"], errors="coerce")
        if (frame["date"].tolist() != expected or not close.map(lambda x: _number(x) is not None and x > 0).all()):
            continue
        valid += 1
        counts["above_ma20_ratio"] += int(close.iloc[-1] > close.tail(20).mean())
        counts["above_ma60_ratio"] += int(close.iloc[-1] > close.tail(60).mean())
        counts["new_high60_ratio"] += int(close.iloc[-1] > close.iloc[:-1].max())
    covered = valid / total_members if total_members else 0
    result.update({"technical_checked": valid, "technical_expected": total_members,
                   "technical_coverage": covered,
                   "technical_status": "ok" if verified and covered >= MIN_COVERAGE else "coverage_insufficient",
                   **{key: round(value / valid, 4) if verified and covered >= MIN_COVERAGE else None
                      for key, value in counts.items()}})
    return result


def rotation(rows, previous=None, consecutive=False):
    """先计算全集排名，再截断展示；缺失成员证据不参加描述性状态判定。"""
    previous = {r["id"]: r for r in (previous or {}).get("rows", [])}
    for period in (5, 20, 60):
        key = f"relative{period}_pct"
        for row in rows:
            value = (row.get("relative", {}).get("returns", {}).get(str(period)) or {}).get("excess_pct")
            row[key] = value
            row[f"rank{period}"] = None
        valid = sorted((r for r in rows if r[key] is not None), key=lambda r: (-r[key], r["id"]))
        for rank, row in enumerate(valid, 1):
            row[f"rank{period}"] = rank
        for row in rows:
            old = previous.get(row["id"], {}).get(f"rank{period}") if consecutive else None
            row[f"rank{period}_change"] = old - row[f"rank{period}"] if old and row[f"rank{period}"] else None
    for row in rows:
        old = previous.get(row["id"], {}) if consecutive else {}
        breadth = row.get("breadth", {})
        row.update({"up_ratio": breadth.get("up_ratio"), "top3_amount_share": breadth.get("top3_amount_share"),
                    "coverage": breadth.get("coverage_ratio"), "state": descriptive_state(row.get("relative", {}), breadth)})
        old_share = old.get("top3_amount_share")
        row["top3_amount_share_change"] = row["top3_amount_share"] - old_share if old_share is not None and row["top3_amount_share"] is not None else None
        names = {r["code"] for r in row.get("roles", {}).get("price_leaders", [])}
        old_names = {r["code"] for r in old.get("roles", {}).get("price_leaders", [])}
        leadership_ready = ((row.get("roles", {}).get("price_coverage") or 0) >= MIN_COVERAGE
                            and (old.get("roles", {}).get("price_coverage") or 0) >= MIN_COVERAGE)
        row["leader_overlap_previous"] = len(names & old_names) / len(names) if names and old_names and leadership_ready else None
        row["leadership_persistence_sessions"] = ((old.get("leadership_persistence_sessions") or 1) + 1
            if names and names == old_names and leadership_ready else 1 if names
            and (row.get("roles", {}).get("price_coverage") or 0) >= MIN_COVERAGE else None)
        leader_changes = [r.get("return5_pct") for r in row.get("roles", {}).get("price_leaders", [])]
        row["follow_breadth"] = breadth.get("above_ma20_ratio")
        row["leader_return5_median"] = float(pd.Series(leader_changes).median()) if leader_changes else None
    return sorted(rows, key=lambda r: (r["relative20_pct"] is None, -(r["relative20_pct"] or 0), r["id"]))
