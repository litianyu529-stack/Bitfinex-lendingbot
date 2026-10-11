"""Bounded V4.2 planning regressions using synthetic accounts only."""

import copy
from collections import Counter
from dataclasses import replace
from decimal import Decimal as D

import pytest

import StrategyV4 as base
import StrategyV42 as core
from Currency import funding_sizing
from StrategyV3 import StrategyPolicyV3

NOW = 1900000000000


@pytest.fixture
def inputs():
    trades = [
        dict(id=i + 1, mts=NOW - i * base.DAY - 1000, period=2, rate=D(".0004"), amount=D(1000)) for i in range(30)
    ]
    model = core.fit_model(
        "USDT",
        trades,
        now_ms=NOW,
        frr=[dict(mts=NOW - i * base.DAY, frr_daily_rate=".0005") for i in range(30)],
    )
    policy = replace(core.template(StrategyPolicyV3(currency="USDT")), model_id=model["id"])
    account = dict(total=D(1000), wallet=D(".000001"), exposure={"short": D(0)})
    book = [dict(period=2, rate=D(".0006"), amount=D(-1000), demandType="FRR")]
    return policy, model, trades, book, account


def forbidden_valuation(*args, **kwargs):
    raise AssertionError("an account without executable capital must not value candidates")


@pytest.mark.parametrize("wallet", ["0", ".000001", "149.99999999"])
def test_below_minimum_skips_valuation_and_retains_metadata(inputs, monkeypatch, wallet):
    policy, model, trades, book, account = inputs
    account["wallet"] = D(wallet)
    monkeypatch.setattr(core, "value_candidate", forbidden_valuation)
    with funding_sizing(D(150)):
        result = core.build_plan(account, policy, model, book, trades, NOW, "active", D(".0005"))
    assert not result.get("blockReasons")
    assert result["plan"] == result["candidates"] == []
    assert result["planned_amount"] == result["variable_amount"] == 0
    assert result["idle_amount"] == D(wallet)
    assert result["funding_cap"] == account["total"]
    assert result["cap_remaining"] == account["total"]
    assert result["cap_limited_available"] == D(wallet)
    assert not result["over_cap"]
    assert result["modelId"] == model["id"]
    assert result["engine"] == core.ENGINE and result["algorithm"] == core.VERSION
    assert result["confidence"] == model["confidence"]
    assert result["dataCoverage"] == model["coverage"]
    assert not result["eligibleForLiveCandidate"]
    assert result["empty_reason"] == "WAIT_FOR_VALUE"
    assert result["plan_hash"] == base.digest(
        dict(
            currency=policy.currency,
            policy=policy.__dict__,
            model=policy.model_id,
            account=account,
            plan=[],
            reason=result["empty_reason"],
        )
    )


def test_over_cap_without_managed_offers_skips_valuation(inputs, monkeypatch):
    policy, model, trades, book, account = inputs
    policy = replace(policy, max_lend_amount=D(360))
    account.update(wallet=D(400), existingExposure={"total": D(600)})
    monkeypatch.setattr(core, "value_candidate", forbidden_valuation)
    result = core.build_plan(account, policy, model, book, trades, NOW, "active", D(".0005"))
    assert not result.get("blockReasons")
    assert result["over_cap"] and result["existing_exposure"] == 600
    assert result["funding_cap"] == 360 and result["cap_remaining"] == 0
    assert result["plan"] == [] and result["idle_amount"] == 400


@pytest.mark.parametrize("fault", ["missing", "corrupt", "currency", "future", "model_id", "frr", "frr_history"])
def test_small_balance_still_validates_model_and_frr(inputs, monkeypatch, fault):
    policy, model, trades, book, account = inputs
    model = copy.deepcopy(model)
    frr = D(".0005")
    if fault == "missing":
        model = None
    elif fault == "corrupt":
        model["pathSeed"] = "corrupt"
    elif fault == "currency":
        policy = replace(policy, currency="USD")
    elif fault == "future":
        model["days"][0]["mts"] = NOW + base.DAY
        model["id"] = base.digest({k: v for k, v in model.items() if k != "id"})
        policy = replace(policy, model_id=model["id"])
    elif fault == "model_id":
        policy = replace(policy, model_id="different-frozen-model")
    elif fault == "frr":
        frr = None
    elif fault == "frr_history":
        model["frrDays"] = model["frrDays"][:19]
        model["id"] = base.digest({k: v for k, v in model.items() if k != "id"})
        policy = replace(policy, model_id=model["id"])
    monkeypatch.setattr(core, "value_candidate", forbidden_valuation)
    result = core.build_plan(account, policy, model, book, trades, NOW, "active", frr)
    assert result["blockReasons"]
    assert result["empty_reason"] == "MODEL_OR_DATA_UNAVAILABLE"
    assert result["plan"] == [] and result["planned_amount"] == 0


