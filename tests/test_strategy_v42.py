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


def cash_chain_inputs(inputs, monkeypatch, known=False):
    policy, model, trades, _, account = inputs
    policy = replace(
        policy, maximum_period=8, long_from_days=8,
        **{field: kind == "LIMIT" for kind, field in core.FIELDS.items()},
    )
    trades = copy.deepcopy(trades)
    trades[0]["amount"] = D(1)
    account = {**account, "wallet": D(600), "passiveQuarantines": {
        "old-price": dict(chainStartMs=NOW - 360 * 60000, closedAtMs=NOW - 2000, evidenceWatermark=[]),
    }}
    book = [dict(period=2, rate=D(".0006"), amount=D(-50 if known else -1000),
                 demandType="FIXED" if known else "UNKNOWN")]
    calls = []
    baseline = fake_value(calls)

    def quotes(_policy, period, _rate, _frr):
        return [dict(rate=rate, submitted_rate=rate, offer_type="LIMIT", display_type="LIMIT")
                for rate in (D(".0006"), D(".0004"))] if period == 2 else []

    def value(*args, **kwargs):
        result = baseline(*args, **kwargs)
        result["cycleNetApr"] = float(args[3]) * 365
        return result

    monkeypatch.setattr(core.previous, "quotes", quotes)
    monkeypatch.setattr(core, "value_candidate", value)
    return policy, model, trades, book, account, calls


@pytest.mark.parametrize("known", [False, True])
def test_expired_cash_chain_selects_lower_executable_quote_not_blocked_high_quote(inputs, monkeypatch, known):
    policy, model, trades, book, account, calls = cash_chain_inputs(inputs, monkeypatch, known)
    result = core.build_plan(account, policy, model, book, trades, NOW, "active")
    assert not result.get("blockReasons")
    assert result["planned_amount"] == 600 and result["idle_amount"] == 0
    assert len(result["plan"]) == 1
    row = result["plan"][0]
    assert row["submitted_rate"] == D(".0004") and row["passive"]
    assert row["evidenceWatermark"] == ["trade:1"]
    assert row["submitted_rate"] >= base.gross_floor(policy, row["period"])
    assert calls[-1][2:4] == (D(".0004"), D(600))
    assert not any(rate == D(".0006") and amount > 150 for _, _, rate, amount, _ in calls)
    if not known:
        assert all(rate != D(".0006") for _, _, rate, _, _ in calls)
    # KNOWN demand under the minimum becomes passive and must pass the same
    # cash-chain gate as an initially passive low-confidence quote.
    assert all(c["passive"] == (not known) for c in result["candidates"])


@pytest.mark.parametrize("fault", ["historic", "already_seen", "latest_close", "book_only", "incompatible", "type"])
def test_expired_cash_chain_without_independent_compatible_trade_keeps_real_cash(inputs, monkeypatch, fault):
    policy, model, trades, book, account, calls = cash_chain_inputs(inputs, monkeypatch)
    if fault == "historic":
        trades[0]["mts"] = NOW - 2000  # A token at the closure boundary is not newer.
    elif fault == "already_seen":
        account["passiveQuarantines"]["other-price"] = dict(
            chainStartMs=NOW - 1000, closedAtMs=NOW - 1500, evidenceWatermark=["trade:1"],
        )
    elif fault == "latest_close":
        account["passiveQuarantines"]["other-price"] = dict(
            chainStartMs=NOW - 1000, closedAtMs=NOW - 500, evidenceWatermark=[],
        )
    elif fault == "book_only":
        trades[0]["mts"] = NOW - 3000
        book[0].update(id=99, firstSeenMs=NOW - 500)
    elif fault == "incompatible":
        trades[0]["period"] = 7
    else:
        trades[0]["demandType"] = "FRR"
    result = core.build_plan(account, policy, model, book, trades, NOW, "active")
    assert not result.get("blockReasons")
    assert not result["plan"] and result["planned_amount"] == 0
    assert result["idle_amount"] == account["wallet"]
    assert not calls  # Unexecutable passive quotes never spend the valuation budget.
    assert result["empty_reason"] == "WAIT_FOR_PASSIVE_EVIDENCE"
    assert any("6小时" in reason and "新成交证据" in reason for reason in result["diagnostics"])


@pytest.mark.parametrize("fresh", [False, True])
def test_normal_demand_exhaustion_rechecks_passive_cash_gate_on_later_allocation(inputs, monkeypatch, fresh):
    policy, model, trades, book, account, calls = cash_chain_inputs(inputs, monkeypatch, known=True)
    book[0]["amount"] = D(-300)
    if not fresh:
        trades[0]["mts"] = NOW - 3000
    result = core.build_plan(account, policy, model, book, trades, NOW, "active")
    normal = [r for r in result["plan"] if not r["passive"]]
    assert len(normal) == 1 and normal[0]["amount"] == 300
    assert normal[0]["submitted_rate"] == D(".0006")
    passive = [r for r in result["plan"] if r["passive"]]
    if fresh:
        assert len(passive) == 1 and passive[0]["amount"] == 300
        assert passive[0]["submitted_rate"] == D(".0004")
        assert result["planned_amount"] == 600 and result["idle_amount"] == 0
    else:
        assert not passive and result["planned_amount"] == 300 and result["idle_amount"] == 300
        assert any("6小时" in reason for reason in result["diagnostics"])
    assert not any(rate == D(".0006") and amount > 150 for _, _, rate, amount, _ in calls)


