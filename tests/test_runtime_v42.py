"""Mock exchange regressions for durable V4.2 decision/execution barriers."""

import json
import configparser
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

import AdaptiveRuntime as integration
import OperationalV41 as operational
import StrategyV4 as base
import StrategyV42 as core
from AdaptiveEngines import algorithm_engine, engine_module, template_for
from AdaptiveExecutionState import PassiveState, evidence, quote_key
from DomainTypes import WriteOutcome, WriteResult
from ResearchV4 import ModelRepository
from RuntimeV3 import LendingRuntimeV3
from StateStore import LendingStateStore
from StrategyV3 import StrategyPolicyV3, json_decimal

NOW = 1900000000000


@pytest.mark.parametrize("adoption", [False, True])
def test_v42_adoption_choice_roundtrips_api_and_config_without_inheriting_legacy(adoption):
    from Configuration import (
        strategy_v3_api_values,
        strategy_v3_from_api_payload,
        strategy_v3_config_values,
        strategy_v3_from_config,
        strategy_v3_from_record,
    )
    from StrategyV3 import policy_v3_with_overrides

    legacy = replace(core.template(StrategyPolicyV3()), strategy_engine="legacy_v3")
    intended = replace(core.template(legacy), adopt_external_offers=adoption)
    restored = strategy_v3_from_api_payload(strategy_v3_api_values(intended), base=legacy)
    assert restored.adopt_external_offers is adoption
    config = configparser.ConfigParser()
    config["STRATEGY_V3"] = strategy_v3_config_values(restored)
    assert strategy_v3_from_config(config, base=legacy).adopt_external_offers is adoption
    assert not strategy_v3_from_api_payload({"strategy_engine": core.ENGINE}, base=legacy).adopt_external_offers
    assert not policy_v3_with_overrides(legacy, {"strategy_engine": core.ENGINE}).adopt_external_offers
    # Existing old-engine fixed behavior and persisted V4.2 choices remain readable.
    assert strategy_v3_from_api_payload({"adopt_external_offers": False}, base=legacy).adopt_external_offers
    assert strategy_v3_from_record({"policy": json_decimal(restored.__dict__)}).adopt_external_offers is adoption


def fixture(tmp_path, monkeypatch, currency="USD"):
    clock = [NOW]
    trades = [dict(id=i, mts=NOW - i * base.DAY - 1000, period=2, rate=D(".0002"), amount=D(100000)) for i in range(30)]
    model = core.fit_model(
        currency, trades, now_ms=NOW, frr=[dict(mts=NOW - i * base.DAY, frr_daily_rate=".0002") for i in range(30)]
    )
    policy = replace(
        core.template(StrategyPolicyV3(currency=currency)),
        model_id=model["id"],
        enable_frr=False,
        enable_frr_delta_fixed=False,
        enable_frr_delta_variable=False,
    )
    store = LendingStateStore(tmp_path / (currency + ".sqlite3"), currency=currency, clock=lambda: clock[0] / 1000)
    ModelRepository(store.path, currency).save(model)
    store.save_strategy(json_decimal(policy.__dict__), "ACTIVE")
    store.set_mode("LIVE")
    monkeypatch.setattr(
        operational,
        "status",
        lambda *_: dict(operationalReady=True, operationalReportHash="test", operationalBlockReasons=[]),
    )
    client = operational._SimulatedExchange(WriteOutcome.CONFIRMED)
    cancels = []
    client.cancel_funding_offer_result = lambda oid: cancels.append(oid) or WriteResult(WriteOutcome.CONFIRMED)
    runtime = SimpleNamespace(
        policy=policy,
        store=store,
        currency=currency,
        client=client,
        clock=lambda: clock[0] / 1000,
        _log=lambda *_: None,
        _pending_cancel_requested=set(),
        _stats=[dict(mts=NOW, frr_daily_rate=".0002", source="FUNDING_TICKER")],
    )
    runtime._submit_plan = lambda *args: LendingRuntimeV3._submit_plan(runtime, *args)
    snapshot = dict(book=[dict(rate=D(".0002"), amount=D(-10000), period=2)], trades=trades, offers=[], bookMts=NOW)
    account = dict(total=D(1000), wallet=D(313), exposure=dict(short=D(0), medium=D(0), long=D(0)))
    return runtime, snapshot, account, model, clock, cancels


