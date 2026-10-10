"""Dashboard integration uses local stores and fake Workers exclusively."""

import datetime
import copy
import json
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

import AdaptiveRuntime
import Lifecycle
import OperationalV41
import ResearchV4
import V4Service
import lendingbot
from AdaptiveEngines import REGISTRY, template_for
from AppContext import AppContext
from StrategyV3 import json_decimal
from V4Service import V4DashboardService, stores_for_profiles, version_performance
from test_v4 import FundingClient, configuration, order


def fixture_service(tmp_path):
    path, settings = configuration(tmp_path)
    client = FundingClient()
    context = AppContext.for_project(
        tmp_path, config_path=str(path), client_factory=lambda *_args: client, now=lambda: client.now / 1000
    )
    service = V4DashboardService(str(path), context.status_path, context)
    stores = stores_for_profiles(settings, context.now)
    return service, settings, stores, context, client


@pytest.mark.parametrize("currency", ["USD", "USDT"])
def test_all_adaptive_models_and_templates_keep_engine_and_currency(tmp_path, monkeypatch, currency):
    service, settings, stores, _, _ = fixture_service(tmp_path)
    models = {
        engine: {
            "id": (str(index + 1) * 64), "algorithm": data[0], "currency": currency,
            "confidence": "LOW", "coverage": {}, "typeObservationCounts": {"LIMIT": 0},
        }
        for index, (engine, data) in enumerate(REGISTRY.items())
    }
    monkeypatch.setattr(ResearchV4.ModelRepository, "candidate", lambda _repo, _now, engine: models[engine])
    monkeypatch.setattr(AdaptiveRuntime, "eligible", lambda *_args: False)
    monkeypatch.setattr(OperationalV41, "status", lambda *_args: {"operationalReady": True})
    old = stores[currency].save_strategy(json_decimal(settings.policies[currency].__dict__), "ACTIVE")
    payload = service.config(currency)
    assert set(payload["adaptiveTemplates"]) == set(REGISTRY)
    assert payload["activeStrategy"]["version_id"] == old
    assert payload["activeStrategy"]["engine"] == "legacy_v3"
    for engine, info in payload["candidateModels"].items():
        assert info["engine"] == engine and info["algorithm"] == REGISTRY[engine][0]
        assert info["currency"] == currency and info["typeConfidence"]["LIMIT"] == "LOW"
        assert not info["eligibleForLiveCandidate"] and info["operationalReady"]
        template = payload["adaptiveTemplates"][engine]
        assert template["strategy_engine"] == template["engine"] == engine
        assert template["algorithm"] == REGISTRY[engine][0] and template["currency"] == currency
    v42 = payload["adaptiveTemplates"]["adaptive_net_yield_v3"]
    assert D(v42["reprice_gain_apr"]) == D(".25")
    assert v42["passive_wait_minutes"] == 60
    assert stores[currency].strategy("ACTIVE")["version_id"] == old


def submitted_offer(store, version, offer_id, index, adopted=False):
    spec = {
        **order(store.currency, index=index), "strategy_version": version, "strategy_variant": "adaptive_net_yield_v3",
    }
    created, intent = store.reserve_intent(spec, D("10000"))
    assert created
    store.mark_submitting(intent["id"])
    store.confirm_intent(intent["id"], offer_id)
    if adopted:
        with store.transaction() as connection:
            connection.execute(
                "UPDATE order_intents SET resolution='PREFLIGHT_ADOPTED', slice_key=?, "
                "strategy_variant='baseline' WHERE id=?",
                (f"adopted:{offer_id}", intent["id"]),
            )
    return offer_id


def fill_and_loan(store, offer_id, index, mts, amount="150", status="ACTIVE", currency=None):
    currency = currency or store.currency
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO funding_trades(trade_id,currency,offer_id,amount,rate,period,mts,managed) "
            "VALUES(?,?,?,?,?,?,?,1)",
            (index, currency, offer_id, amount, ".0004", 2, mts),
        )
        connection.execute(
            """INSERT INTO credits(credit_id,currency,amount,rate,period,status,managed,offer_id,
                                   mts_opening,mts_updated,last_seen_ms)
               VALUES(?,?,?,?,?,?,1,?,?,?,?)""",
            (index, currency, amount, ".0004", 2, status, offer_id, mts, mts, mts),
        )


