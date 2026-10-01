# -*- coding: utf-8 -*-
"""离线研究产物的新鲜度；优先使用有明确覆盖边界的真实交易日历。"""
from datetime import date, datetime, timedelta

from core.stage1_data import load_calendar


def _date(value):
    if not value:
        return None
    try:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def assess(as_of, now=None, max_sessions=2, calendar=None):
    """返回历史/当前状态与跨度；日历未覆盖当日时状态为 unknown。"""
    today = _date(now) or date.today()
    day = _date(as_of)
    stored = load_calendar() if calendar is None else None
    basis = "provided" if calendar is not None else "akshare_sina" if stored else "weekday_proxy"
    if stored:
        calendar = stored["dates"]
    if day is None or day > today:
        return {"status": "unknown", "as_of": as_of, "sessions_behind": None,
                "calendar_basis": basis}
    if stored and not (stored["first"] <= day.isoformat() <= today.isoformat() <= stored["last"]):
        return {"status": "unknown", "as_of": day.isoformat(), "sessions_behind": None,
                "calendar_basis": basis}
    if calendar is not None:
        dates = [_date(raw) for raw in calendar]
        sessions = sum(day < d <= today for d in dates if d is not None)
    else:
        sessions = sum((day + timedelta(days=k)).weekday() < 5
                       for k in range(1, (today - day).days + 1))
        basis = "weekday_proxy"
    return {"status": "historical" if sessions > max_sessions else "current",
            "as_of": day.isoformat(), "sessions_behind": sessions,
            "calendar_basis": basis}


def mark_regime(regime, now=None, calendar=None):
    if regime is None:
        return None
    result = dict(regime)
    result["freshness"] = assess(result.get("as_of"), now=now, calendar=calendar)
    if result["freshness"]["status"] != "current":
        result["advice"] = None
        result["swing"] = None
    return result
