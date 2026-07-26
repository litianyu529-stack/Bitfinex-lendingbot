import hashlib
import json
import math
import random
import time
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP


D = Decimal
SATOSHI = D("0.00000001")
RATE_TICK = D("0.0000001")
RATE_COMPARISON_EPSILON = D("0.000000000000000001")
POOLS = ("short", "medium", "long")
LAYERS = ("quick", "balanced", "high")
WINDOWS_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "6h": 21_600_000,
    "24h": 86_400_000,
    "7d": 604_800_000,
}
SCORE_MODEL_VERSION = "v3-score-2026-07-22"
V3_RESEARCH_SCORE_WEIGHTS = {
    "net_yield": D("45"),
    "fill_probability": D("15"),
    "wait_time": D("8"),
    "book_depth": D("5"),
    "trade_speed": D("4"),
    "utilization": D("3"),
    "trend_volatility": D("5"),
    "term_opportunity": D("5"),
    "hidden_cost": D("5"),
    "variable_risk": D("5"),
}


def _d(value, fallback="0"):
    if value in (None, ""):
        return D(fallback)
    return D(str(value))


def _bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _tuple_of_ints(value, fallback):
    if value in (None, ""):
        return tuple(fallback)
    if isinstance(value, str):
        value = value.split(",")
    return tuple(int(item) for item in value)


def ceil_rate_tick(value):
    value = D(value)
    if value <= 0:
        return D("0")
    ticks = (value / RATE_TICK).to_integral_value(rounding=ROUND_CEILING)
    return ticks * RATE_TICK


def rate_below_floor(rate, floor):
    return D(rate) + RATE_COMPARISON_EPSILON < D(floor)


@dataclass(frozen=True)
class StrategyPolicyV3:
    version: int = 3
    currency: str = "USD"
    short_share: D = D("50")
    medium_share: D = D("30")
    long_share: D = D("20")
    quick_share: D = D("40")
    balanced_share: D = D("40")
    high_share: D = D("20")
    short_floor_apr: D | None = None
    medium_floor_apr: D | None = None
    long_floor_apr: D | None = None
    short_periods: tuple[int, ...] = (2, 3, 5, 7)
    medium_periods: tuple[int, ...] = (8, 14, 21, 30)
    long_periods: tuple[int, ...] = (120,)
    target_slices: int = 50
    min_order_amount: D = D("150")
    max_lend_amount: D | None = None
    max_lend_percent: D = D("100")
    amount_jitter: D = D("0.03")
    max_pool_shift: D = D("10")
    normal_fee_rate: D = D("0.15")
    hidden_fee_rate: D = D("0.18")
    enable_limit: bool = True
    enable_frr: bool = True
    enable_frr_delta_fixed: bool = True
    enable_frr_delta_variable: bool = True
    variable_max_share: D = D("10")
    enable_hidden: bool = False
    adopt_external_offers: bool = False
    hidden_max_share: D | None = None
    minimum_offer_minutes: int = 10
    reprice_cooldown_minutes: int = 10
    max_reprices_per_hour: int = 6
    minimum_rate_change: D = D("0.00002")
    short_reprice_stages_minutes: tuple[int, ...] = (10, 30, 60)
    medium_reprice_stages_minutes: tuple[int, ...] = (20, 60, 120)
    long_reprice_stages_minutes: tuple[int, ...] = (60, 180, 360)
    iqr_change_fraction: D = D("0.25")
    spike_volume_ratio: D = D("1.5")
    outlier_min_volume_share: D = D("0.005")
    ws_fallback_seconds: int = 300
    rest_stale_seconds: int = 60
    market_retention_days: int = 90

    def floor_apr(self, pool):
        return getattr(self, f"{pool}_floor_apr")

    def periods(self, pool):
        return getattr(self, f"{pool}_periods")

    def pool_shares(self):
        return {pool: getattr(self, f"{pool}_share") for pool in POOLS}

    def layer_shares(self):
        return {layer: getattr(self, f"{layer}_share") for layer in LAYERS}

    def reprice_stages(self, pool):
        return getattr(self, f"{pool}_reprice_stages_minutes")


V3_FIELD_CONVERTERS = {
    "version": int,
    "currency": str,
    "short_share": _d,
    "medium_share": _d,
    "long_share": _d,
    "quick_share": _d,
    "balanced_share": _d,
    "high_share": _d,
    "short_floor_apr": lambda value: None if value in (None, "") else _d(value),
    "medium_floor_apr": lambda value: None if value in (None, "") else _d(value),
    "long_floor_apr": lambda value: None if value in (None, "") else _d(value),
    "short_periods": lambda value: _tuple_of_ints(value, (2, 3, 5, 7)),
    "medium_periods": lambda value: _tuple_of_ints(value, (8, 14, 21, 30)),
    "long_periods": lambda value: _tuple_of_ints(value, (120,)),
    "target_slices": int,
    "min_order_amount": _d,
    "max_lend_amount": lambda value: None if value in (None, "") else _d(value),
    "max_lend_percent": _d,
    "amount_jitter": _d,
    "max_pool_shift": _d,
    "normal_fee_rate": _d,
    "hidden_fee_rate": _d,
    "enable_limit": _bool,
    "enable_frr": _bool,
    "enable_frr_delta_fixed": _bool,
    "enable_frr_delta_variable": _bool,
    "variable_max_share": _d,
    "enable_hidden": _bool,
    "adopt_external_offers": _bool,
    "hidden_max_share": lambda value: None if value in (None, "") else _d(value),
    "minimum_offer_minutes": int,
    "reprice_cooldown_minutes": int,
    "max_reprices_per_hour": int,
    "minimum_rate_change": _d,
    "short_reprice_stages_minutes": lambda value: _tuple_of_ints(value, (10, 30, 60)),
    "medium_reprice_stages_minutes": lambda value: _tuple_of_ints(value, (20, 60, 120)),
    "long_reprice_stages_minutes": lambda value: _tuple_of_ints(value, (60, 180, 360)),
    "iqr_change_fraction": _d,
    "spike_volume_ratio": _d,
    "outlier_min_volume_share": _d,
    "ws_fallback_seconds": int,
    "rest_stale_seconds": int,
    "market_retention_days": int,
}


