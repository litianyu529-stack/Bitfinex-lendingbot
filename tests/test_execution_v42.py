import json
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

import AdaptiveRuntime
import ExecutionSafety
import Lifecycle
import lendingbot
from AppContext import AppContext
from DomainTypes import WriteOutcome, WriteResult
from Recovery import resume_target
from RuntimeV3 import LendingRuntimeV3
from RuntimeV4 import V4Coordinator, FundingWriteGate
from StateStore import LendingStateStore
from StrategyV3 import StrategyPolicyV3
from bitfinex import Bitfinex, BitfinexApiError, BitfinexAmbiguousWriteError
from test_v4 import FundingClient, policy

NOW = 1_900_000_000_000


def order(**changes):
    return dict(
        amount=D(150),
        submitted_rate=D(".0002"),
        effective_rate=D(".0003"),
        period=2,
        offer_type="LIMIT",
        display_type="LIMIT",
        flags=0,
        pool="short",
        layer="balanced",
        slice_index=0,
        **changes,
    )


@pytest.mark.parametrize("rate", ["-.0001", "0", ".0001"])
def test_real_client_serializes_signed_fixed_frr_delta(rate):
    client = Bitfinex("fixture", "fixture")
    calls = []
    client._auth_write = lambda path, payload: (
        calls.append((path, payload)) or [NOW, "fon-req", None, None, [77], None, "SUCCESS", "ok"]
    )
    result = client.submit_funding_offer_result("fUSD", "150", rate, 30, "FRRDELTAFIX")
    assert result.outcome == WriteOutcome.CONFIRMED
    assert calls[0][1] == dict(type="FRRDELTAFIX", symbol="fUSD", amount="150", rate=rate, period=30, flags=0)


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount", "NaN"),
        ("amount", "Infinity"),
        ("amount", "0"),
        ("amount", "-1"),
        ("rate", "NaN"),
        ("rate", "Infinity"),
        ("rate", "0"),
        ("period", "2.5"),
        ("period", "NaN"),
        ("period", 1),
        ("period", 121),
        ("flags", 1),
        ("flags", -1),
        ("flags", "64.5"),
        ("offer_type", "HIDDEN"),
    ],
)
def test_actual_client_rejects_invalid_contract_without_network(field, value):
    client = Bitfinex("fixture", "fixture")
    client._auth_write = lambda *_args: pytest.fail("invalid contract reached transport")
    values = dict(symbol="fUSD", amount="150", rate=".0002", period=2, offer_type="LIMIT", flags=0)
    values[field] = value
    result = client.submit_funding_offer_result(**values)
    assert result.outcome == WriteOutcome.DEFINITE_REJECT
    assert result.category == "PARAMETER_INVALID" and not result.retryable


@pytest.mark.parametrize(
    "exc,outcome,category,retryable",
    [
        (
            BitfinexApiError("balance", category="BALANCE_DRIFT", retryable=True),
            WriteOutcome.DEFINITE_REJECT,
            "BALANCE_DRIFT",
            True,
        ),
        (
            BitfinexApiError("invalid", category="WRITE_PARAMETER_INVALID"),
            WriteOutcome.DEFINITE_REJECT,
            "WRITE_PARAMETER_INVALID",
            False,
        ),
        (BitfinexAmbiguousWriteError("lost reply"), WriteOutcome.UNKNOWN, "AMBIGUOUS_WRITE", False),
    ],
)
def test_actual_client_preserves_write_classification(exc, outcome, category, retryable):
    client = Bitfinex("fixture", "fixture")

    def fail(*_args):
        raise exc

    client._auth_write = fail
    result = client.submit_funding_offer_result("fUSD", "150", ".0002", 2)
    assert (result.outcome, result.category, result.retryable) == (outcome, category, retryable)


