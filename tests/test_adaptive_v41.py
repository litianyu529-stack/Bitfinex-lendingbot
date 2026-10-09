import json
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

import AdaptiveRuntime
import ReplayV4
import StrategyV4 as base
import StrategyV41 as multi
from Configuration import strategy_v3_api_values, strategy_v3_from_api_payload
from ResearchV4 import ModelRepository, ResearchJobs, build_from_store
from RuntimeV3 import LendingRuntimeV3
from StateStore import LendingStateStore
from StrategyV3 import StrategyPolicyV3, json_decimal, validate_policy_v3
from test_v4 import FundingClient

NOW = 1_900_000_000_000


def setup(currency="USD", rate=".0008", frr=".0006"):
    trades = [dict(id=1, mts=NOW - 1000, period=2, rate=D(rate), amount=D(100000))]
    history = [dict(mts=NOW - day * base.DAY, frr_daily_rate=frr) for day in range(30)]
    model = multi.fit_model(currency, trades, now_ms=NOW, frr=history)
    policy = replace(
        multi.template(StrategyPolicyV3(currency=currency)), model_id=model["id"], variable_max_share=D(100)
    )
    book = [dict(period=2, rate=D(rate), amount=D(-10000))]
    account = dict(total=D(1000), wallet=D(1000), exposure=dict(short=D(0), medium=D(0), long=D(0)))
    return policy, model, trades, book, account


@pytest.mark.parametrize("currency", ["USD", "USDT"])
def test_template_roundtrip_types_models_and_visibility(currency):
    p, model, *_ = setup(currency)
    assert validate_policy_v3(p) == p
    assert strategy_v3_from_api_payload(strategy_v3_api_values(p), p) == p
    assert base.validate_model(model, currency, NOW) == model
    assert not p.enable_hidden and all(getattr(p, field) for field in multi.FIELDS.values())
    assert multi.display_type(dict(offer_type="FRRDELTAVAR", rate=0)) == "FRR"
    assert multi.display_type(dict(offer_type="FRRDELTAVAR", rate=".0002")) == "FRR_DELTA_VARIABLE"
    assert multi.display_type(dict(offer_type="FRRDELTAFIX", rate="-.0002")) == "FRR_DELTA_FIXED"
    with pytest.raises(ValueError):
        validate_policy_v3(replace(p, enable_hidden=True))
    with pytest.raises(ValueError):
        validate_policy_v3(replace(p, **{field: False for field in multi.FIELDS.values()}))


def test_offset_signs_zero_frr_and_floor_filtering():
    p, *_ = setup()
    quotes = multi.quotes(p, 2, D(".0008"), D(".0006"))
    assert {r["display_type"] for r in quotes} == set(multi.TYPES)
    assert next(r for r in quotes if r["display_type"] == "FRR")["submitted_rate"] == 0
    assert next(r for r in quotes if r["display_type"] == "FRR_DELTA_FIXED")["submitted_rate"] == D(".0002")
    negative = multi.quotes(p, 2, D(".0008"), D(".001"))
    assert next(r for r in negative if r["display_type"] == "FRR_DELTA_FIXED")["submitted_rate"] == D("-.0002")
    assert not any(r["display_type"] == "FRR_DELTA_VARIABLE" for r in negative)
    assert all(r["rate"] >= base.gross_floor(p, 2) for r in quotes)
    assert [r["display_type"] for r in multi.quotes(p, 2, D(".0008"), None)] == ["LIMIT"]
    assert multi.quotes(p, 120, D(".00001"), D(".00001")) == []
    assert not multi.enabled(p, "UNKNOWN")


@pytest.mark.parametrize("kind", multi.TYPES)
def test_each_type_computes_cashflows_and_executable_allocation(kind):
    p, model, trades, book, account = setup()
    p = replace(p, **{field: name == kind for name, field in multi.FIELDS.items()})
    quote = next(r for r in multi.quotes(p, 2, D(".0008"), D(".0006")) if r["display_type"] == kind)
    value = multi.value_candidate(p, model, 2, quote["rate"], D(200), trades, book, NOW, quote=quote)
    assert len(value["pathInterests"]) == 64 and value["expectedNetInterest"] > 0
    assert value["floatingRate"] == (quote["offer_type"] == "FRRDELTAVAR")
    assert value["confidence"] == "LOW"
    plan = multi.build_plan(account, p, model, book, trades, NOW, "test", D(".0006"))
    assert plan["plan"] and all(r["display_type"] == kind for r in plan["plan"])
    assert sum(r["amount"] for r in plan["plan"]) <= 1000
    assert all(r["amount"] >= 150 and r["flags"] == 0 for r in plan["plan"])


