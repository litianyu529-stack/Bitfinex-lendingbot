import copy
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, ROUND_DOWN

from FileUtils import atomic_write_text


SATOSHI = Decimal("0.00000001")
RATE_UNDERCUT = Decimal("0.000001")
OFFER_TYPES = {"AUTO", "LIMIT", "FRRDELTAFIX", "FRRDELTAVAR"}
BUCKETS = ("fast", "balanced", "long")
WINDOW_MS = {"24h": 24 * 60 * 60 * 1000, "7d": 7 * 24 * 60 * 60 * 1000, "30d": 30 * 24 * 60 * 60 * 1000}


@dataclass(frozen=True)
class StrategyPolicy:
    version: int = 2
    profile: str = "balanced_yield"
    auto_order_types: bool = True
    replay_window: str = "7d"
    fast_share: Decimal = Decimal("50")
    long_share: Decimal = Decimal("40")
    fast_period: int = 2
    balanced_period: int = 7
    long_period: int = 60
    fast_wait_minutes: Decimal = Decimal("10")
    balanced_wait_minutes: Decimal = Decimal("30")
    long_wait_minutes: Decimal = Decimal("120")
    rate_offset: Decimal = Decimal("0.00001")
    long_premium: Decimal = Decimal("0.0001")
    fast_depth: Decimal = Decimal("5")
    balanced_depth: Decimal = Decimal("150")
    long_depth: Decimal = Decimal("300")
    floor_depth: Decimal = Decimal("2")
    trend_min_delta: Decimal = Decimal("0.00002")
    utilization_low: Decimal = Decimal("0.65")
    utilization_high: Decimal = Decimal("0.85")
    reprice_min_delta: Decimal = Decimal("0.00002")
    fast_order_type: str = "AUTO"
    balanced_order_type: str = "AUTO"
    long_order_type: str = "AUTO"
    fast_frr_offset: Decimal = Decimal("0")
    balanced_frr_offset: Decimal = Decimal("0")
    long_frr_offset: Decimal = Decimal("0")

    @property
    def balanced_share(self):
        return Decimal("100") - self.fast_share - self.long_share


PRESETS = {
    "utilization": StrategyPolicy(
        profile="utilization",
        fast_share=Decimal("65"),
        long_share=Decimal("10"),
        fast_period=2,
        balanced_period=7,
        long_period=30,
        fast_wait_minutes=Decimal("5"),
        balanced_wait_minutes=Decimal("15"),
        long_wait_minutes=Decimal("60"),
        long_premium=Decimal("0.00005"),
    ),
    "balanced_yield": StrategyPolicy(),
    "yield": StrategyPolicy(
        profile="yield",
        fast_share=Decimal("30"),
        long_share=Decimal("60"),
        fast_period=2,
        balanced_period=14,
        long_period=90,
        fast_wait_minutes=Decimal("15"),
        balanced_wait_minutes=Decimal("60"),
        long_wait_minutes=Decimal("240"),
        long_premium=Decimal("0.0002"),
    ),
}


POLICY_FIELD_TYPES = {
    "version": int,
    "profile": str,
    "auto_order_types": bool,
    "replay_window": str,
    "fast_share": Decimal,
    "long_share": Decimal,
    "fast_period": int,
    "balanced_period": int,
    "long_period": int,
    "fast_wait_minutes": Decimal,
    "balanced_wait_minutes": Decimal,
    "long_wait_minutes": Decimal,
    "rate_offset": Decimal,
    "long_premium": Decimal,
    "fast_depth": Decimal,
    "balanced_depth": Decimal,
    "long_depth": Decimal,
    "floor_depth": Decimal,
    "trend_min_delta": Decimal,
    "utilization_low": Decimal,
    "utilization_high": Decimal,
    "reprice_min_delta": Decimal,
    "fast_order_type": str,
    "balanced_order_type": str,
    "long_order_type": str,
    "fast_frr_offset": Decimal,
    "balanced_frr_offset": Decimal,
    "long_frr_offset": Decimal,
}


def preset_policy(profile="balanced_yield"):
    normalized = str(profile or "balanced_yield").strip().lower()
    return copy.deepcopy(PRESETS.get(normalized, PRESETS["balanced_yield"]))


