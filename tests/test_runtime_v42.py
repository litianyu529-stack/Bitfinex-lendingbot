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


def offer(runtime, snapshot, amount=150, rate=".00021", kind="LIMIT"):
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
    offer(runtime, snap)
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
    offer(runtime, snap)
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
    row = offer(runtime, snap, amount=183)
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
    row = offer(runtime, snap, rate=".0002")
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
    row = offer(runtime, snap)
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
    offer(runtime, snap)
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
    offer(runtime, snap)
    account["wallet"] = D(0)
    integration.cycle(runtime, snap, account, {}, NOW, False)
    advance(runtime, snap, clock)
    integration.cycle(runtime, snap, account, {}, clock[0], False)
    snap["offers"] = []
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
    row = offer(runtime, snap, amount=183)
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
