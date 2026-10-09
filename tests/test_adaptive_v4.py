import json
import time
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

import AdaptiveRuntime
import ReplayV4
import ResearchV4
import StrategyV4 as core
from Configuration import strategy_v3_api_values, strategy_v3_from_api_payload
from Currency import funding_sizing
from DomainTypes import WriteOutcome, WriteResult
from ResearchV4 import ModelRepository, ResearchJobs, build_from_store, connect_readonly, moving_block_interval
from RuntimeV3 import LendingRuntimeV3
from StateStore import LendingStateStore
from StrategyV3 import StrategyPolicyV3, validate_policy_v3

NOW = 1_900_000_000_000


def test_market_distributions_match_scans_and_exclude_future_and_incompatible_trades():
    trades = [
        dict(mts=NOW - age, period=period, rate=D(rate), amount=D(amount))
        for age in (0, 3600000, core.DAY, 7 * core.DAY, 7 * core.DAY + 1, -1)
        for period in (2, 7, 30, 31, 120)
        for rate, amount in ((".0002", "0"), (".0004", "-1.23456789"), (".0004", "2"), (".0008", "9"))
    ]
    market = core.CandidateMarket(trades, NOW)
    for period in (2, 7, 30, 31, 120):
        for interval in (3600000, core.DAY, 7 * core.DAY):
            selected = [r for r in trades if r["period"] <= period and NOW - interval <= r["mts"] <= NOW]
            for q in (0, 0.25, 0.5, 0.75, 0.9, 1):
                assert market.reference(period, interval, q) == core.weighted_rate(selected, q)
        for rate in (D(0), D(".0002"), D(".0004"), D(".0006"), D(".0008"), D(1)):
            volume = sum(
                abs(r["amount"]) for r in trades
                if r["period"] <= period and NOW - core.DAY <= r["mts"] <= NOW and r["rate"] >= rate
            )
            assert market.flow(period, rate) == float(volume) / 1440
    cached = market.distribution(7, core.DAY)
    assert market.distribution(7, core.DAY) is cached
    assert core.CandidateMarket([], NOW).reference(2, core.DAY) == 0
    assert core.CandidateMarket([], NOW).flow(2, D(0)) == 0
    assert core.CandidateMarket(trades, NOW + 8 * core.DAY).reference(2, core.DAY) == 0


def test_candidate_paths_select_holding_distribution_once(monkeypatch):
    rows = [dict(period=2, opened_ms=NOW - core.DAY, closed_ms=None)]
    p, model, trades, book, _ = setup(holdings=rows)
    original = core._holding_curve
    calls = []

    def observed(rows):
        calls.append(rows)
        return original(rows)

    monkeypatch.setattr(core, "_holding_curve", observed)
    value = core.value_candidate(p, model, 2, D(".0008"), D(150), trades, book, NOW)
    assert len(calls) == 1
    assert set(value["pathHoldingHours"]) == {48.0}
    stress = core.value_candidate(p, model, 2, D(".0008"), D(150), trades, book, NOW, stress=True)
    assert set(stress["pathHoldingHours"]) == {1.0}


def test_cached_holding_curve_matches_censored_survival_and_exact_boundaries():
    rows = [
        dict(period=term, hours=hours, event=event)
        for term in (2, 14, 120)
        for hours, event in ((0, True), (1, False), (2, True), (2, False), (4, True), (500, False))
    ]

    def reference(period, u):
        selected = [r for r in rows if core.pool_for_period(r["period"]) == core.pool_for_period(period)]
        survival = 1.0
        for hours in sorted({r["hours"] for r in selected if r["event"]}):
            risk = sum(r["hours"] >= hours for r in selected)
            events = sum(r["event"] and r["hours"] == hours for r in selected)
            survival *= 1 - events / risk
            if u > survival:
                return min(period * 24.0, max(1 / 3600, hours))
        return period * 24.0

    core._holding_curve.cache_clear()
    for period in (2, 7, 14, 30, 31, 120):
        for u in (0, 0.1, 0.3, 0.5, 0.7, 5 / 6, 0.9, 1):
            assert core.holding_hours({"holdings": rows}, period, u) == reference(period, u)
    assert core._holding_curve.cache_info().hits > 0
    assert core.holding_hours({"holdings": [dict(period=2, hours=1, event=False)]}, 2, 0.9) == 48