def offer(runtime, snapshot, amount=150, rate=".00021", kind="LIMIT", account=None):
    version = runtime.store.strategy("ACTIVE")["version_id"]
    row = dict(
        id=10,
        currency=runtime.currency,
        amount=D(amount),
        amount_original=D(amount),
        rate=D(rate),
        submitted_rate=D(rate),
        effective_rate=D(rate),
        rate_real=D(rate),
        period=2,
        offer_type=kind,
        display_type="LIMIT" if kind == "LIMIT" else "FRR",
        flags=0,
        managed=True,
        pool="short",
        layer="balanced",
        status="ACTIVE",
        mts_created=NOW - 38 * 3600000,
    )
    _, intent = runtime.store.reserve_intent(
        {**row, "slice_key": "old:short:balanced:0", "strategy_version": version}, D(1000)
    )
    runtime.store.confirm_intent(intent["id"], 10)
    runtime.store.reconcile_offers([row], NOW)
    snapshot["offers"] = [row]
    if account is not None:
        account["managedOffers"] = snapshot["offers"]
    return row


def advance(runtime, snapshot, clock, minutes=1):
    clock[0] += minutes * 60000
    snapshot["bookMts"] = clock[0]
    runtime._stats = [dict(mts=clock[0], frr_daily_rate=".0002", source="FUNDING_TICKER")]


@pytest.mark.parametrize("currency", ["USD", "USDT"])
def test_real_core_small_passive_plan_and_private_lease_survive_restart(tmp_path, monkeypatch, currency):
    runtime, snap, account, _, clock, _ = fixture(tmp_path, monkeypatch, currency)
    result = integration.cycle(runtime, snap, account, {}, NOW, False)
    assert len(result["submitted"]) == 1 and result["submitted"][0]["amount"] == 150
    assert result["idle_amount"] == 163 and runtime.client.calls == 1
    lease = next(iter(PassiveState(runtime.store).value["leases"].values()))
    assert lease["offerId"] == result["submitted"][0]["offerId"] and lease["expiresAtMs"] == NOW + 3600000
    snap["offers"] = [
        dict(
            offer_id=lease["offerId"],
            amount=D(150),
            rate=D(".0002"),
            period=2,
            offer_type="LIMIT",
            managed=True,
            mts_created=NOW,
        )
    ]
    account["wallet"] -= 150
    account["existingExposure"] = dict(total=D(150), variable=D(0))
    advance(runtime, snap, clock)
    integration.cycle(runtime, snap, account, {}, clock[0], False)
    assert runtime.client.calls == 1


def test_015_usdt_does_not_create_unexecutable_offer(tmp_path, monkeypatch):
    runtime, snap, account, _, _, _ = fixture(tmp_path, monkeypatch, "USDT")
    account["wallet"] = D(".15")
    result = integration.cycle(runtime, snap, account, {}, NOW, False)
    assert not result["submitted"] and runtime.client.calls == 0


def test_waiting_orders_reprice_after_two_stable_confirmations_not_immediately(tmp_path, monkeypatch):
    runtime, snap, account, _, clock, cancels = fixture(tmp_path, monkeypatch)
    offer(runtime, snap, account=account)
    account["wallet"] = D(0)
    first = integration.cycle(runtime, snap, account, {}, NOW, False)
    assert first["decisions"][0]["reason"] == "VALUE_GAIN" and cancels == []
    advance(runtime, snap, clock)
    second = integration.cycle(runtime, snap, account, {}, clock[0], False)
    assert cancels == [10] and second["canceledForReprice"] == [10]
    advance(runtime, snap, clock)
    third = integration.cycle(runtime, snap, account, {}, clock[0], False)
    assert third["blockReasons"] and not third["submitted"] and cancels == [10]


def test_dynamic_effective_frr_does_not_reset_executable_confirmation(tmp_path, monkeypatch):
    runtime, snap, account, _, clock, cancels = fixture(tmp_path, monkeypatch)
    offer(runtime, snap, account=account)
    account["wallet"] = D(0)
    targets = []

    def adjust(_policy, _model, _offer, candidates, *_args):
        targets.append(candidates)
        return dict(
            action="CANCEL",
            reason="VALUE_GAIN",
            hard=False,
            targetType="FRR_DELTA_FIXED",
            targetSubmittedRate=D("-.00001"),
            targetPeriod=2,
            targetRate=runtime._stats[-1]["frr_daily_rate"] + D("-.00001"),
        )

    runtime._stats[-1]["frr_daily_rate"] = D(".0002")
    monkeypatch.setattr(core, "adjustment", adjust)
    integration.cycle(runtime, snap, account, {}, NOW, False)
    advance(runtime, snap, clock)
    runtime._stats[-1]["frr_daily_rate"] = D(".000201")
    integration.cycle(runtime, snap, account, {}, clock[0], False)
    assert cancels == [10] and len(targets[-1]) == 1
    assert targets[-1][0]["submitted_rate"] == D("-.00001")