def test_version_counts_exclude_adoption_old_versions_and_other_currency(tmp_path):
    _, settings, stores, context, _ = fixture_service(tmp_path)
    store = stores["USD"]
    current = template_for("adaptive_net_yield_v3", settings.policies["USD"])
    version = store.save_strategy(json_decimal(current.__dict__), "ACTIVE")
    now = int(context.now() * 1000)
    own = submitted_offer(store, version, 101, 1)
    fill_and_loan(store, own, 1, now, "100.01", "CLOSED")
    fill_and_loan(store, own, 2, now, "-49.99")  # Multiple partial fills and returned loans survive.
    adopted = submitted_offer(store, version, 102, 2, adopted=True)
    fill_and_loan(store, adopted, 3, now)
    old = submitted_offer(store, "older-version", 103, 3)
    fill_and_loan(store, old, 4, now)
    cross = submitted_offer(store, version, 104, 4)
    fill_and_loan(store, cross, 5, now, currency="USDT")
    before = submitted_offer(store, version, 105, 5)
    fill_and_loan(store, before, 6, now - 1)
    result = version_performance(store, store.strategy("ACTIVE"))
    assert result["engine"] == "adaptive_net_yield_v3"
    assert result["newFillCount"] == result["newLoanCount"] == 2
    assert result["newFillPrincipal"] == result["newLoanPrincipal"] == D("150.00")
    assert result["netInterest"] is None and result["interestAttribution"] == "UNAVAILABLE"
    assert version_performance(stores["USDT"], None) is None
    assert version_performance(store, {**store.strategy("ACTIVE"), "activated_at_ms": None}) is None


@pytest.mark.parametrize("stamp,age,fresh", [
    ("recent", 5, True), ("old", 300, False), ("future", 0, False), ("invalid", None, False),
])
def test_status_age_is_currency_snapshot_age(tmp_path, monkeypatch, stamp, age, fresh):
    service, settings, stores, context, _ = fixture_service(tmp_path)
    stores["USD"].save_strategy(json_decimal(settings.policies["USD"].__dict__), "ACTIVE")
    offsets = {"recent": -5, "old": -300, "future": 10}
    date = (
        datetime.datetime.fromtimestamp(context.now() + offsets[stamp], datetime.timezone.utc).isoformat()
        if stamp in offsets else "bad timestamp"
    )
    payload = {
        "schemaVersion": 3, "operationMode": "LIVE", "last_update": "2099-01-01 00:00:00",
        "currencies": {"USD": {"last_update": date}},
    }
    Path(context.status_path).parent.mkdir(parents=True, exist_ok=True)
    Path(context.status_path).write_text(json.dumps(payload), encoding="utf-8")
    event = {"kind": "WORKER_STARTED", "atMs": int(context.now() * 1000), "reason": "confirmed_preflight"}
    monkeypatch.setattr(lendingbot, "controlled_bot_status", lambda *_args: {
        "running": True, "pid": 123, "sourceFresh": True, "dataAgeSeconds": 0,
        "stopReason": None, "lastLifecycleEvent": event,
    })
    result = service.status("USD")
    assert result["control"]["dataAgeSeconds"] == result["dataAgeSeconds"] == age
    assert result["control"]["sourceFresh"] == result["sourceFresh"] == fresh
    assert result["lastLifecycleEvent"] == event
    assert result["activeStrategy"]["engine"] == "legacy_v3"
    absent = service.status("USDT")
    assert not absent["sourceFresh"] and absent["dataAgeSeconds"] is None


