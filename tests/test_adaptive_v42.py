"""Synthetic accounts only: V4.2 cashflow and control regression evidence."""

import copy
import json
import time
from dataclasses import replace
from decimal import Decimal as D

import pytest

import StrategyV4 as base
import StrategyV42 as core
from Currency import funding_sizing
from StrategyV3 import StrategyPolicyV3

NOW = 1900000000000


def setup(currency="USD", calibrated=False, holding=True, rate=".0002"):
    trades = [dict(id=i, mts=NOW - i * base.DAY - 1000, period=2, rate=D(rate), amount=D(100000)) for i in range(30)]
    observations = (
        [
            dict(
                chainId=i,
                displayType="LIMIT",
                currency=currency,
                period=2,
                amount=150,
                start_ms=NOW - 3600000,
                end_ms=NOW - 60000,
                fills=[(NOW - 3000000, 150)],
            )
            for i in range(20)
        ]
        if calibrated
        else []
    )
    holdings = (
        [dict(period=2, displayType="LIMIT", opened_ms=NOW - base.DAY, closed_ms=NOW - base.DAY + 3600000)]
        if holding
        else []
    )
    model = core.fit_model(
        currency,
        trades,
        observations,
        holdings,
        NOW,
        frr=[dict(mts=NOW - i * base.DAY, frr_daily_rate=rate) for i in range(30)],
    )
    policy = replace(core.template(StrategyPolicyV3(currency=currency)), model_id=model["id"])
    account = dict(total=D(1000), wallet=D(1000), exposure=dict(short=D(0), medium=D(0), long=D(0)))
    book = [dict(period=2, rate=D(rate), amount=D(-10000))]
    return policy, model, trades, book, account


def value(p, m, t, b, **kw):
    return core.value_candidate(p, m, kw.pop("period", 2), kw.pop("rate", D(".0002")), D(150), t, b, NOW, **kw)


@pytest.mark.parametrize("currency", ["USD", "USDT"])
def test_template_model_frozen_and_currency_isolation(currency):
    p, model, trades, book, account = setup(currency)
    assert p.strategy_engine == core.ENGINE and model["algorithm"] == core.VERSION
    assert p.reprice_gain_apr == D(".0025") and p.passive_wait_minutes == 60
    assert not p.adopt_external_offers
    assert base.validate_model(model, currency, NOW) == model
    assert model["dataBasis"]["publicOrderType"] == "UNKNOWN"
    assert not model["eligibleForLiveCandidate"]
    before = copy.deepcopy(model)
    r = core.build_plan(account, p, model, book, trades, NOW, "test", D(".0002"))
    assert model == before and not r.get("blockReasons")
    assert len(r["plan"]) <= 1 and r["planned_amount"] <= 150
    wrong = core.build_plan(
        account,
        replace(p, currency="USDT" if currency == "USD" else "USD"),
        model,
        book,
        trades,
        NOW,
        "test",
        D(".0002"),
    )
    assert wrong["blockReasons"] and not wrong["plan"]


def test_no_phantom_rollover_for_unfilled_ordinary_keep_and_no_age_queue_credit():
    p, model, trades, book, _ = setup()
    old = value(p, model, trades, book, rate=D(".0009"), age_minutes=2400)
    assert old["expectedFillProbability"] == 0
    assert old["pathInterests"] == [0.0] * 64
    assert old["cycleNetApr"] == 0
    book += [dict(rate=D(".00019"), period=2, amount=D(10000))]
    fresh = value(p, model, trades, book)
    survivor = value(p, model, trades, book, age_minutes=2400)
    assert survivor["queueVolumeEstimate"] == fresh["queueVolumeEstimate"] == 10000
    assert survivor["expectedWaitMinutes"] > 0


def test_mathematical_short_cycle_repricing_wins_where_120day_threshold_kept_old_quote():
    p, model, trades, book, _ = setup()
    offer = dict(
        managed=True, amount=183, rate=D(".00021"), offer_type="LIMIT", period=2, mts_created=NOW - 38 * 3600000
    )
    candidate = dict(rate=D(".0002"), submitted_rate=D(".0002"), offer_type="LIMIT", display_type="LIMIT", period=2)
    decision = core.adjustment(p, model, offer, [candidate], trades, book, NOW, D(".0002"))
    assert decision["action"] == "CANCEL" and decision["reason"] == "VALUE_GAIN"
    assert decision["cycleEfficiencyGain"] >= 0.0025
    assert decision["p10InterestGain"] >= 0
    assert decision["targetSubmittedRate"] == D(".0002")


