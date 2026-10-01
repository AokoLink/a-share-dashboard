"""Deterministic, long-only cash/share simulation. Never submits real orders."""
import copy
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import time

from core import strategies, strategy_lifecycle as life

VERSION = "cash-shares-ledger-v2"
DEFAULTS = {
    "initial_cash": 1_000_000., "mode": "research_proxy", "lot_size": 100,
    "cash_reserve": .1, "max_stock_weight": .1,
    "strategy_budgets": {"breakout": .3, "pullback": .3, "rebound": .3},
    "max_industry_weight": .4, "max_theme_weight": .3,
    "participation": .01, "fee": life.FEE_PER_SIDE,
    "slippage": life.SLIPPAGE_PER_SIDE, "max_entry_gap": .05,
}
UNKNOWN_ENTRY_REASONS = {
    "unverified_execution_inputs", "industry_theme_budget_unverified", "existing_holding_sector_budget_unknown",
    "invalid_execution_capacity", "signal_raw_price_missing_or_mismatch", "missing_entry_open_not_inferred_halt",
    "account_valuation_incomplete", "missing_previous_liquidity",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def config(values=None):
    values = values or {}
    if set(values) - set(DEFAULTS):
        raise ValueError("unknown_account_config_fields")
    out = {**copy.deepcopy(DEFAULTS), **values}
    if out["mode"] not in ("research_proxy", "verified_inputs"):
        raise ValueError("unknown_execution_mode")
    if not isinstance(out["lot_size"], int) or isinstance(out["lot_size"], bool) or out["lot_size"] < 1:
        raise ValueError("invalid_lot_size")
    if (isinstance(out["initial_cash"], bool) or not isinstance(out["initial_cash"], (int, float))
        or not life._finite(out["initial_cash"])):
        raise ValueError("invalid_initial_cash")
    for key in ("cash_reserve", "max_stock_weight", "max_industry_weight", "max_theme_weight",
                "participation", "fee", "slippage", "max_entry_gap"):
        v = out[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v < 1:
            raise ValueError("invalid_" + key)
    if out["participation"] <= 0 or out["max_stock_weight"] <= 0:
        raise ValueError("zero_risk_or_liquidity_budget")
    budgets = out["strategy_budgets"]
    if not isinstance(budgets, dict) or not budgets or any(k not in strategies.HORIZONS for k in budgets):
        raise ValueError("unknown_strategy_budget")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1
           for v in budgets.values()) or sum(budgets.values()) > 1 - out["cash_reserve"] + 1e-10:
        raise ValueError("strategy_budgets_exceed_available_capital")
    return out


def _known(row, cutoff):
    try:
        return bool(row.get("verified") and row.get("source") and row.get("known_at")
                    and life.observation_clock(row["known_at"]) <= cutoff
                    and (not row.get("received_at") or life.observation_clock(row["received_at"]) <= cutoff))
    except (ValueError, TypeError):
        return False


def _quote(bars, code, day, field):
    return life._finite(bars.get(code, {}).get(day, {}).get(field))


def _relations(links, code, day):
    row = links.get(day, {}).get(code, {})
    return row.get("relations", []) if row.get("status") == "ok" and row.get("as_of") == day else []


def _complete_relations(links, code, day):
    row = links.get(day, {}).get(code, {})
    return (row.get("status") == "ok" and row.get("as_of") == day
            and {"industry", "theme"}.issubset(row.get("categories_complete", [])))


def _snapshot_orders(snapshots, calendar, clock, budgets):
    """First snapshot per day/strategy wins, including across versions; revisions never double-buy."""
    first, rejected = {}, []
    for row in sorted(snapshots, key=lambda r: (life.observation_clock(r["frozen_at"]), r["id"])):
        frozen = life.observation_clock(row["frozen_at"])
        if frozen > clock or row["signal_date"] > clock.date().isoformat():
            continue
        key = (row["signal_date"], row["strategy"])
        if row["strategy"] not in budgets:
            continue
        if frozen < life.observation_clock(row["signal_date"] + "T15:00:00+08:00"):
            rejected.append({"snapshot_id": row["id"], "reason": "frozen_before_signal_close"})
            continue
        if key in first:
            rejected.append({"snapshot_id": row["id"], "reason": "later_revision_or_version_not_double_funded"})
        else:
            first[key] = row
    orders = []
    for row in first.values():
        day, payload = row["signal_date"], row["payload"]
        horizon = payload.get("horizon_sessions")
        if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 1:
            rejected.append({"snapshot_id": row["id"], "reason": "invalid_holding_period"})
            continue
        if day not in calendar:
            rejected.append({"snapshot_id": row["id"], "reason": "signal_day_not_in_calendar"})
            continue
        idx = calendar.index(day)
        entry = calendar[idx + 1] if idx + 1 < len(calendar) else None
        pool = {str(r["code"])[-6:]: r for r in payload.get("pool", [])}
        candidates = payload.get("candidates", [])
        seen = set()
        for rank, item in enumerate(candidates):
            code = str(item["code"])[-6:]
            if len(code) != 6 or not code.isdigit() or code in seen:
                raise ValueError("duplicate_or_invalid_candidate")
            seen.add(code)
            orders.append({"id": f"{row['id']}:{code}", "snapshot_id": row["id"],
                "signal_date": day, "entry_day": entry, "exit_index": idx + horizon + 1,
                "strategy": row["strategy"], "version": row["version"], "code": code, "rank": rank,
                "target_weight": budgets[row["strategy"]] / len(candidates),
                "signal_close": item.get("signal_close", pool.get(code, {}).get("signal_close")),
                "frozen_at": row["frozen_at"], "status": "waiting_entry", "filled_shares": 0})
    return sorted(orders, key=lambda o: (o["signal_date"], o["strategy"], o["rank"], o["code"])), rejected


def replay(snapshots, bars, calendar, *, as_of, settings=None, bases=None, trade_status=None,
           corporate_actions=None, corporate_coverage=None, relations=None, benchmark=None,
           sector_returns=None, status_events=None, execution_rules=None):
    """Inputs are dated: bars[code][day], trade_status[day][code], coverage[day][code].

    Verified open evidence: verified/source/known_at, halted, buy_open_allowed,
    sell_open_allowed; optional max_buy_shares/max_sell_shares. Company events:
    id/code/ex_date/pay_date/net_cash_per_share/share_factor/verified/source/known_at.
    Returns a daily reconciled ledger; missing closes yield null NAV, never a fake exit.
    """
    cfg = config(settings)
    from core import execution_evidence as execution
    execution_rules = execution_rules or {}
    clock = life.observation_clock(as_of)
    if calendar != sorted(set(calendar)):
        raise ValueError("calendar_must_be_unique_sorted")
    for day in calendar:
        life.observation_clock(day)
    bases, trade_status, corporate_coverage = bases or {}, trade_status or {}, corporate_coverage or {}
    relations, benchmark, sector_returns = relations or {}, benchmark or {}, sector_returns or {}
    status_events = status_events or []
    orders, rejected = _snapshot_orders(snapshots, calendar, clock, cfg["strategy_budgets"])
    visible = [s for s in snapshots if s["signal_date"] <= clock.date().isoformat()
               and life.observation_clock(s["frozen_at"]) <= clock]
    start = min([s["signal_date"] for s in visible], default=clock.date().isoformat())
    days = [d for d in calendar if start <= d <= clock.date().isoformat()]
    events, lots, daily, receivables = [], [], [], []
    lot_by_id = {}
    cash, initial = float(cfg["initial_cash"]), float(cfg["initial_cash"])
    last_prices, total_fees, total_slippage = {}, 0., 0.
    previous_nav, previous_values, previous_cashlike = initial, {}, initial
    performance_uncertain = False
    processed_actions = set()
    unresolved_actions = set()
    action_ids = [e.get("id") for e in corporate_actions or []]
    if None in action_ids or len(action_ids) != len(set(action_ids)):
        raise ValueError("corporate_action_ids_must_be_unique")
    strict = cfg["mode"] == "verified_inputs"

    def emit(kind, day, **fields):
        events.append({"sequence": len(events) + 1, "date": day, "type": kind, **fields})

    def evidence(code, day, cutoff):
        reasons = []
        if bases.get(code) != "verified_unadjusted_raw":
            reasons.append("unverified_unadjusted_price")
        coverage = corporate_coverage.get(day, {}).get(code, {})
        if not _known(coverage, cutoff) or not coverage.get("complete"):
            reasons.append("corporate_action_coverage_unknown")
        return reasons

    def gate(code, day, side, cutoff):
        row = trade_status.get(day, {}).get(code, {})
        if _known(row, cutoff):
            if row.get("halted") is True:
                return "verified_halt", [], 0
            if row.get(side + "_open_allowed") is False:
                return "verified_open_execution_blocked", [], 0
        unknown = evidence(code, day, cutoff)
        if not _known(execution_rules.get(day, {}).get(code, {}), cutoff):
            unknown.append("order_quantity_and_fee_rules_unknown")
        if not _known(row, cutoff) or row.get("halted") is not False or row.get(side + "_open_allowed") is not True:
            unknown.append("open_execution_status_unknown")
        cap = row.get("max_" + side + "_shares") if _known(row, cutoff) else None
        if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or cap < 0):
            return "invalid_execution_capacity", unknown, 0
        return "unverified_execution_inputs" if strict and unknown else None, unknown, cap

    def rule_for(code, day, cutoff):
        rule = execution_rules.get(day, {}).get(code, {})
        if _known(rule, cutoff):
            # Direct injected replay must obey the same numeric schema as imports.
            for key in ("buy_min", "buy_step", "sell_min", "sell_step"):
                if type(rule.get(key)) is not int or rule[key] < 1:
                    raise ValueError("invalid_order_quantity_rule")
            for key in ("commission_rate", "minimum_commission", "sell_tax_rate", "transfer_rate"):
                if type(rule.get(key)) not in (int, float) or not math.isfinite(rule[key]) or rule[key] < 0:
                    raise ValueError("invalid_fee_rule")
                if key != "minimum_commission" and rule[key] >= 1:
                    raise ValueError("invalid_fee_rate")
            if type(rule.get("odd_lot_sell_all")) is not bool:
                raise ValueError("odd_lot_rule_unknown")
            return rule
        return {"buy_min": cfg["lot_size"], "buy_step": cfg["lot_size"],
                "sell_min": cfg["lot_size"], "sell_step": cfg["lot_size"], "odd_lot_sell_all": True,
                "commission_rate": cfg["fee"], "minimum_commission": 0.,
                "sell_tax_rate": 0., "transfer_rate": 0., "basis": "research_proxy_uniform_lot"}

    def share_values(prices):
        return {lot["id"]: lot["shares"] * prices[lot["code"]] for lot in lots if lot["shares"] > 0}

    for day in days:
        index = calendar.index(day)
        prev_day = calendar[index - 1] if index else None
        open_clock = life.observation_clock(day + "T09:30:00+08:00")
        close_clock = life.observation_clock(day + "T15:00:00+08:00")
        if clock < open_clock:
            break  # partial current session cannot masquerade as a closing valuation
        before_cost = total_fees + total_slippage
        buys, sells = 0., 0.
        issues, blocked_marks = [], set(unresolved_actions)
        issues.extend({"code": c, "reason": "unresolved_corporate_action"} for c in unresolved_actions)
        liquidity_used = defaultdict(float)
        side_used = defaultdict(int)
        # Corporate rights belong to shares held before this day's opening trades.
        for event in sorted(corporate_actions or [], key=lambda e: (e.get("ex_date", ""), str(e["id"]))):
            if event.get("ex_date") != day or event["id"] in processed_actions:
                continue
            held = [l for l in lots if l["code"] == event.get("code") and l["shares"] > 0]
            if not held:
                continue
            if not _known(event, open_clock):
                issues.append({"code": event.get("code"), "reason": "unverified_corporate_action"})
                blocked_marks.add(event.get("code"))
                unresolved_actions.add(event.get("code"))
                emit("corporate_action_blocked", day, action_id=event["id"], code=event.get("code"))
                continue
            factor = event.get("share_factor", 1.)
            dividend = event.get("net_cash_per_share", 0.)
            pay_day = event.get("pay_date", day)
            if (not life._finite(factor) or not isinstance(dividend, (int, float)) or not math.isfinite(dividend)
                or dividend < 0 or pay_day < day or any(abs(l["shares"] * factor - round(l["shares"] * factor)) > 1e-8 for l in held)
                or event.get("type", "cash_and_split") != "cash_and_split"):
                issues.append({"code": event.get("code"), "reason": "unsupported_corporate_action"})
                blocked_marks.add(event.get("code"))
                unresolved_actions.add(event.get("code"))
                emit("corporate_action_blocked", day, action_id=event["id"], code=event.get("code"))
                continue
            for lot in held:
                old = lot["shares"]
                lot["shares"] = int(round(old * factor))
                amount = old * dividend
                lot["dividends"] += amount
                if amount:
                    receivables.append({"action_id": event["id"], "lot_id": lot["id"], "amount": amount,
                                        "pay_date": pay_day, "paid": False})
                emit("corporate_action", day, action_id=event["id"], lot_id=lot["id"], code=lot["code"],
                     old_shares=old, shares=lot["shares"], dividend_receivable=amount, pay_date=pay_day,
                     source=event["source"])
            processed_actions.add(event["id"])
        for r in receivables:
            if not r["paid"] and r["pay_date"] <= day:
                cash += r["amount"]
                r["paid"] = True
                emit("dividend_payment", day, action_id=r["action_id"], lot_id=r["lot_id"], amount=r["amount"])

        def capacity(code, price, side, cap):
            # Previous session amount only: today's final volume is unknown at the open.
            amount = _quote(bars, code, prev_day, "amount") if prev_day else None
            if amount is None:
                return 0, "missing_previous_liquidity"
            remaining = max(0., amount * cfg["participation"] - liquidity_used[code])
            qty = int(math.floor(remaining / price + 1e-8))
            return min(qty, max(0, cap - side_used[(code, side)])) if cap is not None else qty, None

        # Exit first, reuse only actual proceeds. Failed/partial exits remain exposed.
        for lot in lots:
            if not lot["shares"] or index < lot["exit_index"] or day <= lot["entry_day"]:
                continue
            failure, unknown, cap = gate(lot["code"], day, "sell", open_clock)
            price = _quote(bars, lot["code"], day, "open")
            if lot["code"] in blocked_marks:
                failure = "corporate_action_unresolved"
            if not failure and not price:
                failure = "missing_exit_open_not_inferred_halt"
            qty, reason = capacity(lot["code"], price, "sell", cap) if not failure else (0, None)
            if failure or reason or qty == 0:
                lot["status"] = "pending_exit"
                emit("exit_deferred", day, lot_id=lot["id"], code=lot["code"],
                     reason=failure or reason or "liquidity_capacity_zero", shares=lot["shares"], evidence_limits=unknown)
                issues.extend({"code": lot["code"], "reason": r} for r in unknown)
                continue
            qty = min(qty, lot["shares"])
            rule = rule_for(lot["code"], day, open_clock)
            if qty < lot["shares"] or not rule["odd_lot_sell_all"]:
                qty = execution.quantity(qty, rule["sell_min"], rule["sell_step"])
            if not qty:
                lot["status"] = "pending_exit"
                emit("exit_deferred", day, lot_id=lot["id"], code=lot["code"],
                     reason="sell_lot_capacity_zero", shares=lot["shares"])
                continue
            gross = qty * price
            slip = gross * cfg["slippage"]
            fee, components = execution.charges(gross - slip, "sell", rule)
            cash += gross - slip - fee
            lot["shares"] -= qty
            lot["sales_gross"] += gross
            lot["costs"] += slip + fee
            lot["status"] = "exited" if not lot["shares"] else "pending_exit"
            total_fees += fee
            total_slippage += slip
            sells += gross - slip
            liquidity_used[lot["code"]] += gross
            side_used[(lot["code"], "sell")] += qty
            emit("sell_fill", day, lot_id=lot["id"], code=lot["code"], shares=qty, price=price,
                 reference_open=price, execution_price=price * (1 - cfg["slippage"]),
                 execution_notional=gross - slip, gross=gross, fee=fee, slippage=slip, cash_after=cash,
                 remaining_shares=lot["shares"], evidence_limits=unknown, fee_components=components)
            issues.extend({"code": lot["code"], "reason": r} for r in unknown)

        active_codes = {l["code"] for l in lots if l["shares"]}
        opening = {c: _quote(bars, c, day, "open") for c in active_codes}
        opening_complete = all(opening.values()) and not blocked_marks
        receivable_value = sum(r["amount"] for r in receivables if not r["paid"])
        open_values = share_values(opening) if opening_complete else {}
        open_nav = cash + sum(open_values.values()) + receivable_value if opening_complete else None
        # Fix all target notionals to the preceding complete NAV. Do not recycle a cancelled slot.
        budget_nav = previous_nav
        for order in orders:
            if order["entry_day"] != day:
                continue
            code, strategy = order["code"], order["strategy"]
            target = budget_nav * order["target_weight"] if budget_nav is not None else None
            emit("entry_order", day, order_id=order["id"], code=code, strategy=strategy,
                 target_weight=order["target_weight"], target_notional=target, snapshot_id=order["snapshot_id"])
            failure = None
            if life.observation_clock(order["frozen_at"]) >= open_clock:
                failure = "frozen_after_entry_open"
            snapshot = {"strategy": strategy, "version": order["version"]}
            if life.status_at_entry(snapshot, day, status_events, as_of=clock) in ("paused", "retired"):
                failure = "strategy_paused_or_retired"
            evidence_failure, unknown, cap = gate(code, day, "buy", open_clock)
            failure = failure or evidence_failure
            price = _quote(bars, code, day, "open")
            observed_close = _quote(bars, code, order["signal_date"], "close")
            frozen_close = life._finite(order["signal_close"])
            if not failure and (not observed_close or not frozen_close or abs(observed_close / frozen_close - 1) > .005):
                failure = "signal_raw_price_missing_or_mismatch"
            if not failure and not price:
                failure = "missing_entry_open_not_inferred_halt"
            if not failure and price / frozen_close - 1 > cfg["max_entry_gap"]:
                failure = "entry_gap_above_limit"
            if not failure and (not opening_complete or target is None):
                failure = "account_valuation_incomplete"
            rels = _relations(relations, code, prev_day) if prev_day else []
            if not _complete_relations(relations, code, prev_day):
                unknown.append("industry_theme_budget_unverified")
                if strict:
                    failure = failure or "industry_theme_budget_unverified"
            if strict and any(not _complete_relations(relations, c, prev_day) for c in active_codes):
                failure = failure or "existing_holding_sector_budget_unknown"
            if failure:
                order.update(status="cancelled", reason=failure)
                emit("entry_cancelled", day, order_id=order["id"], code=code, reason=failure, evidence_limits=unknown)
                issues.extend({"code": code, "reason": r} for r in unknown)
                continue
            current_stock = sum(v for lid, v in open_values.items() if lot_by_id[lid]["code"] == code)
            current_strategy = sum(v for lid, v in open_values.items() if lot_by_id[lid]["strategy"] == strategy)
            rule = rule_for(code, day, open_clock)
            constraints = [(cfg["max_stock_weight"], current_stock),
                           (cfg["strategy_budgets"][strategy], current_strategy)]
            cost_fraction = (1 + cfg["slippage"]) * (1 + rule["commission_rate"] + rule["transfer_rate"]) - 1
            def budget(limit, used):
                return max(0., open_nav * limit - used) / (1 + limit * cost_fraction)
            available = min(target, budget(cfg["max_stock_weight"], current_stock),
                            budget(cfg["strategy_budgets"][strategy], current_strategy))
            for rel in rels:
                kind = rel.get("category")
                limit = cfg["max_industry_weight"] if kind in ("industry", "subindustry") else cfg["max_theme_weight"]
                used = sum(v for lid, v in open_values.items()
                           if any(r["id"] == rel["id"] for r in _relations(relations,
                               lot_by_id[lid]["code"], prev_day)))
                available = min(available, budget(limit, used))
                constraints.append((limit, used))
            unit_cost = price * (1 + cfg["slippage"]) * (1 + rule["commission_rate"] + rule["transfer_rate"])
            cash_available = max(0., cash - open_nav * cfg["cash_reserve"])
            qty = execution.quantity(math.floor(min(available / price, cash_available / unit_cost) + 1e-8),
                                     rule["buy_min"], rule["buy_step"])
            cap_qty, reason = capacity(code, price, "buy", cap)
            qty = execution.quantity(min(qty, cap_qty), rule["buy_min"], rule["buy_step"])
            # Minimum commission reduces NAV too. Solve actual cash/risk constraints
            # rather than allowing a fixed minimum to push a full-budget buy over cap.
            def affordable(q):
                gross = q * price
                slip = gross * cfg["slippage"]
                fees, _ = execution.charges(gross + slip, "buy", rule)
                nav_after = open_nav - slip - fees
                return (cash - gross - slip - fees >= nav_after * cfg["cash_reserve"] - 1e-7
                        and all(used + gross <= limit * nav_after + 1e-7 for limit, used in constraints))
            if qty and not affordable(qty):
                low, high, best = 0, (qty - rule["buy_min"]) // rule["buy_step"], 0
                while low <= high:
                    mid = (low + high) // 2
                    candidate = rule["buy_min"] + mid * rule["buy_step"]
                    if affordable(candidate):
                        best, low = candidate, mid + 1
                    else:
                        high = mid - 1
                qty = best
            if reason or qty <= 0:
                order.update(status="cancelled", reason=reason or "cash_risk_lot_or_liquidity_budget")
                emit("entry_cancelled", day, order_id=order["id"], code=code, reason=order["reason"], evidence_limits=unknown)
                issues.extend({"code": code, "reason": r} for r in unknown)
                continue
            gross = qty * price
            slip = gross * cfg["slippage"]
            fee, components = execution.charges(gross + slip, "buy", rule)
            cash -= gross + slip + fee
            total_fees += fee
            total_slippage += slip
            buys += gross + slip
            liquidity_used[code] += gross
            side_used[(code, "buy")] += qty
            order.update(status="filled" if gross >= target - rule["buy_step"] * price else "partial_fill",
                         filled_shares=qty, target_notional=target)
            lot = {"id": order["id"], "code": code, "strategy": strategy, "version": order["version"],
                   "snapshot_id": order["snapshot_id"], "shares": qty, "entry_day": day,
                   "exit_index": order["exit_index"], "status": "holding", "buy_gross": gross,
                   "sales_gross": 0., "dividends": 0., "costs": fee + slip}
            lots.append(lot)
            lot_by_id[lot["id"]] = lot
            open_values[lot["id"]] = gross
            open_nav -= fee + slip
            emit("buy_fill", day, order_id=order["id"], lot_id=lot["id"], code=code, strategy=strategy,
                 shares=qty, price=price, reference_open=price, execution_price=price * (1 + cfg["slippage"]),
                 execution_notional=gross + slip, gross=gross, fee=fee, slippage=slip, cash_after=cash,
                 evidence_limits=unknown, fee_components=components,
                 quantity_rule={k: rule[k] for k in ("buy_min", "buy_step")})
            issues.extend({"code": code, "reason": r} for r in unknown)
            if order["status"] == "partial_fill":
                emit("entry_remainder_cancelled", day, order_id=order["id"], code=code,
                     unspent_target=max(0., target - gross), reason="fixed_slot_no_retry_no_redistribution")

        if clock < close_clock:
            break  # replay output below preserves fills, but has no closing NAV for today
        for order in orders:
            if order["signal_date"] == day:
                emit("signal_frozen", day, order_id=order["id"], snapshot_id=order["snapshot_id"],
                     code=order["code"], strategy=order["strategy"], target_weight=order["target_weight"],
                     frozen_at=order["frozen_at"])
        active_codes = {l["code"] for l in lots if l["shares"]}
        closes = {c: _quote(bars, c, day, "close") for c in active_codes}
        for code in sorted(active_codes):
            reasons = evidence(code, day, close_clock)
            issues.extend({"code": code, "reason": r} for r in reasons)
            if strict and reasons:
                blocked_marks.add(code)
            if closes[code]:
                last_prices[code] = closes[code]
            else:
                issues.append({"code": code, "reason": "missing_close"})
                blocked_marks.add(code)
        complete = all(closes.values()) and not blocked_marks
        if not complete or any(o.get("reason") in UNKNOWN_ENTRY_REASONS for o in orders):
            performance_uncertain = True
        values = share_values(closes) if complete else {}
        position_value = sum(values.values()) if complete else None
        receivable_value = sum(r["amount"] for r in receivables if not r["paid"])
        nav = cash + position_value + receivable_value if complete else None
        pnl = nav - previous_nav if nav is not None and previous_nav is not None else None
        cost = total_fees + total_slippage - before_cost
        weights, strat_weights, exposure = {}, defaultdict(float), {k: defaultdict(float) for k in ("industry", "subindustry", "theme")}
        unknown_sectors, positions, strat_pnl = [], [], defaultdict(float)
        grouped = defaultdict(list)
        for lot in lots:
            if lot["shares"]:
                grouped[lot["code"]].append(lot)
            mark = lot["shares"] * closes[lot["code"]] if complete and lot["shares"] else 0.
            strat_pnl[lot["strategy"]] += lot["sales_gross"] + lot["dividends"] + mark - lot["buy_gross"] - lot["costs"]
        for code, group in sorted(grouped.items()):
            qty = sum(l["shares"] for l in group)
            value = qty * closes[code] if complete else None
            weight = value / nav if nav is not None else None
            amount = _quote(bars, code, day, "amount")
            positions.append({"code": code, "shares": qty, "market_value": value, "weight": weight,
                              "strategies": sorted({l["strategy"] for l in group}),
                              "pending_exit_shares": sum(l["shares"] for l in group if l["status"] == "pending_exit"),
                              "last_known_close": last_prices.get(code), "daily_amount": amount,
                              "position_to_daily_amount": value / amount if value is not None and amount else None})
            weights[code] = weight
            rels = _relations(relations, code, day)
            if not _complete_relations(relations, code, day):
                unknown_sectors.append(code)
            if weight is not None:
                for rel in rels:
                    exposure[rel["category"]][rel["id"]] += weight
                for lot in group:
                    strat_weights[lot["strategy"]] += lot["shares"] * closes[code] / nav
        market_return = None
        if prev_day and life._finite(benchmark.get(day)) and life._finite(benchmark.get(prev_day)):
            market_return = benchmark[day] / benchmark[prev_day] - 1
        # Exact reconciliation: capital-market + cash/receivable drag + sector + residual stock - costs.
        capital_market = previous_nav * market_return if previous_nav is not None and market_return is not None else None
        cash_drag = -previous_cashlike * market_return if capital_market is not None else None
        sector_component, sector_coverage = 0., 0.
        prev_invested = sum(previous_values.values())
        if capital_market is not None:
            for lid, value in previous_values.items():
                lot = lot_by_id[lid]
                rels = [r for r in _relations(relations, lot["code"], prev_day) if r["category"] == "industry"]
                returns = [sector_returns.get(day, {}).get(r["id"]) for r in rels]
                if returns and all(isinstance(r, (int, float)) and math.isfinite(r) for r in returns):
                    sector_component += value * (sum(returns) / len(returns) - market_return)
                    sector_coverage += value
        sector_complete = prev_invested == 0 or abs(sector_coverage - prev_invested) < 1e-8
        stock_component = pnl + cost - capital_market - cash_drag - sector_component if pnl is not None and capital_market is not None else None
        risk_breaches = []
        if nav is not None:
            if cash / nav < cfg["cash_reserve"] - 1e-9:
                risk_breaches.append("cash_reserve_drift")
            risk_breaches.extend("stock:" + c for c, w in weights.items() if w > cfg["max_stock_weight"] + 1e-9)
            risk_breaches.extend("strategy:" + s for s, w in strat_weights.items() if w > cfg["strategy_budgets"][s] + 1e-9)
            for kind, mapping in exposure.items():
                limit = cfg["max_theme_weight"] if kind == "theme" else cfg["max_industry_weight"]
                risk_breaches.extend(kind + ":" + key for key, w in mapping.items() if w > limit + 1e-9)
        row = {"date": day, "cash": cash, "receivables": receivable_value, "position_value": position_value,
               "nav": nav, "unit_nav": nav / initial if nav is not None else None,
               "daily_return": nav / previous_nav - 1 if nav is not None and previous_nav is not None else None,
               "pnl": pnl, "buy_notional": buys, "sell_notional": sells, "cost": cost,
               "buy_turnover": buys / previous_nav if previous_nav is not None else None,
               "sell_turnover": sells / previous_nav if previous_nav is not None else None,
               "gross_turnover": (buys + sells) / previous_nav if previous_nav is not None else None,
               "one_way_turnover": (buys + sells) / (2 * previous_nav) if previous_nav is not None else None,
               "turnover_denominator": "previous_session_closing_nav_or_initial_cash_first_day",
               "positions": positions, "cash_weight": cash / nav if nav is not None else None,
               "strategy_weights": dict(strat_weights), "sector_exposure": {k: dict(v) for k,v in exposure.items()},
               "unknown_sector_codes": unknown_sectors, "risk_breaches": risk_breaches,
               "liquidity_exposure": {"amount_coverage": sum(p["daily_amount"] is not None for p in positions),
                   "holding_codes": len(positions),
                   "low_liquidity_weight": sum(p["weight"] for p in positions if p["daily_amount"] is not None
                       and p["daily_amount"] < strategies.MIN_AMOUNT) if nav is not None else None,
                   "unknown_liquidity_weight": sum(p["weight"] for p in positions if p["daily_amount"] is None)
                       if nav is not None else None},
               "strategy_cumulative_pnl": dict(strat_pnl) if complete else None,
               "attribution": {"capital_market": capital_market, "cash_and_receivable_drag": cash_drag,
                   "covered_sector_increment": sector_component if capital_market is not None else None,
                   "stock_and_unexplained_sector_residual": stock_component, "execution_cost": -cost,
                   "sector_coverage": sector_coverage / prev_invested if prev_invested else None,
                   "sector_complete": sector_complete, "basis": "previous_close_exposure_new_intraday_trades_in_residual"},
               "data_status": "complete_prices" if complete else "valuation_incomplete",
               "performance_evidence_complete": not performance_uncertain,
               "evidence_limits": sorted({(r["code"], r["reason"]) for r in issues}),
               "cash_reconciliation_error": cash + (position_value or 0.) + receivable_value - nav if nav is not None else None,
               "pnl_reconciliation_error": sum(strat_pnl.values()) - (nav - initial) if complete else None}
        if cash < -1e-6 or (row["pnl_reconciliation_error"] is not None and abs(row["pnl_reconciliation_error"]) > 1e-6):
            raise ArithmeticError("account_reconciliation_failed")
        daily.append(row)
        previous_nav = nav
        previous_values = values
        previous_cashlike = cash + receivable_value
    metrics = account_metrics(daily, initial)
    if not visible:
        metrics = {"status": "no_forward_signals", "total_return": None, "max_drawdown": None,
                   "daily_volatility": None, "annualized_return": None}
    evidence_counts = Counter(reason for row in daily for _, reason in row["evidence_limits"])
    unknown_cancellations = [o for o in orders if o.get("reason") in UNKNOWN_ENTRY_REASONS]
    if unknown_cancellations:
        metrics.update(status="incomplete_execution_evidence", total_return=None, max_drawdown=None,
                       daily_volatility=None, annualized_return=None)
    metrics["evidence_status"] = ("no_forward_signals" if not visible else "research_proxy_unverified" if not strict or evidence_counts
                                   else "verified_input_simulation_not_actual_fills")
    return {"version": VERSION, "mode": cfg["mode"], "config": cfg, "config_sha256": digest(cfg),
            "as_of": clock.isoformat(), "source": "frozen_forward_signals", "formal": False,
            "valuation_as_of": daily[-1]["date"] if daily else None,
            "status": "no_signals" if not visible else "intraday_pending_close" if days and clock < life.observation_clock(days[-1] + "T15:00:00+08:00")
                else "valuation_incomplete" if any(r["nav"] is None for r in daily) else "execution_inputs_incomplete" if unknown_cancellations else "simulation",
            "daily": daily, "events": events, "orders": orders, "lots": lots, "receivables": receivables,
            "rejected_snapshots": rejected, "cash": cash, "metrics": metrics,
            "fees": total_fees, "slippage": total_slippage,
            "fee_components": {kind: sum(e.get("fee_components", {}).get(kind, 0.) for e in events)
                               for kind in ("commission", "transfer", "sell_tax")},
            "evidence_summary": dict(evidence_counts),
            "unknown_entry_cancellations": len(unknown_cancellations),
            "execution_status_counts": dict(Counter(e["type"] for e in events)),
            "limits": ["simulation_not_broker_fills", "risk_defaults_unvalidated_not_target_recommendations",
                       "no_cash_interest_or_external_flows", "daily_open_proxy", "verified_actions_only",
                       "no_forced_liquidation_on_missing_exit", "sparse_or_missing_inputs_not_effectiveness_evidence"]}


def account_metrics(daily, initial):
    """No interpolation across missing NAV, no annualization of individual trades."""
    if not daily or any(r["nav"] is None for r in daily):
        return {"status": "no_closing_series" if not daily else "incomplete_nav_series", "total_return": None,
                "max_drawdown": None, "daily_volatility": None, "annualized_return": None}
    navs = [initial] + [r["nav"] for r in daily]
    peak, drawdown = initial, 0.
    for nav in navs:
        peak = max(peak, nav)
        drawdown = min(drawdown, nav / peak - 1)
    returns = [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs))]
    mean = sum(returns) / len(returns)
    volatility = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)) if len(returns) > 1 else None
    return {"status": "descriptive_account_series", "total_return": navs[-1] / initial - 1,
            "max_drawdown": drawdown, "daily_volatility": volatility, "annualized_return": None,
            "sessions": len(daily), "gross_turnover_sum": sum(r["gross_turnover"] for r in daily),
            "one_way_turnover_sum": sum(r["one_way_turnover"] for r in daily)}
