"""Pure V4.2 decisions: conditional waiting, bounded leases and earned cashflows.

Public demand has UNKNOWN order type unless its provenance says otherwise. Its
volume is a shared upper bound, never proof that any particular order will fill.
The continuation baseline places floor-constrained short orders and advances
through simulated fills and returns. It never credits an unfilled KEEP with an
automatic future rollover. No exchange client, network or account writes exist.
"""

import math
import random
import time
import json
from bisect import bisect_right
from dataclasses import replace
from decimal import Decimal as D
from functools import lru_cache
from contextvars import ContextVar

import StrategyV4 as base
import StrategyV41 as previous
from AdaptiveExecutionState import evidence as independent_evidence, quote_key
from Currency import funding_minimum
from StrategyV3 import SATOSHI, ceil_rate_tick, pool_for_period

ENGINE = "adaptive_net_yield_v3"
VERSION = "ADAPTIVE_NET_YIELD_V3"
TYPES, FIELDS = previous.TYPES, previous.FIELDS
MAX_PASSIVE_MINUTES = 360
MAX_EVENTS_PER_PATH = 20000
VALUATION_BUDGET_SECONDS = 15
HOURS = base.HORIZON * 24
_valuation_deadline = ContextVar("v42_valuation_deadline", default=None)


class ValuationIncomplete(ValueError):
    """A bounded calculation must fail closed, not fabricate a profitable tail."""


def template(policy):
    return replace(
        previous.template(policy),
        strategy_engine=ENGINE,
        reprice_gain_apr=D(".0025"),
        passive_wait_minutes=60,
        adopt_external_offers=False,
    )


