"""Pure, currency-local adaptive lending decisions. This module cannot trade."""

import hashlib
import json
import math
import random
from bisect import bisect_right
from collections import Counter
from dataclasses import replace
from decimal import Decimal
from functools import lru_cache

from Currency import funding_minimum, require_currency
from StrategyV3 import SATOSHI, ceil_rate_tick, json_decimal, pool_for_period

D = Decimal
ENGINE = "adaptive_net_yield_v1"
VERSION = "ADAPTIVE_NET_YIELD_V1"
MULTI_ENGINE = "adaptive_net_yield_v2"
MULTI_VERSION = "ADAPTIVE_NET_YIELD_V2"
ENGINES = (ENGINE, MULTI_ENGINE)
DAY = 86_400_000
HORIZON = 120
WAIT_BINS = (0, 5, 15, 30, 60, 120, 360, 720, 1440)
TERMS = (2, 7, 14, 30, 60, 90, 120)
PRIOR_WEIGHT = 20
PATHS = 64


def digest(value):
    encoded = json.dumps(json_decimal(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def adaptive_template(policy):
    return replace(
        policy,
        version=4,
        strategy_engine=ENGINE,
        short_floor_apr=D(".05"),
        medium_floor_apr=D(".05"),
        long_floor_apr=D(".10"),
        long_from_days=31,
        long_max_share=D("95"),
        maximum_period=120,
        model_id="",
        fee_verified=False,
        enable_limit=True,
        enable_frr=False,
        enable_frr_delta_fixed=False,
        enable_frr_delta_variable=False,
        enable_hidden=False,
        minimum_offer_minutes=5,
        reprice_cooldown_minutes=2,
        max_reprices_per_hour=12,
    )


def validate_adaptive(policy):
    if policy.strategy_engine not in ("legacy_v3", *ENGINES):
        raise ValueError("unknown strategy engine")
    if not 8 <= policy.long_from_days <= policy.maximum_period <= 120:
        raise ValueError("long term boundary/max term must satisfy 8 <= long <= max <= 120")
    if not 0 < policy.long_max_share <= 100:
        raise ValueError("long maximum share must be positive and <= 100")
    if policy.strategy_engine in ENGINES:
        if policy.version != 4:
            raise ValueError("adaptive engine requires V4")
        if any(v is None or v <= 0 for v in (policy.short_floor_apr, policy.medium_floor_apr, policy.long_floor_apr)):
            raise ValueError("adaptive floors must be positive")
        if policy.medium_floor_apr != policy.short_floor_apr or policy.long_floor_apr < policy.short_floor_apr:
            raise ValueError("adaptive medium floor equals base floor; long floor cannot be lower")
        if policy.strategy_engine == ENGINE and (
            not policy.enable_limit
            or any(
                (
                    policy.enable_frr,
                    policy.enable_frr_delta_fixed,
                    policy.enable_frr_delta_variable,
                    policy.enable_hidden,
                )
            )
        ):
            raise ValueError("adaptive V1 supports visible LIMIT only")
        if policy.strategy_engine == MULTI_ENGINE:
            if policy.enable_hidden or not any(
                (
                    policy.enable_limit,
                    policy.enable_frr,
                    policy.enable_frr_delta_fixed,
                    policy.enable_frr_delta_variable,
                )
            ):
                raise ValueError("V4.1 requires a visible funding type; Hidden is not supported")
    return policy


def fee(policy):
    # A user-editable flag is not evidence of an account fee discount.
    return max(D(".15"), policy.normal_fee_rate)


def floor(policy, period):
    return policy.long_floor_apr if period >= policy.long_from_days else policy.short_floor_apr


def gross_floor(policy, period):
    return ceil_rate_tick(floor(policy, period) / D(365) / (1 - fee(policy)))


def quantile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int((len(values) - 1) * q))] if values else 0.0


