"""Every passive intent has its own durable clock, even at identical quotes."""

import copy
import json
from decimal import Decimal as D

import pytest

import AdaptiveRuntime as integration
import StrategyV42 as core
from AdaptiveExecutionState import MAX_PASSIVE_MS, PassiveState, quote_key
from StateStore import LendingStateStore
from StrategyV3 import StrategyPolicyV3
from StrategyV4 import digest
from test_runtime_v42 import advance, fixture

NOW = 1900000000000


def row(index=0, rate=".0002", amount=150):
    return dict(
        period=2,
        amount=D(amount),
        submitted_rate=D(rate),
        effective_rate=D(rate),
        offer_type="LIMIT",
        display_type="LIMIT",
        flags=0,
        pool="short",
        layer="balanced",
        slice_index=index,
        passive=True,
        leaseMinutes=60,
        evidenceWatermark=[f"trade:{index}"],
    )


def state_store(tmp_path, currency="USD"):
    store = LendingStateStore(tmp_path / (currency + ".sqlite3"), currency=currency, clock=lambda: NOW / 1000)
    return store, PassiveState(store)


def prepare_and_bind(state, value, plan, offer_id, at=NOW, continuation=None):
    lease = state.prepare(value, plan, "4", at, StrategyPolicyV3(), continuation)
    _, intent = state.store.reserve_intent(
        {**value, "currency": state.store.currency, "slice_key": lease["expectedSliceKey"], "strategy_version": "4"},
        D(10000),
    )
    state.store.confirm_intent(intent["id"], offer_id)
    state.bind({**value, "offerId": offer_id, "intentId": intent["id"]})
    return lease


@pytest.mark.parametrize("second_rate", [".0002", ".00021"])
def test_same_and_different_quotes_have_distinct_clocks_after_restart(tmp_path, second_rate):
    store, state = state_store(tmp_path)
    first = prepare_and_bind(state, row(), "first", 41)
    second = prepare_and_bind(state, row(1, second_rate, 200), "second", 42, NOW + 600000)
    assert first["leaseId"] != second["leaseId"] and len(state.value["leases"]) == 2
    before = copy.deepcopy(state.value)
    restarted = PassiveState(store)
    assert restarted.value == before
    assert restarted.lease_for(41)["expiresAtMs"] == NOW + 3600000
    assert restarted.lease_for(42)["expiresAtMs"] == NOW + 4200000
    # Account amounts can fall after a partial fill without resetting either clock.
    restarted.reconcile([dict(id=41, amount=75), dict(id=42, amount=200)], NOW + 1800000)
    assert restarted.value == before
    assert restarted.renew(restarted.lease_for(41), NOW + 3600000, ["trade:new"], StrategyPolicyV3())
    assert restarted.lease_for(42)["expiresAtMs"] == NOW + 4200000
    restarted.promote(restarted.lease_for(42))
    assert restarted.lease_for(42) is None and restarted.lease_for(41)["offerId"] == 41


def test_prepare_is_idempotent_only_for_exact_intent_and_rejects_mutation(tmp_path):
    _, state = state_store(tmp_path)
    original = row()
    lease = state.prepare(original, "one", "4", NOW, StrategyPolicyV3())
    assert state.prepare(original, "one", "4", NOW + 300000, StrategyPolicyV3()) is lease
    assert lease["startedAtMs"] == NOW and lease["expiresAtMs"] == NOW + 3600000
    assert state.prepare(original, "two", "4", NOW + 300000, StrategyPolicyV3()) is not lease
    for changed in (row(amount=151), row(rate=".0003")):
        with pytest.raises(OSError, match="禁止覆盖"):
            state.prepare(changed, "one", "4", NOW + 300000, StrategyPolicyV3())


def test_closed_intent_suffix_has_own_identity_and_unknowns_are_never_rebound(tmp_path):
    store, state = state_store(tmp_path)
    value = row()
    first = state.prepare(value, "same", "4", NOW, StrategyPolicyV3())
    _, rejected = store.reserve_intent(
        {**value, "currency": "USD", "strategy_version": "4", "slice_key": first["expectedSliceKey"]}, D(1000)
    )
    store.reject_intent(rejected["id"], "mock rejection")
    state.reconcile([], NOW + 1000)
    retry = state.prepare(value, "same", "4", NOW + 2000, StrategyPolicyV3())
    assert retry["expectedSliceKey"].endswith(":r1") and retry["leaseId"] != first["leaseId"]
    _, ambiguous = store.reserve_intent(
        {**value, "currency": "USD", "strategy_version": "4", "slice_key": retry["expectedSliceKey"]}, D(1000)
    )
    store.mark_submitting(ambiguous["id"])
    store.mark_ambiguous(ambiguous["id"], "mock timeout")
    another = state.prepare(row(1), "other", "4", NOW + 3000, StrategyPolicyV3())
    _, pending = store.reserve_intent(
        {**row(1), "currency": "USD", "strategy_version": "4", "slice_key": another["expectedSliceKey"]}, D(1000)
    )
    restarted = PassiveState(store)
    restarted.reconcile([], NOW + 5000)
    assert len(restarted.value["leases"]) == 2
    assert restarted.value["leases"][retry["leaseId"]]["intentId"] == ambiguous["id"]
    assert restarted.value["leases"][another["leaseId"]]["intentId"] == pending["id"]
    store.confirm_intent(ambiguous["id"], 51)
    store.confirm_intent(pending["id"], 52)
    restarted.reconcile([dict(id=51), dict(id=52)], NOW + 6000)
    assert restarted.lease_for(51)["startedAtMs"] == NOW + 2000
    assert restarted.lease_for(52)["startedAtMs"] == NOW + 3000


