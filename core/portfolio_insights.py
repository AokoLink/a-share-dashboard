# -*- coding: utf-8 -*-
"""自选组合的描述性集中风险；不产生目标仓位。"""
from collections import defaultdict

import numpy as np
import pandas as pd

VERSION = "stage3-portfolio-v1"


def analyze(holdings, daily_by_code, sectors_by_code=None, min_sessions=20):
    """holdings=[{code, weight}]，权重为账户净值小数；现金=1-总权重。"""
    sectors_by_code = sectors_by_code or {}
    if not holdings:
        raise ValueError("empty holdings")
    codes = [str(item["code"]) for item in holdings]
    if len(set(codes)) != len(codes) or any(code not in daily_by_code for code in codes):
        raise ValueError("duplicate or missing holding data")
    weights = np.asarray([float(item["weight"]) for item in holdings], dtype=float)
    if not np.isfinite(weights).all() or (weights < 0).any() or weights.sum() > 1 + 1e-9:
        raise ValueError("weights must be finite, nonnegative, and sum to at most 1")
    cash = max(0.0, 1.0 - float(weights.sum()))
    exposure = defaultdict(float)
    unknown = []
    for code, weight in zip(codes, weights):
        sectors = list(dict.fromkeys(sectors_by_code.get(code) or []))
        if not sectors:
            unknown.append(code)
        for sector in sectors:
            exposure[sector] += float(weight)
    prices, last_dates, amounts = {}, {}, {}
    for code in codes:
        data = daily_by_code[code]
        if data is None or len(data) == 0:
            continue
        frame = data[["date", "close"]].copy()
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        frame = frame.dropna().sort_values("date").drop_duplicates("date", keep="last")
        frame = frame[frame["close"] > 0].set_index("date")
        if frame.empty:
            continue
        prices[code] = frame["close"]
        last_dates[code] = str(frame.index[-1].date())
        if "amount" in data:
            amount = pd.to_numeric(data["amount"], errors="coerce").iloc[-1]
            amounts[code] = float(amount) if pd.notna(amount) and amount >= 0 else None
    max_weight = float(max(weights)) if len(weights) else 0.0
    result = {"holdings": len(codes), "invested_weight": round(float(weights.sum()), 6),
              "cash_weight": round(cash, 6), "weight_assumption": "provided_by_user",
              "max_stock_weight": round(max_weight, 6),
              "top3_stock_weight": round(float(sum(sorted(weights, reverse=True)[:3])), 6),
              "weight_hhi": round(float(np.sum(weights ** 2)), 6),
              "sector_exposure_nonexclusive": {k: round(v, 6) for k, v in sorted(exposure.items(), key=lambda x: -x[1])},
              "unknown_sector_codes": unknown,
              "multi_sector_codes": [code for code in codes if len(sectors_by_code.get(code) or []) > 1],
              "data_as_of": min(last_dates.values()) if len(last_dates) == len(codes) else None,
              "per_code_as_of": last_dates,
              "amount_coverage": sum(amounts.get(code) is not None for code in codes),
              "low_liquidity_weight": round(sum(float(weight) for code, weight in zip(codes, weights)
                                                if amounts.get(code) is not None and amounts[code] < 1e8), 6)
              if amounts else None,
              "risk_status": "insufficient_history", "common_return_sessions": 0,
              "correlation_pairs": [], "correlation_clusters": [], "risk_contribution": {},
              "price_basis": "daily_adjustment_unverified"}
    if len(prices) != len(codes):
        return result
    panel = pd.concat([prices[code].rename(code) for code in codes], axis=1, join="inner")
    returns = panel.pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan).dropna().tail(60)
    result["common_return_sessions"] = len(returns)
    if len(returns) < min_sessions:
        return result
    covariance = returns.cov().to_numpy(dtype=float)
    variance = float(weights @ covariance @ weights)
    if not np.isfinite(variance) or variance <= 0:
        return result
    marginal = covariance @ weights
    contribution = weights * marginal / variance
    result["risk_status"] = "descriptive_only"
    result["daily_volatility"] = round(float(np.sqrt(variance)), 6)
    result["risk_contribution"] = {code: round(float(value), 6)
                                   for code, value in zip(codes, contribution)}
    correlations = returns.corr()
    pairs = []
    adjacency = {code: set() for code in codes}
    for i, left in enumerate(codes):
        for right in codes[i + 1:]:
            corr = float(correlations.loc[left, right])
            if not np.isfinite(corr):
                continue
            pairs.append({"left": left, "right": right, "correlation": round(corr, 4)})
            if corr >= 0.7:
                adjacency[left].add(right)
                adjacency[right].add(left)
    result["correlation_pairs"] = sorted(pairs, key=lambda x: -x["correlation"])[:10]
    seen, clusters = set(), []
    for code in codes:
        if code in seen:
            continue
        queue, group = [code], []
        while queue:
            node = queue.pop()
            if node in seen:
                continue
            seen.add(node)
            group.append(node)
            queue.extend(adjacency[node] - seen)
        if len(group) > 1:
            clusters.append(sorted(group))
    result["correlation_clusters"] = clusters
    return result