def test_partial_quantity_change_resets_confirmation_and_hourly_budget_blocks(tmp_path, monkeypatch):
    runtime, snap, account, _, clock, cancels = fixture(tmp_path, monkeypatch)
    row = offer(runtime, snap, amount=183, account=account)
    account["wallet"] = D(0)
    integration.cycle(runtime, snap, account, {}, NOW, False)
    row["amount"] = D(150)
    advance(runtime, snap, clock)
    integration.cycle(runtime, snap, account, {}, clock[0], False)
    assert not cancels
    monkeypatch.setattr(runtime.store, "reprice_count_since", lambda *_: 12)
    advance(runtime, snap, clock)
    integration.cycle(runtime, snap, account, {}, clock[0], False)
    assert not cancels


def test_frr_expiring_during_calculation_blocks_write_and_recovers_only_valid_model(tmp_path, monkeypatch):
    runtime, snap, account, _, clock, _ = fixture(tmp_path, monkeypatch)
    original = core.build_plan

    def slow(*args):
        result = original(*args)
        clock[0] += 61000
        return result

    monkeypatch.setattr(core, "build_plan", slow)
    result = integration.cycle(runtime, snap, account, {}, NOW, False)
    assert not result["submitted"] and runtime.client.calls == 0
    assert runtime.store.runtime()["safe_reason"] == "ADAPTIVE_FRR_STALE"
    assert not integration.recovery_qualified(runtime.store, runtime.policy, snap, clock[0], runtime._stats)
    advance(runtime, snap, clock)
    assert integration.recovery_qualified(runtime.store, runtime.policy, snap, clock[0], runtime._stats)
    monkeypatch.setattr(
        operational, "status", lambda *_: dict(operationalReady=False, operationalBlockReasons=["坏模型"])
    )
    assert not integration.recovery_qualified(runtime.store, runtime.policy, snap, clock[0], runtime._stats)