def test_bind_cannot_overwrite_other_offer_or_guess_a_quote_match(tmp_path):
    store, state = state_store(tmp_path)
    value = row()
    lease = prepare_and_bind(state, value, "one", 41)
    state.bind({**value, "offerId": 41, "intentId": lease["intentId"]})
    with pytest.raises(OSError, match="禁止覆盖"):
        state.bind({**value, "offerId": 42, "intentId": lease["intentId"]})
    _, foreign = store.reserve_intent(
        {**value, "currency": "USD", "strategy_version": "4", "slice_key": "no-lease"}, D(1000)
    )
    store.confirm_intent(foreign["id"], 42)
    with pytest.raises(OSError, match="精确意图"):
        state.bind({**value, "offerId": 42, "intentId": foreign["id"]})
    state.bind({**value, "passive": False, "offerId": 42, "intentId": foreign["id"]})
    assert state.lease_for(41) is lease and state.lease_for(42) is None


def test_same_quote_closures_merge_evidence_and_oldest_waiting_start(tmp_path):
    store, state = state_store(tmp_path)
    first = prepare_and_bind(state, row(), "one", 41, continuation={"chainStartMs": NOW - 1800000})
    second = prepare_and_bind(state, row(1), "two", 42, NOW + 600000)
    state.closing(first)
    state.reconcile([dict(id=42)], NOW + 700000)
    assert state.lease_for(42) is second
    state.closing(second)
    state.reconcile([], NOW + 800000)
    restarted = PassiveState(store)
    assert not restarted.value["leases"]
    quarantine = restarted.value["quarantines"][quote_key("USD", row())]
    assert quarantine == dict(
        closedAtMs=NOW + 800000,
        chainStartMs=NOW - 1800000,
        evidenceWatermark=["trade:0", "trade:1"],
    )
    assert restarted.continuation(row(), NOW + 900000, []) == {"chainStartMs": NOW - 1800000}
    with pytest.raises(ValueError, match="6小时"):
        restarted.continuation(row(), NOW + MAX_PASSIVE_MS, ["trade:0", "trade:1"])
    assert restarted.continuation(row(), NOW + MAX_PASSIVE_MS, ["trade:fresh"]) is None


def test_younger_different_quote_closing_last_cannot_reset_old_cash_waiting(tmp_path):
    store, state = state_store(tmp_path)
    old = prepare_and_bind(state, row(), "old", 41)
    young = prepare_and_bind(state, row(1, ".0003"), "young", 42, NOW + 18000000)
    state.closing(old)
    state.reconcile([dict(id=42)], NOW + MAX_PASSIVE_MS)
    state.closing(young)
    state.reconcile([], NOW + MAX_PASSIVE_MS + 1000)
    restarted = PassiveState(store)
    assert len(restarted.value["quarantines"]) == 2
    with pytest.raises(ValueError, match="6小时"):
        restarted.continuation(row(2, ".0004"), NOW + MAX_PASSIVE_MS + 2000, ["trade:0", "trade:1"])
    assert restarted.continuation(row(2, ".0004"), NOW + MAX_PASSIVE_MS + 2000, ["trade:after-close"]) is None