def test_launch_records_confirmed_worker_start_without_real_process(tmp_path, monkeypatch):
    service, settings, stores, context, _ = fixture_service(tmp_path)
    stores["USD"].save_strategy(json_decimal(settings.policies["USD"].__dict__), "ACTIVE")
    process = SimpleNamespace(pid=9876, poll=lambda: None)
    monkeypatch.setattr(lendingbot, "controlled_bot_running", lambda *_args: False)
    monkeypatch.setattr(V4Service.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(lendingbot, "controlled_bot_status", lambda *_args: {"running": True, "pid": process.pid})
    try:
        result = service._launch(("USD",))
        assert result["pid"] == process.pid
        event = Lifecycle.status(context)["latest"]
        assert event["event"] == "WORKER_STARTED" and event["currencies"] == ["USD"]
        assert event["authorized"] and event["reason"] == "confirmed_preflight"
    finally:
        lendingbot.cleanup_controlled_bot_handle(context)


def test_supervisor_logs_timeout_and_revalidated_restart_only(tmp_path, monkeypatch):
    service, settings, stores, context, client = fixture_service(tmp_path)
    monkeypatch.setattr(lendingbot, "worker_build_id", lambda: "fixture-build")
    version = stores["USD"].save_strategy(json_decimal(settings.policies["USD"].__dict__), "ACTIVE")
    context.process_state.auto_restart_authorization = {
        "v4": True, "session": context.process_state.supervisor_session, "currencies": ["USD"],
        "authorizedAt": context.now() - 1000, "configDigest": lendingbot.config_sha256(service.config_path),
        "buildId": lendingbot.worker_build_id(), "strategies": {"USD": version},
    }
    process = {"running": True, "pid": 123}
    monkeypatch.setattr(lendingbot, "controlled_bot_status", lambda *_args: process)
    stops = []

    def stopped(*_args, **kwargs):
        stops.append(kwargs)
        process["running"] = False

    monkeypatch.setattr(lendingbot, "stop_controlled_bot", stopped)
    monkeypatch.setattr(V4DashboardService, "preflight", lambda *_args, **_kwargs: {"canStart": True})
    launches = []

    def launched(_self, selected, recovering):
        launches.append((selected, recovering))
        return {"pid": 456}

    monkeypatch.setattr(V4DashboardService, "_launch", launched)
    V4Service.supervisor_tick(service.config_path, context.status_path, context)
    assert not launches  # The persisted recovery backoff is still enforced.
    next_probe = stores["USD"].recovery_status()["nextProbeAt"]
    assert next_probe is not None
    client.now = int(next_probe)
    V4Service.supervisor_tick(service.config_path, context.status_path, context)
    assert stops[0]["reason"] == "worker_heartbeat_timeout" and stops[0]["preserve_authorization"]
    assert launches == [(["USD"], True)]
    events = Lifecycle.status(context)["events"]
    assert [row["event"] for row in events] == ["HEARTBEAT_TIMEOUT", "SUPERVISOR_RESTART"]
    assert events[-1]["pid"] == 456


def test_v42_worker_polls_events_every_ten_seconds_with_fake_coordinator(tmp_path, monkeypatch):
    from Logger import Logger

    service, settings, stores, context, client = fixture_service(tmp_path)
    v42 = replace(template_for("adaptive_net_yield_v3", settings.policies["USD"]), model_id="a" * 64)
    stores["USD"].save_strategy(json_decimal(v42.__dict__), "ACTIVE")
    durations = []
    coordinator = SimpleNamespace(
        gate=SimpleNamespace(),
        runtimes={"USD": SimpleNamespace(policy=v42, store=stores["USD"])},
        start=lambda: None, shutdown=lambda: None,
        cycle=lambda: {"USD": {"currency": "USD", "last_update": str(client.now)}},
    )
    monkeypatch.setattr(V4Service, "V4Coordinator", lambda *_args, **_kwargs: coordinator)

    def interrupt(seconds):
        durations.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(V4Service.time, "sleep", interrupt)
    args = lendingbot.parse_args([
        "--config", service.config_path, "--live", "--confirmed-preflight", "--currencies", "USD", "--no-server",
    ])
    assert V4Service.run_worker(args, settings, context, Logger(context.status_path, 20)) == 0
    assert durations == [10] and not client.submissions
    assert stores["USD"].runtime()["mode"] == "PAUSED"


def prepared_adoption_start(tmp_path, monkeypatch, adopt=False):
    service, settings, stores, context, client = fixture_service(tmp_path)
    store = stores["USD"]
    policy = replace(template_for("adaptive_net_yield_v3", settings.policies["USD"]), adopt_external_offers=adopt)
    version = store.save_strategy(json_decimal(policy.__dict__), "ACTIVE")
    external = {
        "id": 901, "currency": "USD", "amount": D(150), "rate": D(".0002"),
        "period": 2, "offer_type": "LIMIT", "flags": 0, "status": "ACTIVE", "managed": False,
    }
    other = {**external, "id": 902}
    store.reconcile_offers([external, other], client.now)
    summary = {
        "activeStrategyVersion": version, "policyHash": "unchanged", "accountDigest": "unchanged",
        "externalAdoptionCandidates": json_decimal([external]) if adopt else [],
    }
    summary["externalAdoptionDigest"] = lendingbot._canonical_sha256(summary["externalAdoptionCandidates"])
    refreshed = {"canStart": True, "profiles": {"USD": {"summary": summary}}}
    service.tokens["confirmed"] = {
        "expires": context.now() + 60, "currencies": ("USD",),
        "digest": lendingbot.config_sha256(service.config_path), "build": lendingbot.worker_build_id(),
        "profiles": copy.deepcopy(refreshed["profiles"]),
    }
    monkeypatch.setattr(service, "preflight", lambda *_args, **_kwargs: refreshed)
    launches = []
    monkeypatch.setattr(service, "_launch", lambda selected: launches.append(selected) or {"running": True})
    return service, store, external, other, refreshed, launches


def test_v42_default_start_does_not_adopt_external_ids(tmp_path, monkeypatch):
    service, store, _, _, _, launches = prepared_adoption_start(tmp_path, monkeypatch)
    assert service.start("confirmed", ["USD"])["running"]
    assert launches == [("USD",)]
    assert all(not row["managed"] for row in store.offers(active_only=True))
    assert not store.intents()


def test_v42_confirmed_start_adopts_only_explicit_ids_and_future_ids_stay_external(tmp_path, monkeypatch):
    service, store, external, other, _, launches = prepared_adoption_start(tmp_path, monkeypatch, adopt=True)
    service.start("confirmed", ["USD"])
    assert launches == [("USD",)]
    assert {row["offer_id"] for row in store.offers(active_only=True) if row["managed"]} == {901}
    intent = store.intents()[0]
    assert intent["exchange_offer_id"] == 901 and intent["resolution"] == "PREFLIGHT_ADOPTED"
    store.reconcile_offers([external, other, {**external, "id": 903}], store._now_ms() + 31000)
    assert {row["offer_id"] for row in store.offers(active_only=True) if row["managed"]} == {901}
    assert len(store.intents()) == 1


def test_changed_adoption_ids_reject_even_if_digest_was_not_updated(tmp_path, monkeypatch):
    service, store, _, other, refreshed, launches = prepared_adoption_start(tmp_path, monkeypatch, adopt=True)
    refreshed["profiles"]["USD"]["summary"]["externalAdoptionCandidates"] = json_decimal([other])
    with pytest.raises(lendingbot.ConfigError, match="挂单集合"):
        service.start("confirmed", ["USD"])
    assert not launches and not store.intents()


def test_recovery_launch_does_not_reuse_external_adoption_permission(tmp_path, monkeypatch):
    service, settings, stores, context, _ = fixture_service(tmp_path)
    policy = replace(template_for("adaptive_net_yield_v3", settings.policies["USD"]), adopt_external_offers=True)
    store = stores["USD"]
    store.save_strategy(json_decimal(policy.__dict__), "ACTIVE")
    external = dict(id=901, currency="USD", amount=D(150), rate=D(".0002"), period=2, offer_type="LIMIT")
    store.reconcile_offers([external], store._now_ms())
    monkeypatch.setattr(lendingbot, "controlled_bot_running", lambda *_args: True)
    monkeypatch.setattr(lendingbot.LiveProcessLock, "inspect", lambda *_args: {"metadata": {"v4": True}})
    monkeypatch.setattr(lendingbot, "controlled_bot_status", lambda *_args: {"running": True})
    monkeypatch.setattr(V4Service.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("no Worker process"))
    service._launch(("USD",), recovering=True)
    assert not store.intents() and not store.offers(active_only=True)[0]["managed"]
    assert context.process_state.auto_restart_authorization["currencies"] == ["USD"]
