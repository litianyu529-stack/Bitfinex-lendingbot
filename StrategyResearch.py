"""Offline-only market backfill and chronological V3 strategy evaluation.

Nothing in this module calls an authenticated write endpoint.  It intentionally
depends only on public market reads, SQLite snapshots, and the pure V3 simulator.
"""

import hashlib
import json
import random
import time
from dataclasses import replace
from decimal import Decimal

from ExchangeModels import parse_funding_stats, parse_funding_trades
from FileUtils import atomic_write_text
from StrategyV3 import (
    SCORE_MODEL_VERSION,
    V3_RESEARCH_SCORE_WEIGHTS,
    json_decimal,
    replay_strategy_v3,
    validate_policy_v3,
)
from bitfinex import BitfinexApiError


D = Decimal
DAY_MS = 86_400_000
REQUIRED_RESEARCH_DAYS = 90


class PublicRateLimiter:
    def __init__(self, minimum_interval_seconds=4.1, clock=time.monotonic, sleeper=time.sleep):
        self.minimum_interval = max(0.0, float(minimum_interval_seconds))
        self.clock = clock
        self.sleeper = sleeper
        self.last_request = None

    def wait(self):
        if self.last_request is not None:
            remaining = self.minimum_interval - (self.clock() - self.last_request)
            if remaining > 0:
                self.sleeper(remaining)
        self.last_request = self.clock()


def _public_read(action, limiter, retry_sleeper, max_attempts=5):
    """Retry only rate-limited public reads; authenticated writes never use this path."""
    for attempt in range(int(max_attempts)):
        limiter.wait()
        try:
            return action()
        except BitfinexApiError as exc:
            message = str(exc).lower()
            if "http 429" not in message and "ratelimit" not in message:
                raise
            if attempt + 1 >= int(max_attempts):
                raise
            interval = float(getattr(limiter, "minimum_interval", 4.1))
            retry_sleeper(min(120.0, max(30.0, interval * 4) * (2**attempt)))


def _coverage(rows):
    timestamps = [int(row["mts"]) for row in rows or [] if row.get("mts")]
    return {
        "count": len(rows or []),
        "earliestMs": min(timestamps) if timestamps else None,
        "latestMs": max(timestamps) if timestamps else None,
    }


def backfill_public_market_data(
    client,
    store,
    days=REQUIRED_RESEARCH_DAYS,
    now_ms=None,
    page_limit=10000,
    minimum_interval_seconds=4.1,
    rate_limiter=None,
    retry_sleeper=time.sleep,
):
    """Page public fUSD trades/stats without any authenticated or write call."""
    days = int(days)
    if days < REQUIRED_RESEARCH_DAYS:
        raise ValueError("market backfill requires at least 90 days")
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    start = now - days * DAY_MS
    limit = min(10000, max(1, int(page_limit)))
    stats_limit = min(250, limit)
    limiter = rate_limiter or PublicRateLimiter(minimum_interval_seconds)

    existing_trades = store.market_trades(start, now)
    existing_trade_coverage = _coverage(existing_trades)
    trade_cursor = start
    if existing_trade_coverage["earliestMs"] is not None and existing_trade_coverage["earliestMs"] <= start + DAY_MS:
        trade_cursor = int(existing_trade_coverage["latestMs"]) + 1
    trade_pages = 0
    while trade_cursor < now:
        raw = _public_read(
            lambda: client.funding_trades("fUSD", start=trade_cursor, end=now, limit=limit, sort=1),
            limiter,
            retry_sleeper,
        )
        parsed = parse_funding_trades(raw)
        store.upsert_market_trades(parsed)
        trade_pages += 1
        if len(raw or []) < limit:
            break
        if not parsed:
            raise RuntimeError("funding trade page contained no usable cursor")
        next_cursor = max(int(row["mts"]) for row in parsed) + 1
        if next_cursor <= trade_cursor:
            raise RuntimeError("funding trade pagination cursor did not advance")
        trade_cursor = next_cursor

    # Funding stats are returned newest-first by the public history endpoint, so
    # page backwards by end timestamp.  Upserts make restart/repetition idempotent.
    existing_stats = store.funding_stats(start, now)
    existing_stats_coverage = _coverage(existing_stats)
    stats_cursor = now
    if existing_stats_coverage["latestMs"] is not None and existing_stats_coverage["latestMs"] >= now - 15 * 60 * 1000:
        stats_cursor = int(existing_stats_coverage["earliestMs"]) - 1
    stats_pages = 0
    while stats_cursor >= start:
        raw = _public_read(
            lambda: client.funding_stats("fUSD", start=start, end=stats_cursor, limit=stats_limit),
            limiter,
            retry_sleeper,
        )
        parsed = parse_funding_stats(raw)
        store.upsert_funding_stats(parsed)
        stats_pages += 1
        if len(raw or []) < stats_limit:
            break
        if not parsed:
            raise RuntimeError("funding stats page contained no usable cursor")
        next_cursor = min(int(row["mts"]) for row in parsed) - 1
        if next_cursor >= stats_cursor:
            raise RuntimeError("funding stats pagination cursor did not move backwards")
        stats_cursor = next_cursor

    trades = store.market_trades(start, now)
    stats = store.funding_stats(start, now)
    trade_coverage = _coverage(trades)
    stats_coverage = _coverage(stats)
    tolerance = DAY_MS
    complete = bool(
        trade_coverage["earliestMs"] is not None
        and trade_coverage["earliestMs"] <= start + tolerance
        and stats_coverage["earliestMs"] is not None
        and stats_coverage["earliestMs"] <= start + tolerance
    )
    return {
        "symbol": "fUSD",
        "requestedDays": days,
        "startMs": start,
        "endMs": now,
        "tradePages": trade_pages,
        "statsPages": stats_pages,
        "trades": trade_coverage,
        "stats": stats_coverage,
        "complete": complete,
        "bookHistory": {
            "backfillable": False,
            "note": "Historical order-book snapshots are unavailable; minute snapshots begin at deployment.",
        },
    }


