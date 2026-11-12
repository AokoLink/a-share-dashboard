"""Verified market observation windows, independent of strategy price readiness."""
from pathlib import Path

import numpy as np

from core import daily_data as data


def market_history_readiness(root, day, calendar):
    """Read-only calendar coverage; absent/invalid observations never become zero returns."""
    sessions = sorted(set(d for d in (calendar or {}).get("dates", []) if d <= day))
    expected = sessions[-61:]
    history, invalid, unverified = {}, [], []
    fields = ("r1", "up_ratio", "limit_up", "limit_down", "turnover")
    for path in sorted((Path(root) / "market_daily").glob("*.json")):
        if path.stem > day or path.stem not in sessions:
            continue
        try:
            row = data.read_json(path)
            values = [float(row[key]) for key in fields]
            if (row.get("date") != path.stem or not np.isfinite(values).all()
                    or values[0] <= -1 or not 0 <= values[1] <= 1
                    or any(value < 0 for value in values[2:])
                    or any(value != int(value) for value in values[2:4])):
                raise ValueError("invalid_market_observation")
            proof = row.get("observation_artifact")
            if proof:
                source = row.get("source_artifact", {})
                if source.get("as_of") != path.stem or not data.intact(source) or not data.intact(proof):
                    raise ValueError("market_observation_hash_mismatch")
                archived = data.read_json(proof["path"])
                if any(archived.get(key) != row.get(key) for key in ("date", *fields, "source_artifact")):
                    raise ValueError("market_observation_content_mismatch")
            else:
                unverified.append(path.stem)
            history[path.stem] = row
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            invalid.append(path.stem)
    missing = [d for d in expected if d not in history]
    consecutive = 0
    for d in reversed(sessions):
        if d not in history:
            break
        consecutive += 1
    unknown_window = [d for d in expected if d in unverified]
    invalid_window = [d for d in expected if d in invalid]
    internal_gaps = [d for d in missing if history and d >= min(history)]
    state = ("calendar_unavailable" if not calendar or day not in sessions else
             "invalid_history" if invalid_window else "history_gaps" if internal_gaps else
             "warming_up" if len(expected) < 61 or missing else
             "unverified_history" if unknown_window else "ready")
    readiness = {"status": state, "required_sessions": 61, "observed_sessions": len(history),
        "contiguous_sessions": consecutive, "window_available_sessions": len(expected) - len(missing),
        "window_verified_sessions": len(expected) - len(missing) - len(unknown_window),
        "window_start": expected[0] if expected else None, "window_end": day,
        "missing_dates": missing, "invalid_dates": invalid_window,
        "unverified_source_dates": unknown_window, "ready": state == "ready"}
    return readiness, [history[d] for d in expected if d in history]