def market(rate=".0008", period=2, amount="100000"):
    return [{"mts": NOW - 1000, "id": "1", "rate": D(rate), "period": period, "amount": D(amount)}]


def setup(currency="USD", rate=".0008", observations=(), holdings=()):
    trades = market(rate)
    model = core.fit_model(currency, trades, observations, holdings, now_ms=NOW)
    policy = replace(core.adaptive_template(StrategyPolicyV3(currency=currency)), model_id=model["id"])
    book = [{"period": 2, "rate": D(rate), "amount": D("-1000")}]
    account = {"total": D(1000), "wallet": D(1000), "exposure": dict(short=D(0), medium=D(0), long=D(0))}
    return policy, model, trades, book, account


@pytest.mark.parametrize("currency", ["USD", "USDT"])
def test_default_floors_fees_limits_and_api_roundtrip(currency):
    p, *_ = setup(currency)
    assert validate_policy_v3(p) == p
    assert core.floor(p, 7) == core.floor(p, 30) == D(".05")
    assert core.floor(p, 31) == core.floor(p, 120) == D(".10")
    assert core.fee(replace(p, normal_fee_rate=D(".147"), fee_verified=True)) == D(".15")
    assert core.fee(replace(p, normal_fee_rate=D(".20"))) == D(".20")
    for days in [2, 7, 14, 30, 31, 120]:
        assert core.gross_floor(p, days) * 365 * D(".85") >= core.floor(p, days)
    reconstructed = strategy_v3_from_api_payload(strategy_v3_api_values(p), base=p)
    assert reconstructed == p
    assert p.long_max_share == 95 and p.maximum_period == 120
    assert core.quantile([], 0.1) == 0 and core.weighted_rate([]) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"strategy_engine": "bad"},
        {"long_from_days": 7},
        {"maximum_period": 121},
        {"long_max_share": D(0)},
        {"version": 3},
        {"short_floor_apr": None},
        {"medium_floor_apr": D(".06")},
        {"long_floor_apr": D(".04")},
        {"enable_limit": False},
        {"enable_frr": True},
        {"enable_hidden": True},
    ],
)
def test_invalid_adaptive_boundaries(changes):
    with pytest.raises(ValueError):
        core.validate_adaptive(replace(setup()[0], **changes))


def test_survival_keeps_censored_partial_chains_and_excludes_future():
    rows = [
        {
            "chainId": "one",
            "period": 2,
            "start_ms": NOW - 3600000,
            "end_ms": NOW,
            "amount": 200,
            "fills": [(NOW - 3500000, 50), (NOW - 3400000, 50)],
        },
        {"chainId": "two", "period": 2, "start_ms": NOW - 600000, "end_ms": NOW, "amount": 100, "fills": []},
        {"chainId": "one", "period": 2, "start_ms": NOW - 300000, "end_ms": NOW, "amount": 100, "fills": []},
        {"period": 2, "start_ms": None, "end_ms": NOW, "amount": 100},
        {"period": 2, "start_ms": NOW, "end_ms": NOW + 1, "amount": 100},
        {"period": 2, "start_ms": NOW - 60000, "end_ms": NOW, "amount": 0},
    ]
    holdings = [
        {"period": 120, "opened_ms": NOW - core.DAY, "closed_ms": None},
        {"period": 120, "opened_ms": NOW - core.DAY, "closed_ms": NOW - 3600000},
        {"period": 2, "opened_ms": NOW + 1},
        {"period": 2, "opened_ms": NOW - 5, "closed_ms": NOW - 6},
    ]
    m = core.fit_model("USD", market() + [{**market()[0], "mts": NOW + 1}], rows, holdings, NOW)
    assert m["ownObservationCount"] == 2
    assert len(m["holdings"]) == 2 and m["holdings"][0]["event"] is False
    assert m["tables"]["all"][0][1] == 0.25
    assert core.holding_hours(m, 120, 0.1) == 120 * 24
    assert core.holding_hours(m, 120, 0.9) == 23
    assert core.holding_hours(m, 120, 0.1, True) == 1
    assert core.validate_model(m, "USD", NOW) == m
    for data in ({**m, "id": "bad"}, {**m, "currency": "USDT"}):
        with pytest.raises(ValueError):
            core.validate_model(data, "USD", NOW)
    with pytest.raises(ValueError, match="cross-currency"):
        core.fit_model("USD", market(), [{**rows[0], "currency": "USDT"}], now_ms=NOW)
    with pytest.raises(ValueError):
        core.fit_model("USD", market(), holdings=[{**holdings[0], "currency": "USDT"}], now_ms=NOW)
    no_days = core.fit_model("USD", [], now_ms=NOW)
    with pytest.raises(ValueError):
        core.validate_model(no_days, "USD", NOW)
    with pytest.raises(ValueError):
        core.validate_model(m, "USD", NOW - 1)
    with pytest.raises(ValueError, match="expired"):
        core.validate_model(m, "USD", NOW + 151 * core.DAY)
    mutated = {**m, "days": [{"mts": NOW + 1, "rate": ".001"}]}
    mutated["id"] = core.digest({k: v for k, v in mutated.items() if k != "id"})
    with pytest.raises(ValueError):
        core.validate_model(mutated, "USD", NOW)


