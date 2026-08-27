import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP


D = Decimal
SATOSHI = D("0.00000001")
USD_ORDER_CHUNK = D("150")
POOL_SHIFT_CAP_PERCENTAGE_POINTS = D("10")
PRIMARY_TERM_MAX_SHARE = D("0.70")
DEMAND_MIN_SHARE = D("0.05")
DEMAND_CONFIRMATION_CYCLES = 2
DUST_REINVEST_MINIMUM = D("1")
RATE_TICK = D("0.0000001")
RATE_COMPARISON_EPSILON = D("0.000000000000000001")
EXACT_TERM_EXPLORATION_CURVE = "EXACT_TERM_EXPLORATION_V3"
EXACT_TERM_EXPLORATION_CURVES = {
    "EXACT_TERM_EXPLORATION_V2",
    EXACT_TERM_EXPLORATION_CURVE,
}
LEGACY_REPRICE_CURVE = "LEGACY"
POOLS = ("short", "medium", "long")
LAYERS = ("quick", "balanced", "high")
TERM_PERIOD_RANGES = {
    "short": (2, 7),
    "medium": (7, 30),
    "long": (30, 120),
}
POPULAR_TERM_PERIODS = {
    "short": (2, 7),
    "medium": (14, 30),
    "long": (120,),
}
PERIOD_DEMAND_WINDOW_WEIGHTS = {"1h": D("0.35"), "24h": D("0.40"), "7d": D("0.25")}
PERIOD_DEMAND_COMPONENT_WEIGHTS = {"trade_count": D("0.60"), "volume": D("0.40")}
PERIOD_FILL_COMPONENT_WEIGHTS = {"executable_depth": D("0.70"), "competitiveness": D("0.30")}
PERIOD_FINAL_COMPONENT_WEIGHTS = {"market_demand": D("0.70"), "fill_probability": D("0.30")}
PERIOD_FALLBACKS = {"short": 2, "medium": 14, "long": 120}
PERIOD_SWITCH_ADVANTAGE = D("0.20")
PERIOD_SWITCH_HOLD_MS = 600_000
REPRICE_STAGE_DEFAULTS = {
    "short": (5, 10, 20, 30, 60, 75, 90, 120, 150, 180),
    "medium": (10, 20, 40, 60, 120, 150, 180, 240, 300, 360),
    "long": (30, 60, 120, 180, 360, 480, 720, 960, 1200, 1440),
}
REPRICE_STAGE_LEGACY_MULTIPLIERS = {
    "short": (D("1.5"), D("2"), D("3")),
    "medium": (D("1.5"), D("2"), D("3")),
    "long": (D("1.333333333333333333"), D("2"), D("4")),
}
MAX_REPRICE_STAGE_MINUTES = 10_080
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