def test_no_duplicate_depth_and_variable_cap_and_missing_frr():
    p, model, trades, book, account = setup()
    trades = [{**trades[0], "mts": NOW - 7200000}]
    book[0]["amount"] = D(-300)
    plan = multi.build_plan(account, p, model, book, trades, NOW, "test", D(".0006"))
    assert plan["planned_amount"] <= 300
    p = replace(p, enable_limit=False, enable_frr_delta_fixed=False, variable_max_share=D(20))
    plan = multi.build_plan(account, p, model, book, trades, NOW, "test", D(".0006"))
    assert plan["variable_amount"] <= 200
    missing = multi.build_plan(account, p, model, book, trades, NOW, "test")
    assert missing["empty_reason"] == "FRR_DATA_UNAVAILABLE" and not missing["plan"]
    empty_model = multi.fit_model("USD", trades, now_ms=NOW)
    assert not multi.build_plan(account, p, empty_model, book, trades, NOW, "test", D(".0006"))["plan"]
    assert not multi.build_plan(account, p, None, book, trades, NOW, "test")["plan"]


def test_frr_floor_stress_future_data_and_type_samples():
    p, model, trades, book, _ = setup()
    history = [dict(mts=NOW - day * base.DAY, frr_daily_rate=".00001" if day else ".0006") for day in range(30)]
    history.append(dict(mts=NOW + 1, frr_daily_rate=".9"))
    obs = [
        dict(chainId=str(i), period=2, start_ms=NOW - 600000, end_ms=NOW, amount=200, displayType="FRR", fills=[])
        for i in range(20)
    ]
    model = multi.fit_model("USD", trades, obs, now_ms=NOW, frr=history)
    assert len(model["frrDays"]) == 30 and model["typeObservationCounts"]["FRR"] == 20
    assert model["typeObservationCounts"]["LIMIT"] == 0
    q = next(r for r in multi.quotes(p, 2, D(".0008"), D(".0006")) if r["display_type"] == "FRR")
    value = multi.value_candidate(p, model, 2, q["rate"], D(200), trades, book, NOW, quote=q)
    assert value["expectedFillProbability"] == 0 and value["confidence"] == "CALIBRATED"
    bad = {**model, "frrDays": [dict(mts=NOW + 1, rate=".001")]}
    bad["id"] = base.digest({k: v for k, v in bad.items() if k != "id"})
    with pytest.raises(ValueError, match="FRR历史"):
        base.validate_model(bad, "USD", NOW)


def test_models_candidates_reports_and_stale_runtime_context_are_separate(tmp_path):
    p, model, trades, book, account = setup()
    store = LendingStateStore(tmp_path / "usd.sqlite3", currency="USD")
    store.save_strategy(json_decimal(p.__dict__), "ACTIVE")
    repo = ModelRepository(store.path, "USD")
    repo.save(model)
    old = base.fit_model("USD", trades, now_ms=NOW)
    repo.save(old)
    assert repo.candidate(NOW, multi.ENGINE)["id"] == model["id"]
    assert repo.candidate(NOW, base.ENGINE)["id"] == old["id"]
    assert repo.report_name(model["algorithm"]) != repo.report_name(old["algorithm"])
    ctx = AdaptiveRuntime.context(store, p, book, trades, NOW, [dict(mts=NOW - 1, frr_daily_rate=".0006")])
    assert ctx["frr"] == D(".0006")
    assert AdaptiveRuntime.plan(account, p, {"adaptiveContext": ctx}, "test")["plan"]
    ctx = AdaptiveRuntime.context(store, p, book, trades, NOW, [dict(mts=NOW - 61000, frr_daily_rate=".0006")])
    assert ctx["frr"] is None and not AdaptiveRuntime.plan(account, p, {"adaptiveContext": ctx}, "test")["plan"]
    future = AdaptiveRuntime.context(store, p, book, trades, NOW, [dict(mts=NOW + 1, frr_daily_rate=".1")])
    assert future["frr"] is None


