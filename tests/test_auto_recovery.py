import http.client
import sqlite3

import pytest

import lendingbot
from Recovery import classify_runtime_error, recovery_delay_seconds
from StateStore import LendingStateStore
from bitfinex import BitfinexApiError, BitfinexTransientError


class StatusLogger:
    def __init__(self):
        self.values = {}

    def updateMetaValue(self, key, value):
        self.values[key] = value

    def refreshStatus(self, value):
        self.values["status"] = value

    def persistStatus(self):
        return None


def test_recovery_backoff_caps_at_five_minutes():
    assert [recovery_delay_seconds(item) for item in range(6)] == [30, 60, 120, 300, 300, 300]


@pytest.mark.parametrize(
    ("error", "category", "retryable"),
    [
        (http.client.IncompleteRead(b""), "NETWORK_TRANSPORT", True),
        (BitfinexTransientError("limited", category="BITFINEX_HTTP_TRANSIENT"), "BITFINEX_HTTP_TRANSIENT", True),
        (BitfinexApiError("forbidden", category="AUTH_PERMISSION", manual_required=True), "AUTH_PERMISSION", False),
        (sqlite3.OperationalError("database is locked"), "DATABASE_BUSY", True),
        (sqlite3.DatabaseError("database disk image is malformed"), "UNEXPECTED_RUNTIME_ERROR", False),
        (TypeError("bug"), "PROGRAM_ERROR", False),
    ],
)
def test_runtime_error_classification(error, category, retryable):
    decision = classify_runtime_error(error)
    assert (decision.category, decision.retryable) == (category, retryable)


def test_incomplete_read_pauses_then_resumes_live_after_two_clean_snapshots(tmp_path):
    store = LendingStateStore(tmp_path / "state.sqlite3", clock=lambda: 1000)
    store.set_mode("LIVE", "test")
    logger = StatusLogger()

    lendingbot.publish_safe_status(logger, store, BitfinexTransientError("IncompleteRead(0 bytes read)"))

    assert store.runtime()["mode"] == "PAUSED"
    recovery = store.recovery_status()
    assert recovery["active"] and recovery["targetMode"] == "LIVE" and not recovery["manualRequired"]
    store.record_consistent_sync(1_030_000)
    assert store.runtime()["mode"] == "PAUSED"
    store.record_consistent_sync(1_060_000)
    assert store.runtime()["mode"] == "LIVE"
    assert store.consume_resume_barrier() is True
    assert store.consume_resume_barrier() is False


def test_failed_probe_resets_consecutive_snapshot_count(tmp_path):
    store = LendingStateStore(tmp_path / "state.sqlite3", clock=lambda: 1000)
    store.set_mode("LIVE", "test")
    store.set_mode("PAUSED", "AUTO_RECOVERY:NETWORK_TRANSPORT")
    store.begin_recovery("NETWORK_TRANSPORT", "offline", origin_mode="LIVE", target_mode="LIVE", now_ms=1_000_000)
    store.record_consistent_sync(1_030_000)
    assert store.recovery_status()["successfulSnapshots"] == 1
    store.record_recovery_failure("offline again", now_ms=1_040_000)
    assert store.recovery_status()["successfulSnapshots"] == 0
    store.record_consistent_sync(1_070_000)
    assert store.runtime()["mode"] == "PAUSED"
    store.record_consistent_sync(1_100_000)
    assert store.runtime()["mode"] == "LIVE"


def test_secondary_market_stale_safe_keeps_original_live_recovery_target(tmp_path):
    store = LendingStateStore(tmp_path / "state.sqlite3", clock=lambda: 1000)
    store.set_mode("LIVE", "test")
    store.set_mode("PAUSED", "AUTO_RECOVERY:NETWORK_TRANSPORT")
    store.begin_recovery(
        "NETWORK_TRANSPORT",
        "socket timed out",
        origin_mode="LIVE",
        target_mode="LIVE",
        now_ms=1_000_000,
    )

    # This is the exact production sequence: the failed REST probe leaves the
    # cached market stale, which enters SAFE while runtime mode is already PAUSED.
    store.enter_safe("MARKET_DATA_STALE")

    recovery = store.recovery_status()
    assert store.runtime()["mode"] == "SAFE"
    assert recovery["originMode"] == "LIVE"
    assert recovery["targetMode"] == "LIVE"
    store.record_consistent_sync(1_030_000)
    store.record_consistent_sync(1_060_000)
    assert store.runtime()["mode"] == "LIVE"


def test_program_error_requires_manual_restart(tmp_path):
    store = LendingStateStore(tmp_path / "state.sqlite3")
    store.set_mode("LIVE", "test")
    lendingbot.publish_safe_status(StatusLogger(), store, TypeError("broken invariant"))

    recovery = store.recovery_status()
    assert store.runtime()["mode"] == "PAUSED"
    assert recovery["active"] and recovery["manualRequired"]
    store.record_consistent_sync(1_000_000)
    store.record_consistent_sync(1_030_000)
    assert store.runtime()["mode"] == "PAUSED"