def test_journal_failure_never_calls_exchange(tmp_path, monkeypatch):
    runtime, snap, account, _, _, _ = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(ModelRepository, "journal", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    result = integration.cycle(runtime, snap, account, {}, NOW, False)
    assert not result["submitted"] and runtime.client.calls == 0
    assert runtime.store.runtime()["safe_reason"] == "ADAPTIVE_JOURNAL_FAILED"


def test_passive_expiry_cancel_barrier_returns_cash_without_unrelated_chain(tmp_path, monkeypatch):
    runtime, snap, account, _, clock, cancels = fixture(tmp_path, monkeypatch)
    row = offer(runtime, snap, rate=".0002", account=account)
    account["wallet"] = D(0)
    state = PassiveState(runtime.store)
    planrow = dict(
        amount=D(150),
        period=2,
        submitted_rate=D(".0002"),
        offer_type="LIMIT",
        pool="short",
        layer="balanced",
        slice_index=0,
        evidenceWatermark=["trade:0"],
    )
    lease = state.prepare(
        planrow, "lease", runtime.store.strategy("ACTIVE")["version_id"], NOW - 3600000, runtime.policy
    )
    lease.update(offerId=10, status="ACTIVE")
    state.persist()
    result = integration.cycle(runtime, snap, account, {}, NOW, False)
    assert cancels == [10] and result["decisions"][0]["reason"] == "LEASE_EXPIRED"
    snap["offers"] = []
    account["managedOffers"] = []
    runtime.store.reconcile_offers([], NOW + 60000)
    advance(runtime, snap, clock)
    integration.cycle(runtime, snap, account, {}, clock[0], False)
    assert not runtime.store.pending_reprices(runtime.store.strategy("ACTIVE")["version_id"])
    closed = PassiveState(runtime.store)
    assert not closed.value["leases"] and quote_key("USD", row) in closed.value["quarantines"]


def test_private_lease_checksum_missing_intent_and_evidence_watermark(tmp_path):
    store = LendingStateStore(tmp_path / "usd.sqlite3", currency="USD")
    state = PassiveState(store)
    policy = StrategyPolicyV3()
    row = dict(period=2, submitted_rate=".0002", amount=150, pool="short", layer="balanced", slice_index=0)
    lease = state.prepare(row, "a", "4", NOW, policy)
    assert state.prepare(row, "b", "4", NOW, policy) is lease
    assert state.renew(lease, NOW + 3600000, ["trade:1"], policy)
    assert not state.renew(lease, NOW + 3700000, ["trade:1"], policy)
    assert not state.renew(lease, NOW + 6 * 3600000, ["trade:2"], policy)
    state.reconcile([], NOW + 60000)
    assert not state.value["leases"]  # no intent was ever reserved; no exchange write happened
    assert evidence([dict(id=1, mts=NOW), dict(id=1, mts=NOW), dict(id=2, mts=NOW + 1)], [], NOW) == {"trade:1"}
    assert not evidence([dict(id=3, mts=NOW - 1)], [], NOW, NOW)
    state.path.write_text(json.dumps({"broken": True}), encoding="utf-8")
    with pytest.raises(OSError, match="损坏"):
        PassiveState(store)


def test_registry_models_and_old_engine_defaults_remain_isolated(tmp_path, monkeypatch):
    runtime, _, _, model, _, _ = fixture(tmp_path, monkeypatch)
    repo = ModelRepository(runtime.store.path, "USD")
    assert algorithm_engine(model["algorithm"]) == core.ENGINE and engine_module(core.ENGINE) is core
    assert template_for(core.ENGINE, StrategyPolicyV3()).strategy_engine == core.ENGINE
    assert repo.candidate(NOW, core.ENGINE)["id"] == model["id"]
    assert repo.candidate(NOW, "adaptive_net_yield_v2") is None
    assert repo.report_name(core.VERSION) == "evaluation-v3.json"
    with pytest.raises(ValueError):
        engine_module("bad")
    with pytest.raises(ValueError):
        algorithm_engine("bad")


def test_cancel_prepared_but_never_sent_is_restored_without_stalling(tmp_path, monkeypatch):
    runtime, snap, account, _, _, cancels = fixture(tmp_path, monkeypatch)
    row = offer(runtime, snap, account=account)
    version = runtime.store.strategy("ACTIVE")["version_id"]
    chain = runtime.store.ensure_reprice_chain(row, version, NOW)
    state = PassiveState(runtime.store)
    state.target(chain["chain_key"], dict(period=2, submitted_rate=".0002", offer_type="LIMIT"), NOW, NOW)
    runtime.store.mark_reprice_pending(chain["chain_key"], core.ENGINE, D(".0002"), now_ms=NOW, source_offer_id=10)
    result = integration.cycle(runtime, snap, account, {}, NOW, False)
    assert result["decisions"][0]["reason"] == "CANCEL_NOT_EFFECTIVE"
    assert not runtime.store.pending_reprices(version) and cancels == [] and runtime.client.calls == 0


def test_unknown_cancel_needs_repeated_account_reads_before_retry(tmp_path, monkeypatch):
    runtime, snap, account, _, clock, _ = fixture(tmp_path, monkeypatch)
    offer(runtime, snap, account=account)
    account["wallet"] = D(0)
    runtime.client.cancel_funding_offer_result = lambda *_: WriteResult(WriteOutcome.UNKNOWN, error="timeout")
    integration.cycle(runtime, snap, account, {}, NOW, False)
    advance(runtime, snap, clock)
    integration.cycle(runtime, snap, account, {}, clock[0], False)
    assert runtime.store.runtime()["safe_reason"] == "AMBIGUOUS_CANCEL:10"
    runtime.store.observe_ambiguous_cancel([10], clock[0])
    runtime.store.observe_ambiguous_cancel([10], clock[0] + 30000)
    runtime.store.record_consistent_sync(clock[0] + 60000)
    runtime.store.record_consistent_sync(clock[0] + 90000)
    assert runtime.store.consume_resume_barrier()
    advance(runtime, snap, clock)
    result = integration.cycle(runtime, snap, account, {}, clock[0], False)
    assert result["decisions"][0]["reason"] == "CANCEL_NOT_EFFECTIVE"
    assert not runtime.store.pending_reprices(runtime.store.strategy("ACTIVE")["version_id"])


def test_confirmed_replacement_receipt_binds_after_crash_without_resubmitting(tmp_path, monkeypatch):
    runtime, snap, account, _, clock, _ = fixture(tmp_path, monkeypatch)
    offer(runtime, snap, account=account)
    account["wallet"] = D(0)
    integration.cycle(runtime, snap, account, {}, NOW, False)
    advance(runtime, snap, clock)
    integration.cycle(runtime, snap, account, {}, clock[0], False)
    snap["offers"] = []
    account["managedOffers"] = []
    advance(runtime, snap, clock)
    runtime.store.reconcile_offers([], clock[0])
    account["wallet"] = D(150)
    bind = runtime.store.bind_reprice_replacement_chain
    monkeypatch.setattr(
        runtime.store, "bind_reprice_replacement_chain", lambda *_: (_ for _ in ()).throw(OSError("crash"))
    )
    result = integration.cycle(runtime, snap, account, {}, clock[0], False)
    assert len(result["submitted"]) == 1 and runtime.client.calls == 1
    monkeypatch.setattr(runtime.store, "bind_reprice_replacement_chain", bind)
    runtime.store.set_mode("LIVE")
    oid = result["submitted"][0]["offerId"]
    snap["offers"] = [
        dict(id=oid, period=2, amount=D(150), rate=D(".0002"), offer_type="LIMIT", managed=True, mts_created=clock[0])
    ]
    account["managedOffers"] = snap["offers"]
    account["wallet"] = D(0)
    advance(runtime, snap, clock)
    result = integration.cycle(runtime, snap, account, {}, clock[0], False)
    assert result["decisions"][-1]["reason"] == "RESTART_REPLACEMENT_BOUND"
    assert runtime.client.calls == 1 and not runtime.store.pending_reprices(
        runtime.store.strategy("ACTIVE")["version_id"]
    )


@pytest.mark.parametrize("source,age", [("REST_FALLBACK", "restAgeMs"), ("WEBSOCKET", "publicAgeMs")])
def test_production_snapshot_age_is_checked_after_valuation(tmp_path, monkeypatch, source, age):
    runtime, snap, _, _, clock, _ = fixture(tmp_path, monkeypatch)
    snap.pop("bookMts")
    snap.update(source=source, as_of=NOW, **{age: 59999})
    clock[0] += 10
    assert not integration._fresh_before_write(runtime, snap, NOW)
    assert runtime.store.runtime()["safe_reason"] == "MARKET_DATA_STALE"


def test_lease_absence_requires_two_views_and_promote_releases_passive_budget(tmp_path):
    store = LendingStateStore(tmp_path / "usd.sqlite3", currency="USD")
    state = PassiveState(store)
    row = dict(
        period=2,
        submitted_rate=".0002",
        offer_type="LIMIT",
        amount=D(150),
        pool="short",
        layer="balanced",
        slice_index=0,
    )
    lease = state.prepare(row, "a", "4", NOW, StrategyPolicyV3())
    lease.update(offerId=10, status="ACTIVE")
    state.persist()
    state.reconcile([], NOW + 1000)
    assert state.lease_for(10)
    state.reconcile([dict(id=10)], NOW + 31000)
    assert "absentFirstMs" not in state.lease_for(10)
    state.reconcile([], NOW + 40000)
    state.reconcile([], NOW + 71000)
    assert state.lease_for(10) is None
    assert state.value["quarantines"][quote_key("USD", row)]["chainStartMs"] == NOW
    assert state.continuation(row, NOW + 72000, []) == dict(chainStartMs=NOW)
    with pytest.raises(ValueError, match="6小时"):
        state.continuation(row, NOW + 360000 * 60, [])
    assert state.continuation(row, NOW + 360000 * 60, ["trade:20"]) is None
    fresh = state.prepare(row, "b", "4", NOW + 72000, StrategyPolicyV3())
    state.promote(fresh)
    assert not state.value["leases"]


def test_cancel_time_fill_reduces_chain_cash_even_with_other_wallet_funds(tmp_path, monkeypatch):
    runtime, snap, account, _, clock, _ = fixture(tmp_path, monkeypatch)
    row = offer(runtime, snap, amount=183, account=account)
    version = runtime.store.strategy("ACTIVE")["version_id"]
    chain = runtime.store.ensure_reprice_chain(row, version, NOW)
    state = PassiveState(runtime.store)
    target = dict(period=2, submitted_rate=".0002", offer_type="LIMIT", amount=183)
    state.target(chain["chain_key"], target, NOW, NOW)
    state.cancel_phase(chain["chain_key"], "CONFIRMED")
    runtime.store.mark_reprice_pending(chain["chain_key"], core.ENGINE, D(".0002"), now_ms=NOW, source_offer_id=10)
    with runtime.store.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO funding_trades(trade_id,currency,offer_id,amount,rate,period,mts,managed) "
            "VALUES(1,'USD',10,'50','.00021',2,?,1)",
            (NOW + 1,),
        )
    pending = runtime.store.pending_reprices(version)[0]
    assert (
        integration._remaining_chain_amount(runtime.store, pending, state.replacement(chain["chain_key"]), NOW + 2)
        == 133
    )
    runtime.store.reconcile_offers([], NOW + 60000)
    snap["offers"] = []
    account["managedOffers"] = []
    advance(runtime, snap, clock)
    account["wallet"] = D(313)  # includes cash unrelated to this source
    result = integration.cycle(runtime, snap, account, {}, clock[0], False)
    assert not result["submitted"] and runtime.client.calls == 0