def test_conditional_survival_excludes_already_survived_early_interval():
    hs = [0.8, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01]
    assert core.residual_wait(hs, 0, 0.5, 0, 150, 0, 1440) < 5
    assert core.residual_wait(hs, 5, 0.5, 0, 150, 0, 1440) is None
    assert core.residual_wait([0] * 8, 2000, 0.5, 100, 150, 1000, 360) > 0
    assert core.residual_wait([0] * 8, 0, 0.99, 1, 150, 0, 1) is None


@pytest.mark.parametrize("kind", core.TYPES)
def test_four_types_quotes_floors_and_cold_start_not_own_sample_blocked(kind):
    p, model, trades, book, account = setup(holding=False, rate=".0008")
    p = replace(p, **{field: typ == kind for typ, field in core.FIELDS.items()}, variable_max_share=D(100))
    result = core.build_plan(account, p, model, book, trades, NOW, "test", D(".0006"))
    assert not result.get("blockReasons")
    assert result["plan"] and result["planned_amount"] == 150
    assert all(row["display_type"] == kind and row["passive"] for row in result["plan"])
    assert all(row["effective_rate"] >= base.gross_floor(p, row["period"]) for row in result["plan"])
    assert all(c["confidence"] == "LOW" for c in result["candidates"])


def test_unknown_capacity_is_shared_and_known_fixed_not_frr_and_delete_not_demand():
    assert core.compatibility("FRR", dict(demandType="FIXED")) == "INCOMPATIBLE"
    assert core.compatibility("LIMIT", dict(demandType="FIXED")) == "KNOWN"
    assert core.compatibility("FRR_DELTA_FIXED", dict(demandType="FIXED")) == "UNKNOWN"
    assert core.compatibility("FRR", dict(demandType="FRR")) == "KNOWN"
    units = core.demand_units(
        [dict(period=2, rate=0, amount=-10000), dict(period=2, rate=".001", amount=-10, count=0)], [], NOW
    )
    assert units == []
    p, model, _, _, account = setup(calibrated=True)
    trades = [dict(id=1, mts=NOW - 1000, period=2, rate=D(".0002"), amount=D(150))]
    book = [dict(period=2, rate=D(".0002"), amount=D(-150))]
    # Only one observed unit (the book), no duplicate historical tape capacity.
    trades[0]["mts"] = NOW - 3600001
    result = core.build_plan(account, p, model, book, trades, NOW, "test", D(".0002"))
    assert result["planned_amount"] <= 300  # 150 shared normal unit + at most one 150 forecast lease.
    assert sum(r["amount"] for r in result["plan"] if not r["passive"]) <= 150
    assert sum(r["passive"] for r in result["plan"]) <= 1


def test_passive_one_minimum_no_remainder_existing_lease_and_quarantine():
    p, model, trades, book, account = setup()
    account["wallet"] = D(280)
    first = core.build_plan(account, p, model, book, trades, NOW, "test", D(".0002"))
    assert first["planned_amount"] == 150 and first["idle_amount"] == 130
    row = first["plan"][0]
    assert row["leaseMinutes"] == 60 and row["evidenceWatermark"] == ["trade:0"]
    account["passiveLeaseStates"] = [dict(active=True, expiresAtMs=NOW + 60000)]
    assert not core.build_plan(account, p, model, book, trades, NOW, "test", D(".0002"))["plan"]
    account.pop("passiveLeaseStates")
    # Quarantine every candidate to prevent choosing another equal-price period.
    account["passiveQuarantines"] = {
        core.quote_key(p.currency, c): dict(closedAtMs=NOW - 750, evidenceWatermark=c["evidenceWatermark"])
        for c in first["candidates"]
    }
    assert not core.build_plan(account, p, model, book, trades, NOW, "test", D(".0002"))["plan"]
    trades[0]["id"] = 999
    # A previously unseen ID from before closure is not new demand evidence.
    assert not core.build_plan(account, p, model, book, trades, NOW, "test", D(".0002"))["plan"]
    trades[0]["mts"] = NOW - 500
    assert core.build_plan(account, p, model, book, trades, NOW, "test", D(".0002"))["plan"]