def test_adjustments_preserve_queue_external_and_hard_rules(monkeypatch):
    p, model, trades, book, _ = setup()
    offer = dict(amount=200, rate=".0002", offer_type="FRRDELTAFIX", managed=True, mts_created=NOW - 600000, period=2)
    assert multi.adjustment(p, model, {**offer, "managed": False}, [], trades, book, NOW)["reason"] == "EXTERNAL_OFFER"
    assert multi.adjustment(p, model, offer, [], trades, book, NOW)["reason"] == "FRR_DATA_UNAVAILABLE"
    assert (
        multi.adjustment(p, model, {**offer, "mts_created": NOW}, [], trades, book, NOW, D(".0006"))["action"] == "KEEP"
    )
    assert multi.adjustment(p, model, {**offer, "amount": 100}, [], trades, book, NOW, D(".0006"))["action"] == "KEEP"
    assert multi.adjustment(p, model, offer, [], trades, book, NOW, D(".0006"))["action"] == "KEEP"
    hard = multi.adjustment(p, model, {**offer, "period": 121}, [], trades, book, NOW, D(".0006"))
    assert hard["action"] == "CANCEL" and hard["hard"]
    assert multi.adjustment(p, model, {**offer, "flags": 64, "amount": 100}, [], trades, book, NOW, D(".0006"))["hard"]

    def value(*args, **kwargs):
        quote = kwargs["quote"]
        gain = 2 if quote["display_type"] == "LIMIT" else 1
        return {
            **quote,
            "period": args[2],
            "conservativeNetApr": gain,
            "pathInterests": [gain] * 64,
            "expectedFillProbability": 1,
        }

    monkeypatch.setattr(multi, "value_candidate", value)
    candidate = dict(period=2, rate=D(".0009"), submitted_rate=D(".0009"), offer_type="LIMIT", display_type="LIMIT")
    decision = multi.adjustment(p, model, offer, [candidate], trades, book, NOW, D(".0006"))
    assert decision["targetType"] == "LIMIT" and decision["p10InterestGain"] > 0
    same = {**candidate, "display_type": "FRR_DELTA_FIXED", "offer_type": "FRRDELTAFIX", "submitted_rate": D(".0002")}
    assert multi.adjustment(p, model, offer, [same], trades, book, NOW, D(".0006"))["action"] == "KEEP"


@pytest.mark.parametrize("kind", ["FRR", "FRR_DELTA_FIXED", "FRR_DELTA_VARIABLE"])
def test_actual_submission_sends_offsets_to_fake_exchange(tmp_path, kind):
    p, model, trades, book, account = setup()
    p = replace(p, **{field: name == kind for name, field in multi.FIELDS.items()})
    store = LendingStateStore(tmp_path / "usd.sqlite3", currency="USD")
    version = store.save_strategy(json_decimal(p.__dict__), "ACTIVE")
    client = FundingClient()
    runtime = LendingRuntimeV3(client, p, store, clock=lambda: NOW / 1000)
    result = multi.build_plan(account, p, model, book, trades, NOW, version, D(".0006"))
    sent = runtime._submit_plan(result, D(1000), version)
    assert sent and client.submissions
    intent = store.intents()[0]
    assert D(intent["submitted_rate"]) == (D(0) if kind == "FRR" else D(".0002"))
    assert D(intent["effective_rate"]) >= D(".0006")