def weighted_rate(rows, q=0.5):
    ordered = sorted(rows, key=lambda r: D(str(r["rate"])))
    volume = sum(abs(D(str(r["amount"]))) for r in ordered)
    cumulative = D(0)
    for row in ordered:
        cumulative += abs(D(str(row["amount"])))
        if cumulative >= volume * D(str(q)):
            return D(str(row["rate"]))
    return D(0)


def condition(period, quote, reference, queue_ratio, trend):
    premium = float((quote - reference) * 365 * D(".85") * 100)
    price_bin = sum(premium > boundary for boundary in (0, 0.25, 1, 3))
    queue_bin = sum(queue_ratio > boundary for boundary in (1, 3, 10))
    return f"{pool_for_period(period)}|{price_bin}|{queue_bin}|{trend}"


def fit_model(currency, trades, observations=(), holdings=(), now_ms=0, coverage=None):
    """Right-censored, amount-fraction survival tables; no future observations."""
    require_currency(currency)
    valid = [r for r in trades if 0 < int(r["mts"]) <= now_ms and D(str(r["rate"])) > 0]
    by_day = {}
    for row in valid:
        by_day.setdefault(int(row["mts"]) // DAY, []).append(row)
    days = [
        {"mts": key * DAY, "rate": str(weighted_rate([r for r in rows if int(r["period"]) <= 7]))}
        for key, rows in sorted(by_day.items())
    ]
    tables = {}
    accepted = set()
    observations = list(observations)
    chain_counts = {}
    for obs in observations:
        key = obs.get("chainId")
        if key is not None:
            chain_counts[key] = chain_counts.get(key, 0) + 1
    for obs in observations:
        if obs.get("currency", currency) != currency:
            raise ValueError("cross-currency training observation")
        start, end = obs.get("start_ms"), obs.get("end_ms")
        if start is None or end is None or start > end or end > now_ms:
            continue
        keys = ("all", pool_for_period(obs["period"]), obs.get("condition", pool_for_period(obs["period"])))
        keys = tuple(dict.fromkeys(keys))
        amount = float(obs["amount"])
        if amount <= 0:
            continue
        accepted.add(obs.get("chainId", f"{start}:{obs.get('offerId', len(accepted))}"))
        fills = [(int(t), float(a)) for t, a in obs.get("fills", []) if start <= t <= end]
        for key in keys:
            table = tables.setdefault(key, [[0.0, 0.0] for _ in WAIT_BINS[1:]])
            for index, (lo, hi) in enumerate(zip(WAIT_BINS, WAIT_BINS[1:])):
                begin = start + lo * 60000
                if begin >= end:
                    continue
                remaining = max(0.0, 1 - sum(a / amount for t, a in fills if t < begin))
                # Censored intervals contribute proportional exposure, never a fake failure.
                risk = remaining * min(1.0, (end - begin) / ((hi - lo) * 60000))
                events = min(remaining, sum(a / amount for t, a in fills if begin <= t < start + hi * 60000))
                weight = 1 / chain_counts.get(obs.get("chainId"), 1)
                table[index][0] += risk * weight
                table[index][1] += events * weight
    hold_rows = []
    for row in holdings:
        if row.get("currency", currency) != currency:
            raise ValueError("cross-currency holding observation")
        opened = row.get("opened_ms")
        if opened is None or opened >= now_ms:
            continue
        closed = row.get("closed_ms")
        observed = min(int(closed or now_ms), now_ms)
        if observed < opened:
            continue
        hold_rows.append(
            {
                "period": int(row["period"]),
                "hours": (observed - opened) / 3600000,
                "event": bool(closed and closed <= now_ms),
            }
        )
    model = {
        "algorithm": VERSION,
        "currency": currency,
        "trainedUntilMs": now_ms,
        "validUntilMs": now_ms + 150 * DAY,
        "days": days,
        "tables": tables,
        "holdings": hold_rows,
        "ownObservationCount": len(accepted),
        "coverage": coverage or {},
        "confidence": "CALIBRATED" if len(accepted) >= 20 else "LOW",
        "eligibleForLiveCandidate": False,
    }
    model["pathSeed"] = digest(
        {key: model[key] for key in ("currency", "trainedUntilMs", "days", "tables", "holdings")}
    )
    model["id"] = digest(model)
    return model


def validate_model(model, currency, now_ms, allow_empty=False):
    if model.get("algorithm") not in (VERSION, MULTI_VERSION) or model.get("currency") != currency:
        raise ValueError("model algorithm/currency mismatch")
    body = {k: v for k, v in model.items() if k != "id"}
    if model.get("id") != digest(body):
        raise ValueError("model checksum mismatch")
    if model["trainedUntilMs"] > now_ms or (not model.get("days") and not allow_empty):
        raise ValueError("model contains future data or no market paths")
    if now_ms > model.get("validUntilMs", model["trainedUntilMs"] + 150 * DAY):
        raise ValueError("model expired")
    if any(r["mts"] > model["trainedUntilMs"] for r in model["days"]):
        raise ValueError("future market path")
    if model.get("algorithm") == MULTI_VERSION:
        from StrategyV41 import validate_frr_paths

        validate_frr_paths(model)
    return model


def hazards(model, period, key, prior):
    parent = model.get("tables", {}).get("all", [[0.0, 0.0] for _ in WAIT_BINS[1:]])
    group = model.get("tables", {}).get(pool_for_period(period), parent)
    cell = model.get("tables", {}).get(key, group)
    result = []
    for index, base in enumerate(prior):
        for rows in {id(rows): rows for rows in (parent, group, cell)}.values():
            risk, events = rows[index]
            base = (events + PRIOR_WEIGHT * base) / (risk + PRIOR_WEIGHT)
        result.append(max(0.0, min(1.0, base)))
    return result


@lru_cache(maxsize=64)
def _holding_curve(rows):
    counts = Counter(hours for hours, _event in rows)
    events = Counter(hours for hours, event in rows if event)
    risk, survival = len(rows), 1.0
    times, negatives = [], []
    for hours in sorted(counts):
        if events[hours]:
            survival *= 1 - events[hours] / risk
            times.append(hours)
            negatives.append(-survival)
        risk -= counts[hours]
    return tuple(times), tuple(negatives)


def holding_hours(model, period, u, stress=False):
    if stress:
        return min(1.0, period * 24.0)
    rows = tuple(
        (r["hours"], r["event"])
        for r in model.get("holdings", [])
        if pool_for_period(r["period"]) == pool_for_period(period)
    )
    if not rows:
        return period * 24.0
    times, negatives = _holding_curve(rows)
    index = bisect_right(negatives, -u)
    if index < len(times):
        return min(period * 24.0, max(1 / 3600, times[index]))
    return period * 24.0


@lru_cache(maxsize=16)
def rolling_paths(model_id, currency, days, net_fee, base_floor):
    seed = int(digest({"currency": currency, "model": model_id})[:12], 16)
    paths = []
    for path in range(PATHS):
        rng = random.Random(seed + path + 10000)
        curve = [0.0]
        for day in range(HORIZON):
            if day % 2 == 0:
                rate = days[rng.randrange(len(days))]
            # One hour re-lending delay per two-day rollover; not instant renewal.
            income = rate * net_fee if rate >= base_floor else 0.0
            curve.append(curve[-1] + income * (23 / 24 if day % 2 == 0 else 1))
        paths.append(tuple(curve))
    return tuple(paths)


def value_candidate(
    policy, model, period, rate, amount, trades, book, now_ms, age_minutes=0, cancel_minutes=0, stress=False, quote=None
):
    compatible = [r for r in trades if int(r["period"]) <= period and now_ms - DAY <= int(r["mts"]) <= now_ms]
    reference = weighted_rate(compatible)
    flow = float(sum(abs(D(str(r["amount"]))) for r in compatible if D(str(r["rate"])) >= rate)) / 1440
    queue = float(
        sum(abs(D(str(r["amount"]))) for r in book if D(str(r["amount"])) > 0 and 0 < D(str(r["rate"])) <= rate)
    )
    # An existing order has already waited; this is an estimate, not a FIFO claim.
    queue = max(0.0, queue - age_minutes * flow)
    ratio = queue / max(float(amount), flow * 60, 1.0)
    recent = [r for r in compatible if int(r["mts"]) >= now_ms - 3600000]
    near = weighted_rate(recent) or reference
    trend = "up" if near > reference * D("1.05") else "down" if near < reference * D(".95") else "flat"
    key = condition(period, rate, reference, ratio, trend)
    base_wait = (queue + float(amount)) / max(flow, 0.000001)
    if stress:
        base_wait *= 2
    prior = [1 - math.exp(-(hi - lo) / max(base_wait, 1.0)) for lo, hi in zip(WAIT_BINS, WAIT_BINS[1:])]
    hs = hazards(model, period, key, prior)
    days = tuple(float(r["rate"]) for r in model["days"] if r["mts"] <= now_ms)
    returns, waits, holds, probabilities = [], [], [], []
    path_waits, path_holds = [], []
    probability = 1 - math.prod(1 - h for h in hs)
    seed = int(digest({"currency": policy.currency, "model": model["id"]})[:12], 16)
    net_fee = 1 - float(fee(policy))
    path_seed = model.get("pathSeed", model["id"])
    seed = int(digest({"currency": policy.currency, "model": path_seed})[:12], 16)
    curves = rolling_paths(path_seed, policy.currency, days, net_fee, float(gross_floor(policy, 2)))
    for path in range(PATHS):
        rng = random.Random(seed + path)
        survival, wait = 1.0, None
        uniform = rng.random()
        for h, lo, hi in zip(hs, WAIT_BINS, WAIT_BINS[1:]):
            survival *= 1 - h
            if uniform > survival:
                wait = (lo + hi) / 2 + cancel_minutes
                break
        elapsed, earned = 0.0, 0.0
        hold = None
        if wait is not None:
            hold = holding_hours(model, period, rng.random(), stress)
            elapsed = wait / 1440
            duration = min(hold / 24, max(0.0, HORIZON - elapsed))
            earned += float(amount) * float(rate) * duration * net_fee
            elapsed += duration
            waits.append(wait)
            holds.append(hold)
        else:
            # The quote expires for re-evaluation after one day, rather than
            # assuming either instant execution or 120 days of forced idleness.
            elapsed = 1.0
        # Bootstrap complete historical day blocks, reinvesting only after return.
        if elapsed < HORIZON:
            whole = int(elapsed)
            accrued = curves[path][whole] + (elapsed - whole) * (curves[path][whole + 1] - curves[path][whole])
            earned += float(amount) * (curves[path][-1] - accrued)
        returns.append(earned)
        path_waits.append(wait)
        path_holds.append(hold)
        probabilities.append(probability)
    scale = 365 / (float(amount) * HORIZON)
    return {
        "period": period,
        "rate": rate,
        "amount": amount,
        "condition": key,
        "netApr": rate * 365 * (1 - fee(policy)),
        "expectedNetInterest": sum(returns) / PATHS,
        "p10NetInterest": quantile(returns, 0.1),
        "conservativeNetApr": quantile(returns, 0.1) * scale,
        "expectedFillProbability": sum(probabilities) / PATHS,
        "expectedWaitMinutes": sum(waits) / len(waits) if waits else None,
        "expectedHoldingHours": sum(holds) / len(holds) if holds else None,
        "pathInterests": returns,
        "pathWaitMinutes": path_waits,
        "pathHoldingHours": path_holds,
        "queueVolumeEstimate": queue,
        "confidence": model["confidence"],
    }


def build_plan(
    account, policy, model, book, trades, now_ms, strategy_version, *, valuator=value_candidate, quotes=None
):
    validate_adaptive(policy)
    total, available = D(account["total"]), D(account["wallet"])
    exposure = D(account.get("existingExposure", {}).get("total", sum(account["exposure"].values())))
    cap = min(
        total * policy.max_lend_percent / 100, policy.max_lend_amount if policy.max_lend_amount is not None else total
    )
    budget = min(available, max(D(0), cap - exposure))
    result = {
        "engine": policy.strategy_engine,
        "algorithm": MULTI_VERSION if policy.strategy_engine == MULTI_ENGINE else VERSION,
        "plan": [],
        "candidates": [],
        "planned_amount": D(0),
        "idle_amount": available,
        "funding_cap": cap,
        "existing_exposure": exposure,
        "cap_remaining": max(D(0), cap - exposure),
        "cap_limited_available": budget,
        "over_cap": exposure > cap,
        "variable_amount": D(0),
        "hidden_amount": D(0),
        "rebalance_cancellations": [],
        "empty_reason": None,
        "modelId": policy.model_id,
        "allocationModel": MULTI_VERSION if policy.strategy_engine == MULTI_ENGINE else VERSION,
        "allocationBasis": "120天本金时间净收益第10百分位",
        "pool_allocation": {},
        "target_offer_amounts": {},
        "current_offer_amounts": {},
        "deviation_amounts": {},
        "periodSelection": {},
        "absorbed_remainder": D(0),
        "target_slice_count": 0,
    }
    try:
        if model is None:
            raise ValueError("model missing")
        validate_model(model, policy.currency, now_ms)
        if model["algorithm"] != result["algorithm"]:
            raise ValueError("模型版本与策略引擎不匹配，请重新研究")
        if policy.model_id != model["id"]:
            raise ValueError("policy model differs from artifact")
        result.update(
            modelId=model["id"],
            confidence=model["confidence"],
            dataCoverage=model["coverage"],
            eligibleForLiveCandidate=model["eligibleForLiveCandidate"],
        )
    except ValueError as exc:
        result["empty_reason"] = "MODEL_UNAVAILABLE"
        result["blockReasons"] = [str(exc)]
        result["eligibleForLiveCandidate"] = False
        result["plan_hash"] = digest({"currency": policy.currency, "policy": policy.__dict__, "reason": str(exc)})
        return result
    live_trades = [r for r in trades if now_ms - 7 * DAY <= int(r["mts"]) <= now_ms]
    terms = sorted(
        {*TERMS, *(int(r["period"]) for r in live_trades), *(int(r["period"]) for r in book)}
        & set(range(2, policy.maximum_period + 1))
    )
    minimum = funding_minimum()
    candidates = []
    for term in terms:
        compatible = [r for r in live_trades if int(r["period"]) <= term]
        rates = {weighted_rate(compatible, q) for q in (0.25, 0.5, 0.75, 0.9)}
        bids = [D(str(r["rate"])) for r in book if D(str(r["amount"])) < 0 and int(r["period"]) <= term]
        asks = [D(str(r["rate"])) for r in book if D(str(r["amount"])) > 0 and int(r["period"]) >= term]
        rates.update((max(bids, default=D(0)), min(asks, default=D(0))))
        rates.update(
            D(str(row.get("rate_real") or row["rate"]))
            for row in account.get("managedOffers", [])
            if int(row["period"]) == term
        )
        # Only quotes supported by observed trades or the book are proposed.
        for rate in sorted({ceil_rate_tick(r) for r in rates if r >= gross_floor(policy, term)}):
            descriptors = (
                quotes(term, rate)
                if quotes
                else [{"rate": rate, "offer_type": "LIMIT", "display_type": "LIMIT", "submitted_rate": rate}]
            )
            for quote in descriptors:
                value = valuator(policy, model, term, quote["rate"], minimum, live_trades, book, now_ms, quote=quote)
                value.update(quote)
                if value["expectedFillProbability"] >= 0.01:
                    candidates.append(value)
    candidates = list({(r["period"], r["rate"], r["offer_type"], r["submitted_rate"]): r for r in candidates}.values())
    short = max(
        (r["conservativeNetApr"] for r in candidates if r["period"] <= 7), default=float(policy.short_floor_apr)
    )
    candidates = [
        r
        for r in candidates
        if r["conservativeNetApr"] > 0
        and (
            r["period"] <= 7 or r["conservativeNetApr"] > short + (0.005 if r["period"] >= policy.long_from_days else 0)
        )
    ]
    candidates.sort(key=lambda r: (-r["conservativeNetApr"], r["period"], r["rate"]))
    result["candidates"] = candidates
    demands = []
    for row in book:
        if D(str(row["amount"])) < 0 and D(str(row["rate"])) > 0:
            demands.append(
                {"period": int(row["period"]), "rate": D(str(row["rate"])), "amount": abs(D(str(row["amount"])))}
            )
    # Public flow is uncertain future capacity, conservatively bounded by one hour.
    for row in live_trades:
        if int(row["mts"]) >= now_ms - 3600000:
            demands.append(
                {"period": int(row["period"]), "rate": D(str(row["rate"])), "amount": abs(D(str(row["amount"])))}
            )
    long_used = (
        sum((D(v) for p, v in account.get("exposureByPeriod", {}).items() if int(p) >= policy.long_from_days), D(0))
        if account.get("exposureByPeriod") is not None
        else D(account.get("adaptiveLongExposure", account["exposure"].get("long", 0)))
    )
    long_limit = total * policy.long_max_share / 100
    allocated = {}
    free_slots = max(0, 8 - int(account.get("openOfferCount", 0)))
    while budget >= minimum and candidates and free_slots:
        choices = []
        for candidate in candidates:
            if candidate["period"] > 7 and candidate["conservativeNetApr"] <= short + (
                0.005 if candidate["period"] >= policy.long_from_days else 0
            ):
                continue
            eligible = [r for r in demands if r["period"] <= candidate["period"] and r["rate"] >= candidate["rate"]]
            capacity = sum((r["amount"] for r in eligible), D(0))
            if policy.strategy_engine == MULTI_ENGINE and candidate["offer_type"] == "FRRDELTAVAR":
                used_variable = D(account.get("existingExposure", {}).get("variable", 0)) + sum(
                    (amount for key, amount in allocated.items() if key[2] == "FRRDELTAVAR"), D(0)
                )
                capacity = min(capacity, max(D(0), total * policy.variable_max_share / 100 - used_variable))
            if candidate["period"] >= policy.long_from_days:
                capacity = min(capacity, max(D(0), long_limit - long_used))
            key = (candidate["period"], candidate["rate"], candidate["offer_type"], candidate["submitted_rate"])
            if capacity >= minimum and (key in allocated or len(allocated) < free_slots):
                choices.append((candidate, eligible, capacity))
        if not choices:
            break
        chosen, eligible, capacity = choices[0]
        amount = min(minimum, budget, capacity)
        if 0 < budget - amount < minimum and capacity >= budget:
            amount = budget
        key = (chosen["period"], chosen["rate"], chosen["offer_type"], chosen["submitted_rate"])
        allocated[key] = allocated.get(key, D(0)) + amount
        remaining = amount
        for row in eligible:
            used = min(row["amount"], remaining)
            row["amount"] -= used
            remaining -= used
        if chosen["period"] >= policy.long_from_days:
            long_used += amount
        budget -= amount
        # Marginal queue cost rises as our own planned supply accumulates.
        book = [*book, {"period": chosen["period"], "rate": chosen["rate"], "amount": amount}]
        for candidate in candidates:
            candidate.update(
                valuator(
                    policy,
                    model,
                    candidate["period"],
                    candidate["rate"],
                    minimum,
                    live_trades,
                    book,
                    now_ms,
                    quote=candidate,
                )
            )
        candidates.sort(key=lambda r: (-r["conservativeNetApr"], r["period"], r["rate"]))
    for index, ((period, rate, offer_type, submitted_rate), amount) in enumerate(allocated.items()):
        descriptor = next(
            r
            for r in candidates
            if (r["period"], r["rate"], r["offer_type"], r["submitted_rate"])
            == (period, rate, offer_type, submitted_rate)
        )
        result["plan"].append(
            {
                "period": period,
                "amount": amount.quantize(SATOSHI),
                "submitted_rate": submitted_rate,
                "effective_rate": rate,
                "offer_type": offer_type,
                "display_type": descriptor["display_type"],
                "flags": 0,
                "hidden": False,
                "pool": pool_for_period(period),
                "layer": "balanced",
                "slice_index": index,
                "strategy_variant": policy.strategy_engine,
                "pricing_curve_version": result["algorithm"],
                "fixed_landing_rate": None,
            }
        )
    result["planned_amount"] = sum((r["amount"] for r in result["plan"]), D(0))
    result["variable_amount"] = sum((r["amount"] for r in result["plan"] if r["offer_type"] == "FRRDELTAVAR"), D(0))
    result["target_slice_count"] = len(result["plan"])
    result["idle_amount"] = available - result["planned_amount"]
    result["empty_reason"] = None if result["plan"] else "WAIT_FOR_VALUE"
    result["plan_hash"] = digest(
        {
            "currency": policy.currency,
            "policy": policy.__dict__,
            "model": model["id"],
            "account": account,
            "plan": result["plan"],
        }
    )
    return result


def adjustment(policy, model, offer, candidates, trades, book, now_ms):
    amount, rate = D(str(offer["amount"])), D(str(offer.get("rate_real") or offer["rate"]))
    period = int(offer["period"])
    if not offer.get("managed"):
        return {"action": "KEEP", "reason": "EXTERNAL_OFFER"}
    hard = (
        rate < gross_floor(policy, period)
        or not 2 <= period <= policy.maximum_period
        or offer.get("offer_type") != "LIMIT"
        or int(offer.get("flags", 0)) != 0
    )
    age = max(0.0, (now_ms - int(offer.get("mts_created") or now_ms)) / 60000)
    if amount < funding_minimum():
        return {
            "action": "CANCEL" if hard else "KEEP",
            "reason": "HARD_FLOOR" if hard else "BELOW_REPOST_MINIMUM",
            "hard": hard,
        }
    if not hard and age < max(5, policy.minimum_offer_minutes):
        return {"action": "KEEP", "reason": "MINIMUM_AGE"}
    current = value_candidate(policy, model, period, rate, amount, trades, book, now_ms, age_minutes=age)
    choices = [
        value_candidate(policy, model, c["period"], c["rate"], amount, trades, book, now_ms, cancel_minutes=1)
        for c in candidates
    ]
    if not choices:
        return {
            "action": "CANCEL" if hard else "KEEP",
            "reason": "HARD_FLOOR" if hard else "NO_BETTER_VALUE",
            "hard": hard,
        }
    best = max(choices, key=lambda r: (r["conservativeNetApr"], -r["period"]))
    gains = [a - b for a, b in zip(best["pathInterests"], current["pathInterests"])]
    improvement = best["conservativeNetApr"] - current["conservativeNetApr"]
    worthwhile = improvement >= 0.0025 and quantile(gains, 0.1) > 0
    if not hard and (not worthwhile or (period == best["period"] and rate == best["rate"])):
        return {"action": "KEEP", "reason": "QUEUE_VALUE", "aprGain": improvement}
    return {
        "action": "CANCEL",
        "reason": "HARD_FLOOR" if hard else "VALUE_GAIN",
        "hard": hard,
        "targetPeriod": best["period"],
        "targetRate": best["rate"],
        "aprGain": improvement,
        "p10InterestGain": quantile(gains, 0.1),
    }