@pytest.mark.parametrize("confirmed", [False, True])
def test_legacy_state_migration_preserves_intent_deadlines_and_quarantine(tmp_path, confirmed):
    store, state = state_store(tmp_path)
    value = row()
    lease = state.prepare(value, "legacy", "4", NOW, StrategyPolicyV3(), {"chainStartMs": NOW - 600000})
    _, intent = store.reserve_intent(
        {**value, "currency": "USD", "strategy_version": "4", "slice_key": lease["expectedSliceKey"]}, D(1000)
    )
    if confirmed:
        store.confirm_intent(intent["id"], 61)
        state.bind({**value, "offerId": 61, "intentId": intent["id"]})
    legacy = copy.deepcopy(state.value)
    legacy["schemaVersion"] = 1
    saved = legacy["leases"].pop(lease["leaseId"])
    saved.pop("leaseId")
    saved.pop("expectedSliceKey")
    legacy["leases"][saved["quoteKey"]] = saved
    legacy["quarantines"]["past"] = dict(
        closedAtMs=NOW - 1000, chainStartMs=NOW - 3600000, evidenceWatermark=["trade:past"]
    )
    state.path.write_text(json.dumps({**legacy, "checksum": digest(legacy)}), encoding="utf-8")
    migrated = PassiveState(store)
    current = next(iter(migrated.value["leases"].values()))
    assert migrated.value["schemaVersion"] == 2
    assert current["leaseId"] == lease["leaseId"] and current["expectedSliceKey"] == lease["expectedSliceKey"]
    assert {k: v for k, v in current.items() if k not in {"leaseId", "expectedSliceKey"}} == saved
    assert migrated.value["quarantines"] == legacy["quarantines"]
    assert json.loads(state.path.read_text(encoding="utf-8"))["schemaVersion"] == 1
    migrated.reconcile([dict(id=61)] if confirmed else [], NOW + 1000)
    migrated.renew(current, NOW + 3600000, ["trade:new"], StrategyPolicyV3())
    assert json.loads(state.path.read_text(encoding="utf-8"))["schemaVersion"] == 2
    assert PassiveState(store).value == migrated.value


@pytest.mark.parametrize("fault", ["identity", "deadline", "quote", "duplicate_offer"])
def test_signed_but_invalid_lease_identity_fails_closed(tmp_path, fault):
    store, state = state_store(tmp_path)
    lease = prepare_and_bind(state, row(), "one", 41)
    if fault == "identity":
        lease["expectedSliceKey"] = "other-intent"
    elif fault == "deadline":
        lease["expiresAtMs"] = NOW + MAX_PASSIVE_MS + 1
    elif fault == "quote":
        lease["quoteKey"] = "different-quote"
    else:
        second = prepare_and_bind(state, row(1), "two", 42)
        second["offerId"] = 41
    state.persist()
    with pytest.raises(OSError, match="损坏"):
        PassiveState(store)


def test_prepare_does_not_reopen_an_expired_chain(tmp_path):
    _, state = state_store(tmp_path)
    for chain_start in (NOW - MAX_PASSIVE_MS, NOW + 1):
        with pytest.raises(ValueError, match="无效|到期"):
            state.prepare(row(), "expired", "4", NOW, StrategyPolicyV3(), {"chainStartMs": chain_start})
    assert not state.value["leases"]


@pytest.mark.parametrize("same_quote", [False, True])
def test_multiple_passive_submissions_survive_restart_and_both_expire(tmp_path, monkeypatch, same_quote):
    runtime, snapshot, account, _, clock, cancels = fixture(tmp_path, monkeypatch)
    account["wallet"] = D(300)
    orders = [row(), row(1, ".0002" if same_quote else ".00021")]

    def planned(*args):
        return {**core._base_result(args[0], args[1]), "plan": copy.deepcopy(orders), "plan_hash": "two-intents"}

    monkeypatch.setattr(core, "build_plan", planned)
    first = integration.cycle(runtime, snapshot, account, {}, NOW, False)
    assert len(first["submitted"]) == 2
    state = PassiveState(runtime.store)
    assert len(state.value["leases"]) == 2
    ids = [r["offerId"] for r in first["submitted"]]
    snapshot["offers"] = [
        {
            **submitted,
            "id": submitted["offerId"],
            "rate": submitted["submitted_rate"],
            "rate_real": submitted["effective_rate"],
            "managed": True,
            "mts_created": NOW,
        }
        for submitted in first["submitted"]
    ]
    account.update(wallet=D(0), managedOffers=snapshot["offers"], existingExposure={"total": D(300)})
    advance(runtime, snapshot, clock, minutes=60)
    monkeypatch.setattr(
        core,
        "build_plan",
        lambda *args: {
            **core._base_result(args[0], args[1]),
            "empty_reason": "VALUATION_INCOMPLETE",
            "plan_hash": "budget",
            "blockReasons": ["budget exhausted"],
        },
    )
    result = integration.cycle(runtime, snapshot, account, {}, clock[0], False)
    assert cancels == ids and result["canceledForReprice"] == ids and not result["submitted"]
    restarted = PassiveState(runtime.store)
    assert len(restarted.value["leases"]) == 2
    assert all(r["status"] == "CANCELLING" and r["chainStartMs"] == NOW for r in restarted.value["leases"].values())
    snapshot["offers"] = []
    advance(runtime, snapshot, clock)
    integration.cycle(runtime, snapshot, account, {}, clock[0], False)
    closed = PassiveState(runtime.store)
    assert not closed.value["leases"]
    assert len(closed.value["quarantines"]) == (1 if same_quote else 2)