def _policy_id(policy):
    encoded = json.dumps(json_decimal(policy.__dict__), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _safe_variant(base, **changes):
    policy = replace(base, **changes)
    validate_policy_v3(policy)
    hard_fields = (
        "currency",
        "short_floor_apr",
        "medium_floor_apr",
        "long_floor_apr",
        "max_lend_amount",
        "max_lend_percent",
        "normal_fee_rate",
        "hidden_fee_rate",
        "variable_max_share",
        "hidden_max_share",
    )
    if any(getattr(policy, name) != getattr(base, name) for name in hard_fields):
        raise ValueError("research variant changed a hard safety boundary")
    return policy


def research_variants(active_policy):
    """Versioned variants limited to term pools, layers, order types, and repricing."""
    return {
        "current_v3": active_policy,
        "frr_only": _safe_variant(
            active_policy,
            enable_limit=False,
            enable_frr=True,
            enable_frr_delta_fixed=False,
            enable_frr_delta_variable=False,
            enable_hidden=False,
        ),
        "limit_near_fill": _safe_variant(
            active_policy,
            enable_limit=True,
            enable_frr=False,
            enable_frr_delta_fixed=False,
            enable_frr_delta_variable=False,
            enable_hidden=False,
            quick_share=D("60"),
            balanced_share=D("30"),
            high_share=D("10"),
        ),
        "candidate_short_cycle": _safe_variant(
            active_policy,
            quick_share=D("55"),
            balanced_share=D("35"),
            high_share=D("10"),
            minimum_offer_minutes=max(5, active_policy.minimum_offer_minutes),
        ),
        "candidate_term_capture": _safe_variant(
            active_policy,
            short_share=D("40"),
            medium_share=D("35"),
            long_share=D("25"),
            balanced_share=D("45"),
            high_share=D("25"),
            quick_share=D("30"),
            reprice_cooldown_minutes=max(15, active_policy.reprice_cooldown_minutes),
        ),
    }


def _window_metrics(policy, trades, stats, principal, start_ms, end_ms):
    result = replay_strategy_v3(
        policy,
        trades,
        stats,
        principal,
        book=None,
        now_ms=end_ms,
        window_ms=end_ms - start_ms,
    )
    elapsed_days = D(max(1, end_ms - start_ms)) / D(DAY_MS)
    principal_time = D(principal) * elapsed_days
    net_interest = D(result["netInterest"])
    fills = result.get("fills", [])
    long_fills = [row for row in fills if int(row.get("period", 0)) >= 30]
    return {
        "netInterest": net_interest,
        "returnOnPrincipalTime": D("0") if principal_time <= 0 else net_interest / principal_time,
        "netAprPercent": D(result["actualNetAprPercent"]),
        "utilizationPercent": D(result["estimatedUtilizationPercent"]),
        "fillCount": len(fills),
        "averageWaitMinutes": D("0") if fills else elapsed_days * D("1440"),
        "cancellationRatePercent": D("0"),
        "longOccupancyPercent": D("0") if not fills else D(len(long_fills)) * D("100") / D(len(fills)),
        "sampleCount": int(result["sampleCount"]),
    }


def _daily_returns(policy, trades, stats, principal, start_ms, end_ms):
    values = []
    cursor = start_ms
    while cursor < end_ms:
        day_end = min(end_ms, cursor + DAY_MS)
        values.append(_window_metrics(policy, trades, stats, principal, cursor, day_end)["returnOnPrincipalTime"])
        cursor = day_end
    return values


def paired_bootstrap_interval(candidate, baseline, iterations=2000, seed=307):
    paired = [D(left) - D(right) for left, right in zip(candidate, baseline)]
    if not paired:
        return {"lower": D("0"), "median": D("0"), "upper": D("0"), "samples": 0}
    randomizer = random.Random(seed)
    means = []
    for _ in range(int(iterations)):
        draw = [paired[randomizer.randrange(len(paired))] for _ in paired]
        means.append(sum(draw, D("0")) / D(len(draw)))
    means.sort()
    last = len(means) - 1
    return {
        "lower": means[int(last * 0.025)],
        "median": means[int(last * 0.5)],
        "upper": means[int(last * 0.975)],
        "samples": len(paired),
    }


def evaluate_strategies(store, active_policy, principal, days=90, now_ms=None):
    """Run fixed chronological 60/15/15 train/validation/test evaluation."""
    days = int(days)
    if days < REQUIRED_RESEARCH_DAYS:
        raise ValueError("strategy evaluation requires at least 90 days")
    principal = D(str(principal))
    if principal <= 0:
        raise ValueError("evaluation principal must be positive")
    validate_policy_v3(active_policy)
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    test_end = now
    test_start = test_end - 15 * DAY_MS
    validation_start = test_start - 15 * DAY_MS
    train_start = validation_start - 60 * DAY_MS
    trades = store.market_trades(train_start, test_end)
    stats = store.funding_stats(train_start, test_end)
    if not trades:
        raise ValueError("no public funding trades are available for evaluation")

    windows = {
        "train": (train_start, validation_start),
        "validation": (validation_start, test_start),
        "test": (test_start, test_end),
    }
    variants = research_variants(active_policy)
    metrics = {
        name: {split: _window_metrics(policy, trades, stats, principal, *bounds) for split, bounds in windows.items()}
        for name, policy in variants.items()
    }

    candidate_names = [name for name in variants if name.startswith("candidate_")]
    selected_name = max(
        candidate_names,
        key=lambda name: metrics[name]["train"]["returnOnPrincipalTime"],
    )
    baseline_name = "current_v3"
    selected = variants[selected_name]
    validation_gain = (
        metrics[selected_name]["validation"]["returnOnPrincipalTime"]
        - metrics[baseline_name]["validation"]["returnOnPrincipalTime"]
    )
    test_gain = (
        metrics[selected_name]["test"]["returnOnPrincipalTime"]
        - metrics[baseline_name]["test"]["returnOnPrincipalTime"]
    )
    selected_daily = _daily_returns(selected, trades, stats, principal, test_start, test_end)
    baseline_daily = _daily_returns(active_policy, trades, stats, principal, test_start, test_end)
    confidence = paired_bootstrap_interval(selected_daily, baseline_daily)
    invariants = {
        "floorsConfigured": all(selected.floor_apr(pool) is not None for pool in ("short", "medium", "long")),
        "hardBoundariesUnchanged": all(
            getattr(selected, name) == getattr(active_policy, name)
            for name in (
                "currency",
                "short_floor_apr",
                "medium_floor_apr",
                "long_floor_apr",
                "max_lend_amount",
                "max_lend_percent",
                "normal_fee_rate",
                "hidden_fee_rate",
                "variable_max_share",
                "hidden_max_share",
            )
        ),
        "periodsWithinExchangeRange": all(
            2 <= period <= 120 for pool in ("short", "medium", "long") for period in selected.periods(pool)
        ),
    }
    eligible = bool(all(invariants.values()) and validation_gain > 0 and test_gain > 0 and confidence["lower"] > 0)
    books = store.book_snapshots(train_start, test_end)
    result = {
        "schemaVersion": 1,
        "generatedAtMs": now,
        "symbol": "fUSD",
        "principal": principal,
        "methodology": {
            "split": "chronological 60-day train / 15-day validation / 15-day test",
            "replayIntervalMinutes": 15,
            "signalAggregation": "volume-weighted trade rate per replay interval",
            "selectionUses": "training split only",
            "promotionGate": "positive validation and test gain plus paired bootstrap 95% lower bound above zero",
            "scoreModelVersion": SCORE_MODEL_VERSION,
            "scoreWeights": V3_RESEARCH_SCORE_WEIGHTS,
        },
        "split": {
            name: {"startMs": bounds[0], "endMs": bounds[1], "days": (bounds[1] - bounds[0]) // DAY_MS}
            for name, bounds in windows.items()
        },
        "data": {
            "trades": _coverage(trades),
            "stats": _coverage(stats),
            "bookSnapshots": _coverage([{"mts": row["mts"]} for row in books]),
            "historicalBookBackfillable": False,
        },
        "baselines": ["current_v3", "frr_only", "limit_near_fill"],
        "variants": {
            name: {
                "versionId": _policy_id(variants[name]),
                "policy": variants[name].__dict__,
                "metrics": metrics[name],
            }
            for name in variants
        },
        "selection": {
            "candidate": selected_name,
            "candidateVersionId": _policy_id(selected),
            "trainingRule": "highest train return on principal-time among candidate variants",
            "validationGain": validation_gain,
            "testGain": test_gain,
            "pairedBootstrap95": confidence,
            "safetyInvariants": invariants,
            "eligibleForLiveCandidate": eligible,
            "requiresManualPromotion": True,
            "rolloutStagesPercent": [10, 25, 50, 100],
            "minimumDaysPerStage": 7,
        },
    }
    return json_decimal(result)


def write_research_report(path, report):
    atomic_write_text(path, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return path