def test_prior_shrink_and_fixed_paths_ignore_promotion_metadata():
    p, m, trades, book, _ = setup()
    v = core.value_candidate(p, m, 7, D(".0008"), D(150), trades, book, NOW)
    assert len(v["pathInterests"]) == 64
    assert v == core.value_candidate(p, m, 7, D(".0008"), D(150), trades, book, NOW)
    promoted = {**m, "eligibleForLiveCandidate": True, "id": "changed"}
    assert v == core.value_candidate(p, promoted, 7, D(".0008"), D(150), trades, book, NOW)
    quiet = core.value_candidate(p, m, 7, D(".05"), D(150), [], [], NOW)
    assert quiet["expectedWaitMinutes"] is None and quiet["expectedHoldingHours"] is None
    stress = core.value_candidate(
        p, m, 7, D(".0008"), D(150), trades, [{"rate": ".0004", "amount": 1000, "period": 2}], NOW, stress=True
    )
    assert stress["expectedWaitMinutes"] >= v["expectedWaitMinutes"]
    table = [[20, 20] for _ in core.WAIT_BINS[1:]]
    assert core.hazards({"tables": {"all": table, "short": table, "cell": table}}, 2, "cell", [0.1] * 8)[0] > 0.1
    assert core.condition(2, D(".001"), D(".0001"), 20, "up") != core.condition(2, D(".0001"), D(".001"), 0, "down")


def test_shared_demand_small_balance_and_independent_coin_plans():
    p, m, t, b, a = setup()
    r = core.build_plan(a, p, m, b, t, NOW, "v4")
    assert D(0) < r["planned_amount"] <= 1000
    assert len(r["plan"]) <= 8 and all(o["amount"] >= 150 and o["flags"] == 0 for o in r["plan"])
    assert all(o["effective_rate"] >= core.gross_floor(p, o["period"]) for o in r["plan"])
    small = core.build_plan({**a, "wallet": D(299)}, p, m, b, t, NOW, "v4")
    assert len(small["plan"]) == 1 and small["planned_amount"] == 299
    with funding_sizing(D(151)):
        assert core.build_plan({**a, "wallet": D(150)}, p, m, b, t, NOW, "v4")["plan"] == []
    only_book = core.build_plan(a, p, m, [{**b[0], "amount": -150}], [], NOW, "v4")
    assert only_book["planned_amount"] <= 150
    assert core.build_plan({**a, "openOfferCount": 8}, p, m, b, t, NOW, "v4")["plan"] == []
    assert core.build_plan(a, p, None, b, t, NOW, "v4")["empty_reason"] == "MODEL_UNAVAILABLE"
    assert core.build_plan(a, replace(p, model_id="another"), m, b, t, NOW, "v4")["empty_reason"] == "MODEL_UNAVAILABLE"
    up, um, ut, ub, ua = setup("USDT")
    assert core.build_plan(ua, up, um, ub, ut, NOW, "v4")["plan_hash"] != r["plan_hash"]
    assert core.build_plan(ua, up, m, ub, ut, NOW, "v4")["plan"] == []
    cap = core.build_plan(a, replace(p, max_lend_amount=D(200)), m, b, t, NOW, "v4")
    assert cap["planned_amount"] <= 200


