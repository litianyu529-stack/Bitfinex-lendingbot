"""Event-driven common replay for legacy and adaptive strategies; read-only input."""

from collections import deque
from dataclasses import replace
from decimal import Decimal

from AdaptiveEngines import REGISTRY, engine_module, template_for
from AdaptiveExecutionState import MAX_PASSIVE_MS, evidence, quote_key
from Configuration import strategy_v3_from_record
from ExchangeModels import parse_book
from Currency import funding_minimum
from ResearchV4 import build_from_store, connect_readonly, moving_block_interval
from StrategyV3 import build_market_signals_v3, build_strategy_plan_v3
from StrategyV3 import ceil_rate_tick, competitive_rate_for_period, gross_daily_floor
from StrategyV4 import (
    DAY,
    ENGINE,
    ENGINES,
    MULTI_ENGINE,
    V42_ENGINE,
    adjustment,
    build_plan,
    digest,
    fee,
    gross_floor,
    holding_hours,
    pool_for_period,
)

D = Decimal
PUBLIC_DYNAMIC = "public_dynamic_limit"


def public_dynamic_plan(state, policy, book):
    """Transparent current-price LIMIT reference, not a commercial predictor."""
    total, exposure = state["total"], state["existingExposure"]["total"]
    cap = min(
        total * policy.max_lend_percent / 100, policy.max_lend_amount if policy.max_lend_amount is not None else total
    )
    budget = min(state["wallet"], max(D(0), cap - exposure))
    rows = []
    for row in book:
        period, rate = int(row["period"]), D(str(row["rate"]))
        if (
            D(str(row["amount"])) >= 0
            or int(row.get("count", 1)) <= 0
            or not 2 <= period <= policy.maximum_period
            or rate < gross_floor(policy, period)
        ):
            continue
        if row.get("demandType", "UNKNOWN") not in ("UNKNOWN", "FIXED", "LIMIT"):
            continue
        rows.append((rate, period, abs(D(str(row["amount"])))))
    rows.sort(key=lambda r: (-r[0], r[1]))
    plan = []
    long_used = sum((D(v) for p, v in state["exposureByPeriod"].items() if int(p) >= policy.long_from_days), D(0))
    for rate, period, capacity in rows:
        if len(plan) >= max(0, 8 - state["openOfferCount"]):
            break
        if period >= policy.long_from_days:
            capacity = min(capacity, max(D(0), total * policy.long_max_share / 100 - long_used))
        amount = min(budget, capacity)
        if amount < funding_minimum():
            continue
        plan.append(
            dict(
                period=period,
                amount=amount,
                effective_rate=rate,
                submitted_rate=rate,
                offer_type="LIMIT",
                display_type="LIMIT",
            )
        )
        budget -= amount
        long_used += amount if period >= policy.long_from_days else 0
    return dict(plan=plan, candidates=[], dynamicTarget=rows[0] if rows else None)