@pytest.mark.parametrize("engine", ["legacy_v3", "adaptive_net_yield_v1", "adaptive_net_yield_v2"])
def test_new_read_defaults_never_rehash_frozen_old_active(engine):
    from Configuration import _normalization_payload, strategy_v3_from_record

    policy = StrategyPolicyV3(strategy_engine=engine)
    if engine != "legacy_v3":
        policy = template_for(engine, policy)
    payload = json_decimal(policy.__dict__)
    payload.pop("reprice_gain_apr")
    payload.pop("passive_wait_minutes")
    record = dict(policy=payload)
    before = base.digest(payload)
    assert base.digest(_normalization_payload(strategy_v3_from_record(record), record)) == before


def test_preflight_planning_uses_existing_private_passive_budget_and_stale_views_preserve_it(tmp_path, monkeypatch):
    runtime, snap, account, _, clock, _ = fixture(tmp_path, monkeypatch)
    result = integration.cycle(runtime, snap, account, {}, NOW, False)
    lease = next(iter(PassiveState(runtime.store).value["leases"].values()))
    ctx = integration.context(runtime.store, runtime.policy, snap["book"], snap["trades"], NOW, runtime._stats)
    planned = integration.plan(account, runtime.policy, {"adaptiveContext": ctx}, "preflight")
    assert not planned["plan"] and result["submitted"]
    snap.update(safeRequired=True, source="STALE")
    runtime.store.pause_currency()
    integration.cycle(runtime, snap, account, {}, NOW + 10000, False)
    integration.cycle(runtime, snap, account, {}, NOW + 50000, False)
    assert PassiveState(runtime.store).lease_for(lease["offerId"])
    path = PassiveState(runtime.store).path
    path.write_text('{"broken":true}', encoding="utf-8")
    ctx = integration.context(runtime.store, runtime.policy, snap["book"], snap["trades"], clock[0], runtime._stats)
    assert ctx["passiveStateError"]
    assert integration.plan(account, runtime.policy, {"adaptiveContext": ctx}, "preview")["blockReasons"]