def test_long_cap_counts_loans_and_never_overallocates(monkeypatch):
    p, m, t, b, a = setup()
    original = core.value_candidate

    def controlled(*args, **kwargs):
        value = original(*args, **kwargs)
        value["conservativeNetApr"] = 0.2 if args[2] >= 31 else 0.05
        return value

    monkeypatch.setattr(core, "value_candidate", controlled)
    a.update(total=D(1000), wallet=D(200), exposureByPeriod={120: D(900)})
    a["exposure"]["long"] = D(900)
    a["existingExposure"] = {"total": D(900)}
    r = core.build_plan(a, p, m, b, t, NOW, "v4")
    assert not any(row["period"] >= 31 for row in r["plan"])
    a.update(wallet=D(1000), exposureByPeriod={120: D(0)}, existingExposure={"total": D(0)})
    r = core.build_plan(a, p, m, b, t, NOW, "v4")
    assert sum(row["amount"] for row in r["plan"] if row["period"] >= 31) <= 950
    assert all(row["period"] <= 120 for row in r["plan"])


def test_adjustment_external_dust_age_keep_raise_lower_term_and_wait_cost(monkeypatch):
    p, m, t, b, _ = setup()
    offer = {
        "amount": D(200),
        "rate": D(".0008"),
        "period": 2,
        "offer_type": "LIMIT",
        "managed": True,
        "mts_created": NOW - 600000,
    }
    assert core.adjustment(p, m, {**offer, "managed": False}, [], t, b, NOW)["reason"] == "EXTERNAL_OFFER"
    assert core.adjustment(p, m, {**offer, "amount": 100}, [], t, b, NOW)["action"] == "KEEP"
    assert core.adjustment(p, m, {**offer, "amount": 100, "rate": D(".00001")}, [], t, b, NOW)["hard"]
    assert core.adjustment(p, m, {**offer, "mts_created": NOW}, [], t, b, NOW)["reason"] == "MINIMUM_AGE"
    assert core.adjustment(p, m, offer, [], t, b, NOW)["action"] == "KEEP"
    assert core.adjustment(p, m, {**offer, "period": 121}, [], t, b, NOW)["action"] == "CANCEL"
    same = [{"period": 2, "rate": offer["rate"]}]
    assert core.adjustment(p, m, offer, same, t, b, NOW)["reason"] == "QUEUE_VALUE"

    def values(policy, model, period, rate, amount, trades, book, now, **kwargs):
        value = 2 if kwargs.get("cancel_minutes") else 1
        return {"period": period, "rate": rate, "conservativeNetApr": value * 0.1, "pathInterests": [value] * 64}

    monkeypatch.setattr(core, "value_candidate", values)
    for period, rate in [(2, ".0009"), (2, ".0007"), (30, ".0008")]:
        result = core.adjustment(p, m, offer, [{"period": period, "rate": D(rate)}], t, b, NOW)
        assert result["action"] == "CANCEL" and result["p10InterestGain"] == 1


def test_models_and_journals_are_isolated_corruption_and_qualification(tmp_path):
    p, m, _, _, _ = setup()
    store = LendingStateStore(tmp_path / "usd.sqlite3", currency="USD")
    repo = ModelRepository(store.path, "USD")
    assert repo.candidate(NOW) is None
    assert repo.save(m) == m["id"]
    assert repo.candidate(NOW) == m
    with pytest.raises(ValueError):
        repo.load("../bad", NOW)
    with pytest.raises(ValueError):
        ModelRepository(store.path, "USDT").load(m["id"], NOW)
    assert AdaptiveRuntime.eligible(store, m) is False
    report = {"eligibleForLiveCandidate": True, "testedModelTrainingHash": m["id"]}
    model = {**m, "eligibleForLiveCandidate": True, "validationReportHash": core.digest(report)}
    model["id"] = core.digest({k: v for k, v in model.items() if k != "id"})
    repo.save(model)
    assert AdaptiveRuntime.eligible(store, model) is False
    (repo.directory / "evaluation.json").write_text(json.dumps(report), encoding="utf-8")
    assert AdaptiveRuntime.eligible(store, model)
    repo.journal({"action": "WAIT", "atMs": NOW})
    assert json.loads((repo.directory / "decisions.jsonl").read_text())["currency"] == "USD"
    with pytest.raises(ValueError):
        repo.journal({"currency": "USDT"})
    (repo.directory / (model["id"] + ".json")).write_text("bad", encoding="utf-8")
    assert repo.candidate(NOW) is None
    with pytest.raises(ValueError):
        repo.load(model["id"], NOW)
    assert AdaptiveRuntime.context(store, p, [], [], NOW)["model"] == m