def _to_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def policy_with_overrides(base, values):
    updates = {}
    for name, converter in POLICY_FIELD_TYPES.items():
        if name not in values or values[name] is None or values[name] == "":
            continue
        value = values[name]
        if converter is bool:
            updates[name] = _to_bool(value)
        elif converter is Decimal:
            updates[name] = Decimal(str(value))
        elif converter is str:
            updates[name] = str(value).strip()
        else:
            updates[name] = converter(value)
    return replace(base, **updates)


def validate_policy(policy):
    if policy.version != 2:
        raise ValueError("strategy version must be 2")
    if policy.profile not in {"utilization", "balanced_yield", "yield", "custom"}:
        raise ValueError("unknown strategy profile")
    if policy.replay_window not in WINDOW_MS:
        raise ValueError("replay window must be 24h, 7d, or 30d")
    if policy.fast_share < 0 or policy.long_share < 0 or policy.fast_share + policy.long_share > 100:
        raise ValueError("fast and long shares must be non-negative and total no more than 100")
    for name in ("fast_period", "balanced_period", "long_period"):
        value = getattr(policy, name)
        if value < 2 or value > 120:
            raise ValueError(f"{name} must be 2-120 days")
    for name in ("fast_wait_minutes", "balanced_wait_minutes", "long_wait_minutes"):
        value = getattr(policy, name)
        if value < 1 or value > 1440:
            raise ValueError(f"{name} must be 1-1440 minutes")
    for name in ("fast_depth", "balanced_depth", "long_depth"):
        value = getattr(policy, name)
        if value < 0 or value > 10000:
            raise ValueError(f"{name} must be 0-10000 percent")
    if policy.floor_depth < 0 or policy.floor_depth > 100:
        raise ValueError("floor_depth must be 0-100 percent")
    if not (Decimal("0") <= policy.utilization_low < policy.utilization_high <= Decimal("1")):
        raise ValueError("utilization thresholds must satisfy 0 <= low < high <= 1")
    for bucket in BUCKETS:
        offer_type = getattr(policy, f"{bucket}_order_type").upper()
        if offer_type not in OFFER_TYPES:
            raise ValueError(f"unsupported {bucket} order type")
        if offer_type == "FRRDELTAVAR" and getattr(policy, f"{bucket}_frr_offset") < 0:
            raise ValueError(f"{bucket} FRRDELTAVAR offset cannot be negative")
    return policy


def policy_to_json(policy):
    result = {}
    for key, value in asdict(policy).items():
        result[key] = str(value) if isinstance(value, Decimal) else value
    result["balanced_share"] = str(policy.balanced_share)
    return result


def parse_funding_trades(rows):
    trades = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        try:
            rate = Decimal(str(row[3]))
            amount = abs(Decimal(str(row[2])))
            period = int(row[4])
            mts = int(row[1])
        except (ValueError, TypeError, ArithmeticError):
            continue
        if rate <= 0 or amount <= 0 or period < 2 or period > 120:
            continue
        trades.append({"id": str(row[0]), "mts": mts, "amount": amount, "rate": rate, "period": period})
    return sorted(trades, key=lambda item: item["mts"])


def parse_funding_stats(rows):
    stats = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 9:
            continue
        try:
            mts = int(row[0])
            frr_daily = Decimal(str(row[3])) * Decimal("365")
            average_period = Decimal(str(row[4]))
            provided = abs(Decimal(str(row[7])))
            used = abs(Decimal(str(row[8])))
        except (ValueError, TypeError, ArithmeticError):
            continue
        utilization = Decimal("0") if provided <= 0 else min(Decimal("1"), used / provided)
        stats.append({
            "mts": mts,
            "frr_daily_rate": frr_daily,
            "average_period": average_period,
            "provided": provided,
            "used": used,
            "utilization": utilization,
        })
    return sorted(stats, key=lambda item: item["mts"])


def weighted_quantile(items, quantile, value_key="rate", weight_key="amount"):
    valid = [item for item in items if item.get(weight_key, Decimal("0")) > 0]
    if not valid:
        return Decimal("0")
    ordered = sorted(valid, key=lambda item: item[value_key])
    total = sum((Decimal(item[weight_key]) for item in ordered), Decimal("0"))
    threshold = total * Decimal(str(quantile))
    cumulative = Decimal("0")
    for item in ordered:
        cumulative += Decimal(item[weight_key])
        if cumulative >= threshold:
            return Decimal(item[value_key])
    return Decimal(ordered[-1][value_key])