def execution_runtime(tmp_path, currency="USD", outcome=WriteOutcome.DEFINITE_REJECT):
    store = LendingStateStore(tmp_path / (currency + ".sqlite3"), currency=currency, clock=lambda: NOW / 1000)
    store.set_mode("LIVE")
    calls = []
    client = SimpleNamespace(
        submit_funding_offer_result=lambda *_a, **_kw: (
            calls.append((_a, _kw))
            or WriteResult(
                outcome,
                response=[NOW, "fon-req", None, None, [77], None, "SUCCESS", "ok"],
                category="WRITE_PARAMETER_INVALID",
                error="invalid contract",
            )
        )
    )
    runtime = LendingRuntimeV3(
        client, StrategyPolicyV3(currency=currency), store, hub=object(), clock=lambda: NOW / 1000
    )
    return runtime, store, calls


def test_permanent_rejection_survives_new_plan_hash_and_restart_and_isolation(tmp_path):
    runtime, store, calls = execution_runtime(tmp_path)
    for hash_value in ("one", "two"):
        assert not runtime._submit_plan(dict(plan=[order()], plan_hash=hash_value), D(500), "v42")
    assert len(calls) == 1
    runtime.store = LendingStateStore(store.path, currency="USD", clock=lambda: NOW / 1000)
    runtime._submit_plan(dict(plan=[order()], plan_hash="three"), D(500), "v42")
    assert len(calls) == 1
    changed = order()
    changed["submitted_rate"] = D(".00021")
    runtime._submit_plan(dict(plan=[changed], plan_hash="four"), D(500), "v42")
    assert len(calls) == 2
    other, _, other_calls = execution_runtime(tmp_path, "USDT")
    other._submit_plan(dict(plan=[order()], plan_hash="one"), D(500), "v42")
    assert len(other_calls) == 1


def test_invalid_local_contract_creates_barrier_without_reserving_intent(tmp_path):
    runtime, store, calls = execution_runtime(tmp_path)
    invalid = order()
    invalid["submitted_rate"] = D("NaN")
    for _ in range(2):
        result = dict(plan=[invalid], plan_hash="invalid")
        assert runtime._submit_plan(result, D(500), "v42") == []
        assert result["executionBlockReasons"]
    assert not calls and not store.intents()


def test_unreadable_rejection_barrier_pauses_instead_of_writing(tmp_path):
    runtime, store, calls = execution_runtime(tmp_path)
    path = ExecutionSafety._path(store)
    path.parent.mkdir(parents=True)
    path.write_text("broken", encoding="utf-8")
    runtime._submit_plan(dict(plan=[order()], plan_hash="bad"), D(500), "v42")
    assert not calls and store.runtime()["safe_reason"] == "EXECUTION_BARRIER_UNAVAILABLE"


def test_unknown_write_never_creates_repeated_submit_after_restart(tmp_path):
    runtime, store, calls = execution_runtime(tmp_path, outcome=WriteOutcome.UNKNOWN)
    plan = dict(plan=[order()], plan_hash="same")
    runtime._submit_plan(plan, D(500), "v42")
    runtime.store = LendingStateStore(store.path, currency="USD", clock=lambda: NOW / 1000)
    runtime._submit_plan(dict(plan=[order()], plan_hash="after-restart-different"), D(500), "v42")
    assert len(calls) == 1 and runtime.store.intents()[0]["state"] == "AMBIGUOUS"


@pytest.mark.parametrize(
    "category,retryable",
    [
        ("BALANCE_DRIFT", True),
        ("USDT_FX_STALE", True),
        ("ACCOUNT_SUBMISSION_BUDGET", False),
        ("FUNDING_MINIMUM_CHANGED", False),
        ("PAUSED", False),
    ],
)
def test_retryable_or_gate_rejections_do_not_create_permanent_barriers(tmp_path, category, retryable):
    store = LendingStateStore(tmp_path / "db.sqlite3")
    value = dict(order(), currency="USD", strategy_version="v42")
    assert not ExecutionSafety.record_rejection(
        store, value, WriteResult(WriteOutcome.DEFINITE_REJECT, category=category, retryable=retryable), NOW
    )
    assert not ExecutionSafety.blocked(store, value)


