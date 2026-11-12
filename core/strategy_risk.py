"""Causal technical risk limits. Policy defaults, not fitted win probabilities."""
import math
import numpy as np
import pandas as pd
from core.analysis import limit_threshold

VERSION = "technical-risk-v1"
POLICY = {"return5": .25, "return10": .40, "bias20": .20,
          "near_limit_days10": 3, "turnover": .25, "volume_ratio": 4.}
LABELS = {"rapid_gain_5d": "5日涨幅过大", "rapid_gain_10d": "10日涨幅过大",
          "extended_ma20": "严重偏离20日均线", "repeated_limit_moves": "近期频繁接近涨停",
          "extreme_turnover": "换手率过高", "extreme_volume": "近期量能异常放大",
          "risk_inputs_unavailable": "风控输入不足或异常", "turnover_unverified": "换手率未核实"}


def assess(frame, code):
    """Input ends on the decision day. Turnover is a ratio, not percentage points."""
    empty = {"version": VERSION, "policy": POLICY, "status": "unavailable", "blocked": True,
             "reasons": ["risk_inputs_unavailable"], "caveats": [], "metrics": {}, "penalty": 0.}
    if frame is None or len(frame) < 25 or not {"close", "volume"}.issubset(frame.columns):
        return empty
    close = pd.to_numeric(frame["close"].tail(25), errors="coerce").to_numpy(dtype=float)
    volume = pd.to_numeric(frame["volume"].tail(25), errors="coerce").to_numpy(dtype=float)
    if not np.isfinite(close).all() or (close <= 0).any() or not np.isfinite(volume).all() or (volume < 0).any() or volume[:20].mean() <= 0:
        return empty
    code = str(code)[-6:]
    scale = round((limit_threshold(code) + .1) / 10)
    try:
        turnover = float(frame.iloc[-1].get("turnover"))
        if not math.isfinite(turnover) or turnover < 0:
            turnover = None
    except (TypeError, ValueError):
        turnover = None
    metrics = {"return5": float(close[-1] / close[-6] - 1),
               "return10": float(close[-1] / close[-11] - 1),
               "bias20": float(close[-1] / close[-20:].mean() - 1),
               "near_limit_days10": int(((close[-10:] / close[-11:-1] - 1) * 100 >= limit_threshold(code)).sum()),
               "turnover": turnover, "volume_ratio": float(volume[-5:].mean() / volume[:20].mean()),
               "board_scale": scale}
    rules = [("return5", POLICY["return5"] * scale, "rapid_gain_5d"),
             ("return10", POLICY["return10"] * scale, "rapid_gain_10d"),
             ("bias20", POLICY["bias20"] * scale, "extended_ma20"),
             ("near_limit_days10", POLICY["near_limit_days10"], "repeated_limit_moves"),
             ("turnover", POLICY["turnover"], "extreme_turnover"),
             ("volume_ratio", POLICY["volume_ratio"], "extreme_volume")]
    reasons = [r for key, threshold, r in rules if metrics[key] is not None and metrics[key] >= threshold - 1e-12]
    penalty = min(25., max(0., metrics["bias20"] / scale - .08) * 100
                  + max(0., metrics["return10"] / scale - .20) * 30
                  + max(0., (turnover or 0) - .10) * 40
                  + max(0., metrics["volume_ratio"] - 2) * 3)
    return {"version": VERSION, "policy": POLICY, "status": "blocked" if reasons else "checked",
            "blocked": bool(reasons), "reasons": reasons,
            "caveats": [] if turnover is not None else ["turnover_unverified"],
            "metrics": metrics, "penalty": round(penalty, 4)}


def describe(risk):
    return [LABELS.get(key, key) for key in risk.get("reasons", []) + risk.get("caveats", [])]
