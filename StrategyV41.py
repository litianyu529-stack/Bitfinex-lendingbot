"""V4.1 visible LIMIT and FRR-family net-yield comparison; no exchange client."""

import random
from dataclasses import replace
from decimal import Decimal as D
from functools import lru_cache

import StrategyV4 as base
from StrategyV3 import ceil_rate_tick

ENGINE = base.MULTI_ENGINE
VERSION = base.MULTI_VERSION
TYPES = ("LIMIT", "FRR", "FRR_DELTA_FIXED", "FRR_DELTA_VARIABLE")
FIELDS = dict(zip(TYPES, ("enable_limit", "enable_frr", "enable_frr_delta_fixed", "enable_frr_delta_variable")))


def template(policy):
    return replace(
        base.adaptive_template(policy),
        strategy_engine=ENGINE,
        enable_frr=True,
        enable_frr_delta_fixed=True,
        enable_frr_delta_variable=True,
    )


def display_type(offer):
    raw = str(offer.get("display_type") or offer.get("offer_type") or "LIMIT")
    if raw == "FRRDELTAVAR":
        return "FRR" if D(str(offer.get("submitted_rate", offer.get("rate", 0)))) == 0 else "FRR_DELTA_VARIABLE"
    return "FRR_DELTA_FIXED" if raw == "FRRDELTAFIX" else raw