def test_inactive_old_pause_target_does_not_override_current_live_recovery(tmp_path):
    store = LendingStateStore(tmp_path / "db.sqlite3", clock=lambda: NOW / 1000)
    store.begin_recovery(
        "NETWORK", "prior paused error", origin_mode="PAUSED", target_mode="PAUSED", now_ms=NOW - 100000
    )
    store.record_recovery_snapshot(NOW - 70000)
    store.record_recovery_snapshot(NOW - 40000)
    store.set_mode("LIVE")
    assert not store.recovery_status()["active"] and store.recovery_status()["targetMode"] == "PAUSED"
    coordinator = SimpleNamespace()
    V4Coordinator._pause(coordinator, store, BitfinexApiError("FX failed"), "USDT_FX_STALE")
    assert store.recovery_status()["targetMode"] == "LIVE"
    store.record_recovery_snapshot(NOW + 31000)
    store.record_recovery_snapshot(NOW + 62000)
    assert store.runtime()["mode"] == "LIVE" and store.consume_resume_barrier()
    store.pause_currency()
    assert resume_target(store.runtime(), store.recovery_status()) == "PAUSED"


def test_default_begin_recovery_ignores_inactive_target_and_stale_previous_mode(tmp_path):
    store = LendingStateStore(tmp_path / "db.sqlite3")
    store.pause_currency()
    store.set_mode("LIVE")
    store.begin_recovery("NETWORK", "new error")
    assert store.recovery_status()["targetMode"] == "LIVE"


@pytest.mark.parametrize(
    "runtime,recovery,expected",
    [
        (dict(mode="LIVE", safe_reason=None), dict(active=False, targetMode="PAUSED"), "LIVE"),
        (dict(mode="LIVE", safe_reason=None), dict(active=True, targetMode="PAUSED"), "PAUSED"),
        (dict(mode="LIVE", safe_reason=None), dict(active=True, targetMode="INVALID"), "LIVE"),
        (dict(mode="PAUSED", safe_reason="data", previous_mode="LIVE"), dict(active=False), "LIVE"),
        (dict(mode="INVALID", safe_reason=None), dict(active=False), "PAUSED"),
    ],
)
def test_resume_destination_selection(runtime, recovery, expected):
    assert resume_target(runtime, recovery) == expected


@pytest.mark.parametrize(
    "reason",
    [
        "ADAPTIVE_MODEL_NOT_QUALIFIED",
        "ADAPTIVE_DATA_UNAVAILABLE",
        "ADAPTIVE_JOURNAL_FAILED",
        "EXECUTION_BARRIER_UNAVAILABLE",
    ],
)
def test_transient_market_or_network_does_not_clear_hard_protected_pause(tmp_path, reason):
    store = LendingStateStore(tmp_path / "db.sqlite3", clock=lambda: NOW / 1000)
    store.set_mode("LIVE")
    store.enter_protected_pause(reason)
    store.enter_protected_pause("MARKET_DATA_STALE")
    assert store.runtime()["safe_reason"] == reason and not store.recovery_status()["active"]
    V4Coordinator._pause(SimpleNamespace(), store, BitfinexApiError("network EOF"), "NETWORK_TRANSPORT")
    store.record_consistent_sync(NOW + 31000)
    store.record_consistent_sync(NOW + 62000)
    assert store.runtime()["mode"] == "PAUSED" and store.runtime()["safe_reason"] == reason