def test_caps_minimum_slots_long_boundary_and_frr_missing():
    p, model, trades, book, account = setup(rate=".0008")
    account.update(wallet=D(149), openOfferCount=0)
    assert not core.build_plan(account, p, model, book, trades, NOW, "test", D(".0008"))["plan"]
    account.update(wallet=D(1000), openOfferCount=8)
    assert not core.build_plan(account, p, model, book, trades, NOW, "test", D(".0008"))["plan"]
    account["openOfferCount"] = 0
    cap = core.build_plan(account, replace(p, max_lend_amount=D(100)), model, book, trades, NOW, "test", D(".0008"))
    assert not cap["plan"] and cap["cap_limited_available"] == 100
    floating = replace(p, **{v: k == "FRR" for k, v in core.FIELDS.items()})
    assert not core.build_plan(account, floating, model, book, trades, NOW, "test", D(".0008"))["plan"]
    for frr in (None, D(0)):
        assert core.build_plan(account, p, model, book, trades, NOW, "test", frr)["blockReasons"]
    with funding_sizing(D(151)):
        result = core.build_plan(account, p, model, book, trades, NOW, "test", D(".0008"))
        assert all(r["amount"] >= 151 for r in result["plan"])
    assert base.floor(p, 30) == D(".05") and base.floor(p, 31) == D(".10")


def test_finite_cashflow_floating_and_fixed_and_partial_window():
    path = tuple((0.001, 0.001 if day == 0 else 0.002, 10000) for day in range(120))
    fixed = core._earned("FRR_DELTA_FIXED", 0.0002, 0.0012, path, 12, 36, 0.85)
    variable = core._earned("FRR_DELTA_VARIABLE", 0.0002, 0.0012, path, 12, 36, 0.85)
    assert fixed == pytest.approx(0.0012 * 0.85)
    assert variable == pytest.approx(0.0017 * 0.85)
    assert core._earned("LIMIT", 0, 0.001, path, 10, 10, 0.85) == 0
    assert core._hold(((), ()), 120, 0.5) == 2880
    assert core._hold(((1,), (-0.2,)), 2, 0.9) == 1
    assert core._hold(((), ()), 2, 0.5, True) == 1


def test_bad_models_and_budget_fail_closed_and_twenty_thousand_trades():
    p, model, trades, book, account = setup()
    for broken in (None, {**model, "id": "x"}, {**model, "trainedUntilMs": NOW + 1}):
        assert core.build_plan(account, p, broken, book, trades, NOW, "test", D(".0002"))["blockReasons"]
    assert core.build_plan(account, replace(p, model_id="x"), model, book, trades, NOW, "test", D(".0002"))[
        "blockReasons"
    ]
    rows = [{**trades[0], "id": n, "mts": NOW - n * 1000} for n in range(20000)]
    start = time.monotonic()
    result = core.build_plan(account, p, model, book, rows, NOW, "test", D(".0002"))
    assert time.monotonic() - start < 15 and not result.get("blockReasons")


def test_explicit_budget_exhaustion_blocks_whole_plan(monkeypatch):
    p, model, trades, book, account = setup()
    monkeypatch.setattr(core, "VALUATION_BUDGET_SECONDS", -1)
    result = core.build_plan(account, p, model, book, trades, NOW, "test", D(".0002"))
    assert result["empty_reason"] == "VALUATION_INCOMPLETE" and not result["plan"]
    candidate = dict(period=2, rate=D(".0002"), submitted_rate=D(".0002"), offer_type="LIMIT", display_type="LIMIT")
    offer = dict(managed=True, amount=150, period=2, rate=D(".0003"), offer_type="LIMIT", mts_created=NOW - 600000)
    assert core.adjustment(p, model, offer, [candidate], trades, book, NOW)["reason"] == "VALUATION_INCOMPLETE"