def winsorized_trades(trades):
    if not trades:
        return []
    low = weighted_quantile(trades, Decimal("0.05"))
    high = weighted_quantile(trades, Decimal("0.95"))
    return [dict(item, rate=max(low, min(high, item["rate"]))) for item in trades]


def _median(values):
    ordered = sorted(Decimal(value) for value in values if value is not None and Decimal(value) > 0)
    if not ordered:
        return Decimal("0")
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def clamp_rate(rate, minimum, maximum):
    return max(Decimal(minimum), min(Decimal(maximum), Decimal(rate)))


def build_market_signals(book_reference, trades, stats, policy, min_rate, max_rate, now_ms=None):
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    recent_24h = winsorized_trades([item for item in trades if item["mts"] >= now - WINDOW_MS["24h"]])
    recent_1h = [item for item in recent_24h if item["mts"] >= now - 60 * 60 * 1000]
    median_24h = weighted_quantile(recent_24h, Decimal("0.5"))
    median_1h = weighted_quantile(recent_1h, Decimal("0.5")) if recent_1h else median_24h
    q25 = weighted_quantile(recent_24h, Decimal("0.25"))
    q75 = weighted_quantile(recent_24h, Decimal("0.75"))
    iqr = max(Decimal("0"), q75 - q25)
    latest_stat = stats[-1] if stats else None
    current_frr = latest_stat["frr_daily_rate"] if latest_stat else Decimal("0")
    utilization = latest_stat["utilization"] if latest_stat else None
    frr_items = [
        {"rate": item["frr_daily_rate"], "amount": Decimal("1")}
        for item in stats
        if item["mts"] >= now - WINDOW_MS["7d"] and item["frr_daily_rate"] > 0
    ]
    frr_p25 = weighted_quantile(frr_items, Decimal("0.25"))
    anchor = clamp_rate(_median([book_reference, median_24h, current_frr]), min_rate, max_rate)
    threshold = max(policy.trend_min_delta, iqr * Decimal("0.25"))
    change = median_1h - median_24h if median_24h > 0 else Decimal("0")
    if change >= threshold or (utilization is not None and utilization >= policy.utilization_high):
        regime = "rising"
    elif change <= -threshold and utilization is not None and utilization <= policy.utilization_low:
        regime = "falling"
    else:
        regime = "neutral"
    warnings = []
    if not recent_24h:
        warnings.append("最近24小时没有可用资金成交数据，定价退回盘口与FRR。")
    if not stats:
        warnings.append("Funding Stats 不可用，FRR订单将退回固定限价。")
    return {
        "as_of": now,
        "regime": regime,
        "book_reference": Decimal(book_reference),
        "trade_median_1h": median_1h,
        "trade_median_24h": median_24h,
        "trade_q25_24h": q25,
        "trade_q75_24h": q75,
        "trade_iqr_24h": iqr,
        "trend_change": change,
        "trend_threshold": threshold,
        "frr_daily_rate": current_frr,
        "frr_p25_7d": frr_p25,
        "average_period": latest_stat["average_period"] if latest_stat else Decimal("0"),
        "utilization": utilization,
        "anchor_rate": anchor,
        "trade_count_24h": len(recent_24h),
        "warnings": warnings,
    }


def signals_to_json(signals):
    result = {}
    for key, value in signals.items():
        if isinstance(value, Decimal):
            result[key] = format(value, "f")
        else:
            result[key] = value
    return result


def _auto_offer_type(bucket, regime):
    if bucket == "fast":
        return "LIMIT"
    if regime == "rising":
        return "LIMIT" if bucket == "long" else "FRRDELTAFIX"
    if regime == "falling":
        return "FRRDELTAVAR"
    return "FRRDELTAFIX"