def normalize_term_period_range(value, pool):
    """Parse editable discrete terms while migrating the former range syntax."""

    minimum, maximum = TERM_PERIOD_RANGES[pool]
    defaults = POPULAR_TERM_PERIODS[pool]
    if value in (None, ""):
        return defaults
    if isinstance(value, str):
        normalized = value.strip().replace("–", "-").replace("—", "-")
        if "-" in normalized:
            parts = tuple(part.strip() for part in normalized.split("-"))
            try:
                endpoints = tuple(int(part) for part in parts if part)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{pool} periods must be comma-separated days within {minimum}-{maximum}") from exc
            if endpoints == (minimum, maximum):
                return defaults
            raise ValueError(f"{pool} periods must be comma-separated days within {minimum}-{maximum}")
        else:
            parts = tuple(part.strip() for part in normalized.replace("/", ",").replace("、", ",").split(","))
    else:
        parts = tuple(value)
    try:
        periods = tuple(int(part) for part in parts if str(part).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{pool} periods must be comma-separated days within {minimum}-{maximum}") from exc
    if (
        not periods
        or len(set(periods)) != len(periods)
        or any(period < minimum or period > maximum for period in periods)
    ):
        raise ValueError(f"{pool} periods must be unique days within {minimum}-{maximum}")
    return tuple(sorted(periods))


def normalize_reprice_stages(value, pool):
    stages = _tuple_of_ints(value, REPRICE_STAGE_DEFAULTS[pool])
    if len(stages) != 3:
        return stages
    last = D(stages[-1])
    extension = tuple(
        int((last * multiplier).to_integral_value(rounding=ROUND_CEILING))
        for multiplier in REPRICE_STAGE_LEGACY_MULTIPLIERS[pool]
    )
    return stages + extension


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
    medium_share: D = D("35")
    long_share: D = D("15")
    quick_share: D = D("40")
    balanced_share: D = D("40")
    high_share: D = D("20")
    short_floor_apr: D | None = None
    medium_floor_apr: D | None = None
    long_floor_apr: D | None = None
    short_periods: tuple[int, ...] = (2, 7)
    medium_periods: tuple[int, ...] = POPULAR_TERM_PERIODS["medium"]
    long_periods: tuple[int, ...] = POPULAR_TERM_PERIODS["long"]
    max_lend_amount: D | None = None
    max_lend_percent: D = D("100")
    # Kept in persisted policy records for Schema 6/9 compatibility. V3.1 fixes
    # this safety boundary at ten percentage points and does not expose it as a
    # configurable control.
    max_pool_shift: D = POOL_SHIFT_CAP_PERCENTAGE_POINTS
    normal_fee_rate: D = D("0.15")
    hidden_fee_rate: D = D("0.18")
    enable_limit: bool = True
    enable_frr: bool = True
    enable_frr_delta_fixed: bool = True
    enable_frr_delta_variable: bool = True
    variable_max_share: D = D("10")
    enable_hidden: bool = False
    adopt_external_offers: bool = True
    hidden_max_share: D | None = None
    minimum_offer_minutes: int = 5
    reprice_cooldown_minutes: int = 5
    max_reprices_per_hour: int = 12
    minimum_rate_change: D = D("0.00001")
    balanced_start_premium_percent: D = D("5")
    high_start_premium_percent: D = D("12")
    balanced_landing_stage: int = 5
    high_landing_stage: int = 7
    short_reprice_stages_minutes: tuple[int, ...] = REPRICE_STAGE_DEFAULTS["short"]
    medium_reprice_stages_minutes: tuple[int, ...] = REPRICE_STAGE_DEFAULTS["medium"]
    long_reprice_stages_minutes: tuple[int, ...] = REPRICE_STAGE_DEFAULTS["long"]
    iqr_change_fraction: D = D("0.25")
    spike_volume_ratio: D = D("1.5")
    outlier_min_volume_share: D = D("0.005")
    ws_fallback_seconds: int = 300
    rest_stale_seconds: int = 60
    market_retention_days: int = 90

    def floor_apr(self, pool):
        return getattr(self, f"{pool}_floor_apr")

    def periods(self, pool):
        return tuple(getattr(self, f"{pool}_periods"))

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
    "short_periods": lambda value: normalize_term_period_range(value, "short"),
    "medium_periods": lambda value: normalize_term_period_range(value, "medium"),
    "long_periods": lambda value: normalize_term_period_range(value, "long"),
    "max_lend_amount": lambda value: None if value in (None, "") else _d(value),
    "max_lend_percent": _d,
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
    "balanced_start_premium_percent": _d,
    "high_start_premium_percent": _d,
    "balanced_landing_stage": int,
    "high_landing_stage": int,
    "short_reprice_stages_minutes": lambda value: normalize_reprice_stages(value, "short"),
    "medium_reprice_stages_minutes": lambda value: normalize_reprice_stages(value, "medium"),
    "long_reprice_stages_minutes": lambda value: normalize_reprice_stages(value, "long"),
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
    updates["adopt_external_offers"] = True
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
    if policy.max_lend_amount is not None and policy.max_lend_amount < 0:
        raise ValueError("max_lend_amount must be non-negative or empty")
    if policy.max_lend_percent < 0 or policy.max_lend_percent > 100:
        raise ValueError("max_lend_percent must be 0-100")
    if policy.max_pool_shift != POOL_SHIFT_CAP_PERCENTAGE_POINTS:
        raise ValueError("V3.5 keeps max_pool_shift at 10 percentage points for policy compatibility")
    for pool, (minimum, maximum) in TERM_PERIOD_RANGES.items():
        periods = tuple(getattr(policy, f"{pool}_periods"))
        if (
            not periods
            or tuple(sorted(periods)) != periods
            or len(set(periods)) != len(periods)
            or any(period < minimum or period > maximum for period in periods)
        ):
            raise ValueError(f"{pool} periods must be unique increasing days within {minimum}-{maximum}")
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
    for name in ("balanced_start_premium_percent", "high_start_premium_percent"):
        value = getattr(policy, name)
        if value < 0 or value > 100:
            raise ValueError(f"{name} must be 0-100")
    for name in ("balanced_landing_stage", "high_landing_stage"):
        value = getattr(policy, name)
        if value < 1 or value > 10:
            raise ValueError(f"{name} must be 1-10")
    for pool in POOLS:
        stages = policy.reprice_stages(pool)
        if (
            len(stages) not in {6, 10}
            or any(value < 1 or value > MAX_REPRICE_STAGE_MINUTES for value in stages)
            or any(left >= right for left, right in zip(stages, stages[1:]))
        ):
            raise ValueError(
                f"{pool} reprice stages must contain six or ten increasing minutes between 1 and "
                f"{MAX_REPRICE_STAGE_MINUTES}"
            )
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
    if 31 <= period <= 120:
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
    support = max(USD_ORDER_CHUNK, total * policy.outlier_min_volume_share)
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


def _period_book_rows(book, period, borrower_side=None):
    rows = []
    for raw in book or []:
        try:
            if int(raw.get("period")) != int(period):
                continue
            amount = D(raw.get("amount", 0))
            rate = D(raw.get("rate", 0))
        except (TypeError, ValueError, ArithmeticError):
            continue
        if rate <= 0 or amount == 0:
            continue
        if borrower_side is True and amount >= 0:
            continue
        if borrower_side is False and amount <= 0:
            continue
        rows.append({"rate": rate, "amount": abs(amount)})
    return rows


def _build_period_selection(policy, signals, filtered_trades, book, now_ms):
    """Score every configured term against one global, actionable demand denominator."""

    configured = []
    period_pool = {}
    for pool in POOLS:
        for period in policy.periods(pool):
            configured.append(int(period))
            period_pool[int(period)] = pool
    configured = tuple(dict.fromkeys(configured))
    rows = {}
    demand_data_present = False
    book_data_present = False
    for period in configured:
        pool = period_pool[period]
        period_trades = [row for row in filtered_trades if int(row["period"]) == period]
        floor_apr = policy.floor_apr(pool)
        floor_rate = (
            D("0")
            if floor_apr is None or floor_apr <= 0
            else ceil_rate_tick(gross_daily_floor(floor_apr, policy.normal_fee_rate))
        )
        rate_windows = {}
        for window in WINDOWS_MS:
            selected = _window_rows(period_trades, now_ms, window)
            median = weighted_quantile(selected, D("0.5"))
            rate_windows[window] = {
                "median": median,
                "q25": weighted_quantile(selected, D("0.25")),
                "q75": weighted_quantile(selected, D("0.75")),
                "volume": sum((row["amount"] for row in selected), D("0")),
                "count": len(selected),
                "volatility": _weighted_std(selected, median),
            }
        window_metrics = {}
        for window in PERIOD_DEMAND_WINDOW_WEIGHTS:
            selected = _window_rows(period_trades, now_ms, window)
            window_metrics[window] = {
                "tradeCount": len(selected),
                "tradeVolume": sum((row["amount"] for row in selected), D("0")),
            }
            demand_data_present = demand_data_present or bool(selected)
        bids = _period_book_rows(book, period, borrower_side=True)
        offers = _period_book_rows(book, period, borrower_side=False)
        book_data_present = book_data_present or bool(bids or offers)
        best_bid = max((row["rate"] for row in bids), default=D("0"))
        best_offer = min((row["rate"] for row in offers), default=D("0"))
        anchor_values = [
            value
            for value in (
                best_offer,
                best_bid,
                rate_windows["1h"]["median"],
                rate_windows["24h"]["median"],
            )
            if value > 0
        ]
        anchor_rate = sorted(anchor_values)[len(anchor_values) // 2] if anchor_values else floor_rate
        period_iqr = max(D("0"), rate_windows["24h"]["q75"] - rate_windows["24h"]["q25"])
        trend_threshold = max(policy.minimum_rate_change, period_iqr * policy.iqr_change_fraction)
        period_trend = (
            rate_windows["5m"]["median"] - rate_windows["1h"]["median"]
            if rate_windows["5m"]["median"] and rate_windows["1h"]["median"]
            else D("0")
        )
        target_rate = ceil_rate_tick(
            max(floor_rate, anchor_rate, D(rate_windows["1h"].get("median") or 0))
        )
        executable_depth = sum((row["amount"] for row in bids if row["rate"] >= target_rate), D("0"))
        recent_rates = [row["rate"] for row in _window_rows(period_trades, now_ms, "7d")]
        rows[period] = {
            "period": period,
            "pool": pool,
            "windows": window_metrics,
            "rateWindows": rate_windows,
            "bestBorrowRate": best_bid,
            "bestOfferRate": best_offer,
            "anchorRate": anchor_rate,
            "trend": period_trend,
            "trendThreshold": trend_threshold,
            "rateDataAvailable": bool(bids or offers or period_trades),
            "executableBorrowDepth": executable_depth,
            "supportedCeiling": max([best_bid, *recent_rates], default=D("0")),
            "targetRate": target_rate,
            "grossDailyFloor": floor_rate,
            "absoluteDemandShare": D("0"),
        }

    for window, window_weight in PERIOD_DEMAND_WINDOW_WEIGHTS.items():
        total_count = sum(row["windows"][window]["tradeCount"] for row in rows.values())
        total_volume = sum((row["windows"][window]["tradeVolume"] for row in rows.values()), D("0"))
        for row in rows.values():
            metrics = row["windows"][window]
            count_share = D(metrics["tradeCount"]) / D(total_count) if total_count else D("0")
            volume_share = metrics["tradeVolume"] / total_volume if total_volume > 0 else D("0")
            metrics.update(
                {
                    "countShare": count_share,
                    "volumeShare": volume_share,
                    "score": count_share * PERIOD_DEMAND_COMPONENT_WEIGHTS["trade_count"]
                    + volume_share * PERIOD_DEMAND_COMPONENT_WEIGHTS["volume"],
                }
            )
            row["absoluteDemandShare"] += metrics["score"] * window_weight

    max_depth = max((row["executableBorrowDepth"] for row in rows.values()), default=D("0"))
    insufficient_market_data = not demand_data_present and not book_data_present
    for row in rows.values():
        floor_rate = row["grossDailyFloor"]
        target_rate = row["targetRate"]
        depth_score = row["executableBorrowDepth"] / max_depth if max_depth > 0 else D("0")
        best_bid = row["bestBorrowRate"]
        competitiveness = (
            D("0")
            if best_bid <= 0 or target_rate <= 0
            else D("1")
            if target_rate <= best_bid
            else max(D("0"), D("1") - (target_rate - best_bid) / best_bid)
        )
        fill_score = (
            depth_score * PERIOD_FILL_COMPONENT_WEIGHTS["executable_depth"]
            + competitiveness * PERIOD_FILL_COMPONENT_WEIGHTS["competitiveness"]
        )
        supported = row["supportedCeiling"] > 0 and row["supportedCeiling"] >= floor_rate
        row.update(
            {
                "depthScore": depth_score,
                "competitivenessScore": competitiveness,
                "fillScore": fill_score,
                "marketEligible": bool(floor_rate > 0 and supported and not insufficient_market_data),
                "eligibilityReason": (
                    "INSUFFICIENT_MARKET_DATA"
                    if insufficient_market_data
                    else "MARKET_SUPPORTS_FLOOR"
                    if floor_rate > 0 and supported
                    else "FLOOR_NOT_CONFIGURED"
                    if floor_rate <= 0
                    else "MARKET_BELOW_FLOOR"
                ),
            }
        )

    by_pool = {}
    for pool in POOLS:
        candidates = tuple(int(value) for value in policy.periods(pool))
        pool_rows = [rows[period] for period in candidates]
        pool_demand = sum((row["absoluteDemandShare"] for row in pool_rows), D("0"))
        for row in pool_rows:
            relative = row["absoluteDemandShare"] / pool_demand if pool_demand > 0 else D("0")
            total_score = (
                row["absoluteDemandShare"] * PERIOD_FINAL_COMPONENT_WEIGHTS["market_demand"]
                + row["fillScore"] * PERIOD_FINAL_COMPONENT_WEIGHTS["fill_probability"]
            )
            row.update(
                {
                    "demandScore": relative,
                    "relativeDemandShare": relative,
                    "totalScore": total_score,
                    "belowDemandThreshold": relative < DEMAND_MIN_SHARE,
                    "eligible": bool(row["marketEligible"]),
                }
            )
        ranked = sorted(
            pool_rows,
            key=lambda row: (row["totalScore"], row["fillScore"], row["demandScore"], -row["period"]),
            reverse=True,
        )
        selected = None if insufficient_market_data else ranked[0] if ranked else None
        runner_up = None if insufficient_market_data else ranked[1] if len(ranked) > 1 else None
        market_qualified = any(row["marketEligible"] for row in pool_rows)
        pool_fill = (
            sum((row["fillScore"] * row["absoluteDemandShare"] for row in pool_rows), D("0")) / pool_demand
            if pool_demand > 0
            else max((row["fillScore"] for row in pool_rows), default=D("0"))
        )
        pool_score = (
            pool_demand * PERIOD_FINAL_COMPONENT_WEIGHTS["market_demand"]
            + pool_fill * PERIOD_FINAL_COMPONENT_WEIGHTS["fill_probability"]
        )
        fallback = PERIOD_FALLBACKS[pool] if PERIOD_FALLBACKS[pool] in candidates else min(candidates)
        by_pool[pool] = {
            "candidates": list(candidates),
            "selectedPeriod": None if selected is None else selected["period"],
            "leaderPeriod": None if selected is None else selected["period"],
            "runnerUpPeriod": None if runner_up is None else runner_up["period"],
            "eligiblePeriods": [row["period"] for row in ranked if row["marketEligible"]],
            "selectedSinceMs": None,
            "selectionMature": False,
            "selectionReason": "HIGHEST_GLOBAL_SCORE" if selected else "NO_MARKET_DATA",
            "fallbackPeriod": fallback,
            "insufficientMarketData": insufficient_market_data,
            "grossDailyFloor": pool_rows[0]["grossDailyFloor"] if pool_rows else D("0"),
            "absoluteDemandShare": pool_demand,
            "fillScore": pool_fill,
            "totalScore": pool_score,
            "marketQualified": market_qualified,
            "additionalQualified": bool(
                market_qualified and (pool != "long" or signals.get("regime") in {"rising", "spike"})
            ),
            "belowDemandThreshold": pool_demand < DEMAND_MIN_SHARE,
            "scores": pool_rows,
        }
    result = {
        "basis": (
            "70% global market demand (trades: count 60%/volume 40%; "
            "windows: 1h 35%/24h 40%/7d 25%) + 30% fill probability "
            "(book depth 70%/rate competitiveness 30%)"
        ),
        "weights": {
            "final": PERIOD_FINAL_COMPONENT_WEIGHTS,
            "demandWindows": PERIOD_DEMAND_WINDOW_WEIGHTS,
            "demandComponents": PERIOD_DEMAND_COMPONENT_WEIGHTS,
            "fillComponents": PERIOD_FILL_COMPONENT_WEIGHTS,
        },
        "byPool": by_pool,
    }
    return result


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
    demand_rows = _window_rows(filtered, now, "7d")
    total_demand_count = len(demand_rows)
    total_demand_volume = sum((row["amount"] for row in demand_rows), D("0"))
    demand_by_period = {}
    for row in demand_rows:
        period = int(row["period"])
        entry = demand_by_period.setdefault(period, {"trade_count": 0, "volume": D("0")})
        entry["trade_count"] += 1
        entry["volume"] += row["amount"]
    period_demand = {}
    for period, entry in demand_by_period.items():
        count_share = D(entry["trade_count"]) / D(total_demand_count) if total_demand_count else D("0")
        volume_share = entry["volume"] / total_demand_volume if total_demand_volume > 0 else D("0")
        period_demand[period] = {
            **entry,
            "count_share": count_share,
            "volume_share": volume_share,
            "score": count_share * D("0.60") + volume_share * D("0.40"),
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
    result = {
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
        "period_demand": period_demand,
        "period_demand_basis": "RECENT_TRADES_COUNT_60_VOLUME_40",
        "filtered_trade_count": len(filtered),
        "raw_trade_count": len(trades or []),
    }
    period_selection = _build_period_selection(policy, result, filtered, book, now)
    result["period_selection"] = period_selection
    result["periodSelection"] = period_selection
    return result


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


def _interleaved_keys(counts, keys):
    """Expand integer counts without grouping one allocation dimension into blocks."""

    remaining = {key: max(0, int(counts.get(key, 0))) for key in keys}
    sequence = []
    while any(remaining.values()):
        for key in keys:
            if remaining[key] <= 0:
                continue
            sequence.append(key)
            remaining[key] -= 1
    return sequence


def evenly_distributed_amounts(total, count):
    """Split an eight-decimal USD total as evenly as possible."""

    total = max(D("0"), D(total)).quantize(SATOSHI, rounding=ROUND_DOWN)
    count = max(0, int(count))
    if count <= 0 or total < USD_ORDER_CHUNK * D(count):
        return []
    total_units = int(total / SATOSHI)
    base_units, extra_units = divmod(total_units, count)
    return [D(base_units + (1 if index < extra_units else 0)) * SATOSHI for index in range(count)]


def _capped_weighted_counts(total_count, keys, weights, maximums):
    """Largest-remainder count allocation with explicit per-key maxima."""

    keys = list(keys)
    total_count = max(0, int(total_count))
    normalized = {key: max(D("0"), D(weights.get(key, 0))) for key in keys}
    if sum(normalized.values(), D("0")) <= 0:
        normalized = {key: D("1") for key in keys}
    weight_total = sum(normalized.values(), D("0"))
    raw = {key: D(total_count) * normalized[key] / weight_total for key in keys}
    counts = {key: min(max(0, int(maximums.get(key, 0))), int(raw[key])) for key in keys}
    remaining = total_count - sum(counts.values())
    while remaining > 0:
        choices = [key for key in keys if counts[key] < int(maximums.get(key, 0))]
        if not choices:
            break
        selected = max(
            choices,
            key=lambda key: (raw[key] - D(counts[key]), normalized[key], -keys.index(key)),
        )
        counts[selected] += 1
        remaining -= 1
    return counts


def _winner_target_shares(selection):
    primary = selection.get("selectedPeriod")
    rows = {int(row["period"]): row for row in selection.get("scores", [])}
    if primary is not None and rows.get(int(primary), {}).get("lowDemandConfirmed", False):
        primary = selection.get("leaderPeriod")
    if primary is None:
        primary = selection.get("fallbackPeriod")
    if primary is None:
        return {}
    primary = int(primary)
    runner = next(
        (
            int(row["period"])
            for row in sorted(
                rows.values(), key=lambda row: (D(row.get("totalScore") or 0), -int(row["period"])), reverse=True
            )
            if int(row["period"]) != primary and not row.get("lowDemandConfirmed", False)
        ),
        None,
    )
    if runner is None:
        return {primary: D("1")}
    runner_demand = D(rows.get(runner, {}).get("relativeDemandShare") or 0)
    if runner_demand < D("0.05"):
        primary_share = D("1")
    elif runner_demand < D("0.20"):
        primary_share = D("0.90")
    elif runner_demand < D("0.35"):
        primary_share = D("0.75")
    else:
        primary_share = D("0.60")
    return {primary: primary_share, runner: D("1") - primary_share}


def _pool_targets_v33(offer_budget, shares, selections):
    configured = [pool for pool in POOLS if D(shares.get(pool, 0)) > 0]
    active_count = min(len(configured), int(max(D("0"), offer_budget) // USD_ORDER_CHUNK))
    active = configured[:active_count]
    targets = {pool: D("0") for pool in POOLS}
    qualified_receivers = []
    for pool in active:
        selection = selections.get(pool, {})
        low = bool(selection.get("lowDemandConfirmed", False))
        qualified = bool(selection.get("additionalQualified", selection.get("marketQualified", False))) and not low
        raw = offer_budget * D(shares[pool]) / D("100")
        targets[pool] = USD_ORDER_CHUNK if low or not qualified else max(USD_ORDER_CHUNK, raw)
        if qualified:
            qualified_receivers.append(pool)

    total = sum(targets.values(), D("0"))
    while total > offer_budget + SATOSHI:
        reducible = {pool: max(D("0"), targets[pool] - USD_ORDER_CHUNK) for pool in active}
        room = sum(reducible.values(), D("0"))
        if room <= 0:
            break
        excess = total - offer_budget
        for pool in active:
            reduction = min(reducible[pool], excess * reducible[pool] / room)
            targets[pool] -= reduction
        total = sum(targets.values(), D("0"))

    remaining = max(D("0"), offer_budget - total)
    if remaining > 0 and qualified_receivers:
        weights = {pool: max(D("0"), D(selections[pool].get("totalScore") or 0)) for pool in qualified_receivers}
        if sum(weights.values(), D("0")) <= 0:
            weights = {pool: D(shares[pool]) for pool in qualified_receivers}
        weight_total = sum(weights.values(), D("0"))
        awarded = D("0")
        for pool in qualified_receivers[:-1]:
            amount = (remaining * weights[pool] / weight_total).quantize(SATOSHI, rounding=ROUND_DOWN)
            targets[pool] += amount
            awarded += amount
        targets[qualified_receivers[-1]] += remaining - awarded
    return targets, tuple(active)


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
    offer_exposure_by_period=None,
):
    """Allocate authoritative cash by global demand and cumulative target deficits."""

    validate_policy_v3(policy)
    total_principal, available = max(D("0"), D(total_principal)), max(D("0"), D(available))
    offer_exposure = {pool: D((offer_exposure_by_pool or exposure_by_pool or {}).get(pool, 0)) for pool in POOLS}
    period_exposure = {
        int(period): max(D("0"), D(amount)) for period, amount in (offer_exposure_by_period or {}).items()
    }
    offer_budget = sum(offer_exposure.values(), D("0")) + available
    shares = policy.pool_shares()
    period_selection = (signals.get("periodSelection") or signals.get("period_selection") or {}).get("byPool", {})
    if not period_selection:
        period_selection = {
            pool: {
                "selectedPeriod": policy.periods(pool)[0],
                "fallbackPeriod": policy.periods(pool)[0],
                "eligiblePeriods": list(policy.periods(pool)),
                "marketQualified": True,
                "totalScore": D(shares[pool]) / D("100"),
                "scores": [
                    {
                        "period": period,
                        "relativeDemandShare": D("1") if index == 0 else D("0"),
                        "totalScore": D("1") if index == 0 else D("0"),
                    }
                    for index, period in enumerate(policy.periods(pool))
                ],
            }
            for pool in POOLS
        }
    insufficient = any(bool(period_selection.get(pool, {}).get("insufficientMarketData")) for pool in POOLS)
    target_offer_amounts, active_pools = _pool_targets_v33(offer_budget, shares, period_selection)
    pool_additions = {
        pool: max(D("0"), target_offer_amounts[pool] - offer_exposure[pool]) if pool in active_pools else D("0")
        for pool in POOLS
    }
    # Layer targets describe the whole managed portfolio, not just the small
    # amount that happens to be waiting in open offers.  Otherwise every
    # isolated repayment starts from an empty offer book and the sole new slice
    # repeatedly falls into the largest configured layer (balanced).
    layer_allocation_basis = "ATTRIBUTED_MANAGED_EXPOSURE_PLUS_AVAILABLE"
    cumulative_layer_exposure = exposure_by_layer if exposure_by_layer is not None else offer_exposure_by_layer or {}
    layer_exposure = {layer: max(D("0"), D(cumulative_layer_exposure.get(layer, 0))) for layer in LAYERS}
    attributed_layer_exposure = sum(layer_exposure.values(), D("0"))
    layer_budget = attributed_layer_exposure + available
    unattributed_layer_exposure = max(D("0"), total_principal - available - attributed_layer_exposure)
    target_layer_amounts = {layer: layer_budget * policy.layer_shares()[layer] / D("100") for layer in LAYERS}
    layer_deficits = {layer: max(D("0"), target_layer_amounts[layer] - layer_exposure[layer]) for layer in LAYERS}
    if sum(layer_deficits.values(), D("0")) <= 0:
        layer_deficits = policy.layer_shares()

    pool_allocation = {}
    for pool in POOLS:
        selection = period_selection.get(pool, {})
        pool_allocation[pool] = {
            "configuredShare": shares[pool],
            "absoluteDemandShare": D(selection.get("absoluteDemandShare") or 0),
            "fillScore": D(selection.get("fillScore") or 0),
            "compositeScore": D(selection.get("totalScore") or 0),
            "marketQualified": bool(selection.get("marketQualified", False)),
            "additionalQualified": bool(selection.get("additionalQualified", selection.get("marketQualified", False))),
            "lowDemandConfirmed": bool(selection.get("lowDemandConfirmed", False)),
            "minimumApplied": pool in active_pools and target_offer_amounts[pool] <= USD_ORDER_CHUNK + SATOSHI,
            "targetAmount": target_offer_amounts[pool],
            "currentManagedOffers": offer_exposure[pool],
            "additionDeficit": pool_additions[pool],
        }
    diagnostics = {
        "allocation_basis": "AUTHORITATIVE_WALLET_PLUS_MANAGED_OPEN_OFFERS",
        "allocation_model": "GLOBAL_DEMAND_V1",
        "period_allocation_basis": "CUMULATIVE_DEMAND_TARGET_DEFICITS",
        "pool_redistribution_basis": "CONFIGURED_TARGET_WITH_150_MINIMUM_AND_GLOBAL_SCORE_RELEASE",
        "period_selection": signals.get("periodSelection") or signals.get("period_selection") or {},
        "pool_allocation": pool_allocation,
        "shares": shares,
        "target_offer_amounts": target_offer_amounts,
        "current_offer_amounts": offer_exposure,
        "deviation_amounts": {pool: offer_exposure[pool] - target_offer_amounts[pool] for pool in POOLS},
        "ratio_tolerance": USD_ORDER_CHUNK,
        "pool_cap_percentages": {pool: D("100") for pool in POOLS},
        "pool_cap_amounts": target_offer_amounts,
        "desired_pool_amounts": target_offer_amounts,
        "planned_pool_additions": pool_additions,
        "ineligible_pools": tuple(pool for pool in POOLS if pool not in active_pools),
        "released_pool_amount": D("0"),
        "redistributed_pool_amounts": {pool: D("0") for pool in POOLS},
        "eligible_pool_weights": {pool: D(period_selection.get(pool, {}).get("totalScore") or 0) for pool in POOLS},
        "primary_term_max_share": None,
        "minimum_order_amount": USD_ORDER_CHUNK,
        "layer_allocation_basis": layer_allocation_basis,
        "target_layer_amounts": target_layer_amounts,
        "current_layer_amounts": layer_exposure,
        "unattributed_layer_amount": unattributed_layer_exposure,
        "layer_deviation_amounts": {layer: layer_exposure[layer] - target_layer_amounts[layer] for layer in LAYERS},
    }
    if available < USD_ORDER_CHUNK or insufficient:
        return {
            "target_slice_count": 0,
            "target_slice_amount": D("0"),
            "slices": [],
            "empty_reason": (
                "MARKET_DATA_INSUFFICIENT"
                if insufficient
                else "NO_AVAILABLE_BALANCE"
                if available <= 0
                else "BELOW_MINIMUM"
            ),
            "term_allocations": {},
            **diagnostics,
        }

    target_deployable = min(available, sum(pool_additions.values(), D("0")))
    target_count = int(target_deployable // USD_ORDER_CHUNK)
    if target_count <= 0:
        return {
            "target_slice_count": 0,
            "target_slice_amount": D("0"),
            "slices": [],
            "empty_reason": "BELOW_MINIMUM",
            "term_allocations": {},
            **diagnostics,
        }
    pool_maximum_counts = {
        pool: int(pool_additions[pool] // USD_ORDER_CHUNK) if pool_additions[pool] >= USD_ORDER_CHUNK else 0
        for pool in POOLS
    }
    pool_counts = _capped_weighted_counts(target_count, POOLS, pool_additions, pool_maximum_counts)
    pool_plan_amounts = {pool: D(pool_counts[pool]) * USD_ORDER_CHUNK for pool in POOLS}
    unassigned = target_deployable - sum(pool_plan_amounts.values(), D("0"))
    while unassigned > SATOSHI:
        rooms = {
            pool: max(D("0"), pool_additions[pool] - pool_plan_amounts[pool]) for pool in POOLS if pool_counts[pool] > 0
        }
        room_total = sum(rooms.values(), D("0"))
        if room_total <= SATOSHI:
            break
        awarded = D("0")
        recipients = [pool for pool in POOLS if rooms.get(pool, D("0")) > 0]
        for pool in recipients[:-1]:
            amount = min(
                rooms[pool],
                (unassigned * rooms[pool] / room_total).quantize(SATOSHI, rounding=ROUND_DOWN),
            )
            pool_plan_amounts[pool] += amount
            awarded += amount
        last = recipients[-1]
        amount = min(rooms[last], unassigned - awarded)
        pool_plan_amounts[last] += amount
        awarded += amount
        if awarded <= SATOSHI:
            break
        unassigned -= awarded

    term_sequence = []
    term_diagnostics = {}
    for pool in POOLS:
        count = pool_counts.get(pool, 0)
        if count <= 0:
            continue
        selection = period_selection[pool]
        target_shares = _winner_target_shares(selection)
        if not target_shares:
            continue
        pool_target = target_offer_amounts[pool]
        target_by_period = {period: pool_target * share for period, share in target_shares.items()}
        for period in tuple(target_by_period):
            if target_by_period[period] < USD_ORDER_CHUNK and period != int(selection.get("selectedPeriod") or period):
                target_by_period[int(selection.get("selectedPeriod") or period)] += target_by_period.pop(period)
        planned = {period: D("0") for period in target_by_period}
        representative_amount = pool_plan_amounts[pool] / D(count)
        for _ in range(count):
            period = max(
                target_by_period,
                key=lambda value: (
                    target_by_period[value] - period_exposure.get(value, D("0")) - planned[value],
                    value == int(selection.get("selectedPeriod") or value),
                    -value,
                ),
            )
            planned[period] += representative_amount
            term_sequence.append((pool, period))
        term_diagnostics[pool] = {
            "primaryPeriod": int(selection.get("selectedPeriod") or next(iter(target_shares))),
            "runnerUpPeriod": next(
                (period for period in target_shares if period != int(selection.get("selectedPeriod") or period)), None
            ),
            "targetShares": target_shares,
            "targetByPeriod": target_by_period,
            "currentByPeriod": {period: period_exposure.get(period, D("0")) for period in target_by_period},
            "plannedByPeriod": planned,
            "minimumOrderAmount": USD_ORDER_CHUNK,
        }

    order_count = len(term_sequence)
    if order_count <= 0:
        return {
            "target_slice_count": 0,
            "target_slice_amount": D("0"),
            "slices": [],
            "empty_reason": "CONCENTRATION_CAP_OR_MINIMUM",
            "term_allocations": term_diagnostics,
            **diagnostics,
        }
    order_amounts = []
    for pool in POOLS:
        order_amounts.extend(evenly_distributed_amounts(pool_plan_amounts[pool], pool_counts[pool]))
    layer_counts = _largest_remainder_counts(order_count, layer_deficits)
    layer_sequence = _interleaved_keys(layer_counts, LAYERS)
    slices = [
        {
            "slice_index": index,
            "pool": pool,
            "layer": layer_sequence[index] if index < len(layer_sequence) else "balanced",
            "amount": order_amounts[index],
            "period": period,
            "minimum_floor_order": bool(
                pool_allocation[pool]["minimumApplied"] and offer_exposure[pool] < USD_ORDER_CHUNK
            ),
        }
        for index, (pool, period) in enumerate(term_sequence)
    ]
    planned_amount = sum((row["amount"] for row in slices), D("0"))
    return {
        "target_slice_count": len(slices),
        "target_slice_amount": (planned_amount / D(len(slices))).quantize(SATOSHI, rounding=ROUND_HALF_UP),
        "slices": slices,
        "empty_reason": None,
        "term_allocations": term_diagnostics,
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


def period_pricing_context(signals, pool, period, floor_rate):
    """Return pricing inputs isolated to one exact funding term."""

    floor_rate = ceil_rate_tick(floor_rate)
    selection = signals.get("periodSelection") or signals.get("period_selection") or {}
    pool_payload = selection.get("byPool", {}).get(str(pool), {})
    row = next(
        (candidate for candidate in pool_payload.get("scores", []) if int(candidate.get("period", 0)) == int(period)),
        {},
    )
    if not row or "rateWindows" not in row:
        # Compatibility for legacy replay fixtures that predate exact-period
        # signals. Live market construction always emits rateWindows, including
        # an explicit empty context that safely falls back to the term floor.
        legacy_windows = signals.get("windows", {})
        legacy_24h = legacy_windows.get("24h", {})
        legacy_q25 = D(legacy_24h.get("q25") or 0)
        legacy_q75 = D(legacy_24h.get("q75") or 0)
        legacy_7d = legacy_windows.get("7d", {})
        legacy_q25_7d = D(legacy_7d.get("q25") or 0)
        legacy_q75_7d = D(legacy_7d.get("q75") or 0)
        legacy_anchor = D(signals.get("anchor_rate") or floor_rate)
        return {
            "pool": str(pool),
            "period": int(period),
            "bestBorrowRate": D(signals.get("best_bid") or 0),
            "bestOfferRate": D(signals.get("best_offer") or 0),
            "rawAnchorRate": legacy_anchor,
            "anchorRate": max(floor_rate, legacy_anchor),
            "median1h": D(legacy_windows.get("1h", {}).get("median") or 0),
            "q25_24h": legacy_q25,
            "q75_24h": legacy_q75,
            "iqr24h": max(D("0"), legacy_q75 - legacy_q25),
            "q25_7d": legacy_q25_7d,
            "q75_7d": legacy_q75_7d,
            "iqr7d": max(D("0"), legacy_q75_7d - legacy_q25_7d),
            "trend": D(signals.get("trend") or 0),
            "trendThreshold": D(signals.get("trend_threshold") or 0),
            "supportedCeiling": max(
                D(signals.get("best_bid") or 0),
                D(signals.get("best_offer") or 0),
                legacy_q75,
            ),
            "rateDataAvailable": bool(legacy_windows or signals.get("best_bid") or signals.get("best_offer")),
            "rateWindows": legacy_windows,
            "source": "LEGACY_SIGNAL_FALLBACK",
        }
    windows = row.get("rateWindows") or {}
    window_1h = windows.get("1h", {})
    window_24h = windows.get("24h", {})
    window_7d = windows.get("7d", {})
    best_borrow = D(row.get("bestBorrowRate") or 0)
    best_offer = D(row.get("bestOfferRate") or 0)
    anchor = D(row.get("anchorRate") or floor_rate)
    q25 = D(window_24h.get("q25") or 0)
    q75 = D(window_24h.get("q75") or 0)
    iqr = max(D("0"), q75 - q25)
    q25_7d = D(window_7d.get("q25") or 0)
    q75_7d = D(window_7d.get("q75") or 0)
    return {
        "pool": str(pool),
        "period": int(period),
        "bestBorrowRate": best_borrow,
        "bestOfferRate": best_offer,
        "rawAnchorRate": anchor,
        "anchorRate": max(floor_rate, anchor),
        "median1h": D(window_1h.get("median") or 0),
        "q25_24h": q25,
        "q75_24h": q75,
        "iqr24h": iqr,
        "q25_7d": q25_7d,
        "q75_7d": q75_7d,
        "iqr7d": max(D("0"), q75_7d - q25_7d),
        "trend": D(row.get("trend") or 0),
        "trendThreshold": D(row.get("trendThreshold") or 0),
        "supportedCeiling": D(row.get("supportedCeiling") or 0),
        "rateDataAvailable": bool(row.get("rateDataAvailable", False)),
        "rateWindows": windows,
        "source": "EXACT_PERIOD" if row else "PERIOD_FLOOR_FALLBACK",
    }


def competitive_rate_for_period(layer, pool, period, signals, floor_rate):
    """Price one order without borrowing signals from any other term."""

    floor_rate = ceil_rate_tick(floor_rate)
    context = period_pricing_context(signals, pool, period, floor_rate)
    if layer == "quick":
        best_borrow = context["bestBorrowRate"]
        target = floor_rate if best_borrow <= 0 else max(floor_rate, best_borrow - RATE_TICK)
    elif layer == "balanced":
        target = max(floor_rate, context["rawAnchorRate"], context["median1h"])
    else:
        target = max(
            floor_rate,
            context["q75_24h"],
            context["rawAnchorRate"] + context["iqr24h"] * D("0.25"),
        )
    return ceil_rate_tick(max(floor_rate, target))


def exploration_start_rate_for_period(layer, pool, period, signals, floor_rate, policy):
    """Return a same-term starting rate that deliberately explores above the floor."""

    floor_rate = ceil_rate_tick(floor_rate)
    landing = competitive_rate_for_period(layer, pool, period, signals, floor_rate)
    if layer == "quick":
        return landing
    context = period_pricing_context(signals, pool, period, floor_rate)
    if layer == "balanced":
        premium = D(policy.balanced_start_premium_percent) / D("100")
        target = max(landing, floor_rate * (D("1") + premium), context["q75_24h"])
    else:
        premium = D(policy.high_start_premium_percent) / D("100")
        target = max(
            landing,
            floor_rate * (D("1") + premium),
            context["q75_24h"],
            context["q75_7d"],
            context["rawAnchorRate"] + context["iqr24h"] * D("0.50"),
        )
    return ceil_rate_tick(max(floor_rate, target))


def _candidate_target_rate(item, signals, floor_rate, policy=None):
    floor_rate = ceil_rate_tick(floor_rate)
    if item.get("pool") is not None and item.get("period") is not None:
        target = (
            competitive_rate_for_period(item["layer"], item["pool"], item["period"], signals, floor_rate)
            if policy is None
            else exploration_start_rate_for_period(
                item["layer"], item["pool"], item["period"], signals, floor_rate, policy
            )
        )
        pricing = period_pricing_context(signals, item["pool"], item["period"], floor_rate)
    else:
        # Retain the small private-helper compatibility used by offline callers;
        # every real order carries both pool and exact period.
        target = competitive_rate_for_layer(item["layer"], signals, floor_rate)
        pricing = {"q75_24h": D(signals.get("windows", {}).get("24h", {}).get("q75") or 0)}
    start_guard = floor_rate
    if item["layer"] == "balanced":
        # A newly returned slice should first try a market-supported rate
        # before the ordinary age stages walk it down.  Keep this guard local
        # to new offers so existing balanced repricing retains its benchmark.
        start_guard = ceil_rate_tick(max(target, D(pricing.get("q75_24h") or 0)))
    ladder_step = (item["slice_index"] % 5) if policy is not None and item["layer"] in {"balanced", "high"} else (
        (item["slice_index"] % 5) - 2
    )
    target += RATE_TICK * D(ladder_step)
    return ceil_rate_tick(max(floor_rate, start_guard, target))


def _score_candidate(candidate, item, signals, policy, floor_apr):
    weights = V3_RESEARCH_SCORE_WEIGHTS
    fee = policy.hidden_fee_rate if candidate["hidden"] else policy.normal_fee_rate
    net_apr = net_apr_from_daily(candidate["effective_rate"], fee)
    floor_apr = max(D("0.00000001"), floor_apr)
    yield_score = min(D("1"), net_apr / (floor_apr * D("1.5")))
    pricing = period_pricing_context(
        signals,
        item["pool"],
        item["period"],
        candidate.get("gross_daily_floor") or candidate["effective_rate"],
    )
    anchor = max(D("0.00000001"), D(pricing.get("anchorRate") or candidate["effective_rate"]))
    best_borrow = D(pricing.get("bestBorrowRate") or anchor)
    distance = max(D("0"), candidate["effective_rate"] - best_borrow) / anchor
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
    offer_exposure_by_period=None,
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
        offer_exposure_by_period=offer_exposure_by_period,
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
        pricing = period_pricing_context(signals, item["pool"], item["period"], visible_floor)
        supported_ceiling = max(
            D(pricing.get("bestBorrowRate") or 0),
            D(pricing.get("bestOfferRate") or 0),
            D(pricing.get("anchorRate") or 0),
            D(pricing.get("q75_24h") or 0) + D(pricing.get("iqr24h") or 0),
        )
        if not pricing.get("rateDataAvailable"):
            supported_ceiling = visible_floor
        support_margin = max(
            policy.minimum_rate_change,
            D(pricing.get("trendThreshold") or 0),
        )
        if (supported_ceiling <= 0 or visible_floor > supported_ceiling + support_margin) and not item.get(
            "minimum_floor_order", False
        ):
            continue
        target = _candidate_target_rate(item, signals, visible_floor, policy=policy)
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
                    "fixed_landing_rate": competitive_rate_for_period(
                        item["layer"],
                        item["pool"],
                        item["period"],
                        signals,
                        floor_rate,
                    ),
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
            "allocationModel": response.get("allocation_model"),
            "poolAllocation": response.get("pool_allocation", {}),
            "targetOfferAmounts": response.get("target_offer_amounts", {}),
            "currentOfferAmounts": response.get("current_offer_amounts", {}),
            "deviationAmounts": response.get("deviation_amounts", {}),
            "rebalanceCancellations": response["rebalance_cancellations"],
            "periodSelection": response.get("period_selection", {}),
            "poolRedistributionBasis": response.get("pool_redistribution_basis"),
            "ineligiblePools": response.get("ineligible_pools", ()),
            "releasedPoolAmount": response.get("released_pool_amount", D("0")),
            "poolCapPercentages": response.get("pool_cap_percentages", {}),
            "primaryTermMaxShare": response.get("primary_term_max_share"),
            "termAllocations": response.get("term_allocations", {}),
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
        rows = []
        for period in sorted({int(row.get("period", 2)) for row in recent}):
            period_rows = [row for row in recent if int(row.get("period", 2)) == period]
            median = weighted_quantile(period_rows, D("0.5"))
            q25 = weighted_quantile(period_rows, D("0.25"))
            if median <= 0:
                continue
            volume = max(
                USD_ORDER_CHUNK,
                sum((abs(D(row["amount"])) for row in period_rows), D("0")),
            )
            rows.extend(
                (
                    {"rate": median, "period": period, "count": len(period_rows), "amount": volume},
                    {"rate": q25 or median, "period": period, "count": len(period_rows), "amount": -volume},
                )
            )
        return rows

    def aggregate_signal_trades(rows, mts):
        aggregated = []
        periods = sorted({int(row.get("period", 2)) for row in rows})
        for period in periods:
            weighted_rate = D("0")
            total_volume = D("0")
            for row in rows:
                if int(row.get("period", 2)) != period:
                    continue
                try:
                    volume = abs(D(row["amount"]))
                    rate = D(row["rate"])
                except (KeyError, TypeError, ValueError, ArithmeticError):
                    continue
                if volume <= 0 or rate <= 0:
                    continue
                weighted_rate += rate * volume
                total_volume += volume
            if total_volume > 0:
                aggregated.append(
                    {
                        "mts": int(mts),
                        "rate": weighted_rate / total_volume,
                        "amount": total_volume,
                        "period": period,
                    }
                )
        return aggregated

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
        if available >= USD_ORDER_CHUNK and signal_history and interval_trades:
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
                if not eligible or available < USD_ORDER_CHUNK:
                    continue
                eligible.sort(key=lambda key: key[1])
                volume = sum((remaining_volume[key] for key in eligible), D("0"))
                fill_amount = min(order["amount"], available, volume)
                if fill_amount < USD_ORDER_CHUNK:
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
        signal_history.extend(aggregate_signal_trades(interval_trades, interval_end - 1))
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