def test_expired_cash_chain_does_not_block_known_normal_demand(inputs, monkeypatch):
    policy, model, trades, book, account, _ = cash_chain_inputs(inputs, monkeypatch, known=True)
    trades[0]["mts"] = NOW - 3000
    book[0]["amount"] = D(-1000)
    result = core.build_plan(account, policy, model, book, trades, NOW, "active")
    assert result["planned_amount"] == account["wallet"]
    assert all(not row["passive"] and row["submitted_rate"] == D(".0006") for row in result["plan"])


def test_passive_cash_chain_is_account_local_and_missing_legacy_clock_preserves_compatibility(inputs, monkeypatch):
    policy, model, trades, book, account, _ = cash_chain_inputs(inputs, monkeypatch)
    trades[0]["mts"] = NOW - 3000
    blocked = core.build_plan(account, policy, model, book, trades, NOW, "active")
    assert not blocked["plan"]
    legacy_account = copy.deepcopy(account)
    del legacy_account["passiveQuarantines"]["old-price"]["chainStartMs"]
    compatible = core.build_plan(legacy_account, policy, model, book, trades, NOW, "active")
    assert compatible["planned_amount"] == 600
    assert compatible["plan"][0]["submitted_rate"] == D(".0006")
    usd_model = core.fit_model("USD", trades, now_ms=NOW)
    usd_policy = replace(policy, currency="USD", model_id=usd_model["id"])
    usd_account = {**account, "passiveQuarantines": {}}
    independent = core.build_plan(usd_account, usd_policy, usd_model, book, trades, NOW, "active")
    assert independent["planned_amount"] == 600
    assert independent["plan"][0]["submitted_rate"] == D(".0006")
    assert not core.build_plan(account, policy, model, book, trades, NOW, "active")["plan"]


@pytest.mark.parametrize("mode", ["reject", "accept", "invalid_reference"])
def test_blocked_short_quote_remains_economic_reference_without_becoming_executable(inputs, monkeypatch, mode):
    policy, model, trades, book, account, _ = cash_chain_inputs(inputs, monkeypatch)
    policy = replace(policy, maximum_period=60, long_from_days=31)
    trades.append(dict(id=999, mts=NOW - 1000, period=14, rate=D(".0005"), amount=D(1000)))
    calls, markets = [], []
    baseline = fake_value(calls)

    def quotes(_policy, period, _rate, _frr):
        rates = (D(".0006"), D(".0004")) if period == 2 else (D(".0005"),) if period == 60 else ()
        return [dict(rate=rate, submitted_rate=rate, offer_type="LIMIT", display_type="LIMIT") for rate in rates]

    def value(*args, **kwargs):
        result = baseline(*args, **kwargs)
        markets.append(id(kwargs["market"]))
        if args[2] == 60:
            result.update(cycleNetApr=0.30, conservativeNetApr=0.12)
        elif args[3] == D(".0006"):
            result["conservativeNetApr"] = 0.11 if mode == "accept" else 0.13
            if mode == "invalid_reference":
                result["typeBlockReason"] = "无效参考收益"
        return result

    monkeypatch.setattr(core.previous, "quotes", quotes)
    monkeypatch.setattr(core, "value_candidate", value)
    result = core.build_plan(account, policy, model, book, trades, NOW, "active")
    assert not result.get("blockReasons") and result["planned_amount"] == 600
    assert len(set(markets)) == 1  # References share the original decision cache and deadline.
    blocked_reference = [call for call in calls if call[2] == D(".0006")]
    assert len(blocked_reference) == 1 and blocked_reference[0][3] == 150
    assert all(c["submitted_rate"] != D(".0006") for c in result["candidates"])
    assert all(row["submitted_rate"] != D(".0006") for row in result["plan"])
    if mode == "reject":
        # 12% beats executable short's 10% + 0.5pp, but fails the original
        # blocked short reference of 13% + 0.5pp. The long quote stays out.
        assert all(c["period"] < 31 for c in result["candidates"])
        assert result["plan"][0]["period"] == 2
    else:
        assert result["plan"][0]["period"] == 60


@pytest.mark.parametrize("long_fault", ["no_new_evidence", "type_floor"])
def test_without_legal_long_candidate_never_values_blocked_short_reference(inputs, monkeypatch, long_fault):
    policy, model, trades, book, account, _ = cash_chain_inputs(inputs, monkeypatch)
    policy = replace(policy, maximum_period=60, long_from_days=31)
    if long_fault == "type_floor":
        trades.append(dict(id=999, mts=NOW - 1000, period=14, rate=D(".0005"), amount=D(1000)))
    calls = []
    baseline = fake_value(calls)

    def quotes(_policy, period, _rate, _frr):
        rates = (D(".0006"), D(".0004")) if period == 2 else (D(".0005"),) if period == 60 else ()
        return [dict(rate=rate, submitted_rate=rate, offer_type="LIMIT", display_type="LIMIT") for rate in rates]

    def value(*args, **kwargs):
        result = baseline(*args, **kwargs)
        if args[2] == 60:
            result["typeBlockReason"] = "FRR保守持有收益低于净年化底线"
        return result

    monkeypatch.setattr(core.previous, "quotes", quotes)
    monkeypatch.setattr(core, "value_candidate", value)
    result = core.build_plan(account, policy, model, book, trades, NOW, "active")
    assert not result.get("blockReasons") and result["planned_amount"] == 600
    assert all(row["period"] == 2 for row in result["plan"])
    assert all(rate != D(".0006") for _, _, rate, _, _ in calls)
    assert any(period == 60 for period, _, _, _, _ in calls) == (long_fault == "type_floor")
