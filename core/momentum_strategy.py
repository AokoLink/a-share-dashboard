"""Causal momentum watch candidates; policy defaults, not fitted to a winning list."""
import numpy as np
import pandas as pd

from core import strategies, strategy_risk, sector_relations
from core.analysis import limit_threshold

VERSION = "momentum-watch-v1"
POLICY = {"gain_limit_fraction": .4, "close_location": .75, "volume_ratio": 1.2,
          "sector_up_ratio": .6, "sector_median_pct": 1., "sector_min_members": 5,
          "sector_coverage": .8}


def technical_setup(frame, code):
    """Called only after common price/history/risk eligibility. Uses no future bars."""
    if frame is None or not {"open", "low", "high", "close", "volume"}.issubset(frame.columns):
        return None
    tail = frame.tail(21)
    values = tail[["open", "low", "high", "close", "volume"]].apply(pd.to_numeric, errors="coerce")
    if len(tail) < 21 or not np.isfinite(values.to_numpy()).all():
        return None
    opened, low, high, close, volume = map(float, values.iloc[-1])
    prev = float(values["close"].iloc[-2])
    previous_volume = float(values["volume"].iloc[-6:-1].mean())
    if min(opened, low, high, close, prev, previous_volume) <= 0 or not low <= min(opened, close) <= max(opened, close) <= high:
        return None
    limit = limit_threshold(str(code)[-6:]) + .1
    gain = (close / prev - 1) * 100
    near_limit = gain >= limit - .1
    location = (close - low) / (high - low) if high > low else 1. if near_limit else 0.
    ratio = volume / previous_volume
    ma5 = float(values["close"].tail(5).mean())
    if (gain < limit * POLICY["gain_limit_fraction"] or location < POLICY["close_location"]
            or ratio < POLICY["volume_ratio"] or close < opened or close < ma5):
        return None
    prior5 = prev / float(values["close"].iloc[-7]) - 1
    setup = "repair" if prior5 < 0 or prev < float(values["close"].iloc[:-1].mean()) else "continuation"
    return {"setup": setup, "change_pct": gain, "limit_pct": limit, "close_location": location,
            "day_volume_ratio": ratio, "signal_close": close, "signal_high": high,
            "near_limit_up": near_limit, "prior5_return": prior5}


def sector_context(snapshot, code, day):
    """Use dated industry breadth, never technical winner cohorts or today's mapping for history."""
    evidence = sector_relations.lookup(snapshot, code, day)
    # Native industry/subindustry avoids 'yesterday limit-up' cohorts masquerading as themes.
    relations = evidence["by_category"]["subindustry"] or evidence["by_category"]["industry"]
    rows = {r["id"]: r for r in (snapshot or {}).get("rows", [])}
    verified = []
    for rel in relations:
        row = rows.get(rel["id"], {})
        breadth = row.get("breadth", {})
        up, median = strategies._finite(breadth.get("up_ratio")), strategies._finite(breadth.get("median_change_pct"))
        coverage = strategies._finite(breadth.get("active_coverage"))
        count = strategies._finite(breadth.get("active_members"))
        if (row.get("as_of") != day or breadth.get("status") != "ok" or up is None or median is None
                or coverage is None or coverage < POLICY["sector_coverage"]
                or count is None or count < POLICY["sector_min_members"]):
            continue
        verified.append({"id": rel["id"], "name": rel["name"], "up_ratio": up,
                         "median_change_pct": median, "active_coverage": coverage, "active_members": count})
    strongest = max(verified, key=lambda r: (r["median_change_pct"], r["up_ratio"], r["id"]), default=None)
    status = "unknown" if not strongest else "confirmed" if (
        strongest["up_ratio"] >= POLICY["sector_up_ratio"] and strongest["median_change_pct"] >= POLICY["sector_median_pct"]
    ) else "not_hot"
    return {"status": status, "as_of": day, "basis": "dated_industry_breadth_not_news_or_fundamentals",
            "selected": strongest, "checked": verified, "snapshot_sha256": (snapshot or {}).get("snapshot_sha256")}


def screen(frame, code, name, day, snapshot=None):
    ok, reason, caveats = strategies.base_eligibility(frame, code, name, allow_limit_up=True)
    if not ok:
        return None
    metrics = technical_setup(frame, code)
    if not metrics:
        return None
    context = sector_context(snapshot, code, day)
    if context["status"] == "not_hot":
        return None
    risk = strategy_risk.assess(frame, code)
    raw = (40 + min(20, metrics["change_pct"] / metrics["limit_pct"] * 20)
           + metrics["close_location"] * 10 + min(10, (metrics["day_volume_ratio"] - 1) * 10)
           + (10 if context["status"] == "confirmed" else 0))
    reasons = ["修复转强" if metrics["setup"] == "repair" else "强势延续观察",
               "当日放量且收盘接近日内高点", "收盘不低于5日均线"]
    if metrics["near_limit_up"]:
        reasons.append("当日接近涨停：可观察，次日成交另核实")
    if context["status"] == "confirmed":
        reasons.append("同日行业上涨广度与中位涨幅共振")
    else:
        caveats = caveats + ["momentum_sector_unverified"]
        reasons.append("仅价格量能入选，行业热度待核实")
    return {"strategy": "momentum", "version": strategies.VERSION, "watch_version": VERSION,
            "score": round(max(0, min(100, raw - risk["penalty"])), 2), "raw_score": round(raw, 2),
            "score_basis": "technical_match_not_probability", "risk": risk, "risk_penalty": risk["penalty"],
            "signal_date": day, "entry": "next_session_open", "horizon_sessions": strategies.HORIZONS["momentum"],
            "candidate_kind": "observation", "actionable": False, "entry_review_required": True,
            "announcement_status": "unverified", "evidence": "exploratory", "variant": "base",
            "reasons": reasons, "caveats": caveats + risk["caveats"] + ["announcement_review_required", "news_fundamentals_lhb_not_scored"],
            "momentum_metrics": metrics, "momentum_sector": context,
            "sector_condition": "applied" if context["status"] == "confirmed" else "unverified"}