def test_research_sql_is_readonly_and_insufficient_data_never_qualifies(tmp_path):
    store = LendingStateStore(tmp_path / "usd.sqlite3", currency="USD")
    p, *_ = setup()
    store.save_strategy(core.json_decimal(p.__dict__), "ACTIVE")
    store.upsert_market_trades(market())
    model = build_from_store(store, NOW)
    assert not model["coverage"]["publicComplete"] and model["confidence"] == "LOW"
    with connect_readonly(store.path) as c:
        with pytest.raises(Exception, match="readonly"):
            c.execute("DELETE FROM market_trades")
    with pytest.raises(InterruptedError):
        build_from_store(store, NOW, lambda: True)
    report, candidate = ReplayV4.evaluate(store, NOW)
    assert report["state"] == "INSUFFICIENT_DATA" and candidate["eligibleForLiveCandidate"] is False
    with pytest.raises(ValueError):
        ResearchV4.training_data(store.path, "USDT", NOW)
    assert moving_block_interval([1], [0])["valid"] is False
    ci = moving_block_interval([2] * 15, [1] * 15)
    assert ci["lower"] == ci["upper"] == 1 and ci["iterations"] == 2000
    empty = core.fit_model("USDT", [], now_ms=NOW)
    repo = ModelRepository(store.path, "USDT")
    repo.save(empty)
    assert repo.candidate(NOW) is None  # retained research artifact is never an executable model


def test_research_async_cancel_resume_and_readonly_shadow(tmp_path):
    store = LendingStateStore(tmp_path / "usd.sqlite3", currency="USD")
    p, *_ = setup()
    store.save_strategy(core.json_decimal(p.__dict__), "ACTIVE")
    store.upsert_market_trades(market())
    jobs = ResearchJobs(lambda currency: store, lambda: NOW / 1000)
    assert jobs.status("USD")["state"] == "IDLE"
    with pytest.raises(ValueError):
        jobs.start("USD", "bad")
    jobs.start("USD")
    deadline = time.monotonic() + 5
    while jobs.status("USD")["state"] == "RUNNING" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert jobs.status("USD")["state"] == "COMPLETED"
    restarted = ResearchJobs(lambda currency: store, lambda: NOW / 1000)
    assert restarted.status("USD")["state"] == "COMPLETED"
    jobs.start("USD", "shadow")
    deadline = time.monotonic() + 5
    while jobs.status("USD").get("phase") != "SHADOW" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert jobs.start("USD", "shadow")["state"] == "RUNNING"
    jobs.stop("USD")
    deadline = time.monotonic() + 5
    while jobs.status("USD")["state"] == "RUNNING" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert jobs.status("USD")["state"] == "CANCELLED"
    assert "ACCOUNT_OR_MARKET_STALE" in (ModelRepository(store.path, "USD").directory / "decisions.jsonl").read_text()


def test_event_replay_partial_fill_interest_seconds_open_credit_unknown_wait_and_cancel():
    p, m, _, b, a = setup()
    p = replace(p, max_lend_amount=D(200))
    start = NOW
    initial = market()
    books = [{"mts": start, "book": b}, {"mts": start + 60000, "book": b}]
    trades = [
        {**market()[0], "mts": start + 1000, "amount": D(50)},
        {**market()[0], "mts": start + 3000, "amount": D(50)},
    ]
    result = ReplayV4.replay(p, m, trades, books, 200, start, start + 10000, initial_trades=initial)
    assert result["fillCount"] == 2 and result["openCreditAmount"] == 100
    expected = D(50) * D(".0008") * D(".85") * D(9000 + 7000) / core.DAY
    assert abs(result["netInterest"] - expected) < D(".0000000001")
    assert abs(result["averageWaitMinutes"] - D(2000) / 60000) < D(".000000001")
    idle = ReplayV4.replay(p, m, [], books, 200, start, start + 10000)
    assert idle["averageWaitMinutes"] is None and idle["netInterest"] == 0
    with pytest.raises(InterruptedError):
        ReplayV4.replay(p, m, [], books, 200, start, start + 1, cancelled=lambda: True)
    assert (
        LendingRuntimeV3._build_plan(
            a, p, {"adaptiveContext": {"model": m, "book": b, "trades": initial, "now_ms": NOW}}, "v4"
        )["engine"]
        == core.ENGINE
    )