def incomplete_plan(account, policy, *_args):
    return {
        **core._base_result(account, policy),
        "empty_reason": "VALUATION_INCOMPLETE",
        "blockReasons": ["VALUATION_INCOMPLETE: computation budget exhausted"],
    }


def test_incomplete_valuation_blocks_only_this_cycle_and_retries_at_normal_interval(tmp_path, monkeypatch):
    runtime, snap, account, _, clock, cancels = fixture(tmp_path, monkeypatch)
    calls = []

    def bounded(*args):
        calls.append(1)
        return incomplete_plan(*args)

    monkeypatch.setattr(core, "build_plan", bounded)
    first = integration.cycle(runtime, snap, account, {}, NOW, False)
    assert first["empty_reason"] == "VALUATION_INCOMPLETE" and first["blockReasons"]
    assert not first["submitted"] and not first["plan"] and cancels == [] and runtime.client.calls == 0
    assert runtime.store.runtime()["mode"] == "LIVE" and runtime.store.runtime()["safe_reason"] is None
    clock[0] += 1000
    second = integration.cycle(runtime, snap, account, {}, clock[0], False)
    assert second["evaluationSkipped"] and second["plan"] == [] and calls == [1]
    advance(runtime, snap, clock)
    integration.cycle(runtime, snap, account, {}, clock[0], False)
    assert calls == [1, 1] and runtime.store.runtime()["mode"] == "LIVE"


def test_evaluation_interval_is_checked_before_full_plan_and_never_reuses_old_quotes(tmp_path, monkeypatch):
    runtime, snap, account, _, clock, _ = fixture(tmp_path, monkeypatch)
    calls = []

    def empty(*args):
        calls.append(1)
        return {**core._base_result(args[0], args[1]), "empty_reason": "WAIT_FOR_VALUE"}

    monkeypatch.setattr(core, "build_plan", empty)
    integration.cycle(runtime, snap, account, {}, NOW, False)
    runtime._adaptive_last_decisions = [{"action": "SUBMIT", "reason": "old executable quote"}]
    clock[0] += 1000
    skipped = integration.cycle(runtime, snap, account, {}, clock[0], False)
    assert calls == [1] and skipped["plan"] == [] and skipped["submitted"] == []
    assert skipped["decisions"] == [{"action": "WAIT", "reason": "EVALUATION_INTERVAL"}]
    assert runtime.client.calls == 0


@pytest.mark.parametrize("paused,resume", [(True, False), (False, True)])
def test_paused_or_first_recovery_cycle_does_not_run_full_valuation(tmp_path, monkeypatch, paused, resume):
    runtime, snap, account, _, _, _ = fixture(tmp_path, monkeypatch)
    if paused:
        runtime.store.pause_currency()
    monkeypatch.setattr(core, "build_plan", lambda *_args: pytest.fail("no full valuation"))
    result = integration.cycle(runtime, snap, account, {}, NOW, resume)
    assert result["plan"] == [] and result["submitted"] == [] and result["recoveryResumeBarrier"] == resume
    assert result["empty_reason"] == ("PAUSED" if paused else "RECOVERY_RESUME_BARRIER")