def test_adjustment_guards_keep_hard_and_lease_expiration():
    p, model, trades, book, _ = setup()
    offer = dict(managed=True, amount=150, period=2, rate=D(".0002"), offer_type="LIMIT", mts_created=NOW - 600000)
    assert core.adjustment(p, model, {**offer, "managed": False}, [], trades, book, NOW)["reason"] == "EXTERNAL_OFFER"
    assert (
        core.adjustment(p, model, {**offer, "offer_type": "FRRDELTAVAR", "rate": 0}, [], trades, book, NOW)["reason"]
        == "FRR_DATA_UNAVAILABLE"
    )
    assert core.adjustment(p, model, {**offer, "rate": D(".00001")}, [], trades, book, NOW)["hard"]
    assert core.adjustment(p, model, {**offer, "period": 121}, [], trades, book, NOW)["hard"]
    assert core.adjustment(p, model, {**offer, "amount": 100}, [], trades, book, NOW)["action"] == "KEEP"
    assert core.adjustment(p, model, {**offer, "mts_created": NOW}, [], trades, book, NOW)["action"] == "KEEP"
    assert core.adjustment(p, model, offer, [], trades, book, NOW)["reason"] == "NO_BETTER_VALUE"
    same = dict(period=2, rate=D(".0002"), submitted_rate=D(".0002"), offer_type="LIMIT", display_type="LIMIT")
    assert core.adjustment(p, model, offer, [same], trades, book, NOW)["reason"] == "QUEUE_VALUE"
    lease = dict(expiresAtMs=NOW, startedAtMs=NOW - 3600000, chainStartMs=NOW - 3600000, evidenceWatermark=[])
    expired = {**offer, "passiveLease": lease}
    assert core.adjustment(p, model, expired, [], trades, book, NOW)["reason"] == "LEASE_EXPIRED"


def test_paired_horizon_guard_and_lease_renewal_bound(monkeypatch):
    p, model, trades, book, _ = setup()
    offer = dict(managed=True, amount=150, period=2, rate=D(".0002"), offer_type="LIMIT", mts_created=NOW - 600000)
    candidate = dict(period=2, rate=D(".0003"), submitted_rate=D(".0003"), offer_type="LIMIT", display_type="LIMIT")

    def negative(_p, _m, period, rate, amount, *_a, quote=None, **kw):
        replacing = kw.get("cancel_minutes", 0) > 0
        return {
            **quote,
            "period": period,
            "expectedFillProbability": 1,
            "cycleNetApr": 0.10 if replacing else 0.05,
            "pathInterests": [1 if replacing else 2] * 64,
            "conservativeNetApr": 0.01,
            "expectedWaitMinutes": 1,
            "confidence": "LOW",
        }

    monkeypatch.setattr(core, "value_candidate", negative)
    assert core.adjustment(p, model, offer, [candidate], trades, book, NOW)["action"] == "KEEP"

    def positive(*args, **kw):
        r = negative(*args, **kw)
        r["pathInterests"] = [2] * 64
        return r

    monkeypatch.setattr(core, "value_candidate", positive)
    lease = dict(expiresAtMs=NOW, startedAtMs=NOW - 3600000, chainStartMs=NOW - 3600000, evidenceWatermark=["trade:1"])
    same = {
        **candidate,
        "rate": D(".0002"),
        "submitted_rate": D(".0002"),
        "evidenceWatermark": ["trade:1", "trade:0"],
        "passive": True,
    }
    assert (
        core.adjustment(p, model, {**offer, "passiveLease": lease}, [same], trades, book, NOW)["reason"]
        == "PASSIVE_LEASE_RENEW"
    )
    historic = {**same, "evidenceWatermark": ["trade:1", "trade:2"]}
    assert (
        core.adjustment(p, model, {**offer, "passiveLease": lease}, [historic], trades, book, NOW)["reason"]
        == "LEASE_EXPIRED"
    )
    normal = {**same, "passive": False, "demandCompatibility": "KNOWN"}
    assert (
        core.adjustment(p, model, {**offer, "passiveLease": lease}, [normal], trades, book, NOW)["reason"]
        == "PASSIVE_TO_NORMAL"
    )
    lease["chainStartMs"] = NOW - 360 * 60000
    assert (
        core.adjustment(p, model, {**offer, "passiveLease": lease}, [same], trades, book, NOW)["reason"]
        == "LEASE_EXPIRED"
    )