def fake_value(calls):
    def value(policy, model, period, rate, amount, trades, book, now_ms, **kwargs):
        quote = kwargs["quote"]
        calls.append((period, quote["display_type"], quote["submitted_rate"], amount, len(book)))
        return {
            **quote,
            "period": period,
            "expectedFillProbability": 0.8,
            "cycleNetApr": 0.1,
            "conservativeNetApr": 0.1,
            "p10IdleInterestGain": 0,
            "floatingRate": quote["offer_type"] == "FRRDELTAVAR",
        }

    return value


def test_decision_local_path_reuse_preserves_full_candidate_value(inputs, monkeypatch):
    policy, model, trades, book, _ = inputs
    market = base.CandidateMarket(trades, NOW)
    first = core.value_candidate(policy, model, 2, D(".0004"), D(150), trades, book, NOW, market=market)
    expected = core.value_candidate(policy, model, 7, D(".0005"), D(150), trades, book, NOW)
    continuation = core._continuation
    calls = []

    def counted(*args):
        calls.append(args)
        return continuation(*args)

    monkeypatch.setattr(core, "_continuation", counted)
    actual = core.value_candidate(policy, model, 7, D(".0005"), D(150), trades, book, NOW, market=market)
    assert actual == expected and not calls
    assert first["pathInterests"] != actual["pathInterests"]
    core.value_candidate(policy, model, 7, D(".0005"), D(151), trades, book, NOW, market=market)
    assert len(calls) == 1
    market.deadline = 0
    with pytest.raises(core.ValuationIncomplete):
        core.value_candidate(policy, model, 7, D(".0005"), D(150), trades, book, NOW, market=market)


def test_passive_candidates_allocate_and_value_full_available_balance(inputs, monkeypatch):
    policy, model, trades, _, account = inputs
    account["wallet"] = D(600)
    book = [dict(period=2, rate=D(".0006"), amount=D(-1000))]
    calls = []
    monkeypatch.setattr(core, "value_candidate", fake_value(calls))
    with funding_sizing(D(150)):
        result = core.build_plan(account, policy, model, book, trades, NOW, "active", D(".0005"))
    assert len(result["plan"]) == 1 and result["planned_amount"] == D(600)
    assert result["idle_amount"] == 0
    assert all(c["passive"] for c in result["candidates"])
    assert {call[-1] for call in calls} == {1}
    assert calls[-1][3] == D(600)


def test_revalued_frr_floor_block_cannot_receive_next_allocation(inputs, monkeypatch):
    policy, model, trades, _, account = inputs
    policy = replace(policy, maximum_period=8, long_from_days=8, variable_max_share=D(100))
    account["wallet"] = D(1000)
    book = [
        dict(period=2, rate=D(".0006"), amount=D(-150), demandType="FIXED"),
        dict(period=2, rate=D(".0006"), amount=D(-10000), demandType="FRR"),
    ]
    calls = []
    baseline = fake_value(calls)

    def quotes(*args):
        return [
            dict(rate=D(".0004"), submitted_rate=D(".0004"), offer_type="LIMIT", display_type="LIMIT"),
            dict(rate=D(".0005"), submitted_rate=D(0), offer_type="FRRDELTAVAR", display_type="FRR"),
        ]

    def value(*args, **kwargs):
        result = baseline(*args, **kwargs)
        is_limit = result["display_type"] == "LIMIT"
        reevaluated = len(args[6]) > len(book)
        result["cycleNetApr"] = (0 if reevaluated else 0.2) if is_limit else 0.1
        if reevaluated and not is_limit:
            result.update(expectedFillProbability=0, typeBlockReason="FRR保守持有收益低于净年化底线")
        return result

    monkeypatch.setattr(core.previous, "quotes", quotes)
    monkeypatch.setattr(core, "value_candidate", value)
    result = core.build_plan(account, policy, model, book, trades, NOW, "active", D(".0005"))
    assert any(call[1] == "FRR" and call[-1] > len(book) for call in calls)
    assert result["planned_amount"] == account["wallet"]
    assert all(row["display_type"] == "LIMIT" for row in result["plan"])