@pytest.mark.parametrize("failure,reason", [
    ("model", "ADAPTIVE_MODEL_NOT_QUALIFIED"),
    ("frr", "ADAPTIVE_FRR_STALE"),
    ("market", "MARKET_DATA_STALE"),
])
def test_interval_cannot_hide_real_model_or_market_failure(tmp_path, monkeypatch, failure, reason):
    runtime, snap, account, _, _, _ = fixture(tmp_path, monkeypatch)
    runtime._adaptive_at = NOW
    if failure == "model":
        runtime.policy = replace(runtime.policy, model_id="e" * 64)
    elif failure == "frr":
        runtime._stats[0]["mts"] = NOW - 61000
    else:
        snap["bookMts"] = NOW - 61000
    monkeypatch.setattr(core, "build_plan", lambda *_args: pytest.fail("no full valuation"))
    result = integration.cycle(runtime, snap, account, {}, NOW, False)
    assert result["blockReasons"] and not result["submitted"]
    assert runtime.store.runtime()["safe_reason"] == reason


def test_non_budget_model_error_remains_a_protected_pause(tmp_path, monkeypatch):
    runtime, snap, account, _, _, _ = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(core, "build_plan", lambda *args: {
        **core._base_result(args[0], args[1]),
        "empty_reason": "MODEL_OR_DATA_UNAVAILABLE", "blockReasons": ["invalid future feature"],
    })
    integration.cycle(runtime, snap, account, {}, NOW, False)
    assert runtime.store.runtime()["safe_reason"] == "ADAPTIVE_DATA_UNAVAILABLE" and runtime.client.calls == 0


@pytest.mark.parametrize("expired", [False, True])
def test_budget_failure_keeps_independent_floor_and_lease_safety_exits(tmp_path, monkeypatch, expired):
    runtime, snap, account, _, _, cancels = fixture(tmp_path, monkeypatch)
    row = offer(runtime, snap, rate=".0002" if expired else ".000001", account=account)
    account["wallet"] = D(0)
    if expired:
        state = PassiveState(runtime.store)
        lease = state.prepare(
            {**row, "slice_index": 0}, "lease", runtime.store.strategy("ACTIVE")["version_id"],
            NOW - 3600000, runtime.policy,
        )
        lease.update(offerId=10, status="ACTIVE")
        state.persist()
    runtime._adaptive_at = NOW
    monkeypatch.setattr(core, "build_plan", incomplete_plan)
    result = integration.cycle(runtime, snap, account, {}, NOW, False)
    assert result["canceledForReprice"] == [10] and cancels == [10] and not result["submitted"]
    assert result["decisions"][0]["reason"] == ("LEASE_EXPIRED" if expired else "HARD_FLOOR")
    assert runtime.store.runtime()["mode"] == "LIVE" and runtime.store.runtime()["safe_reason"] is None


@pytest.mark.parametrize("phase", ["PREPARED", "SENDING"])
def test_interval_preserves_pending_cancel_checks_before_valuation(tmp_path, monkeypatch, phase):
    runtime, snap, account, _, _, cancels = fixture(tmp_path, monkeypatch)
    row = offer(runtime, snap, account=account)
    version = runtime.store.strategy("ACTIVE")["version_id"]
    chain = runtime.store.ensure_reprice_chain(row, version, NOW)
    state = PassiveState(runtime.store)
    state.target(chain["chain_key"], dict(period=2, submitted_rate=".0002", offer_type="LIMIT"), NOW, NOW)
    state.cancel_phase(chain["chain_key"], phase)
    runtime.store.mark_reprice_pending(chain["chain_key"], core.ENGINE, D(".0002"), now_ms=NOW, source_offer_id=10)
    runtime._adaptive_at = NOW
    monkeypatch.setattr(core, "build_plan", lambda *_args: pytest.fail("no full valuation"))
    result = integration.cycle(runtime, snap, account, {}, NOW, False)
    assert not result["submitted"] and cancels == [] and runtime.client.calls == 0
    if phase == "PREPARED":
        assert result["decisions"][0]["reason"] == "CANCEL_NOT_EFFECTIVE"
    else:
        assert runtime.store.runtime()["safe_reason"] == "AMBIGUOUS_CANCEL:10"


def test_one_currency_budget_failure_does_not_change_other_currency_execution(tmp_path, monkeypatch):
    usd, usd_snap, usd_account, _, _, _ = fixture(tmp_path, monkeypatch, "USD")
    usdt, usdt_snap, usdt_account, _, _, _ = fixture(tmp_path, monkeypatch, "USDT")
    original = core.build_plan
    monkeypatch.setattr(
        core, "build_plan",
        lambda *args: incomplete_plan(*args) if args[1].currency == "USDT" else original(*args),
    )
    failed = integration.cycle(usdt, usdt_snap, usdt_account, {}, NOW, False)
    success = integration.cycle(usd, usd_snap, usd_account, {}, NOW, False)
    assert failed["empty_reason"] == "VALUATION_INCOMPLETE" and not failed["submitted"]
    assert len(success["submitted"]) == 1 and usd.client.calls == 1
    assert usd.store.runtime()["mode"] == usdt.store.runtime()["mode"] == "LIVE"