def test_runtime_missing_model_blocks_without_any_write(tmp_path):
    p, _, t, b, account = setup()
    store = LendingStateStore(tmp_path / "usd.sqlite3", currency="USD")
    store.save_strategy(core.json_decimal(p.__dict__), "ACTIVE")
    store.set_mode("LIVE")

    def forbidden(*args, **kwargs):
        raise AssertionError("exchange write attempted")

    runtime = SimpleNamespace(
        policy=p, store=store, client=SimpleNamespace(submit_funding_offer=forbidden, cancel_funding_offer=forbidden)
    )
    result = AdaptiveRuntime.cycle(runtime, {"book": b, "trades": t, "offers": []}, account, {}, NOW, False)
    assert result["submitted"] == [] and store.runtime()["mode"] == "PAUSED"


def qualified_runtime(tmp_path, monkeypatch, outcome=WriteOutcome.CONFIRMED):
    p, m, t, b, account = setup()
    store = LendingStateStore(tmp_path / "usd.sqlite3", clock=lambda: NOW / 1000, currency="USD")
    active = store.save_strategy(core.json_decimal(p.__dict__), "ACTIVE")
    store.set_mode("LIVE")
    monkeypatch.setattr(AdaptiveRuntime, "eligible", lambda *_: True)
    ModelRepository(store.path, "USD").save(m)
    offer = {
        "id": 10,
        "currency": "USD",
        "amount": D(200),
        "amount_original": D(200),
        "rate": D(".0008"),
        "rate_real": D(".0008"),
        "period": 2,
        "offer_type": "LIMIT",
        "display_type": "LIMIT",
        "flags": 0,
        "status": "ACTIVE",
        "managed": True,
        "pool": "short",
        "layer": "balanced",
        "mts_created": NOW - 600000,
    }
    _, intent = store.reserve_intent(
        {
            **offer,
            "submitted_rate": offer["rate"],
            "effective_rate": offer["rate"],
            "strategy_version": active,
            "slice_key": "v4:short:balanced:0",
        },
        D(1000),
    )
    store.confirm_intent(intent["id"], 10)
    store.reconcile_offers([offer], NOW)
    submitted, cancelled = [], []

    def cancel(oid):
        cancelled.append(oid)
        return WriteResult(outcome, response=[0, "SUCCESS"])

    def submit(*args):
        submitted.append(args)
        return []

    runtime = SimpleNamespace(
        policy=p,
        store=store,
        currency="USD",
        client=SimpleNamespace(cancel_funding_offer_result=cancel),
        _pending_cancel_requested=set(),
        _submit_plan=submit,
    )
    snapshot = {"book": b, "trades": t, "offers": [offer]}
    return runtime, snapshot, account, submitted, cancelled


def test_adjustment_debounce_confirmation_barrier_restart_and_isolation(tmp_path, monkeypatch):
    runtime, snapshot, account, submitted, cancelled = qualified_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(
        AdaptiveRuntime,
        "adjustment",
        lambda *_: {
            "action": "CANCEL",
            "reason": "VALUE_GAIN",
            "hard": False,
            "targetRate": D(".0009"),
            "targetPeriod": 7,
        },
    )
    first = AdaptiveRuntime.cycle(runtime, snapshot, account, {}, NOW, False)
    assert first["canceledForReprice"] == []
    submitted.clear()
    second = AdaptiveRuntime.cycle(runtime, snapshot, account, {}, NOW + 60000, False)
    assert second["canceledForReprice"] == [10] and submitted == []
    runtime._adaptive_confirmations = {}
    again = AdaptiveRuntime.cycle(runtime, snapshot, account, {}, NOW + 120000, False)
    assert cancelled == [10] and again["submitted"] == []
    snapshot["offers"] = []
    # An authoritative account snapshot releases funds; only then may the
    # ordinary durable submission path run. No direct exchange submit here.
    runtime.store.reconcile_offers([], NOW + 180000)
    AdaptiveRuntime.cycle(runtime, snapshot, account, {}, NOW + 180000, False)
    assert len(submitted) == 1
    other = LendingStateStore(tmp_path / "usdt.sqlite3", currency="USDT")
    assert other.runtime()["mode"] != runtime.store.runtime()["mode"]