def test_permanent_cancel_rejection_is_not_sent_again_after_restart(tmp_path):
    runtime, store, _ = execution_runtime(tmp_path)
    value = dict(order(), currency="USD", strategy_version="v42", slice_key="owned")
    _, intent = store.reserve_intent(value, D(500))
    store.confirm_intent(intent["id"], 81)
    store.reconcile_offers(
        [
            dict(
                id=81,
                currency="USD",
                amount=D(150),
                amount_original=D(150),
                rate=D(".0002"),
                period=2,
                offer_type="LIMIT",
                flags=0,
                status="ACTIVE",
                managed=True,
                mts_created=NOW,
                mts_updated=NOW,
            )
        ],
        NOW,
    )
    calls = []
    client = SimpleNamespace(
        cancel_funding_offer_result=lambda oid: (
            calls.append(oid)
            or WriteResult(WriteOutcome.DEFINITE_REJECT, category="WRITE_PARAMETER_INVALID", error="invalid order")
        )
    )
    gate = FundingWriteGate(client, {"USD": store}, clock=lambda: NOW / 1000)
    assert gate.client_for("USD").cancel_funding_offer_result(81).category == "WRITE_PARAMETER_INVALID"
    restarted = LendingStateStore(store.path, currency="USD")
    gate = FundingWriteGate(client, {"USD": restarted}, clock=lambda: NOW / 1000)
    assert gate.client_for("USD").cancel_funding_offer_result(81).category == "WRITE_PARAMETER_INVALID"
    assert calls == [81]


def data_runtime(tmp_path, monkeypatch):
    client = FundingClient()

    def ticker(symbol):
        assert symbol == "fUSD"
        client.now += 5000
        return [".0003"] + [0] * 12

    client.ticker = ticker
    configured = replace(policy("USD"), strategy_engine="adaptive_net_yield_v2", model_id="f" * 64)
    store = LendingStateStore(tmp_path / "db.sqlite3", clock=lambda: client.now / 1000)
    runtime = LendingRuntimeV3(client, configured, store, clock=lambda: client.now / 1000)
    monkeypatch.setattr(AdaptiveRuntime, "recovery_qualified", lambda *_a: True, raising=False)
    return runtime, client, store


def test_frr_timestamp_tracks_completed_read_and_explicit_replay_cutoff(tmp_path, monkeypatch):
    runtime, client, _ = data_runtime(tmp_path, monkeypatch)
    cutoff = client.now
    runtime.sync_rest()
    assert runtime._stats[-1]["mts"] == cutoff + 5000
    cutoff = client.now
    runtime.sync_rest(now_ms=cutoff)
    assert runtime._stats[-1]["mts"] == cutoff and client.now == cutoff + 5000


def test_frr_recovery_requires_model_guard_and_manual_pause_remains_paused(tmp_path, monkeypatch):
    runtime, client, store = data_runtime(tmp_path, monkeypatch)
    store.set_mode("LIVE")
    store.enter_protected_pause("ADAPTIVE_FRR_STALE")
    monkeypatch.setattr(AdaptiveRuntime, "recovery_qualified", lambda *_a: False, raising=False)
    client.now += 31000
    runtime.sync_rest()
    assert store.recovery_status()["successfulSnapshots"] == 0 and store.runtime()["mode"] == "PAUSED"
    monkeypatch.setattr(AdaptiveRuntime, "recovery_qualified", lambda *_a: True)
    client.now += 31000
    runtime.sync_rest()
    assert store.recovery_status()["successfulSnapshots"] == 1
    store.pause_currency()
    client.now += 31000
    runtime.sync_rest()
    assert store.runtime()["mode"] == "PAUSED"


def test_v42_does_not_automatically_adopt_future_external_offers(tmp_path, monkeypatch):
    runtime, client, store = data_runtime(tmp_path, monkeypatch)
    runtime.policy = replace(runtime.policy, strategy_engine="adaptive_net_yield_v3", adopt_external_offers=True)
    client.offers["fUSD"] = [
        [
            81,
            "fUSD",
            client.now,
            client.now,
            "150",
            "150",
            "LIMIT",
            None,
            None,
            0,
            "ACTIVE",
            None,
            None,
            None,
            ".0003",
            2,
        ]
    ]
    client.available["USD"] = D(850)
    for _ in range(3):
        client.now += 31000
        runtime.sync_rest()
    assert not store.offers()[0]["managed"]
    confirmed = store.external_takeovers(states={"CONFIRMED"})
    assert len(confirmed) == 1
    assert store.adopt_external_offers([dict(id=81, currency="USD", pool="short", layer="balanced")], "confirmed") == [
        81
    ]
    assert store.offers()[0]["managed"]