def fit_model(
    currency, trades, observations=(), holdings=(), now_ms=0, coverage=None, frr=(), allow_frr_reference=False
):
    trades, observations, holdings = list(trades), list(observations), list(holdings)
    model = previous.fit_model(currency, trades, observations, holdings, now_ms, coverage, frr, allow_frr_reference)
    groups = {}
    for row in trades:
        if 0 < int(row["mts"]) <= now_ms and D(str(row["rate"])) > 0:
            groups.setdefault((int(row["mts"]) // base.DAY, int(row["period"])), []).append(row)
    model["demandDays"] = [
        dict(
            mts=day * base.DAY,
            period=period,
            rate=str(base.weighted_rate(rows, 0.75)),
            amount=str(sum((abs(D(str(r["amount"]))) for r in rows), D(0))),
            demandType="UNKNOWN",
        )
        for (day, period), rows in sorted(groups.items())
    ]
    model.update(algorithm=VERSION, decisionVersion="CONDITIONAL_CYCLE_LEASE_1")
    model["dataBasis"].update(
        queueEvidence="CURRENT_COMPETITION_UPPER_BOUND",
        publicOrderType="UNKNOWN",
        continuation="EVENT_SHORT_RENEWAL_V1",
    )
    model["pathSeed"] = base.digest({"parentSeed": model["pathSeed"], "demandDays": model["demandDays"]})
    model.pop("id")
    model["id"] = base.digest(model)
    return model


def _check_budget(deadline, events=0):
    if events > MAX_EVENTS_PER_PATH or time.monotonic() > deadline:
        raise ValuationIncomplete("VALUATION_INCOMPLETE: 事件或计算时间预算已耗尽")


def _validate_model_features(model):
    cutoff = int(model["trainedUntilMs"])
    for row in model.get("days", []):
        rate = D(str(row["rate"]))
        if int(row["mts"]) > cutoff or not rate.is_finite() or rate < 0:
            raise ValueError("V4.2行情路径包含未来或无效数据")
    for row in model.get("demandDays", []):
        rate, amount = D(str(row["rate"])), D(str(row["amount"]))
        if (
            int(row["mts"]) > cutoff
            or not rate.is_finite()
            or not amount.is_finite()
            or rate <= 0
            or amount < 0
            or not 2 <= int(row["period"]) <= 120
        ):
            raise ValueError("V4.2需求路径包含未来或无效数据")


def compatibility(kind, row):
    """UNKNOWN is deliberately different from confirmed native compatibility."""
    lane = row.get("demandType", row.get("demand_type", "UNKNOWN"))
    if lane in (None, "UNKNOWN") or kind.startswith("FRR_DELTA") or str(lane).startswith("FRR_DELTA"):
        return "UNKNOWN"
    if lane == kind or (lane == "FIXED" and kind == "LIMIT"):
        return "KNOWN"
    return "INCOMPATIBLE"


def demand_units(book, trades, now_ms):
    units, seen = [], set()
    for source, rows in (("BOOK", book), ("TRADE", trades)):
        for row in rows:
            rate, amount = D(str(row["rate"])), D(str(row["amount"]))
            if rate <= 0 or (source == "BOOK" and (amount >= 0 or int(row.get("count", 1)) <= 0)):
                continue
            if source == "TRADE" and not now_ms - 3600000 <= int(row["mts"]) <= now_ms:
                continue
            identity = str(row.get("evidenceId") or row.get("id") or base.digest(row))
            key = source + ":" + identity
            if key in seen:
                continue
            seen.add(key)
            tokens = independent_evidence([row] if source == "TRADE" else [], [row] if source == "BOOK" else [], now_ms)
            evidence = next(iter(tokens), None)
            units.append(
                dict(
                    source=source,
                    evidenceId=evidence,
                    period=int(row["period"]),
                    rate=rate,
                    amount=abs(amount),
                    demandType=row.get("demandType", row.get("demand_type", "UNKNOWN")),
                )
            )
    return units


def _compatible_units(kind, period, rate, units):
    return [
        r for r in units if r["period"] <= period and r["rate"] >= rate and compatibility(kind, r) != "INCOMPATIBLE"
    ]


def _holding_curve(model, kind, period):
    rows = tuple(
        (r["hours"], r["event"])
        for r in model.get("typeHoldings", {}).get(kind, [])
        if pool_for_period(r["period"]) == pool_for_period(period)
    )
    return base._holding_curve(rows)


def _hold(curve, period, uniform, stress=False):
    if stress:
        return 1.0
    times, survival = curve
    index = bisect_right(survival, -uniform)
    return min(period * 24.0, max(1 / 3600, times[index])) if index < len(times) else period * 24.0


def residual_wait(hazards, age, uniform, tail_flow, amount, queue, limit):
    """Draw S(age+t)/S(age); never redraw an old survivor from time zero."""
    target, elapsed = -math.log(max(1e-15, 1 - uniform)), 0.0
    for h, lo, hi in zip(hazards, base.WAIT_BINS, base.WAIT_BINS[1:]):
        length = max(0.0, hi - max(lo, age))
        if not length:
            continue
        intensity = -math.log(max(1e-15, 1 - h)) / (hi - lo)
        if intensity > 0 and target <= intensity * length:
            wait = elapsed + target / intensity
            return wait if wait <= limit else None
        target -= intensity * length
        elapsed += length
    if tail_flow > 0:
        wait = elapsed + target * (queue + float(amount)) / tail_flow
        return wait if wait <= limit else None
    return None


@lru_cache(maxsize=24)
def _paths(seed, currency, days, frr_days, volumes, current, current_frr, current_volume=None):
    anchor, frr_anchor = days[-1], frr_days[-1] if frr_days and frr_days[-1] > 0 else 1
    paths = []
    for index in range(base.PATHS):
        rng = random.Random(int(base.digest({"seed": seed, "currency": currency})[:12], 16) + index)
        path = []
        selected = rng.randrange(len(days))
        for day in range(base.HORIZON):
            if day == 0:
                # The decision's current point is observed, not a randomly
                # sampled future price that the cash alternative could know.
                path.append((current, current_frr, volumes[-1] if current_volume is None else current_volume))
                continue
            if day % 2 == 0:
                selected = rng.randrange(len(days))
            point = min(selected + day % 2, len(days) - 1)
            frr = current_frr * frr_days[min(point, len(frr_days) - 1)] / frr_anchor if frr_days else 0
            path.append((current * days[point] / anchor, frr, volumes[min(point, len(volumes) - 1)]))
        paths.append(tuple(path))
    return tuple(paths)


def _earned(kind, raw, rate, path, start, end, net_fee):
    if end <= start:
        return 0.0
    if kind == "LIMIT":
        return rate * (end - start) / 24 * net_fee
    fixed = path[min(int(start / 24), base.HORIZON - 1)][1] + raw
    earned, cursor = 0.0, start
    while cursor < end:
        boundary = min(end, (int(cursor / 24) + 1) * 24)
        floating = path[min(int(cursor / 24), base.HORIZON - 1)][1] + raw
        earned += max(0, fixed if kind == "FRR_DELTA_FIXED" else floating) * (boundary - cursor) / 24 * net_fee
        cursor = boundary
    return earned


def _interest_p10(values):
    # Comparing equivalent cashflows must not reject them because of binary
    # roundoff. Use the same 1e-8 monetary precision as executable principal.
    return float(D(str(base.quantile(values, 0.1))).quantize(SATOSHI))


def _prefix_at(prefix, hour):
    if hour >= HOURS:
        return prefix[-1]
    day = max(0, int(hour / 24))
    return prefix[day] + (prefix[day + 1] - prefix[day]) * ((hour % 24) / 24)


def _waiting_spans(hs, initial_intensity):
    return tuple(
        (lo / 60, hi / 60, -math.log(max(1e-15, 1 - h)) / ((hi - lo) / 60))
        for h, lo, hi in zip(hs, base.WAIT_BINS, base.WAIT_BINS[1:])
    ) + ((24, HOURS, initial_intensity),)


def _matched_hour(prefix, at, initial_intensity, hs, threshold, spans=None):
    """Use the same own-order survival evidence for every renewal's waiting."""
    spans = _waiting_spans(hs, initial_intensity) if spans is None else spans
    for lo, hi, hazard in spans:
        begin, end = at + lo, min(HOURS, at + hi)
        if begin >= HOURS:
            return None
        scale = hazard / initial_intensity if initial_intensity > 0 else 0
        integrated = (_prefix_at(prefix, end) - _prefix_at(prefix, begin)) * scale
        if scale > 0 and threshold <= integrated:
            target = _prefix_at(prefix, begin) + threshold / scale
            day = bisect_right(prefix, target) - 1
            if day >= base.HORIZON:
                return None
            intensity = prefix[day + 1] - prefix[day]
            return max(at + 1 / 60, day * 24 + (target - prefix[day]) / intensity * 24)
        threshold -= integrated
    return None


@lru_cache(maxsize=24)
def _continuation(seed, currency, paths, floor_rate, net_fee, kind, curve, amount, queue, confidence, tables="{}"):
    """Cached hourly event baseline. Rounding release upward is conservative.

    Waiting branches earn zero. Filled branches accrue only through return/end;
    renewal uses the cash available after return, with another simulated wait.
    Candidate choice uses the current scenario point, never a future path value.
    """
    result = []
    deadline = _valuation_deadline.get() or time.monotonic() + VALUATION_BUDGET_SECONDS
    local = {"tables": json.loads(tables)}
    for index, path in enumerate(paths):
        _check_budget(deadline)
        values = [0.0] * (HOURS + 1)
        rng = random.Random(int(base.digest({"seed": seed, "currency": currency})[:12], 16) + index + 10000)
        uniforms = [(rng.random(), rng.random()) for _ in range(HOURS)]
        events = 0
        prefixes, daily_waits = {}, {}
        for at in range(HOURS - 1, -1, -1):
            events += 1
            if events % 64 == 0:
                _check_budget(deadline, events)
            price, frr, volume = path[int(at / 24)]
            raw = 1e-8 if kind == "FRR_DELTA_VARIABLE" else 0
            quote = price if kind == "LIMIT" else frr + raw
            flow = volume / 1440 * (1 if confidence == "CALIBRATED" else 0.25)
            if kind == "NONE" or quote < floor_rate or flow <= 0:
                values[at] = values[at + 1]
                continue
            # Once quoted, principal remains reserved until an actual simulated
            # matching event. No implicit hourly cancel/refund is permitted.
            key = quote if kind == "LIMIT" else None
            if key not in prefixes:
                prefix = [0.0]
                for future_price, future_frr, future_volume in path:
                    compatible = future_price >= quote if kind == "LIMIT" else future_frr + raw >= floor_rate
                    intensity = future_volume * (1 if confidence == "CALIBRATED" else 0.25) / max(queue + amount, 1)
                    prefix.append(prefix[-1] + (intensity if compatible else 0))
                prefixes[key] = prefix
            prefix = prefixes[key]
            wait_key = (quote, price, flow)
            if wait_key not in daily_waits:
                initial_intensity = flow * 60 / max(queue + amount, 1)
                prior = [
                    1 - math.exp(-(hi - lo) / 60 * initial_intensity)
                    for lo, hi in zip(base.WAIT_BINS, base.WAIT_BINS[1:])
                ]
                condition = base.condition(2, D(str(quote)), D(str(price)), queue / max(amount, flow * 60, 1), "flat")
                hs = base.hazards(local, 2, condition, prior)
                daily_waits[wait_key] = (initial_intensity, hs, _waiting_spans(hs, initial_intensity))
            initial_intensity, hs, spans = daily_waits[wait_key]
            wait_uniform, hold_uniform = uniforms[at]
            start = _matched_hour(prefix, at, initial_intensity, hs, -math.log(max(1e-15, 1 - wait_uniform)), spans)
            if start is None:
                values[at] = 0.0
                continue
            if start >= HOURS:
                values[at] = 0.0
                continue
            hold = _hold(curve, 2, hold_uniform)
            end = min(HOURS, start + hold)
            earned = _earned(kind, raw, quote, path, start, end, net_fee)
            returned = min(HOURS, math.ceil(end))
            values[at] = earned + values[returned]
        result.append(tuple(values))
    return tuple(result)


def _future_inputs(policy, model, market, book, amount, current_frr, trades=()):
    days = tuple(float(r["rate"]) for r in model["days"] if D(str(r["rate"])) > 0)
    # Historical observations are joined by date, not by list position. An
    # absent FRR day can only inherit a previously observed value, never a
    # later observation. Zero before the first observation remains unknown.
    observed = sorted((int(r["mts"]) // base.DAY, float(r["rate"])) for r in model.get("frrDays", []))
    frr_days, cursor, last_frr = [], 0, 0.0
    for row in model["days"]:
        if D(str(row["rate"])) <= 0:
            continue
        day = int(row["mts"]) // base.DAY
        while cursor < len(observed) and observed[cursor][0] <= day:
            last_frr = observed[cursor][1]
            cursor += 1
        frr_days.append(last_frr)
    frr_days = tuple(frr_days)
    rows = model.get("demandDays", [])
    volumes_by_day = {}
    for row in rows:
        if int(row["period"]) <= 2:
            day = int(row["mts"]) // base.DAY
            volumes_by_day[day] = volumes_by_day.get(day, 0) + float(row["amount"])
    volumes = tuple(volumes_by_day.get(int(r["mts"]) // base.DAY, 0) for r in model["days"] if D(str(r["rate"])) > 0)
    current = float(market.reference(2, base.DAY) or market.reference(2, 7 * base.DAY) or D(str(days[-1])))
    kind = next((k for k in TYPES if previous.enabled(policy, k)), "NONE")
    typed = getattr(market, "typed_markets", {})
    if kind not in typed:
        typed[kind] = base.CandidateMarket(
            [r for r in trades if compatibility(kind, r) != "INCOMPATIBLE"], market.now_ms
        )
        market.typed_markets = typed
    current_volume = typed[kind].flow(2, D(str(current))) * 1440
    paths = _paths(
        model["pathSeed"], policy.currency, days, frr_days, volumes, current, float(current_frr or 0), current_volume
    )
    curve = _holding_curve(model, kind, 2)
    if not hasattr(market, "baseline_queue"):
        market.baseline_queue = sum(
            float(abs(D(str(r["amount"]))))
            for r in book
            if D(str(r["amount"])) > 0 and 0 < D(str(r["rate"])) <= D(str(current))
        )
    queue = market.baseline_queue
    confidence = "CALIBRATED" if model.get("typeObservationCounts", {}).get(kind, 0) >= 20 else "LOW"
    token = _valuation_deadline.set(getattr(market, "deadline", time.monotonic() + VALUATION_BUDGET_SECONDS))
    try:
        continuation = _continuation(
            model["pathSeed"],
            policy.currency,
            paths,
            float(base.gross_floor(policy, 2)),
            1 - float(base.fee(policy)),
            kind,
            curve,
            float(amount),
            queue,
            confidence,
            json.dumps(model.get("typeTables", {}).get(kind, {}), sort_keys=True),
        )
    finally:
        _valuation_deadline.reset(token)
    return paths, continuation


def value_candidate(
    policy,
    model,
    period,
    rate,
    amount,
    trades,
    book,
    now_ms,
    age_minutes=0,
    cancel_minutes=0,
    stress=False,
    quote=None,
    market=None,
):
    quote = quote or dict(rate=rate, submitted_rate=rate, offer_type="LIMIT", display_type="LIMIT")
    kind = quote["display_type"]
    market = market or base.CandidateMarket(trades, now_ms)
    deadline = getattr(market, "deadline", time.monotonic() + VALUATION_BUDGET_SECONDS)
    _check_budget(deadline)
    # Typed observations can exclude a native lane. UNKNOWN observations remain
    # uncertain shared prior; fixed public flow is never certain FRR demand.
    typed = getattr(market, "typed_markets", {})
    if kind not in typed:
        typed[kind] = base.CandidateMarket([r for r in trades if compatibility(kind, r) != "INCOMPATIBLE"], now_ms)
        market.typed_markets = typed
    type_market = typed[kind]
    confidence = "CALIBRATED" if model.get("typeObservationCounts", {}).get(kind, 0) >= 20 else "LOW"
    flow = type_market.flow(period, rate)
    if quote.get("passive") and flow <= 0:
        rows = [r for r in model.get("demandDays", []) if int(r["period"]) <= period and D(str(r["rate"])) >= rate]
        day_count = len({int(r["mts"]) // base.DAY for r in model.get("demandDays", [])})
        flow = sum(float(r["amount"]) for r in rows) / max(1, day_count) / 1440 * 0.25
    flow *= 1 if confidence == "CALIBRATED" else 0.25
    queue = sum(
        float(abs(D(str(r["amount"])))) for r in book if D(str(r["amount"])) > 0 and 0 < D(str(r["rate"])) <= rate
    )
    if stress:
        queue *= 2
        flow *= 0.5
    reference = type_market.reference(period, base.DAY)
    near = type_market.reference(period, 3600000) or reference
    trend = "up" if near > reference * D("1.05") else "down" if near < reference * D(".95") else "flat"
    key = base.condition(period, rate, reference, queue / max(float(amount), flow * 60, 1), trend)
    prior = [
        1 - math.exp(-(hi - lo) * flow / max(queue + float(amount), 1))
        for lo, hi in zip(base.WAIT_BINS, base.WAIT_BINS[1:])
    ]
    local = {"tables": model.get("typeTables", {}).get(kind, {})}
    hs = base.hazards(local, period, key, prior) if flow > 0 else [0.0] * len(prior)
    passive = bool(quote.get("passive"))
    lease = float(quote.get("leaseMinutes", getattr(policy, "passive_wait_minutes", 60)))
    limit = max(0, min(MAX_PASSIVE_MINUTES, lease)) if passive else HOURS * 60
    current_frr = rate - D(str(quote["submitted_rate"])) if kind != "LIMIT" else quote.get("current_frr", D(0))
    paths, continuation = _future_inputs(policy, model, market, book, amount, current_frr, trades)
    _check_budget(deadline)
    curve = _holding_curve(model, kind, period)
    seed = int(base.digest({"seed": model["pathSeed"], "currency": policy.currency})[:12], 16)
    interests, waits, holds, cycle_interests, cycle_times, holding_aprs = [], [], [], [], [], []
    for index, path in enumerate(paths):
        _check_budget(deadline, index)
        rng = random.Random(seed + index + 10000)
        wait = residual_wait(hs, age_minutes, rng.random(), flow, amount, queue, max(0, limit - cancel_minutes))
        start = (cancel_minutes + wait) / 60 if wait is not None else None
        if start is None:
            # An expired lease actually returns cash. An ordinary KEEP does not.
            release = min(HOURS, math.ceil((cancel_minutes + limit) / 60)) if passive else HOURS
            earned = continuation[index][release] * float(amount) if passive else 0.0
            interests.append(earned)
            waits.append(None)
            holds.append(None)
            cycle_interests.append(0.0)
            cycle_times.append(max(1 / 3600, (cancel_minutes + limit) / 60))
            continue
        hold = _hold(curve, period, rng.random(), stress)
        end = min(HOURS, start + hold)
        first = _earned(
            kind, float(quote["submitted_rate"]), float(rate), path, start, end, 1 - float(base.fee(policy))
        ) * float(amount)
        returned = min(HOURS, math.ceil(end))
        interests.append(first + continuation[index][returned] * float(amount))
        waits.append(wait + cancel_minutes)
        holds.append(hold)
        cycle_interests.append(first)
        cycle_times.append(max(end, 1 / 3600))
        if end > start:
            holding_aprs.append(first * 365 * 24 / (float(amount) * (end - start)))
    score = sum(cycle_interests) * 365 * 24 / (float(amount) * max(sum(cycle_times), 1e-12))
    result = dict(quote)
    result.update(
        period=period,
        amount=amount,
        rate=rate,
        condition=key,
        netApr=rate * 365 * (1 - base.fee(policy)),
        cycleNetApr=score,
        cycleEfficiencyNetApr=score,
        expectedNetInterest=sum(interests) / base.PATHS,
        p10NetInterest=base.quantile(interests, 0.1),
        conservativeNetApr=base.quantile(interests, 0.1) * 365 / (float(amount) * base.HORIZON),
        pathInterests=interests,
        pathWaitMinutes=waits,
        pathHoldingHours=holds,
        firstCycleInterests=cycle_interests,
        firstCycleHours=cycle_times,
        expectedFillProbability=sum(w is not None for w in waits) / base.PATHS,
        expectedWaitMinutes=sum(w for w in waits if w is not None) / max(1, sum(w is not None for w in waits)),
        expectedHoldingHours=sum(h for h in holds if h is not None) / max(1, sum(h is not None for h in holds)),
        queueVolumeEstimate=queue,
        confidence=confidence,
        floatingRate=quote["offer_type"] == "FRRDELTAVAR",
        demandCompatibility=quote.get("demandCompatibility", "UNKNOWN"),
        continuationPolicy="EVENT_SHORT_RENEWAL_V1",
        censoredFraction=sum(w is None for w in waits) / base.PATHS,
    )
    result["p10IdleInterestGain"] = _interest_p10(
        [earned - continuation[i][0] * float(amount) for i, earned in enumerate(interests)]
    )
    if kind != "LIMIT":
        result["p10HoldingNetApr"] = base.quantile(holding_aprs, 0.1)
        result["rateRiskNote"] = "FRR预测不能锁定未来最低年化；公共类型与Delta撮合边界未知"
        if result["p10HoldingNetApr"] < float(base.floor(policy, period)):
            result.update(expectedFillProbability=0, typeBlockReason="FRR保守持有收益低于净年化底线")
    return result


def _base_result(account, policy):
    total, wallet = D(account["total"]), D(account["wallet"])
    exposure = D(account.get("existingExposure", {}).get("total", sum(account.get("exposure", {}).values())))
    cap = min(
        total * policy.max_lend_percent / 100, policy.max_lend_amount if policy.max_lend_amount is not None else total
    )
    return dict(
        engine=ENGINE,
        algorithm=VERSION,
        plan=[],
        candidates=[],
        planned_amount=D(0),
        idle_amount=wallet,
        funding_cap=cap,
        existing_exposure=exposure,
        cap_remaining=max(D(0), cap - exposure),
        cap_limited_available=min(wallet, max(D(0), cap - exposure)),
        over_cap=exposure > cap,
        variable_amount=D(0),
        hidden_amount=D(0),
        rebalance_cancellations=[],
        empty_reason=None,
        modelId=policy.model_id,
        allocationModel=VERSION,
        allocationBasis="资金循环效率及120天悲观收益保护",
        pool_allocation={},
        target_offer_amounts={},
        current_offer_amounts={},
        deviation_amounts={},
        periodSelection={},
        absorbed_remainder=D(0),
        target_slice_count=0,
        diagnostics=[],
    )


def build_plan(account, policy, model, book, trades, now_ms, strategy_version, current_frr=None):
    result = _base_result(account, policy)
    try:
        base.validate_adaptive(policy)
        base.validate_model(model or {}, policy.currency, now_ms)
        _validate_model_features(model)
        if model["algorithm"] != VERSION or model["id"] != policy.model_id:
            raise ValueError("V4.2模型与策略不匹配")
        if any(previous.enabled(policy, k) for k in TYPES[1:]) and (
            current_frr is None or current_frr <= 0 or len(model.get("frrDays", [])) < 20
        ):
            raise ValueError("V4.2需要新鲜FRR及至少20天真实FRR历史")
        market = base.CandidateMarket(trades, now_ms)
        market.deadline = time.monotonic() + VALUATION_BUDGET_SECONDS
        minimum, budget = funding_minimum(), result["cap_limited_available"]
        units = demand_units(book, trades, now_ms)
        terms = sorted(
            (
                {
                    *base.TERMS,
                    *(int(r["period"]) for r in book),
                    *(int(r["period"]) for r in account.get("managedOffers", [])),
                }
            )
            & set(range(2, policy.maximum_period + 1))
        )
        candidates = []
        for term in terms:
            rates = {
                base.gross_floor(policy, term),
                *(market.reference(term, 7 * base.DAY, q) for q in (0.25, 0.5, 0.75, 0.9)),
            }
            rates.update(r["rate"] for r in units if r["period"] <= term)
            rates.update(
                D(str(r.get("rate_real") or r["rate"])) for r in account.get("managedOffers", []) if r["period"] == term
            )
            for rate in sorted({ceil_rate_tick(r) for r in rates if r >= base.gross_floor(policy, term)}):
                for quote in previous.quotes(policy, term, rate, current_frr):
                    eligible = _compatible_units(quote["display_type"], term, quote["rate"], units)
                    known = any(compatibility(quote["display_type"], r) == "KNOWN" for r in eligible)
                    calibrated = model.get("typeObservationCounts", {}).get(quote["display_type"], 0) >= 20
                    quote.update(
                        passive=not eligible or (not known and not calibrated),
                        leaseMinutes=policy.passive_wait_minutes,
                        current_frr=current_frr or D(0),
                        evidenceWatermark=sorted(r["evidenceId"] for r in eligible if r["evidenceId"]),
                        demandCompatibility="KNOWN" if known else "UNKNOWN",
                    )
                    value = value_candidate(
                        policy, model, term, quote["rate"], minimum, trades, book, now_ms, quote=quote, market=market
                    )
                    if (
                        value["expectedFillProbability"] > 0
                        and value["cycleNetApr"] > 0
                        and value["p10IdleInterestGain"] >= 0
                    ):
                        candidates.append(value)
        short = max((c["conservativeNetApr"] for c in candidates if c["period"] <= 7), default=0)
        candidates = [
            c for c in candidates if c["period"] < policy.long_from_days or c["conservativeNetApr"] > short + 0.005
        ]
        candidates = list({(c["period"], c["offer_type"], c["submitted_rate"]): c for c in candidates}.values())
        candidates.sort(key=lambda c: (-c["cycleNetApr"], -c["conservativeNetApr"], c["period"], c["rate"]))
        result["candidates"] = candidates
        slots = max(0, 8 - int(account.get("openOfferCount", 0)))
        long_used = sum(
            (D(v) for p, v in account.get("exposureByPeriod", {}).items() if int(p) >= policy.long_from_days), D(0)
        )
        variable_used = D(account.get("existingExposure", {}).get("variable", 0))
        states = account.get("passiveLeaseStates", [])
        passive_used = any(
            bool(s.get("active", True)) for s in (states.values() if isinstance(states, dict) else states)
        )
        quarantines = account.get("passiveQuarantines", {})
        allocated = {}
        while budget >= minimum and slots:
            _check_budget(market.deadline)
            selected = None
            for c in candidates:
                key = (c["period"], c["offer_type"], c["submitted_rate"])
                key_digest = quote_key(policy.currency, c)
                quarantine = quarantines.get(key_digest, {}) if isinstance(quarantines, dict) else {}
                after = int(quarantine.get("closedAtMs", 0))
                fresh = set(c.get("evidenceWatermark", [])) & independent_evidence(trades, book, now_ms, after) - set(
                    quarantine.get("evidenceWatermark", [])
                )
                if (
                    c["expectedFillProbability"] <= 0
                    or c["cycleNetApr"] <= 0
                    or (c.get("passive") and (passive_used or quarantine and not fresh))
                ):
                    continue
                compatible = _compatible_units(c["display_type"], c["period"], c["rate"], units)
                capacity = minimum if c.get("passive") else sum((r["amount"] for r in compatible), D(0))
                if c["period"] >= policy.long_from_days:
                    capacity = min(capacity, max(D(0), D(account["total"]) * policy.long_max_share / 100 - long_used))
                if c["floatingRate"]:
                    capacity = min(
                        capacity, max(D(0), D(account["total"]) * policy.variable_max_share / 100 - variable_used)
                    )
                if capacity >= minimum and (key in allocated or len(allocated) < slots):
                    selected = c, key, compatible, capacity
                    break
            if selected is None:
                break
            c, key, compatible, capacity = selected
            amount = min(minimum, budget, capacity)
            if not c.get("passive") and 0 < budget - amount < minimum and capacity >= budget:
                amount = budget
            allocated[key] = (c, allocated.get(key, (None, D(0)))[1] + amount)
            remaining = amount
            for unit in compatible:
                used = min(remaining, unit["amount"])
                unit["amount"] -= used
                remaining -= used
            budget -= amount
            long_used += amount if c["period"] >= policy.long_from_days else 0
            variable_used += amount if c["floatingRate"] else 0
            passive_used = passive_used or c.get("passive", False)
            # All candidates see the same remaining shared UNKNOWN budget.
            book = [*book, dict(rate=c["rate"], period=c["period"], amount=amount)]
            for candidate in candidates:
                candidate.update(
                    value_candidate(
                        policy,
                        model,
                        candidate["period"],
                        candidate["rate"],
                        minimum,
                        trades,
                        book,
                        now_ms,
                        quote=candidate,
                        market=market,
                    )
                )
            candidates.sort(key=lambda v: (-v["cycleNetApr"], v["period"]))
        for index, (c, amount) in enumerate(allocated.values()):
            result["plan"].append(
                dict(
                    period=c["period"],
                    amount=amount.quantize(SATOSHI),
                    submitted_rate=c["submitted_rate"],
                    effective_rate=c["rate"],
                    offer_type=c["offer_type"],
                    display_type=c["display_type"],
                    flags=0,
                    hidden=False,
                    pool=pool_for_period(c["period"]),
                    layer="balanced",
                    slice_index=index,
                    strategy_variant=ENGINE,
                    pricing_curve_version=VERSION,
                    fixed_landing_rate=None,
                    passive=c.get("passive", False),
                    leaseMinutes=policy.passive_wait_minutes if c.get("passive") else None,
                    evidenceWatermark=c.get("evidenceWatermark", []),
                    demandCompatibility=c.get("demandCompatibility", "UNKNOWN"),
                    source="MODEL_PASSIVE_ARRIVAL" if c.get("passive") else "SHARED_DEMAND_ESTIMATE",
                    quoteKey=quote_key(policy.currency, c),
                )
            )
        result.update(confidence=model["confidence"], dataCoverage=model["coverage"], eligibleForLiveCandidate=False)
        result["planned_amount"] = sum((r["amount"] for r in result["plan"]), D(0))
        result["variable_amount"] = sum((r["amount"] for r in result["plan"] if r["offer_type"] == "FRRDELTAVAR"), D(0))
        result["idle_amount"] = D(account["wallet"]) - result["planned_amount"]
        result["target_slice_count"] = len(result["plan"])
        result["empty_reason"] = None if result["plan"] else "WAIT_FOR_VALUE"
        if result["idle_amount"] > 0:
            result["diagnostics"].append("余额、收益底线、共享需求上界、长期/浮动上限或被动租约预算限制")
    except (ValueError, KeyError, ArithmeticError) as exc:
        result.update(
            plan=[],
            planned_amount=D(0),
            idle_amount=D(account["wallet"]),
            empty_reason="VALUATION_INCOMPLETE"
            if isinstance(exc, ValuationIncomplete)
            else "MODEL_OR_DATA_UNAVAILABLE",
            blockReasons=[str(exc)],
        )
    result["plan_hash"] = base.digest(
        {
            "currency": policy.currency,
            "policy": policy.__dict__,
            "model": policy.model_id,
            "account": account,
            "plan": result["plan"],
            "reason": result["empty_reason"],
        }
    )
    return result


def adjustment(policy, model, offer, candidates, trades, book, now_ms, current_frr=None, market=None):
    if not offer.get("managed"):
        return dict(action="KEEP", reason="EXTERNAL_OFFER")
    kind, amount = previous.display_type(offer), D(str(offer["amount"]))
    raw = D(str(offer.get("submitted_rate", offer.get("rate", 0))))
    rate = raw if kind == "LIMIT" else current_frr + raw if current_frr is not None else None
    if rate is None:
        return dict(action="KEEP", reason="FRR_DATA_UNAVAILABLE")
    period = int(offer["period"])
    hard = (
        not previous.enabled(policy, kind)
        or rate < base.gross_floor(policy, period)
        or not 2 <= period <= policy.maximum_period
        or bool(int(offer.get("flags", 0)))
    )
    lease = offer.get("passiveLease") or offer.get("lease") or {}
    expired = bool(lease) and now_ms >= int(lease.get("expiresAtMs", now_ms + 1))
    age = max(0, (now_ms - int(offer.get("mts_created") or now_ms)) / 60000)
    if hard:
        return dict(action="CANCEL", reason="HARD_FLOOR", hard=True)
    if amount < funding_minimum() or (age < max(5, policy.minimum_offer_minutes) and not expired):
        return dict(action="KEEP", reason="MINIMUM_AGE_OR_AMOUNT")
    try:
        market = market or base.CandidateMarket(trades, now_ms)
        quote = dict(
            rate=rate,
            submitted_rate=raw,
            offer_type=offer.get("offer_type", "LIMIT"),
            display_type=kind,
            current_frr=current_frr or D(0),
            passive=bool(lease),
            leaseMinutes=max(0, (int(lease.get("expiresAtMs", now_ms)) - now_ms) / 60000) if lease else 0,
        )
        current = value_candidate(
            policy, model, period, rate, amount, trades, book, now_ms, age_minutes=age, quote=quote, market=market
        )

        def executable_replacement(c):
            quoted_amount = min(amount, funding_minimum()) if c.get("passive") else amount
            result = value_candidate(
                policy,
                model,
                c["period"],
                c["rate"],
                quoted_amount,
                trades,
                book,
                now_ms,
                cancel_minutes=1,
                quote=c,
                market=market,
            )
            remainder = amount - quoted_amount
            if remainder > 0:
                # A passive replacement cannot absorb the old order's tail.
                # Unallocated cash earns zero, and remains occupied through
                # the horizon; do not simulate an illegal sub-minimum loan.
                occupied = float(quoted_amount) * sum(result["firstCycleHours"]) + float(remainder) * HOURS * base.PATHS
                score = sum(result["firstCycleInterests"]) * 365 * 24 / max(occupied, 1e-12)
                result.update(
                    cycleNetApr=score,
                    cycleEfficiencyNetApr=score,
                    conservativeNetApr=base.quantile(result["pathInterests"], 0.1)
                    * 365
                    / (float(amount) * base.HORIZON),
                    unallocatedCash=remainder,
                )
            return result

        choices = [executable_replacement(c) for c in candidates]
        choices = [c for c in choices if c["expectedFillProbability"] > 0]
        # Recheck the long-term premium on the current market and remaining
        # amount, not the minimum block used by an earlier preview.
        short_apr = max((c["conservativeNetApr"] for c in choices if c["period"] <= 7), default=0)
        choices = [
            c for c in choices if c["period"] < policy.long_from_days or c["conservativeNetApr"] > short_apr + 0.005
        ]
        if not choices:
            return dict(
                action="CANCEL" if expired else "KEEP",
                reason="LEASE_EXPIRED" if expired else "NO_BETTER_VALUE",
                hard=expired,
            )
        best = max(choices, key=lambda c: (c["cycleNetApr"], c["display_type"] == kind, -c["period"]))
        gain = best["cycleNetApr"] - current["cycleNetApr"]
        p10 = _interest_p10([a - b for a, b in zip(best["pathInterests"], current["pathInterests"])])
        same = (period, kind, raw) == (best["period"], best["display_type"], best["submitted_rate"])
        if lease and same:
            old = set(lease.get("evidenceWatermark", []))
            after = int(lease.get("startedAtMs", lease.get("chainStartMs", 0)))
            fresh = (set(best.get("evidenceWatermark", [])) & independent_evidence(trades, book, now_ms, after)) - old
            if (
                fresh
                and not best.get("passive")
                and best.get("demandCompatibility") == "KNOWN"
                and best["cycleNetApr"] > 0
                and p10 >= 0
            ):
                return dict(
                    action="KEEP",
                    reason="PASSIVE_TO_NORMAL",
                    evidenceWatermark=best["evidenceWatermark"],
                    cycleEfficiencyGain=gain,
                    p10InterestGain=p10,
                    horizonAprGain=best["conservativeNetApr"] - current["conservativeNetApr"],
                )
            cumulative = (
                now_ms - int(lease.get("chainStartMs", lease.get("chainStartedAtMs", lease.get("startedAtMs", now_ms))))
            ) / 60000
            if (
                expired
                and fresh
                and cumulative + policy.passive_wait_minutes <= MAX_PASSIVE_MINUTES
                and best["cycleNetApr"] > 0
                and p10 >= 0
            ):
                return dict(
                    action="KEEP",
                    reason="PASSIVE_LEASE_RENEW",
                    leaseMinutes=policy.passive_wait_minutes,
                    evidenceWatermark=best["evidenceWatermark"],
                    aprGain=gain,
                    p10InterestGain=p10,
                    horizonAprGain=best["conservativeNetApr"] - current["conservativeNetApr"],
                )
        if expired and (same or gain < float(policy.reprice_gain_apr) or p10 < 0):
            return dict(action="CANCEL", reason="LEASE_EXPIRED", hard=True)
        if same or gain < float(policy.reprice_gain_apr) or p10 < 0:
            return dict(
                action="KEEP",
                reason="QUEUE_VALUE",
                aprGain=gain,
                cycleEfficiencyGain=gain,
                p10InterestGain=p10,
                horizonAprGain=best["conservativeNetApr"] - current["conservativeNetApr"],
                remainingWaitMinutes=current["expectedWaitMinutes"],
                confidence=current["confidence"],
                expectedFillProbability=current["expectedFillProbability"],
            )
        return dict(
            action="CANCEL",
            reason="VALUE_GAIN",
            hard=False,
            targetPeriod=best["period"],
            targetRate=best["rate"],
            targetType=best["display_type"],
            targetSubmittedRate=best["submitted_rate"],
            aprGain=gain,
            cycleEfficiencyGain=gain,
            p10InterestGain=p10,
            horizonAprGain=best["conservativeNetApr"] - current["conservativeNetApr"],
            remainingWaitMinutes=current["expectedWaitMinutes"],
            confidence=current["confidence"],
            expectedFillProbability=current["expectedFillProbability"],
        )
    except ValuationIncomplete as exc:
        return dict(action="KEEP", reason="VALUATION_INCOMPLETE", blockReasons=[str(exc)])