def test_unknown_cancel_and_journal_failure_block_all_replacement_writes(tmp_path, monkeypatch):
    runtime, snapshot, account, submitted, cancelled = qualified_runtime(tmp_path, monkeypatch, WriteOutcome.UNKNOWN)
    monkeypatch.setattr(
        AdaptiveRuntime, "adjustment", lambda *_: {"action": "CANCEL", "reason": "HARD_FLOOR", "hard": True}
    )
    AdaptiveRuntime.cycle(runtime, snapshot, account, {}, NOW, False)
    assert runtime.store.runtime()["mode"] == "PAUSED" and submitted == [] and cancelled == [10]
    runtime.store.set_mode("LIVE")
    snapshot["offers"] = []

    def failure(*_):
        raise OSError("disk full")

    monkeypatch.setattr(ModelRepository, "journal", failure)
    result = AdaptiveRuntime.cycle(runtime, snapshot, account, {}, NOW + 180000, False)
    assert "无法持久化" in result["blockReasons"][0] and submitted == []


def test_runtime_ordinary_budget_cooldown_and_wait_interval(tmp_path, monkeypatch):
    runtime, snapshot, account, submitted, cancelled = qualified_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(
        AdaptiveRuntime, "adjustment", lambda *_: {"action": "CANCEL", "reason": "VALUE_GAIN", "hard": False}
    )
    monkeypatch.setattr(runtime.store, "reprice_count_since", lambda *_: 12)
    AdaptiveRuntime.cycle(runtime, snapshot, account, {}, NOW, False)
    interval = AdaptiveRuntime.cycle(runtime, snapshot, account, {}, NOW + 1000, False)
    assert interval["decisions"][0]["reason"] == "EVALUATION_INTERVAL"
    AdaptiveRuntime.cycle(runtime, snapshot, account, {}, NOW + 60000, False)
    assert cancelled == []
    # Resume barriers never allow writes even with a qualified model.
    before = len(submitted)
    result = AdaptiveRuntime.cycle(runtime, snapshot, account, {}, NOW + 120000, True)
    assert result["recoveryResumeBarrier"] and len(submitted) == before


def test_pagination_overlap_saturated_timestamp_exposes_gap(tmp_path):
    from StrategyResearch import backfill_public_market_data

    store = LendingStateStore(tmp_path / "usd.sqlite3")
    start = NOW - 90 * core.DAY

    class PublicOnly:
        def funding_trades(self, *args, **kwargs):
            return [[1, start + 100, "150", ".0008", 2], [2, start + 100, "150", ".0008", 2]]

        def funding_stats(self, *args, **kwargs):
            return []

    result = backfill_public_market_data(
        PublicOnly(), store, now_ms=NOW, page_limit=2, rate_limiter=SimpleNamespace(wait=lambda: None)
    )
    assert result["complete"] is False and result["gaps"] and len(store.market_trades()) == 2
    assert not build_from_store(store, NOW)["coverage"]["publicComplete"]


