"""A legacy session cannot approve V4.2 external ownership or recovery."""

from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

import lendingbot as app
import StrategyV42 as core
from StateStore import LendingStateStore
from StrategyV3 import StrategyPolicyV3, json_decimal


def prepared(tmp_path, monkeypatch, engine=core.ENGINE):
    config = tmp_path / "mock.cfg"
    config.write_text("[mock]\nvalue = synthetic\n", encoding="utf-8")
    policy = replace(core.template(StrategyPolicyV3()), strategy_engine=engine, adopt_external_offers=True)
    context = app.AppContext.for_project(tmp_path, config_path=str(config), now=lambda: 100)
    store = LendingStateStore(context.state_db_path, currency="USD", clock=context.now)
    version = store.save_strategy(json_decimal(policy.__dict__), "ACTIVE")
    external = dict(
        id=901, currency="USD", amount=D(150), rate=D(".0002"), period=2, offer_type="LIMIT", flags=0, status="ACTIVE"
    )
    other = {**external, "id": 902}
    store.reconcile_offers([external, other], 100000)
    settings = SimpleNamespace(strategy_v3=policy)
    monkeypatch.setattr(app, "v3_store_for_config", lambda *_a, **_kw: (store, settings))
    monkeypatch.setattr(app, "controlled_bot_running", lambda *_a, **_kw: False)
    monkeypatch.setattr(app, "evaluate_live_preflight", lambda *_a, **_kw: pytest.fail("no authenticated preflight"))
    monkeypatch.setattr(app.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("no real Worker"))
    context.process_state.preflight = dict(
        preflightId="old-session",
        expiresAt=200,
        configDigest=app.config_sha256(config),
        activeStrategyVersion=version,
        externalAdoptionIds=[901],
    )
    return context, store, external, other


@pytest.mark.parametrize("recovering", [False, True])
def test_v42_legacy_start_rejected_before_consuming_ownership_confirmation(tmp_path, monkeypatch, recovering):
    context, store, _, _ = prepared(tmp_path, monkeypatch)
    with pytest.raises(app.ConfigError, match="V4.2.*V4.*预检和启动接口"):
        app.start_controlled_bot(
            context.config_path, context.status_path, "old-session", context=context, preserve_recovery=recovering
        )
    assert not store.intents() and all(not r["managed"] for r in store.offers(active_only=True))
    assert store.runtime()["mode"] == "PAUSED" and context.process_state.process is None


def test_direct_legacy_preflight_consumption_also_cannot_adopt_v42_ids(tmp_path, monkeypatch):
    context, store, _, _ = prepared(tmp_path, monkeypatch)
    with pytest.raises(app.ConfigError, match="V4.2.*V4.*预检和启动接口"):
        app.consume_controlled_bot_preflight(context.config_path, "old-session", context=context)
    assert not store.intents() and all(not r["managed"] for r in store.offers(active_only=True))
    assert context.process_state.preflight is None


class OneIteration:
    def __init__(self):
        self.calls = 0

    def wait(self, _seconds):
        self.calls += 1
        return self.calls > 1


@pytest.mark.parametrize("running", [False, True])
def test_legacy_watchdog_revokes_v42_scope_before_preflight_or_adoption(tmp_path, monkeypatch, running):
    context, store, external, _ = prepared(tmp_path, monkeypatch)
    store.adopt_external_offers([external], store.strategy("ACTIVE")["version_id"])
    store.begin_recovery("NETWORK_TRANSPORT", "synthetic", origin_mode="LIVE", target_mode="LIVE", now_ms=0)
    before = store.recovery_status()
    state = context.process_state
    state.supervisor_stop = OneIteration()
    state.supervisor_session = "legacy-session"
    state.auto_restart_authorization = dict(session="legacy-session", authorizedAt=0)
    monkeypatch.setattr(app, "controlled_bot_status", lambda *_a, **_kw: dict(running=running))
    monkeypatch.setattr(app, "create_controlled_bot_preflight", lambda *_a, **_kw: pytest.fail("no renewed approval"))
    app.worker_supervisor_loop(context.config_path, context.status_path, context)
    assert state.auto_restart_authorization is None and state.preflight is None
    assert state.stop_reason == "v42_requires_v4_preflight"
    assert store.recovery_status() == before
    assert {r["offer_id"] for r in store.offers(active_only=True) if r["managed"]} == {901}
    assert len(store.intents()) == 1  # Previously authorized IDs survive; a future ID stays external.


@pytest.mark.parametrize("engine", ["legacy_v3", "adaptive_net_yield_v1", "adaptive_net_yield_v2"])
def test_legacy_control_guard_keeps_existing_engines_compatible(engine):
    app._require_single_currency_control(StrategyPolicyV3(strategy_engine=engine))