def choose_offer_type(policy, bucket, signals, min_rate):
    requested = getattr(policy, f"{bucket}_order_type").upper()
    offer_type = _auto_offer_type(bucket, signals["regime"]) if policy.auto_order_types or requested == "AUTO" else requested
    warning = ""
    if offer_type in {"FRRDELTAFIX", "FRRDELTAVAR"} and signals["frr_daily_rate"] <= 0:
        offer_type = "LIMIT"
        warning = "FRR不可用，已退回固定限价。"
    if offer_type == "FRRDELTAVAR" and (
        signals["frr_daily_rate"] < min_rate or signals["frr_p25_7d"] < min_rate
    ):
        offer_type = "LIMIT"
        warning = "当前或历史FRR低于最低利率，已退回固定限价。"
    return offer_type, warning


def weighted_offer_amounts(principal, min_loan_size, shares):
    principal = Decimal(principal)
    minimum = Decimal(min_loan_size)
    possible = min(len(shares), int(principal // minimum))
    if possible < 1:
        return []
    selected = [Decimal(value) for value in shares[:possible]]
    total_weight = sum(selected, Decimal("0"))
    if total_weight <= 0:
        selected = [Decimal("1")] * possible
        total_weight = Decimal(possible)
    selected = [value / total_weight for value in selected]
    remainder = principal - minimum * Decimal(possible)
    amounts = []
    allocated = Decimal("0")
    for index, weight in enumerate(selected):
        amount = principal - allocated if index == possible - 1 else minimum + remainder * weight
        amount = amount.quantize(SATOSHI, rounding=ROUND_DOWN)
        amounts.append(amount)
        allocated += amount
    return amounts


def build_strategy_plan(principal, min_loan_size, min_rate, max_rate, policy, signals, max_parts=3):
    validate_policy(policy)
    balanced_share = max(Decimal("0"), policy.balanced_share)
    if int(max_parts) <= 1:
        shares = [Decimal("100")]
        buckets = ["fast"]
    elif int(max_parts) == 2:
        shares = [policy.fast_share, policy.long_share]
        buckets = ["fast", "long"]
    else:
        shares = [policy.fast_share, balanced_share, policy.long_share]
        buckets = list(BUCKETS)
    amounts = weighted_offer_amounts(principal, min_loan_size, shares)
    if len(amounts) == 1:
        buckets = ["fast"]
    elif len(amounts) == 2:
        buckets = ["fast", "long"]
    book_rate = signals.get("fast_book_rate") or signals["book_reference"] or signals["anchor_rate"]
    balanced_book_rate = signals.get("balanced_book_rate") or signals["anchor_rate"]
    long_book_rate = signals.get("long_book_rate") or signals["anchor_rate"]
    short_rate = signals["trade_median_1h"] or signals["anchor_rate"]
    fast_rate = clamp_rate(min(book_rate, short_rate) - RATE_UNDERCUT, min_rate, max_rate)
    balanced_rate = clamp_rate(
        max(fast_rate, balanced_book_rate, signals["anchor_rate"] + policy.rate_offset),
        min_rate,
        max_rate,
    )
    long_rate = clamp_rate(
        max(balanced_rate, long_book_rate, signals["trade_q75_24h"], signals["anchor_rate"] + policy.long_premium),
        min_rate,
        max_rate,
    )
    targets = {"fast": fast_rate, "balanced": balanced_rate, "long": long_rate}
    plan = []
    for bucket, amount in zip(buckets, amounts):
        target = targets[bucket]
        offer_type, warning = choose_offer_type(policy, bucket, signals, Decimal(min_rate))
        offset_override = getattr(policy, f"{bucket}_frr_offset")
        if offer_type == "LIMIT":
            submitted_rate = target
            effective_rate = target
        else:
            calculated_offset = target - signals["frr_daily_rate"]
            requested_type = getattr(policy, f"{bucket}_order_type").upper()
            submitted_rate = calculated_offset if policy.auto_order_types or requested_type == "AUTO" else offset_override
            if offer_type == "FRRDELTAVAR":
                submitted_rate = max(Decimal("0"), submitted_rate)
            effective_rate = signals["frr_daily_rate"] + submitted_rate
        period = getattr(policy, f"{bucket}_period")
        reason = {
            "fast": "使用竞争价优先降低闲置资金。",
            "balanced": "围绕稳健市场锚兼顾成交和收益。",
            "long": "参考高分位成交价等待长期高息机会。",
        }[bucket]
        plan.append({
            "bucket": bucket,
            "amount": amount,
            "share": Decimal("0") if Decimal(principal) <= 0 else amount / Decimal(principal) * Decimal("100"),
            "offer_type": offer_type,
            "submitted_rate": submitted_rate,
            "effective_rate": effective_rate,
            "target_rate": target,
            "period": period,
            "wait_minutes": getattr(policy, f"{bucket}_wait_minutes"),
            "reason": reason,
            "warning": warning,
        })
    return plan


def plan_to_json(plan):
    result = []
    for item in plan:
        converted = {}
        for key, value in item.items():
            converted[key] = format(value, "f") if isinstance(value, Decimal) else value
        result.append(converted)
    return result


def replay_strategy(policy, trades, stats, principal, min_loan_size, min_rate, max_rate, window="7d", now_ms=None):
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    window_ms = WINDOW_MS.get(window, WINDOW_MS["7d"])
    start = now - window_ms
    selected = [item for item in trades if start <= item["mts"] <= now]
    if not selected:
        return {
            "window": window,
            "sampleCount": 0,
            "estimatedUtilizationPercent": "0",
            "weightedMatchedDailyRatePercent": "0",
            "longFillSharePercent": "0",
            "medianWaitMinutes": None,
            "estimatedReprices": 0,
            "disclaimer": "历史情景回放，并非完整盘口回测或收益保证。",
        }
    interval_ms = 15 * 60 * 1000
    first_bucket = (selected[0]["mts"] // interval_ms) * interval_ms
    offered = Decimal("0")
    filled = Decimal("0")
    matched_rate_amount = Decimal("0")
    long_filled = Decimal("0")
    waits = []
    reprices = 0
    samples = 0
    previous_targets = {}
    cursor = first_bucket
    while cursor < now:
        history = [item for item in trades if cursor - WINDOW_MS["24h"] <= item["mts"] < cursor]
        future = [item for item in selected if cursor <= item["mts"] < cursor + interval_ms]
        if history:
            book_reference = weighted_quantile(winsorized_trades(history), Decimal("0.5"))
            historical_stats = [item for item in stats if item["mts"] <= cursor]
            signals = build_market_signals(book_reference, history, historical_stats, policy, min_rate, max_rate, cursor)
            plan = build_strategy_plan(principal, min_loan_size, min_rate, max_rate, policy, signals)
            samples += 1
            remaining = {trade["id"]: trade["amount"] for trade in future}
            for item in plan:
                offered += item["amount"]
                matches = [
                    trade for trade in future
                    if remaining.get(trade["id"], Decimal("0")) > 0
                    and trade["rate"] >= item["effective_rate"]
                    and trade["period"] <= item["period"]
                ]
                match_amount = min(
                    item["amount"],
                    sum((remaining[trade["id"]] for trade in matches), Decimal("0")),
                )
                if match_amount > 0:
                    filled += match_amount
                    matched_rows = []
                    amount_left = match_amount
                    for trade in matches:
                        allocated = min(amount_left, remaining[trade["id"]])
                        if allocated <= 0:
                            continue
                        matched_rows.append(dict(trade, amount=allocated))
                        remaining[trade["id"]] -= allocated
                        amount_left -= allocated
                        if amount_left <= 0:
                            break
                    match_rate = weighted_quantile(matched_rows, Decimal("0.5"))
                    matched_rate_amount += match_amount * match_rate
                    if item["bucket"] == "long":
                        long_filled += match_amount
                    waits.append(Decimal("15"))
                old = previous_targets.get(item["bucket"])
                if old is not None and abs(old - item["target_rate"]) > policy.reprice_min_delta:
                    reprices += 1
                previous_targets[item["bucket"]] = item["target_rate"]
        cursor += interval_ms
    utilization = Decimal("0") if offered <= 0 else filled / offered * Decimal("100")
    average_rate = Decimal("0") if filled <= 0 else matched_rate_amount / filled * Decimal("100")
    long_share = Decimal("0") if filled <= 0 else long_filled / filled * Decimal("100")
    waits.sort()
    median_wait = None if not waits else waits[len(waits) // 2]
    return {
        "window": window,
        "sampleCount": samples,
        "estimatedUtilizationPercent": format(utilization, "f"),
        "weightedMatchedDailyRatePercent": format(average_rate, "f"),
        "longFillSharePercent": format(long_share, "f"),
        "medianWaitMinutes": None if median_wait is None else format(median_wait, "f"),
        "estimatedReprices": reprices,
        "disclaimer": "历史情景回放，并非完整盘口回测或收益保证。",
    }


class PublicMarketCache:
    def __init__(self, max_requests_per_minute=12, stale_seconds=1800):
        self.max_requests_per_minute = int(max_requests_per_minute)
        self.stale_seconds = int(stale_seconds)
        self._entries = {}
        self._requests = []
        self._lock = threading.RLock()

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._requests.clear()

    def _allow_request(self, now):
        cutoff = now - 60
        self._requests = [item for item in self._requests if item > cutoff]
        if len(self._requests) >= self.max_requests_per_minute:
            return False
        self._requests.append(now)
        return True

    def get(self, key, ttl_seconds, fetcher, now=None):
        current = float(now if now is not None else time.time())
        with self._lock:
            entry = self._entries.get(key)
            if entry and current - entry["fetched_at"] <= ttl_seconds:
                return entry["value"], False, ""
            if not self._allow_request(current):
                if entry and current - entry["fetched_at"] <= self.stale_seconds:
                    return entry["value"], True, "公共接口达到限速，正在使用缓存数据。"
                return [], True, "公共接口达到限速且没有可用缓存。"
        try:
            value = fetcher()
        except Exception as exc:
            with self._lock:
                entry = self._entries.get(key)
                if entry and current - entry["fetched_at"] <= self.stale_seconds:
                    return entry["value"], True, f"公共接口失败，正在使用缓存：{exc}"
            return [], True, f"公共接口失败且没有可用缓存：{exc}"
        with self._lock:
            self._entries[key] = {"value": value, "fetched_at": current}
        return value, False, ""


class ManagedOfferRegistry:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._offers = {}
        self.load_error = ""
        self.load()

    def load(self):
        with self._lock:
            self._offers = {}
            self.load_error = ""
            if not self.path or not os.path.exists(self.path):
                return
            try:
                with open(self.path, "r", encoding="utf-8") as file:
                    payload = json.load(file)
                offers = payload.get("offers", {}) if isinstance(payload, dict) else {}
                if not isinstance(offers, dict):
                    raise ValueError("offers must be an object")
                self._offers = {str(key): value for key, value in offers.items() if isinstance(value, dict)}
            except (OSError, ValueError, TypeError) as exc:
                self.load_error = str(exc)
                self._offers = {}

    def _persist(self):
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        atomic_write_text(
            self.path,
            json.dumps({"version": 1, "offers": self._offers}, ensure_ascii=False, indent=2),
        )

    def record(self, offer_id, currency, bucket, offer_type, config_hash, created_at=None):
        with self._lock:
            self._offers[str(offer_id)] = {
                "currency": str(currency).upper(),
                "bucket": bucket,
                "offerType": offer_type,
                "configHash": config_hash,
                "createdAt": int(created_at if created_at is not None else time.time() * 1000),
            }
            self._persist()

    def remove(self, offer_id):
        with self._lock:
            if self._offers.pop(str(offer_id), None) is not None:
                self._persist()

    def reconcile(self, active_offer_ids):
        active = {str(value) for value in active_offer_ids}
        with self._lock:
            changed = False
            for offer_id in list(self._offers):
                if offer_id not in active:
                    self._offers.pop(offer_id, None)
                    changed = True
            if changed:
                self._persist()

    def is_managed(self, offer_id):
        return str(offer_id) in self._offers

    def metadata(self, offer_id):
        return dict(self._offers.get(str(offer_id), {}))


def extract_submitted_offer_id(response):
    if not isinstance(response, list) or len(response) < 5:
        return None
    offer = response[4]
    if isinstance(offer, list) and offer:
        if isinstance(offer[0], list):
            offer = offer[0]
        if offer and not isinstance(offer[0], (list, dict)):
            return offer[0]
    if isinstance(offer, dict):
        return offer.get("id") or offer.get("ID")
    return None