def test_normalized_quote_key_and_book_requires_actual_first_seen_time():
    raw = dict(offer_type="FRRDELTAVAR", submitted_rate=D(".00020000"), period=7)
    assert core.quote_key("USD", raw) == core.quote_key("USD", {**raw, "submitted_rate": D(".0002")})
    book = [dict(id=8, period=2, rate=".0002", amount=-150)]
    assert core.demand_units(book, [], NOW)[0]["evidenceId"] is None
    book[0]["firstSeenMs"] = NOW - 1000
    assert core.demand_units(book, [], NOW)[0]["evidenceId"] == "book:8:2:.0002:-150"
    assert not core.independent_evidence([], book, NOW, NOW - 500)


def test_confirmed_fixed_trade_flow_is_not_used_as_frr_lane():
    p, model, trades, book, _ = setup(rate=".0008")
    typed = [{**r, "demandType": "LIMIT"} for r in trades]
    quote = dict(offer_type="FRRDELTAVAR", display_type="FRR", submitted_rate=D(0), rate=D(".0008"))
    assert value(p, model, typed, book, rate=D(".0008"), quote=quote)["expectedFillProbability"] == 0
    assert value(p, model, trades, book, rate=D(".0008"), quote=quote)["expectedFillProbability"] > 0


@pytest.mark.parametrize(
    "field,bad", [("mts", NOW + 1), ("amount", "-1"), ("amount", "Infinity"), ("rate", "Infinity"), ("period", 121)]
)
def test_rehashed_future_or_invalid_demand_path_never_used_for_valuation(field, bad):
    p, model, trades, book, account = setup()
    model["demandDays"][0][field] = bad
    model["id"] = base.digest({k: v for k, v in model.items() if k != "id"})
    p = replace(p, model_id=model["id"])
    result = core.build_plan(account, p, model, book, trades, NOW, "test", D(".0002"))
    assert not result["plan"] and "未来或无效" in result["blockReasons"][0]


def test_adjustment_rechecks_long_premium_from_current_short_choice(monkeypatch):
    p, model, trades, book, _ = setup()
    offer = dict(managed=True, amount=150, period=2, rate=D(".0002"), offer_type="LIMIT", mts_created=NOW - 600000)
    short = dict(period=2, rate=D(".0003"), submitted_rate=D(".0003"), offer_type="LIMIT", display_type="LIMIT")
    long = {**short, "period": 60, "rate": D(".0006"), "submitted_rate": D(".0006")}

    def values(_p, _m, period, rate, amount, *_a, quote=None, **_kw):
        return {
            **quote,
            "period": period,
            "expectedFillProbability": 1,
            "cycleNetApr": 0.05 if period == 2 else 0.30,
            "pathInterests": [1 if rate == D(".0002") else 2] * 64,
            "conservativeNetApr": 0.10 if period == 2 else 0.104,
            "expectedWaitMinutes": 1,
            "confidence": "LOW",
        }

    monkeypatch.setattr(core, "value_candidate", values)
    decision = core.adjustment(p, model, offer, [short, long], trades, book, NOW)
    assert decision["action"] == "KEEP"  # Long appears better on cycle efficiency but misses latest +0.5pp.


def test_equivalent_cashflows_use_executable_monetary_precision():
    assert core._interest_p10([-1.7e-15] * 64) == 0
    assert core._interest_p10([-0.00001] * 64) == -0.00001


def test_renewals_use_own_censored_wait_evidence_and_never_hourly_refund():
    path = tuple((0.001, 0.001, 100000) for _ in range(120))
    args = ("renewal-evidence", "USD", (path,) * 64, 0.0002, 0.85, "LIMIT", ((), ()), 150, 0, "CALIBRATED")
    prior = core._continuation(*args)
    slow = core._continuation(*args, json.dumps({"all": [[10000, 0]] * 8}))
    assert sum(row[0] for row in slow) < sum(row[0] for row in prior)
    # After quoting a price with no later compatible flow, there is no phantom
    # hourly refund or interest from the unrelated lower-rate demand.
    incompatible = (path[0],) + tuple((0.0009, 0.001, 100000) for _ in range(119))
    prefix = [0] * 121
    assert core._matched_hour(prefix, 0, 1, [0] * 8, 0.5) is None
    absent = core._continuation(*((*args[:2], (incompatible,) * 64, *args[3:])), json.dumps({"all": [[1e20, 0]] * 8}))
    assert all(row[0] == 0 for row in absent)