def policy_v3_with_overrides(base, values):
    updates = {}
    for name, converter in V3_FIELD_CONVERTERS.items():
        if name in values:
            updates[name] = converter(values[name])
    return replace(base, **updates)


def validate_policy_v3(policy, require_live_floors=False):
    if policy.version != 3:
        raise ValueError("strategy version must be 3")
    if policy.currency.upper() != "USD":
        raise ValueError("strategy v3 currently supports USD only")
    if sum(policy.pool_shares().values(), D("0")) != D("100"):
        raise ValueError("short, medium, and long shares must total 100")
    if sum(policy.layer_shares().values(), D("0")) != D("100"):
        raise ValueError("quick, balanced, and high shares must total 100")
    for name, value in {**policy.pool_shares(), **policy.layer_shares()}.items():
        if value < 0 or value > 100:
            raise ValueError(f"{name} share must be 0-100")
    floors = [policy.floor_apr(pool) for pool in POOLS]
    if any(value is not None and (value < 0 or value > D("10")) for value in floors):
        raise ValueError("net APR floors must be between 0 and 1000 percent")
    if require_live_floors and any(value is None or value <= 0 for value in floors):
        raise ValueError("LIVE requires positive short, medium, and long net APR floors")
    if policy.target_slices < 1 or policy.target_slices > 100:
        raise ValueError("target_slices must be 1-100")
    if policy.min_order_amount < 150:
        raise ValueError("USD min_order_amount must be at least 150")
    if policy.max_lend_amount is not None and policy.max_lend_amount < 0:
        raise ValueError("max_lend_amount must be non-negative or empty")
    if policy.max_lend_percent < 0 or policy.max_lend_percent > 100:
        raise ValueError("max_lend_percent must be 0-100")
    if policy.amount_jitter < 0 or policy.amount_jitter > D("0.10"):
        raise ValueError("amount_jitter must be 0-0.10")
    if policy.max_pool_shift < 0 or policy.max_pool_shift > 25:
        raise ValueError("max_pool_shift must be 0-25 percentage points")
    for pool in POOLS:
        periods = policy.periods(pool)
        if not periods or any(period < 2 or period > 120 for period in periods):
            raise ValueError(f"{pool} periods must be 2-120 days")
    if any(period > 7 for period in policy.short_periods):
        raise ValueError("short periods must be 2-7 days")
    if any(period < 8 or period > 30 for period in policy.medium_periods):
        raise ValueError("medium periods must be 8-30 days")
    if policy.long_periods != (120,):
        raise ValueError("long period must be exactly 120 days")
    for name in ("normal_fee_rate", "hidden_fee_rate"):
        value = getattr(policy, name)
        if value < 0 or value >= 1:
            raise ValueError(f"{name} must be a decimal fraction below 1")
    if policy.variable_max_share < 0 or policy.variable_max_share > 100:
        raise ValueError("variable_max_share must be 0-100")
    if policy.enable_hidden and (policy.hidden_max_share is None or policy.hidden_max_share <= 0):
        raise ValueError("hidden_max_share must be positive when Hidden is enabled")
    if policy.hidden_max_share is not None and not 0 <= policy.hidden_max_share <= 100:
        raise ValueError("hidden_max_share must be 0-100")
    if not any(
        (policy.enable_limit, policy.enable_frr, policy.enable_frr_delta_fixed, policy.enable_frr_delta_variable)
    ):
        raise ValueError("at least one funding offer type must be enabled")
    if sum(V3_RESEARCH_SCORE_WEIGHTS.values(), D("0")) != D("100"):
        raise ValueError("candidate score weights must total 100")
    if policy.minimum_offer_minutes < 1 or policy.reprice_cooldown_minutes < 1:
        raise ValueError("offer and reprice cooldown minutes must be positive")
    if policy.max_reprices_per_hour < 0 or policy.max_reprices_per_hour > 90:
        raise ValueError("max_reprices_per_hour must be 0-90")
    for pool in POOLS:
        stages = policy.reprice_stages(pool)
        if (
            len(stages) != 3
            or any(value < 1 or value > 1440 for value in stages)
            or not (stages[0] < stages[1] < stages[2])
        ):
            raise ValueError(f"{pool} reprice stages must contain three increasing minutes between 1 and 1440")
    return policy


def policy_v3_to_json(policy):
    payload = {}
    for key, value in asdict(policy).items():
        if isinstance(value, D):
            payload[key] = format(value, "f")
        elif isinstance(value, tuple):
            payload[key] = list(value)
        else:
            payload[key] = value
    payload["floorsConfigured"] = all(
        policy.floor_apr(pool) is not None and policy.floor_apr(pool) > 0 for pool in POOLS
    )
    payload["gross_daily_floors"] = {
        pool: None
        if policy.floor_apr(pool) is None
        else format(gross_daily_floor(policy.floor_apr(pool), policy.normal_fee_rate), "f")
        for pool in POOLS
    }
    return payload


def gross_daily_floor(net_apr, fee_rate):
    net_apr = D(net_apr)
    fee_rate = D(fee_rate)
    if fee_rate < 0 or fee_rate >= 1:
        raise ValueError("fee rate must be in [0, 1)")
    return net_apr / D("365") / (D("1") - fee_rate)