@pytest.mark.parametrize("over_cap", [False, True])
def test_managed_offer_still_gets_candidates_without_executable_cash(inputs, monkeypatch, over_cap):
    policy, model, trades, book, account = inputs
    account["managedOffers"] = [dict(managed=True, period=2, amount=D(150), rate=D(".0004"))]
    if over_cap:
        policy = replace(policy, max_lend_amount=D(360))
        account["existingExposure"] = {"total": D(600)}
    calls = []
    monkeypatch.setattr(core, "value_candidate", fake_value(calls))
    result = core.build_plan(account, policy, model, book, trades, NOW, "active", D(".0005"))
    assert calls and result["candidates"]
    assert not result.get("blockReasons") and result["plan"] == []
    assert result["planned_amount"] == 0


def test_frr_valued_once_per_phase_and_same_executable_plan(inputs, monkeypatch):
    policy, model, trades, book, account = inputs
    policy = replace(
        policy,
        maximum_period=8,
        long_from_days=8,
        variable_max_share=D(100),
        **{field: kind == "FRR" for kind, field in core.FIELDS.items()},
    )
    account["wallet"] = D(150)
    book.append(dict(period=2, rate=D(".0004"), amount=D(-1000), demandType="FRR"))
    calls = []
    monkeypatch.setattr(core, "value_candidate", fake_value(calls))
    with funding_sizing(D(150)):
        result = core.build_plan(account, policy, model, book, trades, NOW, "active", D(".0005"))
    assert Counter(call[-1] for call in calls) == {2: 2}
    assert Counter((call[0], call[-1]) for call in calls) == {(2, 2): 1, (7, 2): 1}
    assert len(result["candidates"]) == 2 and len(result["plan"]) == 1
    assert result["planned_amount"] == D(150)
    order = result["plan"][0]
    assert order["display_type"] == "FRR" and order["submitted_rate"] == 0
    assert order["effective_rate"] == D(".0005") and order["period"] == 2
    # The baseline generator emits this same native quote just once; duplicate
    # parent prices must not alter the final principal, term, price or plan hash.
    original_quotes = core.previous.quotes
    yielded = set()

    def unique_quotes(policy, period, rate, frr):
        result = []
        for quote in original_quotes(policy, period, rate, frr):
            identity = period, quote["display_type"], quote["submitted_rate"]
            if identity not in yielded:
                yielded.add(identity)
                result.append(quote)
        return result

    monkeypatch.setattr(core.previous, "quotes", unique_quotes)
    with funding_sizing(D(150)):
        baseline = core.build_plan(account, policy, model, book, trades, NOW, "active", D(".0005"))
    assert result["plan"] == baseline["plan"]
    assert result["plan_hash"] == baseline["plan_hash"]


def test_quote_dedup_preserves_different_terms_types_and_native_rates(inputs, monkeypatch):
    policy, model, trades, book, account = inputs
    policy = replace(policy, maximum_period=8, long_from_days=8)
    account["managedOffers"] = [dict(managed=True, period=7, amount=D(150), rate=D(".0007"))]
    book.extend(
        [
            dict(period=2, rate=D(".0007"), amount=D(-1000)),
            dict(period=7, rate=D(".0008"), amount=D(-1000)),
        ]
    )
    calls = []
    monkeypatch.setattr(core, "value_candidate", fake_value(calls))
    result = core.build_plan(account, policy, model, book, trades, NOW, "active", D(".0005"))
    keys = [(period, kind, rate) for period, kind, rate, _, _ in calls]
    assert len(keys) == len(set(keys))
    assert (2, "FRR", D(0)) in keys and (7, "FRR", D(0)) in keys
    assert {kind for _, kind, _ in keys} == set(core.TYPES)
    assert (2, "LIMIT", D(".0006")) in keys and (2, "LIMIT", D(".0007")) in keys
    assert (7, "LIMIT", D(".0008")) in keys
    assert not result.get("blockReasons") and result["plan"] == []