def fit_model(
    currency, trades, observations=(), holdings=(), now_ms=0, coverage=None, frr=(), allow_frr_reference=False
):
    observations = list(observations)
    model = base.fit_model(currency, trades, observations, holdings, now_ms, coverage)
    days = {}
    for row in sorted(frr, key=lambda r: int(r["mts"])):
        if 0 < int(row["mts"]) <= now_ms and D(str(row["frr_daily_rate"])) > 0:
            days[int(row["mts"]) // base.DAY] = {"mts": int(row["mts"]), "rate": str(row["frr_daily_rate"])}
    model.update(
        algorithm=VERSION, frrDays=list(days.values()), typeTables={}, typeHoldings={}, typeObservationCounts={}
    )
    model["dataBasis"] = {"rollingRateSource": "PUBLIC_TRADES", "queueEvidence": "CURRENT_BOOK_ESTIMATE"}
    if allow_frr_reference and len({r["mts"] // base.DAY for r in model["days"] if D(r["rate"]) > 0}) < 20:
        model["days"] = [dict(mts=r["mts"], rate=r["rate"], source="FRR_HISTORY") for r in model["frrDays"]]
        model["dataBasis"]["rollingRateSource"] = "FRR_HISTORY"
        model["dataBasis"]["note"] = "FRR仅用于滚动收益情景，不是逐笔成交、排队或自身成交证据"
    for kind in TYPES:
        subset = [r for r in observations if r.get("displayType", "LIMIT") == kind]
        trained = base.fit_model(
            currency, trades, subset, [r for r in holdings if r.get("displayType") == kind], now_ms, coverage
        )
        model["typeTables"][kind] = trained["tables"]
        model["typeHoldings"][kind] = trained["holdings"]
        model["typeObservationCounts"][kind] = trained["ownObservationCount"]
    model["pathSeed"] = base.digest(
        {"seed": model["pathSeed"], "frrDays": model["frrDays"], "types": model["typeTables"], "days": model["days"]}
    )
    model.pop("id")
    model["id"] = base.digest(model)
    return model


def validate_frr_paths(model):
    if any(
        int(r["mts"]) > model["trainedUntilMs"] or not D(str(r["rate"])).is_finite() or D(str(r["rate"])) <= 0
        for r in model.get("frrDays", [])
    ):
        raise ValueError("FRR历史包含未来或无效数据")


def enabled(policy, kind):
    return kind in FIELDS and getattr(policy, FIELDS[kind])


def quotes(policy, period, rate, frr):
    floor = base.gross_floor(policy, period)
    result = []
    if policy.enable_limit and rate >= floor:
        result.append(dict(rate=rate, submitted_rate=rate, offer_type="LIMIT", display_type="LIMIT"))
    if frr is None or frr <= 0:
        return result
    if policy.enable_frr and frr >= floor:
        result.append(dict(rate=frr, submitted_rate=D(0), offer_type="FRRDELTAVAR", display_type="FRR"))
    delta = ceil_rate_tick(abs(rate - frr)) * (1 if rate >= frr else -1)
    if policy.enable_frr_delta_fixed and frr + delta >= floor:
        result.append(
            dict(rate=frr + delta, submitted_rate=delta, offer_type="FRRDELTAFIX", display_type="FRR_DELTA_FIXED")
        )
    if policy.enable_frr_delta_variable and delta > 0 and frr + delta >= floor:
        result.append(
            dict(rate=frr + delta, submitted_rate=delta, offer_type="FRRDELTAVAR", display_type="FRR_DELTA_VARIABLE")
        )
    return result


@lru_cache(maxsize=16)
def frr_paths(seed_id, currency, rates, current_frr):
    seed = int(base.digest({"currency": currency, "model": seed_id})[:12], 16)
    paths = []
    anchor = rates[-1]
    for index in range(base.PATHS):
        rng = random.Random(seed + index + 10000)
        path = []
        for day in range(base.HORIZON):
            if day % 2 == 0:
                selected = rng.randrange(len(rates))
            # Historical two-day blocks preserve within-block FRR changes.
            path.append(current_frr * rates[min(selected + day % 2, len(rates) - 1)] / anchor)
        paths.append(tuple(path))
    return tuple(paths)


def value_candidate(
    policy, model, period, rate, amount, trades, book, now_ms, age_minutes=0, cancel_minutes=0, stress=False, quote=None
):
    quote = quote or dict(rate=rate, offer_type="LIMIT", display_type="LIMIT", submitted_rate=rate)
    kind = quote["display_type"]
    local = {
        **model,
        "tables": model.get("typeTables", {}).get(kind, {}),
        "holdings": model.get("typeHoldings", {}).get(kind, []),
    }
    value = base.value_candidate(
        policy, local, period, rate, amount, trades, book, now_ms, age_minutes, cancel_minutes, stress
    )
    value["confidence"] = "CALIBRATED" if model.get("typeObservationCounts", {}).get(kind, 0) >= 20 else "LOW"
    value.update({key: quote[key] for key in ("rate", "submitted_rate", "offer_type", "display_type")})
    value["floatingRate"] = quote["offer_type"] == "FRRDELTAVAR"
    if kind == "LIMIT":
        return value
    rows = model.get("frrDays", [])
    current = float(quote["rate"] - quote["submitted_rate"])
    paths = frr_paths(model["pathSeed"], policy.currency, tuple(float(r["rate"]) for r in rows), current)
    net_fee = 1 - float(base.fee(policy))
    delta, fixed = float(quote["submitted_rate"]), quote["offer_type"] == "FRRDELTAFIX"
    hold_aprs = []
    for index, (wait, hold) in enumerate(zip(value["pathWaitMinutes"], value["pathHoldingHours"])):
        if wait is None:
            continue
        start = min(base.HORIZON, wait / 1440)
        end = min(base.HORIZON, start + hold / 24)
        rates = paths[index]
        at_fill = max(0.0, rates[min(int(start), base.HORIZON - 1)] + delta)
        earned, cursor = 0.0, start
        while cursor < end:
            next_at = min(end, int(cursor) + 1)
            floating = max(0.0, rates[min(int(cursor), base.HORIZON - 1)] + delta)
            earned += (at_fill if fixed else floating) * (next_at - cursor)
            cursor = next_at
        old = float(rate) * (end - start)
        value["pathInterests"][index] += float(amount) * net_fee * (earned - old)
        if end > start:
            hold_aprs.append(earned * net_fee * 365 / (end - start))
    value["expectedNetInterest"] = sum(value["pathInterests"]) / base.PATHS
    value["p10NetInterest"] = base.quantile(value["pathInterests"], 0.1)
    value["conservativeNetApr"] = value["p10NetInterest"] * 365 / (float(amount) * base.HORIZON)
    value["p10HoldingNetApr"] = base.quantile(hold_aprs, 0.1)
    if value["p10HoldingNetApr"] < float(base.floor(policy, period)):
        value["expectedFillProbability"] = 0
        value["typeBlockReason"] = "FRR路径的保守持有收益低于净年化底线"
    value["rateRiskNote"] = "FRR预测与底线检查不保证未来浮动利率始终达到底线"
    return value


def build_plan(account, policy, model, book, trades, now_ms, strategy_version, current_frr=None):
    requires_frr = any((policy.enable_frr, policy.enable_frr_delta_fixed, policy.enable_frr_delta_variable))
    if model and requires_frr and (len(model.get("frrDays", [])) < 20 or current_frr is None or current_frr <= 0):
        result = base.build_plan(account, policy, None, book, trades, now_ms, strategy_version)
        result.update(
            empty_reason="FRR_DATA_UNAVAILABLE",
            blockReasons=["V4.1需要至少20天真实FRR历史及新鲜FRR报价；禁止用成交价代替FRR"],
        )
        return result
    return base.build_plan(
        account,
        policy,
        model,
        book,
        trades,
        now_ms,
        strategy_version,
        valuator=value_candidate,
        quotes=lambda term, rate: quotes(policy, term, rate, current_frr),
    )


def adjustment(policy, model, offer, candidates, trades, book, now_ms, current_frr=None):
    kind, amount = display_type(offer), D(str(offer["amount"]))
    if not offer.get("managed"):
        return dict(action="KEEP", reason="EXTERNAL_OFFER")
    raw_rate = D(str(offer.get("submitted_rate", offer.get("rate", 0))))
    effective = raw_rate if kind == "LIMIT" else (current_frr + raw_rate if current_frr else None)
    if effective is None:
        return dict(action="KEEP", reason="FRR_DATA_UNAVAILABLE")
    period = int(offer["period"])
    hard = (
        not enabled(policy, kind)
        or effective < base.gross_floor(policy, period)
        or not 2 <= period <= policy.maximum_period
        or bool(int(offer.get("flags", 0)))
    )
    age = max(0, (now_ms - int(offer.get("mts_created") or now_ms)) / 60000)
    if amount < base.funding_minimum() or (not hard and age < max(5, policy.minimum_offer_minutes)):
        return dict(
            action="CANCEL" if hard else "KEEP", hard=hard, reason="HARD_FLOOR" if hard else "MINIMUM_AGE_OR_AMOUNT"
        )
    quote = dict(
        rate=effective, submitted_rate=raw_rate, offer_type=offer.get("offer_type", "LIMIT"), display_type=kind
    )
    current = value_candidate(
        policy, model, period, effective, amount, trades, book, now_ms, age_minutes=age, quote=quote
    )
    choices = [
        value_candidate(policy, model, r["period"], r["rate"], amount, trades, book, now_ms, cancel_minutes=1, quote=r)
        for r in candidates
    ]
    choices = [r for r in choices if r["expectedFillProbability"] > 0]
    if not choices:
        return dict(action="CANCEL" if hard else "KEEP", hard=hard, reason="HARD_FLOOR" if hard else "NO_BETTER_VALUE")
    best = max(choices, key=lambda r: (r["conservativeNetApr"], r["display_type"] == kind, -r["period"]))
    gain = best["conservativeNetApr"] - current["conservativeNetApr"]
    p10 = base.quantile([a - b for a, b in zip(best["pathInterests"], current["pathInterests"])], 0.1)
    if not hard and (
        gain < 0.0025
        or p10 <= 0
        or (period, kind, raw_rate) == (best["period"], best["display_type"], best["submitted_rate"])
    ):
        return dict(action="KEEP", reason="QUEUE_VALUE", aprGain=gain)
    return dict(
        action="CANCEL",
        hard=hard,
        reason="HARD_FLOOR" if hard else "VALUE_GAIN",
        targetPeriod=best["period"],
        targetRate=best["rate"],
        targetType=best["display_type"],
        targetSubmittedRate=best["submitted_rate"],
        aprGain=gain,
        p10InterestGain=p10,
    )