def test_manual_dashboard_pause_clears_auto_recovery(tmp_path):
    store = LendingStateStore(tmp_path / "state.sqlite3")
    store.set_mode("LIVE", "test")
    store.set_mode("PAUSED", "AUTO_RECOVERY:NETWORK_TRANSPORT")
    store.begin_recovery("NETWORK_TRANSPORT", "offline", origin_mode="LIVE", target_mode="LIVE")
    assert store.recovery_status()["active"]

    store.set_mode("PAUSED", "dashboard_pause")

    assert not store.recovery_status()["active"]


def test_schema_11_preserves_runtime_and_adds_recovery_and_allocation_state(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = LendingStateStore(path)
    store.set_mode("PAUSED", "dashboard_stop")
    with store.read_connection() as connection:
        version = connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        recovery_rows = connection.execute("SELECT COUNT(*) FROM recovery_state").fetchone()[0]
    assert (version, integrity, recovery_rows) == ("11", "ok", 1)


class OneSupervisorIteration:
    def __init__(self):
        self.calls = 0

    def wait(self, _seconds):
        self.calls += 1
        return self.calls > 1


def test_watchdog_hung_worker_enters_recovery_and_stops_exact_process(tmp_path, monkeypatch):
    context = lendingbot.AppContext.for_project(tmp_path, now=lambda: 400)
    context.process_state.supervisor_stop = OneSupervisorIteration()
    context.process_state.supervisor_session = "session"
    context.process_state.auto_restart_authorization = {
        "session": "session",
        "authorizedAt": 1,
    }
    store = LendingStateStore(context.state_db_path, clock=lambda: 400)
    store.set_mode("LIVE", "test")
    monkeypatch.setattr(lendingbot, "v3_store_for_config", lambda _path: (store, None))
    monkeypatch.setattr(lendingbot, "controlled_bot_status", lambda *_args, **_kwargs: {"running": True})
    stopped = []
    monkeypatch.setattr(
        lendingbot,
        "stop_controlled_bot",
        lambda *_args, **kwargs: stopped.append(kwargs.get("preserve_authorization")),
    )

    lendingbot.worker_supervisor_loop(context.config_path, context.status_path, context)

    recovery = store.recovery_status()
    assert stopped == [True]
    assert recovery["active"] and recovery["targetMode"] == "LIVE"
    assert recovery["category"] == "WORKER_HEARTBEAT_TIMEOUT"


def test_watchdog_restarts_only_after_valid_session_preflight(tmp_path, monkeypatch):
    context = lendingbot.AppContext.for_project(tmp_path, now=lambda: 100)
    context.process_state.supervisor_stop = OneSupervisorIteration()
    context.process_state.supervisor_session = "session"
    authorization = {"session": "session", "authorizedAt": 0}
    context.process_state.auto_restart_authorization = authorization
    store = LendingStateStore(context.state_db_path, clock=lambda: 100)
    store.set_mode("PAUSED", "AUTO_RECOVERY:NETWORK_TRANSPORT")
    store.begin_recovery(
        "NETWORK_TRANSPORT",
        "offline",
        origin_mode="LIVE",
        target_mode="LIVE",
        now_ms=0,
    )
    monkeypatch.setattr(lendingbot, "v3_store_for_config", lambda _path: (store, None))
    monkeypatch.setattr(lendingbot, "controlled_bot_status", lambda *_args, **_kwargs: {"running": False})
    monkeypatch.setattr(lendingbot, "_watchdog_authorization_valid", lambda *_args: True)
    monkeypatch.setattr(
        lendingbot,
        "create_controlled_bot_preflight",
        lambda *_args, **_kwargs: {"canStart": True, "preflightId": "fresh"},
    )
    started = []
    monkeypatch.setattr(
        lendingbot,
        "start_controlled_bot",
        lambda *_args, **_kwargs: started.append(True),
    )

    lendingbot.worker_supervisor_loop(context.config_path, context.status_path, context)

    assert started == [True]
    assert context.process_state.stop_reason is None


def test_watchdog_invalid_build_or_config_requires_manual_action(tmp_path, monkeypatch):
    context = lendingbot.AppContext.for_project(tmp_path, now=lambda: 100)
    context.process_state.supervisor_stop = OneSupervisorIteration()
    context.process_state.supervisor_session = "session"
    context.process_state.auto_restart_authorization = {"session": "session", "authorizedAt": 0}
    store = LendingStateStore(context.state_db_path, clock=lambda: 100)
    store.set_mode("PAUSED", "AUTO_RECOVERY:NETWORK_TRANSPORT")
    store.begin_recovery(
        "NETWORK_TRANSPORT",
        "offline",
        origin_mode="LIVE",
        target_mode="LIVE",
        now_ms=0,
    )
    monkeypatch.setattr(lendingbot, "v3_store_for_config", lambda _path: (store, None))
    monkeypatch.setattr(lendingbot, "controlled_bot_status", lambda *_args, **_kwargs: {"running": False})
    monkeypatch.setattr(lendingbot, "_watchdog_authorization_valid", lambda *_args: False)

    lendingbot.worker_supervisor_loop(context.config_path, context.status_path, context)

    assert store.recovery_status()["manualRequired"]
    assert context.process_state.auto_restart_authorization is None