def test_historical_frr_is_date_joined_and_short_renewal_requires_two_day_demand(monkeypatch):
    p, model, trades, book, _ = setup()
    day = NOW // base.DAY - 10
    model["days"] = [dict(mts=(day + i) * base.DAY, rate=".0002") for i in (0, 1, 3)]
    model["frrDays"] = [dict(mts=(day + i) * base.DAY, rate=str(i / 1000)) for i in (1, 2, 3)]
    model["demandDays"] = [
        dict(mts=day * base.DAY, period=7, amount="99999"),
        dict(mts=(day + 1) * base.DAY, period=2, amount="150"),
    ]
    captured = {}

    def paths(*args):
        captured["frrDays"], captured["volumes"] = args[3], args[4]
        return ()

    def continuation(*_args):
        return ()

    monkeypatch.setattr(core, "_paths", paths)
    monkeypatch.setattr(core, "_continuation", continuation)
    market = base.CandidateMarket(trades, NOW)
    core._future_inputs(p, model, market, book, D(150), D(".0002"), trades)
    assert captured["frrDays"] == (0, 0.001, 0.003)
    assert captured["volumes"] == (0, 150, 0)


def test_path_starts_at_observed_point_and_matched_wait_handles_tail():
    paths = core._paths("observed", "USD", (0.0001, 0.001), (0.0002, 0.002), (100, 1000), 0.0003, 0.0004, 123)
    assert all(path[0] == (0.0003, 0.0004, 123) for path in paths)
    prefix = [float(i) for i in range(121)]
    assert core._matched_hour(prefix, 0, 1 / 24, [0] * 8, 0.5) == pytest.approx(36)
    assert core._matched_hour(prefix, 2879, 1, [0] * 8, 1) is None


def test_passive_replacement_uses_minimum_and_zero_interest_cash_tail(monkeypatch):
    p, model, trades, book, _ = setup()
    amounts = []

    def valuations(_p, _m, period, rate, amount, *_args, quote=None, **_kw):
        amounts.append(amount)
        return {
            **quote,
            "period": period,
            "expectedFillProbability": 1,
            "cycleNetApr": 0.3 if quote.get("passive") else 0.05,
            "pathInterests": [2 if quote.get("passive") else 1] * 64,
            "firstCycleInterests": [0.1] * 64,
            "firstCycleHours": [2] * 64,
            "conservativeNetApr": 0.1,
            "expectedWaitMinutes": 1,
            "confidence": "LOW",
        }

    monkeypatch.setattr(core, "value_candidate", valuations)
    offer = dict(managed=True, amount=183, period=2, rate=D(".0002"), offer_type="LIMIT", mts_created=NOW - 600000)
    candidate = dict(
        period=2, rate=D(".0003"), submitted_rate=D(".0003"), offer_type="LIMIT", display_type="LIMIT", passive=True
    )
    decision = core.adjustment(p, model, offer, [candidate], trades, book, NOW)
    assert amounts == [D(183), D(150)]
    assert decision["action"] == "KEEP" and decision["cycleEfficiencyGain"] < 0


def test_multi_block_allocation_freezes_renewal_basis_and_finishes_within_budget():
    p, model, trades, book, account = setup(calibrated=True)
    book[0]["demandType"] = "LIMIT"
    account["wallet"] = D(350)
    before = core._continuation.cache_info().misses
    start = time.monotonic()
    result = core.build_plan(account, p, model, book, trades, NOW, "multi-block", D(".0002"))
    assert time.monotonic() - start < 15
    assert not result.get("blockReasons") and result["planned_amount"] == 350
    assert core._continuation.cache_info().misses - before <= 1
