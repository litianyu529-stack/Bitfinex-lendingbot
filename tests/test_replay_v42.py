"""Synthetic, event-driven replay; no real exchange or account clients."""

from dataclasses import replace
from decimal import Decimal as D

import pytest

import ReplayV4 as replay
import StrategyV4 as base
import StrategyV42 as core
from StateStore import LendingStateStore
from StrategyV3 import StrategyPolicyV3, json_decimal

NOW = 1900000000000


def fixture(kind="LIMIT", passive=False, rate=".0008"):
    trade = dict(id=1, mts=NOW - 1000, period=2, rate=D(rate), amount=D(10000))
    model = core.fit_model(
        "USD", [trade], now_ms=NOW, frr=[dict(mts=NOW - d * base.DAY, frr_daily_rate=".0006") for d in range(30)]
    )
    policy = replace(core.template(StrategyPolicyV3()), model_id=model["id"])
    native = {"LIMIT": "LIMIT", "FRR": "FRR", "FRR_DELTA_FIXED": "FRRDELTAFIX", "FRR_DELTA_VARIABLE": "FRRDELTAVAR"}[
        kind
    ]
    quote = dict(
        period=2,
        amount=D(150),
        effective_rate=D(rate),
        submitted_rate=D(rate) if kind == "LIMIT" else D(0) if kind == "FRR" else D(".0002"),
        offer_type=native,
        display_type=kind,
        passive=passive,
        leaseMinutes=60,
        evidenceWatermark=["trade:1"],
    )
    return policy, model, trade, quote


def mocked_quotes(monkeypatch, quote, decide=None):
    seen = []

    def plan(account, *_a, **_kw):
        seen.append(account)
        return dict(plan=[quote] if account["wallet"] >= 150 else [], candidates=[quote])

    monkeypatch.setattr(core, "build_plan", plan)
    monkeypatch.setattr(core, "adjustment", decide or (lambda *_a, **_kw: dict(action="KEEP", reason="TEST")))
    return seen


def book_series(end, rate=".001", step=60000, frr=".0006"):
    return [
        dict(mts=NOW + at, book=[dict(rate=rate, period=2, amount=-1000)], frr=frr) for at in range(0, end + 1, step)
    ]


def test_unfinished_loans_accumulate_only_actual_seconds_partial_fills_and_no_idle_tail(monkeypatch):
    policy, model, _, quote = fixture()
    mocked_quotes(monkeypatch, quote)
    trades = [
        dict(id=n, mts=NOW + t, rate=".0008", period=2, amount=50, demandType="LIMIT")
        for n, t in enumerate((1000, 3000))
    ]
    out = replay.replay(policy, model, trades, book_series(10000), 150, NOW, NOW + 10000)
    assert out["fillCount"] == 2 and out["openCreditAmount"] == 100
    assert out["netInterest"] == pytest.approx(D(50) * D(".0008") * D(".85") * D(16000) / base.DAY)
    assert out["unknownTypeSimulatedAmount"] == 0 and not out["ownsRealFillEvidence"]
    idle = replay.replay(policy, model, [], book_series(10000), 150, NOW, NOW + 10000)
    assert idle["netInterest"] == 0 and idle["averageWaitMinutes"] is None