def net_apr_from_daily(gross_daily_rate, fee_rate):
    return D(gross_daily_rate) * D("365") * (D("1") - D(fee_rate))


def pool_for_period(period):
    period = int(period)
    if 2 <= period <= 7:
        return "short"
    if 8 <= period <= 30:
        return "medium"
    if period == 120:
        return "long"
    return "external"


def weighted_quantile(rows, quantile, value_key="rate", weight_key="amount"):
    valid = []
    for row in rows or []:
        try:
            value = D(row[value_key])
            weight = abs(D(row[weight_key]))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
        if value > 0 and weight > 0:
            valid.append((value, weight))
    if not valid:
        return D("0")
    valid.sort(key=lambda item: item[0])
    total = sum((item[1] for item in valid), D("0"))
    target = total * D(quantile)
    seen = D("0")
    for value, weight in valid:
        seen += weight
        if seen >= target:
            return value
    return valid[-1][0]


def filter_supported_trades(trades, policy):
    rows = []
    for trade in trades or []:
        try:
            row = {
                "mts": int(trade["mts"]),
                "rate": D(trade["rate"]),
                "amount": abs(D(trade["amount"])),
                "period": int(trade.get("period", 2)),
            }
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
        if row["rate"] > 0 and row["amount"] > 0:
            rows.append(row)
    if not rows:
        return []
    q95 = weighted_quantile(rows, D("0.95"))
    total = sum((row["amount"] for row in rows), D("0"))
    support = max(policy.min_order_amount, total * policy.outlier_min_volume_share)
    high_volume = sum((row["amount"] for row in rows if row["rate"] > q95), D("0"))
    if high_volume < support:
        rows = [row for row in rows if row["rate"] <= q95 or row["amount"] >= support]
    low = weighted_quantile(rows, D("0.05"))
    high = weighted_quantile(rows, D("0.95"))
    return [dict(row, rate=max(low, min(high, row["rate"]))) for row in rows]


def _window_rows(rows, now_ms, window):
    threshold = int(now_ms) - WINDOWS_MS[window]
    return [row for row in rows if row["mts"] >= threshold]


def _weighted_std(rows, center):
    if not rows or center <= 0:
        return D("0")
    total = sum((row["amount"] for row in rows), D("0"))
    if total <= 0:
        return D("0")
    variance = sum((row["amount"] * (row["rate"] - center) ** 2 for row in rows), D("0")) / total
    return D(str(math.sqrt(float(max(D("0"), variance)))))