@pytest.mark.parametrize("kind", ["FRR_DELTA_FIXED", "FRR_DELTA_VARIABLE"])
def test_replay_locks_fixed_after_fill_but_updates_variable(monkeypatch, kind):
    p, model, _, _, _ = setup()
    p = replace(p, normal_fee_rate=D(".15"))
    quote = dict(
        period=2,
        amount=D(1000),
        effective_rate=D(".0008"),
        submitted_rate=D(".0002"),
        offer_type="FRRDELTAFIX" if kind == "FRR_DELTA_FIXED" else "FRRDELTAVAR",
        display_type=kind,
    )
    monkeypatch.setattr(multi, "build_plan", lambda *a, **kw: dict(plan=[quote], candidates=[]))
    monkeypatch.setattr(multi, "adjustment", lambda *a, **kw: dict(action="KEEP", reason="TEST"))
    books = [
        dict(mts=NOW, book=[dict(rate=".001", period=2, amount="-1000")], frr=".0006"),
        dict(mts=NOW + base.DAY // 2, book=[dict(rate=".001", period=2, amount="-1000")], frr=".001"),
    ]
    trades = [dict(mts=NOW + 1, rate=".001", period=2, amount=1000)]
    out = ReplayV4.replay(p, model, trades, books, 1000, NOW, NOW + base.DAY, interval_ms=base.DAY)
    expected = D(".68") if kind == "FRR_DELTA_FIXED" else D(".85")
    assert abs(out["netInterest"] - expected) < D(".000001")


def test_readonly_research_and_insufficient_data_do_not_qualify(tmp_path):
    store = LendingStateStore(tmp_path / "usd.sqlite3", currency="USD")
    store.save_strategy(json_decimal(multi.template(StrategyPolicyV3()).__dict__), "ACTIVE")
    model = build_from_store(store, NOW, engine=multi.ENGINE)
    assert model["algorithm"] == multi.VERSION and model["frrDays"] == []
    report, model = ReplayV4.evaluate(store, NOW, engine=multi.ENGINE)
    assert not report["eligibleForLiveCandidate"] and report["state"] == "INSUFFICIENT_DATA"
    with pytest.raises(ValueError):
        ResearchJobs(lambda _: store).start("USD", engine="wrong")
    repo = ModelRepository(store.path, "USD")
    repo.save(model)
    assert not repo.candidate(NOW, multi.ENGINE)


def test_research_job_persists_and_resumes_selected_engine(tmp_path):
    import time

    store = LendingStateStore(tmp_path / "usd.sqlite3", currency="USD")
    store.save_strategy(json_decimal(StrategyPolicyV3().__dict__), "ACTIVE")
    jobs = ResearchJobs(lambda _: store, clock=lambda: NOW / 1000)
    jobs.start("USD", engine=multi.ENGINE)
    for _ in range(100):
        if jobs.status("USD")["state"] != "RUNNING":
            break
        time.sleep(0.01)
    assert jobs.status("USD")["state"] == "COMPLETED"
    assert jobs.status("USD")["engine"] == multi.ENGINE
    assert jobs.status("USD")["report"]["algorithm"] == multi.VERSION
    assert (ModelRepository(store.path, "USD").directory / "evaluation-v2.json").exists()
    fresh = ResearchJobs(lambda _: store, clock=lambda: (NOW + 60000) / 1000)
    fresh.start("USD", resume=True)
    for _ in range(100):
        if fresh.status("USD")["state"] != "RUNNING":
            break
        time.sleep(0.01)
    assert fresh.status("USD")["engine"] == multi.ENGINE
    assert fresh.status("USD")["startedAtMs"] == NOW


def test_missing_frr_blocks_runtime_writes_and_report_binding(tmp_path, monkeypatch):
    p, model, trades, book, account = setup()
    store = LendingStateStore(tmp_path / "usd.sqlite3", currency="USD")
    store.save_strategy(json_decimal(p.__dict__), "ACTIVE")
    store.set_mode("LIVE")
    repo = ModelRepository(store.path, "USD")
    repo.save(model)
    monkeypatch.setattr(AdaptiveRuntime, "eligible", lambda *_: True)
    runtime = SimpleNamespace(policy=p, store=store, _stats=[], client=object())
    result = AdaptiveRuntime.cycle(runtime, dict(book=book, trades=trades, offers=[]), account, {}, NOW, False)
    assert result["blockReasons"] and store.runtime()["mode"] != "LIVE"
    assert not result["submitted"]
    training_hash = base.digest({k: v for k, v in model.items() if k != "id"})
    report = dict(eligibleForLiveCandidate=True, testedModelTrainingHash=training_hash)
    model.update(eligibleForLiveCandidate=True, validationReportHash=base.digest(report))
    model["id"] = base.digest({k: v for k, v in model.items() if k != "id"})
    (repo.directory / "evaluation-v2.json").write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.undo()
    assert AdaptiveRuntime.eligible(store, model)


def test_preflight_offer_checks_use_effective_frr_and_unknown_is_not_zero():
    import lendingbot

    p, *_ = setup()
    offer = dict(offer_type="FRRDELTAVAR", display_type="FRR", rate=0, period=2, flags=0)
    assert lendingbot.v3_offer_violations(offer, p, D(".0006")) == []
    assert lendingbot.v3_offer_violations(offer, p) == ["rate_unavailable"]
    assert "below_new_floor" in lendingbot.v3_offer_violations(offer, p, D(".00001"))