@pytest.mark.parametrize(
    "kind,expected", [("LIMIT", ".68"), ("FRR", ".68"), ("FRR_DELTA_FIXED", ".68"), ("FRR_DELTA_VARIABLE", ".85")]
)
def test_all_native_type_interest_and_locked_delta_after_fill(monkeypatch, kind, expected):
    policy, model, _, quote = fixture(kind, rate=".0006" if kind == "FRR" else ".0008")
    quote["amount"] = D(1000)
    mocked_quotes(monkeypatch, quote)
    books = [
        dict(mts=NOW, book=[dict(rate=".001", period=2, amount=-1000)], frr=".0006"),
        dict(mts=NOW + base.DAY // 2, book=[dict(rate=".001", period=2, amount=-1000)], frr=".001"),
    ]
    trade = dict(
        id=2,
        mts=NOW + 1,
        rate=".001",
        period=2,
        amount=1000,
        demandType="FRR" if kind == "FRR" else "LIMIT" if kind == "LIMIT" else "UNKNOWN",
    )
    out = replay.replay(policy, model, [trade], books, 1000, NOW, NOW + base.DAY, interval_ms=base.DAY)
    assert abs(out["netInterest"] - D(expected)) < D(".000001")
    assert out["fills"][0]["evidence"] == "SIMULATED_PUBLIC_CAPACITY"


def test_known_fixed_cannot_match_frr_and_unknown_volume_consumed_only_once(monkeypatch):
    policy, model, _, quote = fixture("FRR", rate=".0006")
    mocked_quotes(monkeypatch, quote)
    fixed = dict(id=3, mts=NOW + 1000, rate=".001", period=2, amount=150, demandType="FIXED")
    assert replay.replay(policy, model, [fixed], book_series(2000), 150, NOW, NOW + 2000)["fillCount"] == 0
    uncertain = {**fixed, "demandType": "UNKNOWN"}
    out = replay.replay(policy, model, [uncertain], book_series(2000), 300, NOW, NOW + 2000)
    assert out["unknownTypeSimulatedAmount"] == 150 and out["openCreditAmount"] == 150
    pessimistic = replay.replay(policy, model, [uncertain], book_series(2000), 300, NOW, NOW + 2000, stress=True)
    assert pessimistic["fillCount"] == 0


def test_expired_passive_cancels_confirms_then_quarantines_same_price_without_new_evidence(monkeypatch):
    policy, model, old, quote = fixture(passive=True, rate=".0009")
    seen = mocked_quotes(monkeypatch, quote)
    out = replay.replay(policy, model, [], book_series(70 * 60000), 150, NOW, NOW + 70 * 60000, initial_trades=[old])
    assert out["submissionCount"] == 1 and out["cancellationCount"] == 1
    assert out["cancellations"][0]["atMs"] == NOW + 60 * 60000
    assert out["cancellations"][0]["reason"] == "LEASE_EXPIRED"
    assert out["leaseEvents"][-1]["action"] == "CLOSED"
    assert seen[-1]["wallet"] == 150 and seen[-1]["passiveQuarantines"]


def test_stale_market_still_expires_lease_and_never_renews_on_timestamp_only(monkeypatch):
    policy, model, _, quote = fixture(passive=True)
    mocked_quotes(
        monkeypatch,
        quote,
        lambda *_a, **_kw: dict(action="KEEP", reason="PASSIVE_LEASE_RENEW", evidenceWatermark=["book:fake"]),
    )
    out = replay.replay(policy, model, [], book_series(0), 150, NOW, NOW + 70 * 60000, interval_ms=10 * 60000)
    assert out["cancellations"][0]["atMs"] == NOW + 60 * 60000
    assert not any(r["action"] == "RENEW" for r in out["leaseEvents"])


def test_new_trade_renews_but_continuous_same_funds_wait_cannot_pass_six_hours(monkeypatch):
    policy, model, _, quote = fixture(passive=True, rate=".0009")

    def decide(*args, **_kw):
        tokens = [f"trade:{r['id']}" for r in args[4]]
        return dict(action="KEEP", reason="PASSIVE_LEASE_RENEW", evidenceWatermark=tokens)

    mocked_quotes(monkeypatch, quote, decide)
    trades = [dict(id=100 + h, mts=NOW + h * 3600000 - 1000, rate=".0002", period=2, amount=100) for h in range(1, 7)]
    out = replay.replay(policy, model, trades, book_series(370 * 60000), 150, NOW, NOW + 370 * 60000)
    # Native choice can remain unchanged, but expiry and evidence impose a hard ending.
    assert any(r["action"] == "RENEW" for r in out["leaseEvents"])
    assert out["cancellations"][0]["atMs"] == NOW + 360 * 60000
    assert out["submissionCount"] == 1


def test_ordinary_confirmation_clears_on_keep_and_uses_native_delta_not_effective_frr(monkeypatch):
    policy, model, _, quote = fixture("FRR_DELTA_VARIABLE")

    def decide(*args, **_kw):
        minute = (args[6] - NOW) // 60000
        if minute < 5 or minute == 6:
            return dict(action="KEEP", reason="TEST")
        return dict(
            action="CANCEL",
            reason="VALUE_GAIN",
            hard=False,
            targetPeriod=7,
            targetRate=D(".0008") + D(minute) / D(10000000),
            targetSubmittedRate=D(".0002"),
            targetType="FRR_DELTA_VARIABLE",
        )

    mocked_quotes(monkeypatch, quote, decide)
    out = replay.replay(policy, model, [], book_series(8 * 60000), 150, NOW, NOW + 8 * 60000 + 1)
    assert out["cancellations"][0]["atMs"] == NOW + 8 * 60000
    assert out["cancellations"][0]["reason"] == "VALUE_GAIN"


def test_cancel_barrier_allows_partial_fill_and_replaces_only_confirmed_remaining_cash(monkeypatch):
    policy, model, _, quote = fixture()
    seen = mocked_quotes(monkeypatch, quote, lambda *_a, **_kw: dict(action="CANCEL", reason="HARD_FLOOR", hard=True))
    trade = dict(id=2, mts=NOW + 30000, period=2, amount=50, rate=".001", demandType="LIMIT")
    out = replay.replay(policy, model, [trade], book_series(2 * 60000), 150, NOW, NOW + 2 * 60000 + 1)
    assert out["openCreditAmount"] == 50 and out["submissionCount"] == 1
    assert any(r["wallet"] == 100 for r in seen)


def test_public_dynamic_reference_floor_deadlines_and_no_private_model_claim():
    policy, _, _, _ = fixture()
    policy = replace(policy, strategy_engine=replay.PUBLIC_DYNAMIC)
    state = dict(
        total=D(1000), wallet=D(1000), existingExposure=dict(total=D(0)), exposureByPeriod={}, openOfferCount=0
    )
    plan = replay.public_dynamic_plan(
        state,
        policy,
        [
            dict(rate=".00001", period=2, amount=-500),
            dict(rate=".0008", period=31, amount=-500),
            dict(rate=".0009", period=121, amount=-500),
            dict(rate=".0009", period=2, amount=-500, count=0),
        ],
    )
    assert len(plan["plan"]) == 1 and plan["plan"][0]["period"] == 31
    books = book_series(7 * 60000, rate=".0008")
    for row in books[1:]:
        row["book"] = [dict(rate=".0006", period=2, amount=-150)]
    out = replay.replay(policy, {}, [], books, 150, NOW, NOW + 7 * 60000)
    assert out["cancellations"][0]["atMs"] == NOW + 5 * 60000


def test_evaluate_v42_reports_missing_data_without_fabricating_qualification(tmp_path):
    store = LendingStateStore(tmp_path / "usd.sqlite3", currency="USD")
    policy, _, _, _ = fixture()
    store.save_strategy(json_decimal(replace(policy, strategy_engine="legacy_v3").__dict__), "ACTIVE")
    report, model = replay.evaluate(store, NOW, engine=core.ENGINE)
    assert report["state"] == "INSUFFICIENT_DATA" and not report["eligibleForLiveCandidate"]
    assert report["requestedBaselines"] == ["original_active", "repaired_v41", "public_dynamic_limit", "v42"]
    assert model["algorithm"] == core.VERSION and not report["simulationIsOwnFillEvidence"]


def test_evaluate_frozen_v41_missing_baseline_and_unknown_fill_never_qualify(tmp_path, monkeypatch):
    store = LendingStateStore(tmp_path / "usd.sqlite3", currency="USD")
    policy, _, _, _ = fixture()
    store.save_strategy(json_decimal(replace(policy, strategy_engine="legacy_v3").__dict__), "ACTIVE")

    def train(_s, cutoff, *_a, **_kw):
        model = core.fit_model(
            "USD",
            [dict(mts=cutoff - 1, period=2, rate=".0008", amount=10000)],
            now_ms=cutoff,
            coverage=dict(publicComplete=True, bookSnapshots=dict(earliestMs=cutoff - 90 * base.DAY)),
            frr=[dict(mts=cutoff - d * base.DAY, frr_daily_rate=".0006") for d in range(30)],
        )
        model.update(ownObservationCount=80, typeObservationCounts={k: 20 for k in core.TYPES})
        model["id"] = base.digest({k: v for k, v in model.items() if k != "id"})
        return model

    monkeypatch.setattr(replay, "build_from_store", train)
    monkeypatch.setattr(replay, "streams", lambda *_a: ([], []))

    def metrics(p, *_a, **_kw):
        gain = D(2) if p.strategy_engine == core.ENGINE else D(1)
        return dict(
            netInterest=gain * 15,
            returnOnPrincipalTime=gain / 1000,
            netAprPercent=gain,
            dailyNetInterest=[gain] * 15,
            bookCoverageFraction=1,
            frrCoverageFraction=1,
            unknownTypeSimulatedAmount=150,
        )

    monkeypatch.setattr(replay, "replay", metrics)
    report, _ = replay.evaluate(store, NOW, engine=core.ENGINE)
    assert "v42" in report["metrics"]["test"] and "public_dynamic_limit" in report["metrics"]["test"]
    assert report["comparisons"]["repaired_v41"]["state"] == "DATA_UNAVAILABLE"
    assert report["unknownPublicTypeAffectedFills"] and not report["eligibleForLiveCandidate"]
    assert "冻结模型" in report["missingBaselineReason"]