def build_market_signals_v3(book, trades, stats, policy, now_ms=None):
    validate_policy_v3(policy)
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    filtered = filter_supported_trades(trades, policy)
    windows = {}
    for window in WINDOWS_MS:
        selected = _window_rows(filtered, now, window)
        median = weighted_quantile(selected, D("0.5"))
        volume = sum((row["amount"] for row in selected), D("0"))
        windows[window] = {
            "median": median,
            "q25": weighted_quantile(selected, D("0.25")),
            "q75": weighted_quantile(selected, D("0.75")),
            "volume": volume,
            "count": len(selected),
            "volatility": _weighted_std(selected, median),
        }
    asks = [row for row in book or [] if D(row.get("amount", 0)) > 0]
    bids = [row for row in book or [] if D(row.get("amount", 0)) < 0]
    best_offer = min((D(row["rate"]) for row in asks), default=D("0"))
    best_bid = max((D(row["rate"]) for row in bids), default=D("0"))
    latest_stat = stats[-1] if stats else {}
    frr = _d(latest_stat.get("frr_daily_rate"))
    utilization = latest_stat.get("utilization")
    utilization = None if utilization is None else _d(utilization)
    anchor_values = [
        value for value in (best_offer, best_bid, windows["1h"]["median"], windows["24h"]["median"], frr) if value > 0
    ]
    anchor = sorted(anchor_values)[len(anchor_values) // 2] if anchor_values else D("0")
    iqr = max(D("0"), windows["24h"]["q75"] - windows["24h"]["q25"])
    threshold = max(policy.minimum_rate_change, iqr * policy.iqr_change_fraction)
    trend = (
        windows["5m"]["median"] - windows["1h"]["median"]
        if windows["5m"]["median"] and windows["1h"]["median"]
        else D("0")
    )
    hourly_baseline_5m = windows["1h"]["volume"] / D("12") if windows["1h"]["volume"] > 0 else D("0")
    volume_ratio = windows["5m"]["volume"] / hourly_baseline_5m if hourly_baseline_5m > 0 else D("0")
    spike = bool(
        windows["24h"]["q75"] > 0
        and windows["5m"]["median"] >= windows["24h"]["q75"]
        and trend >= threshold
        and volume_ratio >= policy.spike_volume_ratio
    )
    low_rate = bool(windows["7d"]["q25"] > 0 and anchor < windows["7d"]["q25"])
    if spike:
        regime = "spike"
    elif low_rate or trend <= -threshold:
        regime = "low"
    elif trend >= threshold:
        regime = "rising"
    else:
        regime = "neutral"
    depth = {}
    for side, rows in (
        ("offer", sorted(asks, key=lambda row: D(row["rate"]))),
        ("bid", sorted(bids, key=lambda row: D(row["rate"]), reverse=True)),
    ):
        cumulative = D("0")
        levels = []
        for row in rows:
            cumulative += abs(D(row["amount"]))
            levels.append({"rate": D(row["rate"]), "amount": abs(D(row["amount"])), "cumulative": cumulative})
        depth[side] = levels
    return {
        "as_of": now,
        "regime": regime,
        "spike": spike,
        "low_rate": low_rate,
        "best_bid": best_bid,
        "best_offer": best_offer,
        "anchor_rate": anchor,
        "frr_daily_rate": frr,
        "utilization": utilization,
        "trend": trend,
        "trend_threshold": threshold,
        "volume_ratio_5m": volume_ratio,
        "windows": windows,
        "depth": depth,
        "filtered_trade_count": len(filtered),
        "raw_trade_count": len(trades or []),
    }


def dynamic_pool_shares(policy, signals):
    base = policy.pool_shares()
    shift = int(policy.max_pool_shift)
    candidates = []
    for short in range(max(0, int(base["short"]) - shift), min(100, int(base["short"]) + shift) + 1):
        for medium in range(max(0, int(base["medium"]) - shift), min(100, int(base["medium"]) + shift) + 1):
            long = 100 - short - medium
            if long < 0 or abs(D(long) - base["long"]) > policy.max_pool_shift:
                continue
            if signals.get("regime") == "spike" and (
                short > base["short"] or medium < base["medium"] or long < base["long"]
            ):
                continue
            if signals.get("regime") == "low" and short < base["short"]:
                continue
            utility = D("0")
            if signals.get("regime") == "spike":
                utility = D(medium) * D("0.6") + D(long) - D(short) * D("0.2")
            elif signals.get("regime") == "low":
                utility = D(short) - D(medium) * D("0.2") - D(long) * D("0.6")
            elif signals.get("regime") == "rising":
                utility = D(medium) * D("0.4") + D(long) * D("0.6")
            else:
                utility = -sum(
                    (
                        abs(D(value) - base[name])
                        for name, value in (("short", short), ("medium", medium), ("long", long))
                    ),
                    D("0"),
                )
            candidates.append((utility, -abs(D(short) - base["short"]), short, medium, long))
    if not candidates:
        return base
    _, _, short, medium, long = max(candidates)
    return {"short": D(short), "medium": D(medium), "long": D(long)}


def _largest_remainder_counts(total_count, weights):
    if total_count <= 0:
        return {key: 0 for key in weights}
    total_weight = sum((max(D("0"), D(value)) for value in weights.values()), D("0"))
    if total_weight <= 0:
        return {key: 0 for key in weights}
    raw = {key: D(total_count) * max(D("0"), D(value)) / total_weight for key, value in weights.items()}
    counts = {key: int(value) for key, value in raw.items()}
    remaining = total_count - sum(counts.values())
    order = sorted(raw, key=lambda key: (raw[key] - D(counts[key]), str(key)), reverse=True)
    for key in order[:remaining]:
        counts[key] += 1
    return counts


def deterministic_amounts(total, count, minimum, jitter, seed):
    total, minimum, jitter = D(total), D(minimum), D(jitter)
    if count <= 0 or total < minimum:
        return []
    count = min(int(count), int(total // minimum))
    if count <= 0:
        return []
    rng = random.Random(int(hashlib.sha256(str(seed).encode("utf-8")).hexdigest()[:16], 16))
    factors = [D(str(1 + rng.uniform(-float(jitter), float(jitter)))) for _ in range(count)]
    factor_sum = sum(factors, D("0"))
    amounts = []
    remaining = total
    for index, factor in enumerate(factors):
        slots_after = count - index - 1
        if index == count - 1:
            amount = remaining
        else:
            proposed = total * factor / factor_sum
            upper = remaining - minimum * D(slots_after)
            amount = min(upper, max(minimum, proposed))
        amount = amount.quantize(SATOSHI, rounding=ROUND_DOWN)
        amounts.append(amount)
        remaining -= amount
    if remaining > 0:
        amounts[-1] += remaining
    return amounts


def _pool_deficits(total_principal, shares, exposure):
    return {
        pool: max(D("0"), D(total_principal) * shares[pool] / D("100") - D(exposure.get(pool, 0))) for pool in POOLS
    }


def allocate_slices_v3(
    total_principal,
    available,
    exposure_by_pool,
    policy,
    signals,
    strategy_version="3",
    exposure_by_layer=None,
    offer_exposure_by_pool=None,
    offer_exposure_by_layer=None,
):
    validate_policy_v3(policy)
    total_principal, available = max(D("0"), D(total_principal)), max(D("0"), D(available))
    offer_exposure = {pool: D((offer_exposure_by_pool or exposure_by_pool or {}).get(pool, 0)) for pool in POOLS}
    offer_budget = sum(offer_exposure.values(), D("0")) + available
    shares = policy.pool_shares()
    target_offer_amounts = {pool: offer_budget * shares[pool] / D("100") for pool in POOLS}
    deviations = {pool: offer_exposure[pool] - target_offer_amounts[pool] for pool in POOLS}
    tolerance = max(policy.min_order_amount, offer_budget * D("0.02"))
    diagnostics = {
        "allocation_basis": "MANAGED_OPEN_OFFERS",
        "shares": shares,
        "target_offer_amounts": target_offer_amounts,
        "current_offer_amounts": offer_exposure,
        "deviation_amounts": deviations,
        "ratio_tolerance": tolerance,
    }
    possible_slices = min(policy.target_slices, int(total_principal // policy.min_order_amount))
    if possible_slices <= 0 or available < policy.min_order_amount:
        return {
            "target_slice_count": possible_slices,
            "target_slice_amount": D("0"),
            "slices": [],
            "empty_reason": "NO_AVAILABLE_BALANCE" if available <= 0 else "BELOW_MINIMUM",
            **diagnostics,
        }
    target_amount = (total_principal / D(possible_slices)).quantize(SATOSHI, rounding=ROUND_DOWN)
    deficits = {pool: max(D("0"), target_offer_amounts[pool] - offer_exposure[pool]) for pool in POOLS}
    allocatable = min(available, sum(deficits.values(), D("0")))
    if allocatable < policy.min_order_amount:
        return {
            "target_slice_count": possible_slices,
            "target_slice_amount": target_amount,
            "slices": [],
            "empty_reason": "OFFER_RATIOS_SATISFIED",
            **diagnostics,
        }
    new_count = min(
        int(allocatable // policy.min_order_amount),
        max(1, int((allocatable / target_amount).to_integral_value(rounding=ROUND_HALF_UP))),
    )
    pool_counts = _largest_remainder_counts(new_count, deficits)
    active_pools = [pool for pool in POOLS if pool_counts[pool] > 0]
    while active_pools and any(deficits[pool] < policy.min_order_amount * pool_counts[pool] for pool in active_pools):
        invalid = min(active_pools, key=lambda pool: deficits[pool] / D(pool_counts[pool]))
        pool_counts[invalid] -= 1
        new_count -= 1
        active_pools = [pool for pool in POOLS if pool_counts[pool] > 0]
    if new_count <= 0:
        return {
            "target_slice_count": possible_slices,
            "target_slice_amount": target_amount,
            "slices": [],
            "empty_reason": "OFFER_RATIOS_SATISFIED",
            **diagnostics,
        }
    layer_exposure = offer_exposure_by_layer or exposure_by_layer or {}
    layer_deficits = {
        layer: max(
            D("0"),
            total_principal * policy.layer_shares()[layer] / D("100") - D(layer_exposure.get(layer, 0)),
        )
        for layer in LAYERS
    }
    if sum(layer_deficits.values(), D("0")) <= 0:
        layer_deficits = policy.layer_shares()
    layer_counts = _largest_remainder_counts(new_count, layer_deficits)
    layer_sequence = [layer for layer in LAYERS for _ in range(layer_counts[layer])]
    layer_cursor = 0
    pool_amount_weights = {pool: deficits[pool] for pool in active_pools}
    total_deficit = sum(pool_amount_weights.values(), D("0"))
    remaining_amount = allocatable
    slices = []
    for pool_index, pool in enumerate(active_pools):
        count = pool_counts[pool]
        minimum_for_rest = sum(
            (policy.min_order_amount * pool_counts[other] for other in active_pools[pool_index + 1 :]), D("0")
        )
        if pool_index == len(active_pools) - 1:
            pool_amount = remaining_amount
        else:
            pool_amount = min(deficits[pool], allocatable * pool_amount_weights[pool] / total_deficit)
            pool_amount = max(policy.min_order_amount * count, min(pool_amount, remaining_amount - minimum_for_rest))
        amounts = deterministic_amounts(
            pool_amount, count, policy.min_order_amount, policy.amount_jitter, f"{strategy_version}:USD:{pool}"
        )
        periods = policy.periods(pool)
        for index, amount in enumerate(amounts):
            layer = layer_sequence[layer_cursor] if layer_cursor < len(layer_sequence) else "balanced"
            layer_cursor += 1
            slices.append(
                {
                    "slice_index": len(slices),
                    "pool": pool,
                    "layer": layer,
                    "amount": amount,
                    "period": periods[(index + pool_index) % len(periods)],
                }
            )
        remaining_amount -= sum(amounts, D("0"))
    return {
        "target_slice_count": possible_slices,
        "target_slice_amount": target_amount,
        "slices": slices,
        "empty_reason": None,
        **diagnostics,
    }


def competitive_rate_for_layer(layer, signals, floor_rate):
    windows = signals.get("windows", {})
    floor_rate = ceil_rate_tick(floor_rate)
    anchor = D(signals.get("anchor_rate") or floor_rate)
    best_bid = D(signals.get("best_bid") or anchor)
    q75 = D(windows.get("24h", {}).get("q75") or anchor)
    iqr = max(D("0"), q75 - D(windows.get("24h", {}).get("q25") or q75))
    if layer == "quick":
        target = max(floor_rate, best_bid - RATE_TICK)
    elif layer == "balanced":
        target = max(floor_rate, anchor, D(windows.get("1h", {}).get("median") or 0))
    else:
        target = max(floor_rate, q75, anchor + iqr * D("0.25"))
    return ceil_rate_tick(max(floor_rate, target))


def _candidate_target_rate(item, signals, floor_rate):
    floor_rate = ceil_rate_tick(floor_rate)
    target = competitive_rate_for_layer(item["layer"], signals, floor_rate)
    target += RATE_TICK * D((item["slice_index"] % 5) - 2)
    return ceil_rate_tick(max(floor_rate, target))


def _score_candidate(candidate, item, signals, policy, floor_apr):
    weights = V3_RESEARCH_SCORE_WEIGHTS
    fee = policy.hidden_fee_rate if candidate["hidden"] else policy.normal_fee_rate
    net_apr = net_apr_from_daily(candidate["effective_rate"], fee)
    floor_apr = max(D("0.00000001"), floor_apr)
    yield_score = min(D("1"), net_apr / (floor_apr * D("1.5")))
    anchor = max(D("0.00000001"), D(signals.get("anchor_rate") or candidate["effective_rate"]))
    distance = max(D("0"), candidate["effective_rate"] - D(signals.get("best_bid") or anchor)) / anchor
    layer_base = {"quick": D("0.90"), "balanced": D("0.65"), "high": D("0.30")}[item["layer"]]
    fill = max(D("0"), min(D("1"), layer_base - distance * D("10")))
    wait = fill
    depth = D("0.8") if item["layer"] == "quick" else D("0.6") if item["layer"] == "balanced" else D("0.4")
    speed = min(D("1"), D(signals.get("volume_ratio_5m") or 0) / max(D("1"), policy.spike_volume_ratio))
    utilization = signals.get("utilization")
    utilization_score = D("0.5") if utilization is None else max(D("0"), min(D("1"), D(utilization)))
    regime = signals.get("regime")
    trend_score = (
        D("1")
        if (regime == "spike" and item["pool"] in {"medium", "long"}) or (regime == "low" and item["pool"] == "short")
        else D("0.6")
    )
    term_score = {"short": D("0.7"), "medium": D("0.75"), "long": D("0.8")}[item["pool"]]
    if regime == "spike":
        term_score += D("0.2") if item["pool"] in {"medium", "long"} else D("0")
    hidden_score = D("0") if candidate["hidden"] else D("1")
    variable_score = D("0") if candidate["offer_type"] == "FRRDELTAVAR" else D("1")
    components = {
        "net_yield": yield_score,
        "fill_probability": fill,
        "wait_time": wait,
        "book_depth": depth,
        "trade_speed": speed,
        "utilization": utilization_score,
        "trend_volatility": trend_score,
        "term_opportunity": min(D("1"), term_score),
        "hidden_cost": hidden_score,
        "variable_risk": variable_score,
    }
    score = sum((weights[name] * components[name] for name in weights), D("0")) / D("100")
    if candidate["offer_type"] == "FRRDELTAFIX" and regime in {"rising", "spike"}:
        score += D("0.025")
    if candidate["offer_type"] == "FRRDELTAVAR" and regime in {"neutral", "low"}:
        score += D("0.015")
    if candidate["offer_type"] == "LIMIT" and item["layer"] in {"quick", "high"}:
        score += D("0.01")
    return score, components, net_apr


def _candidate_types(policy, frr, target):
    candidates = []
    if policy.enable_limit:
        candidates.append(("LIMIT", target, target, "LIMIT"))
    if policy.enable_frr and frr > 0:
        candidates.append(("FRRDELTAVAR", D("0"), frr, "FRR"))
    if policy.enable_frr_delta_fixed and frr > 0:
        # Bitfinex rejects negative FRR delta offsets as ``rate: invalid``.
        # A target below FRR is represented by LIMIT instead.
        offset = ceil_rate_tick(max(D("0"), target - frr))
        candidates.append(("FRRDELTAFIX", offset, frr + offset, "FRR_DELTA_FIXED"))
    if policy.enable_frr_delta_variable and frr > 0:
        offset = ceil_rate_tick(max(D("0"), target - frr))
        candidates.append(("FRRDELTAVAR", offset, frr + offset, "FRR_DELTA_VARIABLE"))
    return candidates


def _plan_hash(strategy_version, total_principal, available, exposure_by_pool, existing_exposure, plan):
    payload = {
        "strategyVersion": str(strategy_version),
        "totalPrincipal": format(D(total_principal), "f"),
        "available": format(D(available), "f"),
        "exposureByPool": {key: format(D(value), "f") for key, value in sorted((exposure_by_pool or {}).items())},
        "existingExposure": {key: format(D(value), "f") for key, value in sorted((existing_exposure or {}).items())},
        "orders": [
            {
                "sliceKey": str(row.get("slice_key") or row.get("slice_index")),
                "pool": row["pool"],
                "layer": row["layer"],
                "amount": format(D(row["amount"]), "f"),
                "submittedRate": format(D(row["submitted_rate"]), "f"),
                "effectiveRate": format(D(row["effective_rate"]), "f"),
                "period": int(row["period"]),
                "offerType": row["offer_type"],
                "displayType": row["display_type"],
                "flags": int(row.get("flags", 0)),
            }
            for row in plan
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_strategy_plan_v3(
    total_principal,
    available,
    exposure_by_pool,
    policy,
    signals,
    strategy_version="3",
    existing_exposure=None,
    exposure_by_layer=None,
    offer_exposure_by_pool=None,
    offer_exposure_by_layer=None,
):
    validate_policy_v3(policy)
    total_principal = max(D("0"), D(total_principal))
    requested_available = max(D("0"), D(available))
    existing_exposure = existing_exposure or {}
    account_exposure = max(D("0"), D(existing_exposure.get("total", 0)))
    percent_cap = total_principal * policy.max_lend_percent / D("100")
    hard_cap = percent_cap
    if policy.max_lend_amount is not None:
        hard_cap = min(hard_cap, max(D("0"), D(policy.max_lend_amount)))
    cap_remaining = max(D("0"), hard_cap - account_exposure)
    available = min(requested_available, cap_remaining)
    allocation = allocate_slices_v3(
        total_principal,
        available,
        exposure_by_pool,
        policy,
        signals,
        strategy_version,
        exposure_by_layer=exposure_by_layer,
        offer_exposure_by_pool=offer_exposure_by_pool,
        offer_exposure_by_layer=offer_exposure_by_layer,
    )
    plan = []
    variable_used = max(D("0"), D(existing_exposure.get("variable", 0)))
    hidden_used = max(D("0"), D(existing_exposure.get("hidden", 0)))
    variable_limit = D(total_principal) * policy.variable_max_share / D("100")
    hidden_limit = (
        D("0") if policy.hidden_max_share is None else D(total_principal) * policy.hidden_max_share / D("100")
    )
    frr = D(signals.get("frr_daily_rate") or 0)
    for item in allocation["slices"]:
        floor_apr = policy.floor_apr(item["pool"])
        if floor_apr is None or floor_apr <= 0:
            continue
        visible_floor = gross_daily_floor(floor_apr, policy.normal_fee_rate)
        window_24h = signals.get("windows", {}).get("24h", {})
        q25 = D(window_24h.get("q25") or 0)
        q75 = D(window_24h.get("q75") or 0)
        iqr = max(D("0"), q75 - q25)
        supported_ceiling = max(
            D(signals.get("best_bid") or 0),
            D(signals.get("best_offer") or 0),
            D(signals.get("anchor_rate") or 0),
            D(signals.get("frr_daily_rate") or 0),
            q75 + iqr,
        )
        support_margin = D(signals.get("trend_threshold") or policy.minimum_rate_change)
        if supported_ceiling <= 0 or visible_floor > supported_ceiling + support_margin:
            continue
        target = _candidate_target_rate(item, signals, visible_floor)
        visible = []
        hidden = []
        for offer_type, submitted_rate, effective_rate, display_type in _candidate_types(policy, frr, target):
            is_variable = offer_type == "FRRDELTAVAR"
            if is_variable and variable_used + item["amount"] > variable_limit:
                continue
            for is_hidden in (False, True) if policy.enable_hidden else (False,):
                if is_hidden and hidden_used + item["amount"] > hidden_limit:
                    continue
                fee = policy.hidden_fee_rate if is_hidden else policy.normal_fee_rate
                floor_rate = gross_daily_floor(floor_apr, fee)
                if rate_below_floor(effective_rate, floor_rate):
                    continue
                candidate = {
                    **item,
                    "offer_type": offer_type,
                    "display_type": display_type,
                    "submitted_rate": submitted_rate,
                    "effective_rate": effective_rate,
                    "target_rate": target,
                    "hidden": is_hidden,
                    "flags": 64 if is_hidden else 0,
                    "gross_daily_floor": floor_rate,
                }
                score, components, net_apr = _score_candidate(candidate, item, signals, policy, floor_apr)
                candidate.update({"score": score, "score_components": components, "net_apr": net_apr})
                (hidden if is_hidden else visible).append(candidate)
        if not visible and not hidden:
            continue
        best_visible = (
            max(visible, key=lambda row: (row["score"], row["effective_rate"], row["display_type"]))
            if visible
            else None
        )
        best_hidden = (
            max(hidden, key=lambda row: (row["score"], row["effective_rate"], row["display_type"])) if hidden else None
        )
        chosen = (
            best_hidden
            if best_hidden and (best_visible is None or best_hidden["score"] > best_visible["score"])
            else best_visible
        )
        if chosen is None:
            continue
        if chosen["offer_type"] == "FRRDELTAVAR":
            variable_used += chosen["amount"]
        if chosen["hidden"]:
            hidden_used += chosen["amount"]
        plan.append(chosen)
    new_variable_amount = max(D("0"), variable_used - max(D("0"), D(existing_exposure.get("variable", 0))))
    new_hidden_amount = max(D("0"), hidden_used - max(D("0"), D(existing_exposure.get("hidden", 0))))
    plan_hash = _plan_hash(strategy_version, total_principal, available, exposure_by_pool, existing_exposure, plan)
    empty_reason = allocation.get("empty_reason")
    if not plan and allocation.get("slices") and empty_reason is None:
        empty_reason = "MARKET_BELOW_FLOOR"
    if requested_available > 0 and available <= 0:
        empty_reason = "FUNDING_CAP_REACHED"
    for row in plan:
        row["plan_hash"] = plan_hash
    response = {
        **allocation,
        "plan": plan,
        "planned_amount": sum((row["amount"] for row in plan), D("0")),
        "idle_amount": max(D("0"), requested_available - sum((row["amount"] for row in plan), D("0"))),
        "variable_amount": new_variable_amount,
        "hidden_amount": new_hidden_amount,
        "plan_hash": plan_hash,
        "funding_cap": hard_cap,
        "existing_exposure": account_exposure,
        "cap_remaining": cap_remaining,
        "cap_limited_available": available,
        "over_cap": account_exposure > hard_cap,
        "empty_reason": empty_reason,
        "rebalance_cancellations": [],
    }
    response.update(
        {
            "emptyReason": response["empty_reason"],
            "allocationBasis": response.get("allocation_basis"),
            "targetOfferAmounts": response.get("target_offer_amounts", {}),
            "currentOfferAmounts": response.get("current_offer_amounts", {}),
            "deviationAmounts": response.get("deviation_amounts", {}),
            "rebalanceCancellations": response["rebalance_cancellations"],
        }
    )
    return response


def json_decimal(value):
    if isinstance(value, D):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: json_decimal(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_decimal(item) for item in value]
    return value


def replay_strategy_v3(policy, trades, stats, principal, book=None, now_ms=None, window_ms=None):
    validate_policy_v3(policy)
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    window = int(window_ms if window_ms is not None else WINDOWS_MS["7d"])
    if window <= 0:
        raise ValueError("replay window must be positive")
    principal = D(principal)
    selected = sorted(
        [row for row in trades or [] if now - window <= int(row.get("mts", 0)) < now],
        key=lambda row: int(row["mts"]),
    )
    if not selected:
        return {
            "mode": "REPLAY",
            "sampleCount": 0,
            "estimatedUtilizationPercent": "0",
            "netInterest": "0",
            "actualNetAprPercent": "0",
            "orders": [],
            "fills": [],
            "returns": [],
            "idlePrincipalTime": "0",
        }
    step_ms = 15 * 60_000
    start = max(now - window, int(selected[0]["mts"]))
    cursor = start - (start % step_ms)
    available = principal
    active = []
    fills = []
    returns = []
    interest = D("0")
    utilized_time = D("0")
    idle_time = D("0")
    last_signals = None
    trade_index = 0
    signal_history = []
    stat_rows = sorted(
        [row for row in stats or [] if int(row.get("mts", row.get("timestamp", 0)) or 0) <= now],
        key=lambda row: int(row.get("mts", row.get("timestamp", 0)) or 0),
    )
    stat_index = 0
    latest_stat = None

    def replay_book(history):
        if book and cursor + step_ms >= now:
            return book
        recent = [row for row in history if int(row["mts"]) >= cursor - WINDOWS_MS["1h"]]
        median = weighted_quantile(recent, D("0.5"))
        q25 = weighted_quantile(recent, D("0.25"))
        if median <= 0:
            return []
        volume = max(policy.min_order_amount, sum((abs(D(row["amount"])) for row in recent), D("0")))
        return [
            {"rate": median, "period": 2, "count": len(recent), "amount": volume},
            {"rate": q25 or median, "period": 2, "count": len(recent), "amount": -volume},
        ]

    def aggregate_signal_trade(rows, mts):
        weighted_rate = D("0")
        total_volume = D("0")
        for row in rows:
            try:
                volume = abs(D(row["amount"]))
                rate = D(row["rate"])
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue
            if volume <= 0 or rate <= 0:
                continue
            weighted_rate += rate * volume
            total_volume += volume
        if total_volume <= 0:
            return None
        return {
            "mts": int(mts),
            "rate": weighted_rate / total_volume,
            "amount": total_volume,
            "period": 2,
        }

    while cursor < now:
        interval_end = min(now, cursor + step_ms)
        still_active = []
        for credit in active:
            if credit["return_mts"] <= cursor:
                elapsed_days = D(credit["return_mts"] - credit["fill_mts"]) / D("86400000")
                fee = policy.hidden_fee_rate if credit["hidden"] else policy.normal_fee_rate
                earned = credit["amount"] * credit["effective_rate"] * elapsed_days * (D("1") - fee)
                interest += earned
                available += credit["amount"]
                returns.append({**credit, "interest": earned})
            else:
                still_active.append(credit)
        active = still_active

        active_amount = sum((credit["amount"] for credit in active), D("0"))
        duration_days = D(interval_end - cursor) / D("86400000")
        utilized_time += active_amount * duration_days
        idle_time += max(D("0"), principal - active_amount) * duration_days

        interval_start = trade_index
        while trade_index < len(selected) and int(selected[trade_index]["mts"]) < interval_end:
            trade_index += 1
        interval_trades = selected[interval_start:trade_index]
        signal_history = [row for row in signal_history if int(row["mts"]) >= cursor - WINDOWS_MS["7d"]]
        while stat_index < len(stat_rows):
            stat_mts = int(stat_rows[stat_index].get("mts", stat_rows[stat_index].get("timestamp", 0)) or 0)
            if stat_mts > cursor:
                break
            latest_stat = stat_rows[stat_index]
            stat_index += 1
        if available >= policy.min_order_amount and signal_history and interval_trades:
            synthetic_book = replay_book(signal_history)
            replay_stats = [] if latest_stat is None else [latest_stat]
            last_signals = build_market_signals_v3(
                synthetic_book,
                signal_history,
                replay_stats,
                policy,
                cursor,
            )
            exposure = {pool: D("0") for pool in POOLS}
            for credit in active:
                exposure[credit["pool"]] += credit["amount"]
            plan = build_strategy_plan_v3(principal, available, exposure, policy, last_signals, f"replay:{cursor}")
            remaining_volume = {}
            for trade in interval_trades:
                key = (int(trade.get("period", 2)), D(trade["rate"]))
                remaining_volume[key] = remaining_volume.get(key, D("0")) + abs(D(trade["amount"]))
            for order in plan["plan"]:
                eligible = [
                    key
                    for key, volume in remaining_volume.items()
                    if volume > 0 and key[0] == int(order["period"]) and key[1] >= order["effective_rate"]
                ]
                if not eligible or available < policy.min_order_amount:
                    continue
                eligible.sort(key=lambda key: key[1])
                volume = sum((remaining_volume[key] for key in eligible), D("0"))
                fill_amount = min(order["amount"], available, volume)
                if fill_amount < policy.min_order_amount:
                    continue
                unconsumed = fill_amount
                for key in eligible:
                    consumed = min(unconsumed, remaining_volume[key])
                    remaining_volume[key] -= consumed
                    unconsumed -= consumed
                    if unconsumed <= 0:
                        break
                credit = {
                    **order,
                    "amount": fill_amount,
                    "fill_mts": cursor,
                    "return_mts": cursor + int(order["period"]) * 86_400_000,
                }
                active.append(credit)
                fills.append(credit)
                available -= fill_amount
        aggregate = aggregate_signal_trade(interval_trades, interval_end - 1)
        if aggregate is not None:
            signal_history.append(aggregate)
        cursor = interval_end

    for credit in active:
        elapsed_days = D(max(0, now - credit["fill_mts"])) / D("86400000")
        fee = policy.hidden_fee_rate if credit["hidden"] else policy.normal_fee_rate
        interest += credit["amount"] * credit["effective_rate"] * elapsed_days * (D("1") - fee)
    elapsed_days = D(max(1, now - start)) / D("86400000")
    principal_time = principal * elapsed_days
    utilization = D("0") if principal_time <= 0 else utilized_time / principal_time
    apr = D("0") if principal_time <= 0 else interest / principal_time * D("365")
    return json_decimal(
        {
            "mode": "REPLAY",
            "sampleCount": len(selected),
            "replayAggregationMinutes": step_ms // 60_000,
            "estimatedUtilizationPercent": utilization * D("100"),
            "netInterest": interest,
            "actualNetAprPercent": apr * D("100"),
            "orders": fills,
            "fills": fills,
            "returns": returns,
            "activeCredits": active,
            "idlePrincipalTime": idle_time,
            "signals": last_signals or build_market_signals_v3(book or [], selected, stats or [], policy, now),
        }
    )