def test_lifecycle_survives_context_restart_and_does_not_store_credentials(tmp_path, monkeypatch):
    context = AppContext.for_project(tmp_path, now=lambda: NOW / 1000)
    assert Lifecycle.record(
        context,
        "WORKER_STARTED",
        pid=81,
        currencies=["USD", "USDT"],
        reason="manual_start",
        api_secret="never-written",
        configPath="private.cfg",
    )
    new_context = AppContext.for_project(tmp_path, now=lambda: NOW / 1000)
    latest = Lifecycle.status(new_context)["latest"]
    assert latest["event"] == "WORKER_STARTED" and "api_secret" not in latest and "configPath" not in latest
    monkeypatch.setattr(lendingbot, "external_live_process", lambda *_a: None)
    monkeypatch.setattr(lendingbot, "worker_build_id", lambda: "fixture")
    monkeypatch.setattr(lendingbot, "dashboard_build_id", lambda: "fixture")
    control = lendingbot.controlled_bot_status(context=new_context)
    assert not control["running"] and not control["sourceFresh"] and control["stopReason"] is None
    assert control["lastLifecycleEvent"]["kind"] == "WORKER_STARTED"
    assert not control["watchdogAuthorized"]


def test_corrupt_lifecycle_does_not_invent_stop_reason(tmp_path):
    context = AppContext.for_project(tmp_path)
    path = Lifecycle._path(context)
    path.parent.mkdir(parents=True)
    path.write_text("broken", encoding="utf-8")
    assert Lifecycle.status(context)["latest"] is None
    assert Lifecycle.status(context)["recordingError"] == "JSONDecodeError"


@pytest.mark.parametrize(
    "body",
    [
        [],
        dict(currency="USDT", entries={}),
        dict(currency="USD", entries=[]),
        dict(currency="USD", entries={"key": None}),
        dict(currency="USD", entries={"key": dict(category=1)}),
    ],
)
def test_signed_rejection_file_still_requires_valid_currency_and_entries(tmp_path, body):
    store = LendingStateStore(tmp_path / "db.sqlite3")
    path = ExecutionSafety._path(store)
    path.parent.mkdir(parents=True)
    value = {**body, "checksum": ExecutionSafety._checksum(body)} if isinstance(body, dict) else body
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(OSError, match="Invalid durable"):
        ExecutionSafety.blocked(store, dict(order(), currency="USD"))


def test_tampered_rejection_file_fails_closed_and_frr_alias_uses_same_payload_key(tmp_path):
    store = LendingStateStore(tmp_path / "db.sqlite3")
    value = dict(order(), currency="USD", strategy_version="v42")
    result = WriteResult(WriteOutcome.DEFINITE_REJECT, category="WRITE_PARAMETER_INVALID")
    ExecutionSafety.record_rejection(store, value, result, NOW)
    path = ExecutionSafety._path(store)
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["entries"] = {}
    path.write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(OSError, match="checksum"):
        ExecutionSafety.blocked(store, value)
    plain = dict(value, offer_type="FRR")
    mapped = dict(value, offer_type="FRRDELTAVAR", submitted_rate=0)
    assert ExecutionSafety.execution_fingerprint(plain) == ExecutionSafety.execution_fingerprint(mapped)
    invalid = dict(value, submitted_rate="invalid")
    assert ExecutionSafety.execution_fingerprint(invalid)


def test_lifecycle_permission_failure_is_reported_without_claiming_shutdown(tmp_path, monkeypatch):
    context = AppContext.for_project(tmp_path)
    path = Lifecycle._path(context)
    with pytest.raises(ValueError, match="Invalid lifecycle event"):
        Lifecycle.record(context, "unsafe event")

    def fail(*_a, **_kw):
        raise PermissionError("private path")

    monkeypatch.setattr(type(path), "open", fail)
    assert not Lifecycle.record(context, "WORKER_STARTED", reason="unexpected user text", api_key="secret")
    status = Lifecycle.status(context)
    assert status["latest"] is None and status["recordingError"] == "PermissionError"