def replay(
    policy,
    model,
    trades,
    books,
    principal,
    start_ms,
    end_ms,
    cancelled=lambda: False,
    stress=False,
    interval_ms=60000,
    initial_trades=(),
):
    """Volume is consumed once; interest accrues at every fill/return/end boundary."""
    wallet = D(str(principal))
    orders, credits, fills, cancellations, returns, continuations = [], [], [], [], [], []
    daily = {}
    history = deque(initial_trades)
    history = deque(r for r in history if start_ms - 7 * DAY <= int(r["mts"]) < start_ms)
    book_iter, trade_iter = iter(books), iter(trades)
    next_book, next_trade = next(book_iter, None), next(trade_iter, None)
    book, book_at, current_frr, frr_at = [], 0, None, 0
    cursor, next_decision = start_ms, start_ms
    interest, capital_time, idle_time = D(0), D(0), D(0)
    coverage_ms, frr_coverage_ms, total_ms = 0, 0, end_ms - start_ms
    actions = 0
    quarantines, lease_events, blocked, unknown_volume = {}, [], [], D(0)
    core = engine_module(policy.strategy_engine) if policy.strategy_engine in ENGINES else None

    def account():
        exposure = {p: D(0) for p in ("short", "medium", "long")}
        for row in orders + credits:
            exposure[pool_for_period(row["period"])] += row["amount"]
        return {
            "total": D(str(principal)),
            "wallet": wallet,
            "exposure": exposure,
            "existingExposure": {
                "total": sum(exposure.values()),
                "variable": sum(
                    (r["amount"] for r in orders + credits if r.get("offer_type") in ("FRR", "FRRDELTAVAR")), D(0)
                ),
                "hidden": D(0),
            },
            "exposureByPeriod": {
                period: sum((r["amount"] for r in orders + credits if r["period"] == period), D(0))
                for period in {r["period"] for r in orders + credits}
            },
            "openOfferCount": len(orders),
            "managedOffers": [{**r, "rate": r["rate"], "managed": True} for r in orders],
            "passiveLeaseStates": [r["lease"] for r in orders if r.get("lease")],
            "passiveQuarantines": quarantines,
        }

    while cursor < end_ms:
        if cancelled():
            raise InterruptedError("replay cancelled")
        if next_book and next_book["mts"] <= cursor:
            book, book_at = next_book["book"], next_book["mts"]
            frr = next_book.get("frr")
            if frr is not None:
                current_frr = D(str(frr))
                frr_at = next_book.get("frr_mts", book_at)
                for row in orders:
                    if row.get("offer_type") in ("FRR", "FRRDELTAVAR", "FRRDELTAFIX"):
                        updated_rate = max(D(0), current_frr + row.get("submitted_rate", D(0)))
                        if updated_rate != row["rate"]:
                            supply = sum(
                                (
                                    abs(D(str(r["amount"])))
                                    for r in book
                                    if D(str(r["amount"])) > 0 and D(str(r["rate"])) <= updated_rate
                                ),
                                D(0),
                            )
                            # Auto-floating quotes do not receive a fabricated queue advantage.
                            row["queue"] = max(row["queue"], supply * (2 if stress else 1))
                        row["rate"] = updated_rate
                for row in credits:
                    if row.get("offer_type") in ("FRR", "FRRDELTAVAR"):
                        row["rate"] = max(D(0), current_frr + row.get("submitted_rate", D(0)))
            next_book = next(book_iter, None)
            continue
        for credit in list(credits):
            if credit["return_ms"] <= cursor:
                wallet += credit["amount"]
                returns.append({"atMs": cursor, "amount": credit["amount"], "period": credit["period"]})
                credits.remove(credit)
        for order in list(orders):
            if order.get("cancel_at", end_ms + 1) <= cursor:
                wallet += order["amount"]
                orders.remove(order)
                continuations.append(order)
                if order.get("lease"):
                    lease = order["lease"]
                    quarantines[lease["quoteKey"]] = {
                        "closedAtMs": cursor,
                        "chainStartMs": lease["chainStartMs"],
                        "evidenceWatermark": lease["evidenceWatermark"],
                    }
                    lease_events.append(dict(atMs=cursor, action="CLOSED", quoteKey=lease["quoteKey"]))
        if cursor >= next_decision:
            if book and cursor - book_at <= 60000:
                state = account()
                if policy.strategy_engine == PUBLIC_DYNAMIC:
                    plan = public_dynamic_plan(state, policy, book)
                elif core and policy.strategy_engine != ENGINE:
                    fresh_frr = current_frr if cursor - frr_at <= policy.rest_stale_seconds * 1000 else None
                    plan = core.build_plan(state, policy, model, book, list(history), cursor, "replay", fresh_frr)
                elif policy.strategy_engine == ENGINE:
                    plan = build_plan(state, policy, model, book, list(history), cursor, "replay")
                else:
                    stats = [] if current_frr is None else [{"mts": cursor, "frr_daily_rate": current_frr}]
                    signals = build_market_signals_v3(book, list(history), stats, policy, cursor)
                    plan = build_strategy_plan_v3(
                        state["total"],
                        wallet,
                        state["exposure"],
                        policy,
                        signals,
                        "replay",
                        existing_exposure=state["existingExposure"],
                    )
                if plan.get("blockReasons"):
                    blocked.append(dict(atMs=cursor, reasons=plan["blockReasons"]))
                for order in orders:
                    if order.get("cancel_at"):
                        continue
                    if policy.strategy_engine in ENGINES:
                        if policy.strategy_engine != ENGINE:
                            decide = core.adjustment
                            extra = {
                                "current_frr": current_frr
                                if cursor - frr_at <= policy.rest_stale_seconds * 1000
                                else None
                            }
                        else:
                            decide, extra = adjustment, {}
                        decision = decide(
                            policy,
                            model,
                            {
                                **order,
                                "rate": order["submitted_rate"],
                                "rate_real": order["rate"],
                                "managed": True,
                                "mts_created": order["created_ms"],
                                "passiveLease": order.get("lease"),
                            },
                            plan["candidates"],
                            list(history),
                            book,
                            cursor,
                            **extra,
                        )
                        lease = order.get("lease")
                        if lease and decision["reason"] == "PASSIVE_TO_NORMAL":
                            order.pop("lease")
                            lease_events.append(dict(atMs=cursor, action="NORMAL", quoteKey=lease["quoteKey"]))
                            lease = None
                        if lease and decision["reason"] == "PASSIVE_LEASE_RENEW":
                            fresh = set(decision.get("evidenceWatermark", [])) & evidence(
                                history, book, cursor, lease["startedAtMs"]
                            )
                            fresh -= set(lease["evidenceWatermark"])
                            if fresh and cursor < lease["chainStartMs"] + MAX_PASSIVE_MS:
                                lease["evidenceWatermark"] = sorted(set(lease["evidenceWatermark"]) | fresh)
                                lease["expiresAtMs"] = min(
                                    cursor + policy.passive_wait_minutes * 60000, lease["chainStartMs"] + MAX_PASSIVE_MS
                                )
                                lease_events.append(dict(atMs=cursor, action="RENEW", quoteKey=lease["quoteKey"]))
                        if policy.strategy_engine == V42_ENGINE:
                            # Compare the exact native submitted value, not an FRR-dependent effective quote.
                            key = (
                                model["id"],
                                decision.get("targetPeriod"),
                                decision.get("targetType"),
                                str(D(str(decision.get("targetSubmittedRate", 0))).normalize()),
                            )
                            if decision["action"] != "CANCEL" or decision.get("hard"):
                                order.update(confirmations=0, confirmation_key=None)
                                count = 0
                            else:
                                count = order.get("confirmations", 0) + 1 if key == order.get("confirmation_key") else 1
                                order.update(confirmations=count, confirmation_key=key)
                        else:
                            key = (
                                decision.get("targetPeriod"),
                                decision.get("targetRate"),
                                decision.get("targetType"),
                                decision["reason"],
                            )
                            count = order.get("confirmations", 0) + 1 if key == order.get("confirmation_key") else 1
                            order.update(confirmations=count, confirmation_key=key)
                        recent_adjustments = sum(r["atMs"] > cursor - 3600000 for r in cancellations)
                        ordinary = (
                            count >= 2
                            and cursor - order.get("last_adjustment", 0) >= 120000
                            and recent_adjustments < 12
                        )
                        change = decision["action"] == "CANCEL" and (decision.get("hard") or ordinary)
                        if lease and cursor >= lease["expiresAtMs"]:
                            # Expiry cannot be silently extended by an unconfirmed ordinary adjustment.
                            change = True
                            decision = dict(action="CANCEL", reason="LEASE_EXPIRED", hard=True)
                    elif policy.strategy_engine == PUBLIC_DYNAMIC:
                        target = plan.get("dynamicTarget")
                        change = bool(
                            target
                            and cursor - order["created_ms"] >= 5 * 60000
                            and (target[0], target[1]) != (order["rate"], order["period"])
                        )
                        change = change and cursor - order.get("last_adjustment", 0) >= 120000
                        change = change and sum(r["atMs"] > cursor - 3600000 for r in cancellations) < 12
                        decision = dict(
                            reason="PUBLIC_DYNAMIC_QUOTE",
                            targetRate=target[0] if target else None,
                            targetPeriod=target[1] if target else None,
                        )
                    else:
                        from RuntimeV3 import _exploration_age_stage_target

                        pool = pool_for_period(order["period"])
                        stages = policy.reprice_stages(pool, order["layer"], order["pricing_curve_version"])
                        stage = order.get("stage", 0) + 1
                        floor_rate = gross_daily_floor(policy.floor_apr(pool), policy.normal_fee_rate)
                        benchmark = competitive_rate_for_period(
                            order["layer"], pool, order["period"], signals, floor_rate, order["pricing_curve_version"]
                        )
                        chain = {
                            "origin_rate": order["origin_rate"],
                            "fixed_landing_rate": order.get("fixed_landing_rate")
                            or max(floor_rate, min(order["origin_rate"], benchmark)),
                        }
                        target_rate = _exploration_age_stage_target(
                            min(stage, len(stages)),
                            chain,
                            benchmark,
                            floor_rate,
                            min(
                                len(stages),
                                policy.high_landing_stage
                                if order["layer"] == "high"
                                else policy.balanced_landing_stage,
                            ),
                            len(stages),
                        )
                        due = stage <= len(stages) and cursor - order["chain_start"] >= stages[stage - 1] * 60000
                        change = (
                            due
                            and order["offer_type"] != "FRR"
                            and order["rate"] - target_rate >= policy.minimum_rate_change
                        )
                        if due:
                            order["stage"] = min(stage, len(stages))
                        decision = {
                            "reason": "LEGACY_AGE_STAGE",
                            "targetRate": ceil_rate_tick(target_rate),
                            "targetPeriod": order["period"],
                        }
                    if change:
                        order.update(cancel_at=cursor + 60000, last_adjustment=cursor, replacement=decision)
                        cancellations.append({"atMs": cursor, "remainingAmount": order["amount"], **decision})
                if policy.strategy_engine not in ENGINES and policy.strategy_engine != PUBLIC_DYNAMIC:
                    # Repricing keeps term and chain timing, with a one-minute
                    # cancel-confirmation barrier during which fills remain possible.
                    for old in continuations:
                        if old["amount"] >= funding_minimum():
                            plan["plan"].insert(0, {**old, "effective_rate": old["replacement"]["targetRate"]})
                for proposed in plan["plan"][: max(0, 8 - len(orders))]:
                    amount = min(wallet, proposed["amount"])
                    if amount < funding_minimum():
                        continue
                    lease = None
                    if policy.strategy_engine == V42_ENGINE and proposed.get("passive"):
                        if any(r.get("lease") for r in orders):
                            continue
                        key = quote_key(policy.currency, proposed)
                        closed = quarantines.get(key)
                        if closed and not (
                            evidence(history, book, cursor, closed["closedAtMs"])
                            & set(proposed.get("evidenceWatermark", []))
                        ) - set(closed["evidenceWatermark"]):
                            continue
                        latest = max(quarantines.values(), key=lambda r: r["closedAtMs"], default=None)
                        chain_start = latest["chainStartMs"] if latest else cursor
                        if latest and cursor >= chain_start + MAX_PASSIVE_MS:
                            new = evidence(history, book, cursor, latest["closedAtMs"])
                            if not new - set(latest["evidenceWatermark"]):
                                continue
                            chain_start = cursor
                        lease = dict(
                            quoteKey=key,
                            startedAtMs=cursor,
                            chainStartMs=chain_start,
                            expiresAtMs=min(cursor + policy.passive_wait_minutes * 60000, chain_start + MAX_PASSIVE_MS),
                            evidenceWatermark=list(proposed.get("evidenceWatermark", [])),
                        )
                        amount = min(amount, funding_minimum())
                        lease_events.append(dict(atMs=cursor, action="OPEN", quoteKey=lease["quoteKey"]))
                    rate = proposed["effective_rate"]
                    if proposed["offer_type"] == "FRR" and current_frr is not None:
                        rate = current_frr
                    queue = sum(
                        (
                            abs(D(str(r["amount"])))
                            for r in book
                            if D(str(r["amount"])) > 0 and 0 < D(str(r["rate"])) <= rate
                        ),
                        D(0),
                    )
                    orders.append(
                        {
                            "created_ms": cursor,
                            "amount": amount,
                            "original_amount": amount,
                            "rate": rate,
                            "period": proposed["period"],
                            "queue": queue * (2 if stress else 1),
                            "offer_type": proposed["offer_type"],
                            "display_type": proposed.get("display_type", proposed["offer_type"]),
                            "submitted_rate": proposed.get(
                                "submitted_rate", D(0) if proposed["offer_type"] == "FRR" else rate
                            ),
                            "flags": proposed.get("flags", 0),
                            "layer": proposed.get("layer", "balanced"),
                            "pricing_curve_version": proposed.get("pricing_curve_version", "EXACT_TERM_EXPLORATION_V1"),
                            "origin_rate": proposed.get("origin_rate", rate),
                            "chain_start": proposed.get(
                                "chain_start", continuations[0]["chain_start"] if continuations else cursor
                            ),
                            "stage": proposed.get("stage", 0),
                            "last_adjustment": proposed.get("last_adjustment", cursor),
                            "lease": lease,
                        }
                    )
                    wallet -= amount
                    actions += 1
                continuations.clear()
            else:
                # No fresh quote data means no renewal, even if the timer expired between books.
                for order in orders:
                    lease = order.get("lease")
                    if lease and cursor >= lease["expiresAtMs"] and not order.get("cancel_at"):
                        order["cancel_at"] = cursor + 60000
                        cancellations.append(
                            dict(
                                atMs=cursor,
                                remainingAmount=order["amount"],
                                action="CANCEL",
                                reason="LEASE_EXPIRED",
                                hard=True,
                            )
                        )
            next_decision = cursor + interval_ms
            next_decision = min(
                [
                    next_decision,
                    *[
                        r["lease"]["expiresAtMs"]
                        for r in orders
                        if r.get("lease") and r["lease"]["expiresAtMs"] > cursor
                    ],
                ]
            )
        if next_trade and int(next_trade["mts"]) <= cursor:
            trade = next_trade
            volume = abs(D(str(trade["amount"])))
            for order in sorted(orders, key=lambda r: (r["rate"], r["created_ms"])):
                if order["period"] < int(trade["period"]) or order["rate"] > D(str(trade["rate"])) or volume <= 0:
                    continue
                compatibility = "UNKNOWN"
                if policy.strategy_engine == V42_ENGINE:
                    compatibility = core.compatibility(order["display_type"], trade)
                    if compatibility == "INCOMPATIBLE" or (
                        stress and compatibility == "UNKNOWN" and order["display_type"] != "LIMIT"
                    ):
                        continue
                queued = min(volume, order["queue"])
                order["queue"] -= queued
                volume -= queued
                amount = min(volume, order["amount"])
                if amount <= 0:
                    continue
                order["amount"] -= amount
                volume -= amount
                if policy.strategy_engine == V42_ENGINE:
                    duration = core._hold(
                        core._holding_curve(model, order["display_type"], order["period"]), order["period"], 0.5, stress
                    )
                    if compatibility == "UNKNOWN":
                        unknown_volume += amount
                else:
                    duration = holding_hours(model, order["period"], 0.5, stress) if model else order["period"] * 24
                credits.append(
                    {
                        "amount": amount,
                        "rate": order["rate"],
                        "period": order["period"],
                        "offer_type": order["offer_type"],
                        "submitted_rate": order["submitted_rate"],
                        "return_ms": cursor + int(duration * 3600000),
                    }
                )
                fills.append(
                    {
                        "atMs": cursor,
                        "amount": amount,
                        "rate": order["rate"],
                        "period": order["period"],
                        "waitMinutes": (cursor - order["created_ms"]) / 60000,
                        "cumulativeWaitMinutes": (cursor - order["chain_start"]) / 60000,
                        "evidence": "SIMULATED_PUBLIC_CAPACITY",
                        "demandCompatibility": compatibility,
                        "displayType": order["display_type"],
                    }
                )
            orders = [r for r in orders if r["amount"] > 0]
            history.append(trade)
            while history and int(history[0]["mts"]) < cursor - 7 * DAY:
                history.popleft()
            next_trade = next(trade_iter, None)
            continue
        targets = [end_ms, next_decision, (cursor // DAY + 1) * DAY]
        if next_book:
            targets.append(next_book["mts"])
        if next_trade:
            targets.append(int(next_trade["mts"]))
        targets.extend(r["return_ms"] for r in credits)
        targets.extend(r["cancel_at"] for r in orders if r.get("cancel_at"))
        targets.extend(
            r["lease"]["expiresAtMs"] for r in orders if r.get("lease") and r["lease"]["expiresAtMs"] > cursor
        )
        target = min(t for t in targets if t > cursor)
        if current_frr is not None and cursor - frr_at <= policy.rest_stale_seconds * 1000:
            frr_coverage_ms += min(target - cursor, max(0, frr_at + policy.rest_stale_seconds * 1000 - cursor))
        duration = D(target - cursor) / DAY
        lent = sum((r["amount"] for r in credits), D(0))
        earned = sum((r["amount"] * r["rate"] * duration * (1 - fee(policy)) for r in credits), D(0))
        interest += earned
        daily[cursor // DAY] = daily.get(cursor // DAY, D(0)) + earned
        capital_time += D(str(principal)) * duration
        idle_time += (D(str(principal)) - lent) * duration
        if book and cursor - book_at <= 60000:
            coverage_ms += min(target - cursor, max(0, book_at + 60000 - cursor))
        cursor = target
    amount = sum((r["amount"] for r in fills), D(0))
    return {
        "netInterest": interest,
        "returnOnPrincipalTime": interest / capital_time if capital_time else D(0),
        "netAprPercent": interest / capital_time * 36500 if capital_time else D(0),
        "dailyNetInterest": [daily.get(day, D(0)) for day in range(start_ms // DAY, (end_ms - 1) // DAY + 1)],
        "idlePrincipalTime": idle_time,
        "principalTime": capital_time,
        "averageWaitMinutes": sum((r["amount"] * D(str(r["waitMinutes"])) for r in fills), D(0)) / amount
        if amount
        else None,
        "unfilledAmount": sum((r["amount"] for r in orders), D(0)),
        "fillCount": len(fills),
        "cancellationCount": len(cancellations),
        "cancellations": cancellations,
        "submissionCount": actions,
        "openCreditAmount": sum((r["amount"] for r in credits), D(0)),
        "bookCoverageFraction": coverage_ms / total_ms if total_ms else 0,
        "frrCoverageFraction": frr_coverage_ms / total_ms if total_ms else 0,
        "fills": fills,
        "returns": returns,
        "leaseEvents": lease_events,
        "blockedDecisions": blocked,
        "unknownTypeSimulatedAmount": unknown_volume,
        "ownsRealFillEvidence": False,
        "assumptions": ["公共成交是成交容量上限，不是自己的成交证明", "未知真实队列以可见竞争供给估计"],
    }


def streams(path, start, end):
    def trades():
        with connect_readonly(path) as c:
            for row in c.execute("SELECT * FROM market_trades WHERE mts>=? AND mts<? ORDER BY mts", (start, end)):
                yield dict(row)

    def books():
        import json

        with connect_readonly(path) as c:
            for row in c.execute(
                "SELECT b.*, (SELECT s.frr_daily_rate FROM funding_stats s WHERE s.mts<=b.mts "
                "ORDER BY s.mts DESC LIMIT 1) AS frr, (SELECT s.mts FROM funding_stats s WHERE s.mts<=b.mts "
                "ORDER BY s.mts DESC LIMIT 1) AS frr_mts FROM book_snapshots b "
                "WHERE b.mts>=? AND b.mts<? ORDER BY b.mts",
                (start, end),
            ):
                raw = json.loads(row["book_json"])
                yield {
                    "mts": row["mts"],
                    "book": raw if not raw or isinstance(raw[0], dict) else parse_book(raw),
                    "frr": row["frr"],
                    "frr_mts": row["frr_mts"],
                }

    return trades(), books()


def evaluate(store, now_ms, cancelled=lambda: False, engine=ENGINE):
    import json
    from pathlib import Path
    from FileUtils import atomic_write_text

    current = strategy_v3_from_record(store.strategy("ACTIVE"))
    template = template_for(engine, current)
    training_options = {"engine": engine} if engine != ENGINE else {}
    final_model = build_from_store(store, now_ms, cancelled, **training_options)
    train_end, validation_end = now_ms - 30 * DAY, now_ms - 15 * DAY
    report = {
        "currency": store.currency,
        "algorithm": final_model["algorithm"],
        "state": "INSUFFICIENT_DATA",
        "split": "60/15/15 chronological days",
        "coverage": final_model["coverage"],
        "eligibleForLiveCandidate": False,
        "requiresManualPromotion": True,
        "comparisons": {},
        "requestedBaselines": ["original_active", "repaired_v41", "public_dynamic_limit", "v42"]
        if engine == V42_ENGINE
        else ["original_active", "same_floor_legacy"],
        "simulationIsOwnFillEvidence": False,
    }
    # Missing raw historical books or own evidence never yields an eligible result.
    cov = final_model["coverage"]
    if (
        not cov.get("publicComplete")
        or not cov["bookSnapshots"]["earliestMs"]
        or final_model["ownObservationCount"] < 20
    ):
        report["reason"] = "逐笔成交、历史盘口或自己的订单证据不足；K线和影子成交不能替代"
        return report, final_model
    model = build_from_store(store, train_end, cancelled, lookback_days=60, **training_options)
    if engine in (MULTI_ENGINE, V42_ENGINE) and (
        len(model.get("frrDays", [])) < 20
        or any(
            model.get("typeObservationCounts", {}).get(kind, 0) < 20
            for kind in ("LIMIT", "FRR", "FRR_DELTA_FIXED", "FRR_DELTA_VARIABLE")
        )
    ):
        report["reason"] = "缺少至少20天FRR历史或各订单类型20条自身资金链；收益研究保持未通过"
        return report, model
    if model["ownObservationCount"] < 20:
        report["reason"] = "训练窗口自身资金链不足20条；测试集不能用于拟合或调参"
        return report, model
    original_model = model
    if current.strategy_engine in ENGINES:
        from ResearchV4 import ModelRepository

        try:
            original_model = ModelRepository(store.path, store.currency).load(current.model_id, train_end)
        except ValueError:
            report["reason"] = "ACTIVE冻结模型在验证窗口开始时不可用；不能用后来训练的模型伪造历史基线"
            return report, model
    policy = replace(template, model_id=model["id"])
    same_floor = replace(
        current,
        strategy_engine="legacy_v3",
        short_floor_apr=template.short_floor_apr,
        medium_floor_apr=template.medium_floor_apr,
        long_floor_apr=template.long_floor_apr,
    )
    frr = replace(
        same_floor,
        enable_limit=False,
        enable_frr=True,
        enable_frr_delta_fixed=False,
        enable_frr_delta_variable=False,
        enable_hidden=False,
    )
    near = replace(same_floor, quick_share=D(100), balanced_share=D(0), high_share=D(0))
    variants = {
        "original_active": current,
        "same_floor_legacy": same_floor,
        "frr": frr,
        "near_limit": near,
        "adaptive": policy,
    }
    variant_models = {"original_active": original_model}
    repaired_available = True
    if engine == V42_ENGINE:
        from ResearchV4 import ModelRepository

        variants["v42"] = variants.pop("adaptive")
        variants[PUBLIC_DYNAMIC] = replace(
            policy,
            strategy_engine=PUBLIC_DYNAMIC,
            enable_limit=True,
            enable_frr=False,
            enable_frr_delta_fixed=False,
            enable_frr_delta_variable=False,
        )
        repository = ModelRepository(store.path, store.currency)
        old_model = (
            original_model if current.strategy_engine == MULTI_ENGINE else repository.candidate(train_end, MULTI_ENGINE)
        )
        repaired_available = old_model is not None
        report["baselineAvailability"] = {
            "repaired_v41": "AVAILABLE_FROZEN_MODEL" if repaired_available else "MISSING_HISTORICAL_FROZEN_MODEL",
            PUBLIC_DYNAMIC: "TRANSPARENT_CURRENT_COMPATIBLE_LIMIT_5_MINIMUM_MINUTES",
        }
        if old_model:
            old_policy = current if current.strategy_engine == MULTI_ENGINE else template_for(MULTI_ENGINE, current)
            variants["repaired_v41"] = replace(old_policy, model_id=old_model["id"])
            variant_models["repaired_v41"] = old_model
            report["repairedV41ModelId"] = old_model["id"]
        else:
            report["missingBaselineReason"] = "缺少当时可用的V4.1冻结模型，不用新模型补造旧引擎成绩"
    adaptive_name = "v42" if engine == V42_ENGINE else "adaptive"
    if engine in (MULTI_ENGINE, V42_ENGINE):
        from StrategyV41 import FIELDS

        for kind, field in FIELDS.items():
            variants["type_" + kind.lower()] = replace(policy, **{name: name == field for name in FIELDS.values()})
    with connect_readonly(store.path) as c:
        sample = c.execute(
            "SELECT total_principal FROM account_samples WHERE mts<=? ORDER BY mts DESC LIMIT 1", (train_end,)
        ).fetchone()
    principal = D(sample[0]) if sample else D(1000)
    checkpoint = Path(store.path).resolve().parent / "research" / store.currency / REGISTRY[engine][4]
    binding = digest(
        {
            "now": now_ms,
            "model": model["id"],
            "principal": principal,
            "policies": {k: p.__dict__ for k, p in variants.items()},
            "baselineModels": {k: m["id"] for k, m in variant_models.items()},
        }
    )
    metrics = {}
    try:
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        if saved.get("binding") == binding:
            metrics = saved.get("metrics", {})
    except (OSError, ValueError):
        pass
    for split, start, end in [("validation", train_end, validation_end), ("test", validation_end, now_ms)]:
        metrics.setdefault(split, {})
        for name, p in variants.items():
            if cancelled():
                raise InterruptedError("replay cancelled")
            if name in metrics[split]:
                # Decimal financial values are restored explicitly from JSON.
                row = metrics[split][name]
                for key in ("netInterest", "returnOnPrincipalTime", "netAprPercent"):
                    row[key] = D(str(row[key]))
                continue
            raw, books = streams(store.path, start, end)
            metrics[split][name] = replay(
                p, variant_models.get(name, model), raw, books, principal, start, end, cancelled
            )
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            from StrategyV3 import json_decimal

            atomic_write_text(str(checkpoint), json.dumps(json_decimal({"binding": binding, "metrics": metrics})))
    good = repaired_available
    baselines = (
        ("original_active", "same_floor_legacy", "repaired_v41", PUBLIC_DYNAMIC)
        if engine == V42_ENGINE
        else ("original_active", "same_floor_legacy")
    )
    for baseline in baselines:
        if baseline not in variants:
            report["comparisons"][baseline] = dict(state="DATA_UNAVAILABLE", gainVerified=False)
            continue
        gains = []
        for split in ("validation", "test"):
            new, old = metrics[split][adaptive_name], metrics[split][baseline]
            gains.append(new["netInterest"] > old["netInterest"] and new["bookCoverageFraction"] >= 0.95)
            if engine in (MULTI_ENGINE, V42_ENGINE):
                gains[-1] = gains[-1] and new["frrCoverageFraction"] >= 0.95
        ci = moving_block_interval(
            metrics["test"][adaptive_name]["dailyNetInterest"], metrics["test"][baseline]["dailyNetInterest"]
        )
        report["comparisons"][baseline] = {
            "validationGainPositive": gains[0],
            "testGainPositive": gains[1],
            "bootstrap95": ci,
        }
        good = good and all(gains) and ci["valid"] and ci["lower"] > 0
    raw, books = streams(store.path, validation_end, now_ms)
    pessimistic = replay(policy, model, raw, books, principal, validation_end, now_ms, cancelled, stress=True)
    stress_ok = all(pessimistic["netInterest"] > metrics["test"][b]["netInterest"] for b in baselines if b in variants)
    good = good and stress_ok
    if engine == V42_ENGINE:
        # UNKNOWN public type has no identifiable native lane or observed own queue.
        # A favorable assumed fill is not sufficient for a profitability verdict.
        unknown = any(
            D(str(metrics[s][adaptive_name].get("unknownTypeSimulatedAmount", 0))) > 0 for s in ("validation", "test")
        )
        report["unknownPublicTypeAffectedFills"] = unknown
        if unknown:
            good = False
            report["qualificationReason"] = "公共成交类型不可识别；模拟容量不能证明自己的真实成交或收益优势"
    report.update(
        state="EVALUATED",
        metrics=metrics,
        pessimistic=pessimistic,
        eligibleForLiveCandidate=good,
        stressPassed=stress_ok,
    )
    report["testedModelTrainingHash"] = model["id"]
    # Only the frozen training model actually tested can become eligible.
    model["eligibleForLiveCandidate"] = good
    model["validationReportHash"] = digest(report)
    model["id"] = digest({k: v for k, v in model.items() if k != "id"})
    report["testedModelTrainingUntilMs"] = train_end
    model["validationReportHash"] = digest(report)
    model["id"] = digest({k: v for k, v in model.items() if k != "id"})
    return report, model