@pytest.mark.parametrize("level", ["plan", "adjustment"])
def test_incomplete_evaluation_breaks_consecutive_reprice_confirmations(tmp_path, monkeypatch, level):
    runtime, snap, account, _, clock, cancels = fixture(tmp_path, monkeypatch)
    offer(runtime, snap, account=account)
    account["wallet"] = D(0)
    phase = ["complete"]

    def planned(*args):
        if phase[0] == "incomplete" and level == "plan":
            return incomplete_plan(*args)
        return {**core._base_result(args[0], args[1]), "empty_reason": "WAIT_FOR_VALUE"}

    def adjusted(*_args):
        if phase[0] == "incomplete" and level == "adjustment":
            return {"action": "KEEP", "reason": "VALUATION_INCOMPLETE", "blockReasons": ["budget exhausted"]}
        return {
            "action": "CANCEL", "reason": "VALUE_GAIN", "hard": False,
            "targetType": "LIMIT", "targetPeriod": 2, "targetRate": D(".0002"),
        }

    monkeypatch.setattr(core, "build_plan", planned)
    monkeypatch.setattr(core, "adjustment", adjusted)
    integration.cycle(runtime, snap, account, {}, NOW, False)
    assert runtime._adaptive_confirmations[10]["count"] == 1 and not cancels
    phase[0] = "incomplete"
    advance(runtime, snap, clock)
    integration.cycle(runtime, snap, account, {}, clock[0], False)
    assert runtime._adaptive_confirmations == {} and not cancels
    phase[0] = "complete"
    advance(runtime, snap, clock)
    integration.cycle(runtime, snap, account, {}, clock[0], False)
    assert runtime._adaptive_confirmations[10]["count"] == 1 and not cancels
    advance(runtime, snap, clock)
    integration.cycle(runtime, snap, account, {}, clock[0], False)
    assert cancels == [10]


@pytest.mark.parametrize("expired", [False, True])
def test_adjustment_budget_failure_does_not_skip_later_safety_exit(tmp_path, monkeypatch, expired):
    runtime, snap, account, _, _, cancels = fixture(tmp_path, monkeypatch)
    first = offer(runtime, snap, account=account)
    second = {**first, "id": 11, "rate": D(".0002" if expired else ".000001")}
    second.update(submitted_rate=second["rate"], effective_rate=second["rate"], rate_real=second["rate"])
    version = runtime.store.strategy("ACTIVE")["version_id"]
    _, intent = runtime.store.reserve_intent(
        {**second, "strategy_version": version, "slice_key": "second:short:balanced:0"}, D(1000)
    )
    runtime.store.confirm_intent(intent["id"], 11)
    snap["offers"] = [first, second]
    runtime.store.reconcile_offers(snap["offers"], NOW)
    account.update(wallet=D(0), managedOffers=snap["offers"])
    if expired:
        state = PassiveState(runtime.store)
        lease = state.prepare({**second, "slice_index": 1}, "lease", version, NOW - 3600000, runtime.policy)
        lease.update(offerId=11, status="ACTIVE")
        state.persist()
    monkeypatch.setattr(core, "build_plan", lambda *args: core._base_result(args[0], args[1]))
    checks = []

    def adjusted(_policy, _model, row, *_args):
        checks.append(row["id"])
        return {"action": "KEEP", "reason": "VALUATION_INCOMPLETE", "blockReasons": ["budget exhausted"]}

    monkeypatch.setattr(core, "adjustment", adjusted)
    runtime._adaptive_confirmations = {999: {"count": 1}}
    result = integration.cycle(runtime, snap, account, {}, NOW, False)
    assert checks == [10] and cancels == [11] and result["canceledForReprice"] == [11]
    assert not result["submitted"] and runtime._adaptive_confirmations == {}
    assert result["decisions"][-1]["reason"] == ("LEASE_EXPIRED" if expired else "HARD_FLOOR")
    assert runtime.store.runtime()["mode"] == "LIVE" and runtime.store.runtime()["safe_reason"] is None


def test_non_budget_adjustment_failure_keeps_protection(tmp_path, monkeypatch):
    runtime, snap, account, _, _, cancels = fixture(tmp_path, monkeypatch)
    offer(runtime, snap, account=account)
    monkeypatch.setattr(core, "build_plan", lambda *args: core._base_result(args[0], args[1]))
    monkeypatch.setattr(core, "adjustment", lambda *_args: {
        "action": "KEEP", "reason": "MODEL_OR_DATA_UNAVAILABLE", "blockReasons": ["invalid future model"],
    })
    result = integration.cycle(runtime, snap, account, {}, NOW, False)
    assert not result["submitted"] and not cancels
    assert runtime.store.runtime()["safe_reason"] == "ADAPTIVE_DATA_UNAVAILABLE"