def test_research_gate_checkpoint_frozen_model_and_report_binding(tmp_path, monkeypatch):
    store = LendingStateStore(tmp_path / "usd.sqlite3", currency="USD")
    p = replace(setup()[0], strategy_engine="legacy_v3")
    store.save_strategy(core.json_decimal(p.__dict__), "ACTIVE")
    trained_at = []

    def training(_store, cutoff, cancelled, lookback_days=90):
        trained_at.append((cutoff, lookback_days))
        model = core.fit_model(
            "USD",
            [{**market()[0], "mts": cutoff - 1}],
            now_ms=cutoff,
            coverage={"publicComplete": True, "bookSnapshots": {"earliestMs": cutoff - 90 * core.DAY}},
        )
        model["ownObservationCount"] = 20
        model["id"] = core.digest({k: v for k, v in model.items() if k != "id"})
        return model

    monkeypatch.setattr(ReplayV4, "build_from_store", training)
    monkeypatch.setattr(ReplayV4, "streams", lambda *args: ([], []))
    calls = []

    def result(policy, model, trades, books, principal, start, end, cancelled, stress=False):
        calls.append((policy.strategy_engine, stress))
        gain = D(2) if policy.strategy_engine == core.ENGINE else D(1)
        return {
            "netInterest": gain * 15,
            "returnOnPrincipalTime": gain / 1000,
            "netAprPercent": gain,
            "dailyNetInterest": [gain] * 15,
            "bookCoverageFraction": 1,
        }

    monkeypatch.setattr(ReplayV4, "replay", result)
    report, model = ReplayV4.evaluate(store, NOW)
    assert report["eligibleForLiveCandidate"] and model["trainedUntilMs"] == NOW - 30 * core.DAY
    assert (NOW - 30 * core.DAY, 60) in trained_at
    repo = ModelRepository(store.path, "USD")
    repo.save(model)
    (repo.directory / "evaluation.json").write_text(json.dumps(core.json_decimal(report)), encoding="utf-8")
    assert AdaptiveRuntime.eligible(store, model)
    changed = {**model, "days": [{"mts": NOW - 90 * core.DAY, "rate": ".009"}]}
    assert not AdaptiveRuntime.eligible(store, changed)
    count = len(calls)
    ReplayV4.evaluate(store, NOW)
    assert len(calls) == count + 1  # finished ten comparisons reused; stress is rechecked
    with pytest.raises(InterruptedError):
        ReplayV4.evaluate(store, NOW, lambda: True)

    def adverse(*args, **kwargs):
        row = result(*args, **kwargs)
        if kwargs.get("stress"):
            row["netInterest"] = D(0)
        return row

    monkeypatch.setattr(ReplayV4, "replay", adverse)
    report, _ = ReplayV4.evaluate(store, NOW)
    assert not report["eligibleForLiveCandidate"] and not report["stressPassed"]


def test_common_legacy_replay_keeps_chain_wait_and_accrues_early_returns():
    p, model, _, _, _ = setup()
    p = replace(
        p,
        strategy_engine="legacy_v3",
        short_share=D(100),
        medium_share=D(0),
        long_share=D(0),
        max_lend_amount=D(300),
        quick_share=D(100),
        balanced_share=D(0),
        high_share=D(0),
        minimum_rate_change=D(".000000001"),
    )
    book = [
        {"period": 2, "rate": D(".0004"), "amount": D(-5000)},
        {"period": 2, "rate": D(".0008"), "amount": D(100000)},
    ]
    books = [{"mts": NOW + minute * 60000, "book": book} for minute in range(61)]
    trades = [{**market(".0004")[0], "mts": NOW + minute * 60000 + 1000, "amount": D(1000)} for minute in range(61)]
    r = ReplayV4.replay(p, model, trades, books, 300, NOW, NOW + 3600000, initial_trades=market(".0008"))
    assert r["cancellationCount"] > 0 and r["bookCoverageFraction"] == 1
    p = replace(p, enable_limit=False, enable_frr=True)
    no_frr = ReplayV4.replay(
        p,
        model,
        trades[:1],
        [{"mts": NOW, "book": book, "frr": ".0004"}],
        300,
        NOW,
        NOW + 2000,
        initial_trades=market(".0004"),
    )
    assert no_frr["netInterest"] >= 0


def test_explicit_cancel_rejection_preserves_original_chain(tmp_path, monkeypatch):
    runtime, snapshot, account, submitted, cancelled = qualified_runtime(
        tmp_path, monkeypatch, WriteOutcome.DEFINITE_REJECT
    )
    monkeypatch.setattr(
        AdaptiveRuntime, "adjustment", lambda *_: {"action": "CANCEL", "reason": "HARD_FLOOR", "hard": True}
    )
    result = AdaptiveRuntime.cycle(runtime, snapshot, account, {}, NOW, False)
    assert result["submitted"] == [] and submitted == []
    assert runtime.store.runtime()["mode"] == "LIVE"
    assert runtime.store.pending_reprices(runtime.store.strategy("ACTIVE")["version_id"]) == []
    assert runtime.store.reprice_chain_for_offer(10)["current_offer_id"] == 10
