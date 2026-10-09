import configparser
import json
import threading
from dataclasses import replace
from decimal import Decimal
from http.server import ThreadingHTTPServer
from urllib import error, request

import pytest

import lendingbot
from AppContext import AppContext
from Configuration import ConfigError, build_settings, validate_settings
from Currency import funding_minimum, funding_sizing, normalize_currency, usdt_minimum
from DomainTypes import WriteOutcome, WriteResult
from ExchangeModels import parse_credit_rows, parse_offer_rows, parse_wallet_rows
from MarketDataStream import BitfinexMarketDataHub
from RuntimeV3 import LendingRuntimeV3
from RuntimeV4 import FundingWriteGate, V4Coordinator
from StateStore import LendingStateStore, StateStoreError
from StrategyV3 import StrategyPolicyV3, evenly_distributed_amounts, json_decimal, validate_policy_v3
from V4Service import V4DashboardService, restart_control_digest, run_worker, stores_for_profiles, supervisor_tick
from bitfinex import Bitfinex, BitfinexApiError, currency_to_symbol, symbol_to_currency


D = Decimal


class FundingClient:
    api_key = "fixture-key"
    api_secret = "fixture-secret"

    def __init__(self, *_args):
        self.available = {"USD": D("1000"), "USDT": D("700")}
        self.offers = {"fUSD": [], "fUST": []}
        self.submissions = []
        self.bid = "0.99"
        self.failure = None
        self.now = 1_900_000_000_000

    def has_credentials(self):
        return True

    def key_permissions(self):
        return [["wallets", 1, 0], ["funding", 1, 1], ["withdraw", 0, 0], ["ui_withdraw", 0, 0]]

    def wallets(self):
        return [
            ["funding", "USD", "1000", None, str(self.available["USD"])],
            ["funding", "UST", "700", None, str(self.available["USDT"])],
            ["funding", "USTF0", "9999", None, "9999"],
        ]

    def ticker(self, symbol):
        assert symbol == "tUSTUSD"
        if self.bid is None:
            raise BitfinexApiError("quote unavailable")
        return [self.bid, 1000, "1.01"]

    def funding_book(self, symbol, _length=250):
        if self.failure == symbol:
            raise BitfinexApiError("market unavailable", category="MARKET_DATA")
        return [["0.0004", 2, 1, "1000"], ["0.00039", 2, 1, "-1000"]]

    def funding_trades(self, _symbol, **_kwargs):
        return [[1, self.now - 1000, "1000", "0.0004", 2]]

    def funding_stats(self, _symbol, **_kwargs):
        return []

    def active_funding_offers(self, symbol):
        return self.offers[symbol]

    def active_funding_credits(self, _symbol):
        return []

    active_funding_loans = active_funding_credits
    funding_trades_history = funding_stats
    funding_offers_history = funding_stats
    funding_credits_history = funding_stats

    def ledgers(self, currency, **_kwargs):
        return [
            [
                10,
                "UST" if currency == "USDT" else "USD",
                "funding",
                self.now - 10,
                None,
                "2.5",
                "1000",
                None,
                "Margin Funding Payment",
            ]
        ]

    def submit_funding_offer_result(self, symbol, amount, rate, period, offer_type, flags=0):
        identifier = 1000 + len(self.submissions)
        self.submissions.append((symbol, D(amount)))
        self.available[normalize_currency(symbol)] -= D(amount)
        row = [
            identifier,
            symbol,
            self.now,
            self.now,
            amount,
            amount,
            offer_type,
            None,
            None,
            flags,
            "ACTIVE",
            None,
            None,
            None,
            rate,
            period,
        ]
        self.offers[symbol].append(row)
        return WriteResult(WriteOutcome.CONFIRMED, [self.now, "fon-req", None, None, row, None, "SUCCESS", "submitted"])


def policy(currency="USD"):
    return StrategyPolicyV3(
        version=4,
        currency=currency,
        short_share=D("100"),
        medium_share=D("0"),
        long_share=D("0"),
        short_floor_apr=D("0.01"),
        medium_floor_apr=D("0.01"),
        long_floor_apr=D("0.01"),
        max_lend_amount=D("1000"),
        enable_frr=False,
        enable_frr_delta_fixed=False,
        enable_frr_delta_variable=False,
    )


def configuration(tmp_path, usdt=True):
    config = configparser.ConfigParser()
    config.read_dict(
        {
            "BITFINEX": {"apikey": "fixture-key", "secret": "fixture-secret", "currencies": "USD,USDT"},
            "BOT": {"statedbfile": str(tmp_path / "usd.sqlite3")},
            "STRATEGY_V4_USD": {
                "enabled": "true",
                "short_floor_apr": "1",
                "medium_floor_apr": "1",
                "long_floor_apr": "1",
                "max_lend_amount": "1000",
            },
            "STRATEGY_V4_USDT": {
                "enabled": str(usdt).lower(),
                "short_floor_apr": "1" if usdt else "",
                "medium_floor_apr": "1" if usdt else "",
                "long_floor_apr": "1" if usdt else "",
                "max_lend_amount": "700" if usdt else "",
            },
        }
    )
    path = tmp_path / "test.cfg"
    with path.open("w", encoding="utf-8") as stream:
        config.write(stream)
    settings = build_settings(lendingbot.parse_args([]), config)
    validate_settings(settings)
    return path, settings


def order(currency, index=0, amount="150"):
    return {
        "currency": currency,
        "amount": D(amount),
        "submitted_rate": D("0.0004"),
        "effective_rate": D("0.0004"),
        "period": 2,
        "offer_type": "LIMIT",
        "flags": 0,
        "strategy_version": "test",
        "pool": "short",
        "layer": "quick",
        "slice_key": f"test:short:quick:{index}",
    }


@pytest.mark.parametrize("alias", ["USDT", "UST", "fUST", "fUSDT", "usdt"])
def test_currency_aliases_do_not_use_derivatives(alias):
    assert normalize_currency(alias) == "USDT"
    assert currency_to_symbol(alias) == "fUST"
    assert symbol_to_currency(alias) == "USDT"
    assert normalize_currency("USTF0") == "USTF0"


def test_mixed_account_rows_are_filtered_and_normalized():
    client = FundingClient()
    assert parse_wallet_rows(client.wallets(), "USDT")[0]["balance"] == 700
    offer = [1, "fUST", 1, 2, "150", "150", "LIMIT", None, None, 0, "ACTIVE", None, None, None, "0.1", 2]
    assert parse_offer_rows([offer], "USDT")[0]["currency"] == "USDT"
    assert parse_offer_rows([offer], "USD") == []
    credit = [2, "fUST", 1, 1, 2, "150", None, "ACTIVE", "FIXED", None, None, "0.1", 2]
    assert parse_credit_rows([credit], "USDT")[0]["currency"] == "USDT"


def test_usdt_wallet_and_ledger_requests_use_ust(monkeypatch):
    client = Bitfinex("key", "secret")
    calls = []
    monkeypatch.setattr(client, "_auth_post", lambda path, payload: calls.append((path, payload)) or [])
    monkeypatch.setattr(client, "_auth_write_result", lambda path, payload: calls.append((path, payload)))
    client.ledgers("USDT")
    client.transfer_between_wallets_result("exchange", "funding", "USDT", "150")
    assert calls[0][0] == "v2/auth/r/ledgers/UST/hist"
    assert calls[1][1]["currency"] == calls[1][1]["currency_to"] == "UST"


def test_usdt_requires_own_floors_and_absolute_cap(tmp_path):
    path, settings = configuration(tmp_path, usdt=False)
    assert settings.enabled_currencies == ("USD",)
    assert settings.policies["USD"].short_floor_apr == D("0.01")
    assert settings.policies["USDT"].short_floor_apr is None
    with pytest.raises(ValueError, match="absolute funding cap"):
        validate_policy_v3(replace(policy("USDT"), max_lend_amount=None), require_live_floors=True)
    config = configparser.ConfigParser()
    config.read(path, encoding="utf-8")
    config.remove_section("STRATEGY_V4_USDT")
    assert build_settings(lendingbot.parse_args([]), config).enabled_currencies == ("USD",)


def test_shared_database_path_is_rejected(tmp_path):
    _, settings = configuration(tmp_path)
    settings.state_databases["USDT"] = settings.state_databases["USD"]
    with pytest.raises(ConfigError, match="different state database"):
        validate_settings(settings)


def test_currency_namespaces_preserve_same_ids_without_data_mixing(tmp_path):
    stores = {
        currency: LendingStateStore(tmp_path / f"{currency}.sqlite3", currency=currency) for currency in ("USD", "USDT")
    }
    for currency, store in stores.items():
        store.reserve_intent(order(currency), D("300"))
        store.upsert_market_trades(
            [
                {
                    "id": "same-id",
                    "mts": 100,
                    "amount": D("1"),
                    "rate": D("0.1" if currency == "USD" else "0.2"),
                    "period": 2,
                }
            ]
        )
        store.upsert_income_ledgers(
            [
                {
                    "id": 1,
                    "currency": currency,
                    "wallet": "funding",
                    "mts": 100,
                    "amount": D("2" if currency == "USD" else "3"),
                    "description": "interest",
                }
            ]
        )
    assert stores["USD"].market_trades()[0]["rate"] == D("0.1")
    assert stores["USDT"].market_trades()[0]["rate"] == D("0.2")
    assert stores["USDT"].realized_income("USDT") == 3
    with pytest.raises(StateStoreError):
        stores["USD"].reserve_intent(order("USDT"), D("300"))
    with pytest.raises(StateStoreError):
        LendingStateStore(stores["USD"].path, currency="USDT")


def test_v16_upgrade_backs_up_and_preserves_manual_pause_and_pending_intent(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    store = LendingStateStore(path)
    _, intent = store.reserve_intent(order("USD"), D("300"))
    store.mark_submitting(intent["id"])
    store.mark_ambiguous(intent["id"], "timeout")
    before = store.runtime()
    with store.transaction() as connection:
        connection.execute("UPDATE schema_meta SET value='16' WHERE key='schema_version'")
        connection.execute("DELETE FROM schema_meta WHERE key='currency'")
    config_path = tmp_path / "test.cfg"
    config_path.write_text("[BOT]\n", encoding="utf-8")
    upgraded = LendingStateStore(path, config_path=config_path)
    assert upgraded.runtime() == before
    assert upgraded.intent(intent["id"])["state"] == "AMBIGUOUS"
    assert list((tmp_path / "backups").glob("schema-v16-*.sqlite3"))
    assert list((tmp_path / "backups").glob("schema-v16-*.sqlite3.cfg"))[0].read_text(encoding="utf-8") == "[BOT]\n"


def test_native_minimum_is_context_local_and_rounds_up():
    minimum = usdt_minimum("0.99")
    assert minimum * D("0.99") >= 150
    with funding_sizing(minimum):
        assert evenly_distributed_amounts(D("300"), 2) == []
        with funding_sizing(D("150")):
            assert evenly_distributed_amounts(D("300"), 2) == [D("150"), D("150")]
    assert funding_minimum() == 150


def test_account_submission_budget_is_shared_and_durable(tmp_path):
    client = FundingClient()
    stores = {
        currency: LendingStateStore(
            tmp_path / f"{currency}.sqlite3", currency=currency, clock=lambda: client.now / 1000
        )
        for currency in ("USD", "USDT")
    }
    gate = FundingWriteGate(client, stores, lambda: client.now / 1000)
    for store in stores.values():
        store.set_mode("LIVE")
    for index in range(60):
        currency = "USD" if index < 30 else "USDT"
        stores[currency].reserve_intent(order(currency, index), D("100000"))
    stores["USDT"].reserve_intent(order("USDT", 61, "160"), D("100000"))
    result = gate.client_for("USDT").submit_funding_offer_result("fUST", "160", "0.0004", 2, "LIMIT")
    assert result.category == "ACCOUNT_SUBMISSION_BUDGET"
    assert client.submissions == []


def test_quote_failure_and_changed_minimum_never_submit(tmp_path):
    client = FundingClient()
    store = LendingStateStore(tmp_path / "usdt.sqlite3", currency="USDT")
    store.set_mode("LIVE")
    gate = FundingWriteGate(client, {"USDT": store})
    writer = gate.client_for("USDT")
    assert writer.submit_funding_offer_result("fUST", "150", "0.0004", 2, "LIMIT").category == "FUNDING_MINIMUM_CHANGED"
    client.bid = None
    gate.bid_at = None
    assert writer.submit_funding_offer_result("fUST", "200", "0.0004", 2, "LIMIT").category == "USDT_FX_STALE"
    assert client.submissions == []


def test_rest_and_websocket_snapshots_select_usdt_only():
    hub = BitfinexMarketDataHub("key", "secret", symbol="fUST")
    hub.handle_auth_message([0, "ws", FundingClient().wallets()])
    hub.handle_auth_message([0, "fos", []])
    hub.handle_auth_message([0, "fcs", []])
    hub.handle_auth_message([0, "fls", []])
    snapshot = hub.snapshot()
    assert {row["currency"] for row in snapshot["wallets"]} == {"USDT"}
    assert LendingRuntimeV3._account(snapshot, "USDT")["total"] == 700


def coordinator(tmp_path, monkeypatch):
    _, settings = configuration(tmp_path)
    client = FundingClient()
    stores = stores_for_profiles(settings, lambda: client.now / 1000)
    for store in stores.values():
        store.set_mode("LIVE")
    monkeypatch.setattr(BitfinexMarketDataHub, "start", lambda _self: None)
    monkeypatch.setattr(LendingRuntimeV3, "start_income_history_sync", lambda _self: None)
    runtime = V4Coordinator(
        client, stores, {currency: policy(currency) for currency in stores}, settings, clock=lambda: client.now / 1000
    )
    return runtime, client, stores


def test_both_currencies_submit_only_to_their_own_funding_market(tmp_path, monkeypatch):
    runtime, client, stores = coordinator(tmp_path, monkeypatch)
    statuses = runtime.cycle()
    assert set(statuses) == {"USD", "USDT"}
    assert {symbol for symbol, _amount in client.submissions} == {"fUSD", "fUST"}
    assert sum(amount for symbol, amount in client.submissions if symbol == "fUSD") <= 1000
    assert sum(amount for symbol, amount in client.submissions if symbol == "fUST") <= 700
    assert all(amount >= usdt_minimum(client.bid) for symbol, amount in client.submissions if symbol == "fUST")
    for currency, store in stores.items():
        assert {row["currency"] for row in store.intents()} == {currency}


def test_market_failure_pauses_only_affected_currency(tmp_path, monkeypatch):
    runtime, client, stores = coordinator(tmp_path, monkeypatch)
    client.failure = "fUST"
    runtime.cycle()
    assert stores["USD"].runtime()["mode"] == "LIVE"
    assert stores["USDT"].runtime()["mode"] == "PAUSED"
    assert {symbol for symbol, _amount in client.submissions} == {"fUSD"}


def test_global_pause_requires_all_recovery_barriers(tmp_path, monkeypatch):
    runtime, client, stores = coordinator(tmp_path, monkeypatch)
    runtime.pause_all(BitfinexApiError("auth expired", category="AUTH_PERMISSION"))
    runtime.cycle()
    assert client.submissions == []
    assert runtime.gate.global_block
    client.now += 31_000
    runtime.cycle()
    assert client.submissions == []
    client.now += 31_000
    runtime.cycle()
    assert client.submissions == []
    client.now += 31_000
    runtime.cycle()
    assert {symbol for symbol, _amount in client.submissions} == {"fUSD", "fUST"}


def test_manual_pause_does_not_resume_other_currency(tmp_path, monkeypatch):
    runtime, client, stores = coordinator(tmp_path, monkeypatch)
    stores["USDT"].set_mode("PAUSED", "dashboard_pause")
    runtime.cycle()
    assert stores["USDT"].runtime()["mode"] == "PAUSED"
    assert {symbol for symbol, _amount in client.submissions} == {"fUSD"}


def test_manual_pause_during_unknown_write_preserves_evidence_and_revokes_resume(tmp_path):
    store = LendingStateStore(tmp_path / "usdt.sqlite3", currency="USDT")
    store.set_mode("LIVE")
    _, intent = store.reserve_intent(order("USDT"), D("500"))
    store.mark_submitting(intent["id"])
    store.mark_ambiguous(intent["id"], "transport timeout")
    store.pause_currency()
    assert store.intents()[0]["state"] == "AMBIGUOUS"
    assert store.runtime()["previous_mode"] == "PAUSED"
    assert store.recovery_status()["targetMode"] == "PAUSED"


@pytest.mark.parametrize("currency", ["USD", "USDT"])
@pytest.mark.parametrize("reason", [
    "ADAPTIVE_DATA_UNAVAILABLE", "ADAPTIVE_MODEL_NOT_QUALIFIED", "ADAPTIVE_JOURNAL_FAILED"
])
def test_fresh_preflight_can_resume_inactive_adaptive_pause(tmp_path, currency, reason):
    store = LendingStateStore(tmp_path / (currency + ".sqlite3"), currency=currency)
    store.set_mode("LIVE")
    store.enter_protected_pause(reason)
    store.pause_currency()
    store.authorize_live_after_preflight()
    assert store.runtime()["mode"] == "PAUSED"
    assert store.runtime()["safe_reason"] == reason
    store.authorize_live_after_preflight(revalidated_adaptive=True)
    assert store.runtime()["mode"] == "LIVE"
    assert store.runtime()["safe_reason"] is None


@pytest.mark.parametrize("kind", ["manual", "active_recovery", "other_reason"])
def test_adaptive_revalidation_preserves_other_safety_barriers(tmp_path, kind):
    store = LendingStateStore(tmp_path / "usd.sqlite3")
    store.set_mode("LIVE")
    reason = "OTHER_SAFETY_BARRIER" if kind == "other_reason" else "ADAPTIVE_DATA_UNAVAILABLE"
    store.enter_protected_pause(reason, manual=kind == "manual")
    if kind == "manual":
        with pytest.raises(StateStoreError, match="manual"):
            store.authorize_live_after_preflight(revalidated_adaptive=True)
    else:
        if kind == "active_recovery":
            store.begin_recovery("WORKER_EXIT", "interrupted", origin_mode="LIVE", target_mode="LIVE")
        store.authorize_live_after_preflight(revalidated_adaptive=True)
    assert store.runtime()["mode"] == "PAUSED"
    assert store.runtime()["safe_reason"] == reason


@pytest.mark.parametrize("currency", ["USD", "USDT"])
def test_currency_start_clears_only_revalidated_adaptive_pause(tmp_path, monkeypatch, currency):
    path, settings = configuration(tmp_path)
    client = FundingClient()
    context = AppContext.for_project(
        tmp_path, config_path=str(path), client_factory=lambda *_args: client, now=lambda: client.now / 1000
    )
    service = V4DashboardService(str(path), context.status_path, context)
    stores = stores_for_profiles(settings, context.now)
    for coin, store in stores.items():
        store.save_strategy(json_decimal(policy(coin).__dict__), "ACTIVE")
        store.set_mode("LIVE")
        store.enter_protected_pause("ADAPTIVE_DATA_UNAVAILABLE")
        store.pause_currency()
    preflight = service.preflight([currency])
    assert preflight["canStart"]
    monkeypatch.setattr(lendingbot, "controlled_bot_running", lambda *_: True)
    monkeypatch.setattr(lendingbot.LiveProcessLock, "inspect", lambda *_: {"metadata": {"v4": True}})
    monkeypatch.setattr(lendingbot, "controlled_bot_status", lambda *_: {"running": True})
    service.start(preflight["preflightId"], [currency])
    assert stores[currency].runtime()["mode"] == "LIVE"
    other = "USDT" if currency == "USD" else "USD"
    assert stores[other].runtime()["mode"] == "PAUSED"
    assert client.submissions == []


def test_usdt_history_sync_and_statistics_do_not_use_usd(tmp_path):
    client = FundingClient()
    store = LendingStateStore(tmp_path / "usdt.sqlite3", currency="USDT", clock=lambda: client.now / 1000)
    runtime = LendingRuntimeV3(client, policy("USDT"), store, hub=object(), clock=lambda: client.now / 1000)
    runtime.sync_income_history_once()
    assert store.realized_income("USDT") == D("2.5")
    assert store.realized_income("USD") == 0
    assert D(store.statistics()["netInterest"]) == D("2.5")


def test_paused_usdt_strategy_can_activate_while_usd_worker_runs(tmp_path, monkeypatch):
    path, _ = configuration(tmp_path)
    client = FundingClient()
    context = AppContext.for_project(
        tmp_path, config_path=str(path), client_factory=lambda *_args: client, now=lambda: client.now / 1000
    )
    payload = {"strategyV3": {"max_lend_percent": "80"}}
    preview = lendingbot.strategy_v3_preview(str(path), payload, app_context=context, currency="USDT", v4=True)
    saved = lendingbot.save_strategy_v3_draft(
        str(path), {**payload, "previewToken": preview["previewToken"]}, app_context=context, currency="USDT", v4=True
    )
    monkeypatch.setattr(lendingbot, "controlled_bot_running", lambda *_args: True)
    applied = lendingbot.apply_strategy_v3_draft(str(path), saved, app_context=context, currency="USDT")
    assert applied["status"] == "ACTIVE"
    store, _ = lendingbot.v3_store_for_config(str(path), "USDT")
    assert store.strategy("PENDING") is None
    assert store.strategy("ACTIVE")["policy"]["max_lend_percent"] == "80"


def test_failed_global_permission_probe_keeps_both_currencies_blocked(tmp_path, monkeypatch):
    runtime, client, stores = coordinator(tmp_path, monkeypatch)
    runtime.pause_all(BitfinexApiError("auth expired", category="AUTH_PERMISSION"))
    runtime.cycle()
    client.now += 31_000
    runtime.cycle()
    client.now += 31_000
    monkeypatch.setattr(client, "key_permissions", lambda: [["wallets", 1, 0], ["funding", 1, 0]])
    runtime.cycle()
    client.now += 31_000
    runtime.cycle()
    assert runtime.gate.global_block
    assert client.submissions == []


def test_preflight_is_bound_to_currency_and_account_and_does_not_write(tmp_path, monkeypatch):
    path, _ = configuration(tmp_path)
    client = FundingClient()
    context = AppContext.for_project(
        tmp_path, config_path=str(path), client_factory=lambda *_args: client, now=lambda: client.now / 1000
    )
    service = V4DashboardService(str(path), context.status_path, context)
    result = service.preflight(["USDT"])
    assert result["canStart"]
    assert result["summary"]["account"]["total"] == "700"
    assert client.submissions == []
    monkeypatch.setattr(service, "_launch", lambda _selected: pytest.fail("must not launch"))
    with pytest.raises(ConfigError, match="预检"):
        service.start(result["preflightId"], ["USD"])
    result = service.preflight(["USDT"])
    client.available["USDT"] -= 1
    with pytest.raises(ConfigError, match="账户"):
        service.start(result["preflightId"], ["USDT"])


def test_currency_bound_strategy_tokens_and_new_v4_records(tmp_path):
    path, _ = configuration(tmp_path)
    client = FundingClient()
    context = AppContext.for_project(
        tmp_path, config_path=str(path), client_factory=lambda *_args: client, now=lambda: client.now / 1000
    )
    preview = lendingbot.strategy_v3_preview(
        str(path), {"strategyV3": {"max_lend_percent": "80"}}, app_context=context, currency="USDT", v4=True
    )
    with pytest.raises(lendingbot.ApiRequestError, match="预览"):
        lendingbot.save_strategy_v3_draft(
            str(path),
            {"strategyV3": {"max_lend_percent": "80"}, "previewToken": preview["previewToken"]},
            app_context=context,
            currency="USD",
            v4=True,
        )
    preview = lendingbot.strategy_v3_preview(
        str(path), {"strategyV3": {"max_lend_percent": "80"}}, app_context=context, currency="USDT", v4=True
    )
    saved = lendingbot.save_strategy_v3_draft(
        str(path),
        {"strategyV3": {"max_lend_percent": "80"}, "previewToken": preview["previewToken"]},
        app_context=context,
        currency="USDT",
        v4=True,
    )
    assert saved["strategy"]["policy"]["version"] == 4
    assert saved["strategy"]["policy"]["currency"] == "USDT"


def test_watchdog_respects_currency_pause_and_config_authorization(tmp_path, monkeypatch):
    path, settings = configuration(tmp_path)
    client = FundingClient()
    context = AppContext.for_project(
        tmp_path, config_path=str(path), client_factory=lambda *_args: client, now=lambda: client.now / 1000
    )
    service = V4DashboardService(str(path), context.status_path, context)
    stores = stores_for_profiles(settings, context.now)
    versions = {}
    for currency, store in stores.items():
        versions[currency] = store.save_strategy(json_decimal(policy(currency).__dict__), "ACTIVE")
        store.set_mode("LIVE")
    context.process_state.supervisor_session = "session"
    context.process_state.auto_restart_authorization = {
        "v4": True,
        "session": "session",
        "currencies": ["USD", "USDT"],
        "configDigest": lendingbot.config_sha256(str(path)),
        "buildId": lendingbot.worker_build_id(),
        "strategies": versions,
        "authorizedAt": context.now(),
    }
    service.pause("USDT")
    assert context.process_state.auto_restart_authorization["currencies"] == ["USD"]
    monkeypatch.setattr(lendingbot, "controlled_bot_status", lambda *_args: {"running": False})
    context.process_state.auto_restart_authorization["configDigest"] = "different"
    supervisor_tick(str(path), context.status_path, context)
    assert context.process_state.auto_restart_authorization is None
    assert stores["USDT"].runtime()["mode"] == "PAUSED"


def test_v4_http_currency_validation_csrf_and_usd_compatibility(tmp_path):
    path, _ = configuration(tmp_path)
    client = FundingClient()
    context = AppContext.for_project(
        tmp_path, config_path=str(path), client_factory=lambda *_args: client, now=lambda: client.now / 1000
    )
    handler = lendingbot.make_dashboard_handler(str(tmp_path), str(path), context.status_path, context=context)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        for endpoint in ("/api/runtime/v4", "/api/stats/v4"):
            with request.urlopen(base + endpoint) as response:
                assert set(json.load(response)["currencies"]) == {"USD", "USDT"}
        with request.urlopen(base + "/api/status/v4?currency=USDT") as response:
            status = json.load(response)
            assert status["currency"] == "USDT"
            assert status["releaseComparison"]["version"] == "4.0.0"
        with request.urlopen(base + "/api/runtime/v3") as response:
            assert json.load(response)["policy"]["currency"] == "USD"
        body = json.dumps({"currency": "USDT"}).encode()
        with pytest.raises(error.HTTPError):
            request.urlopen(
                request.Request(base + "/api/control/v4/preflight", body, headers={"Content-Type": "application/json"})
            )
        with request.urlopen(
            request.Request(
                base + "/api/control/v4/preflight",
                body,
                headers={
                    "Content-Type": "application/json",
                    "X-Mika-CSRF": handler.csrf_token,
                    "Origin": "http://127.0.0.1:8000",
                    "Host": "127.0.0.1:8000",
                },
            )
        ) as response:
            assert json.load(response)["canStart"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_confirmed_cli_worker_runs_both_coins_once_and_preserves_pause(tmp_path, monkeypatch):
    from Logger import Logger

    path, settings = configuration(tmp_path)
    client = FundingClient()
    context = AppContext.for_project(
        tmp_path, config_path=str(path), client_factory=lambda *_args: client, now=lambda: client.now / 1000
    )
    monkeypatch.setattr(BitfinexMarketDataHub, "start", lambda _self: None)
    monkeypatch.setattr(LendingRuntimeV3, "start_income_history_sync", lambda _self: None)
    log = Logger(context.status_path, 20)
    args = lendingbot.parse_args(
        ["--config", str(path), "--live", "--confirmed-preflight", "--currencies", "USD,USDT", "--once", "--no-server"]
    )
    run_worker(args, settings, context, log)
    published = json.loads(open(context.status_path, encoding="utf-8").read())
    assert {row[0] for row in client.submissions} == {"fUSD", "fUST"}
    assert set(published["currencies"]) == {"USD", "USDT"}
    assert isinstance(published["releaseComparison"]["after"]["submitted"][0]["amount"], str)
    for store in stores_for_profiles(settings, context.now).values():
        assert store.runtime()["mode"] == "PAUSED"


def test_watchdog_renews_only_confirmed_policy_mirror(tmp_path, monkeypatch):
    path, settings = configuration(tmp_path)
    client = FundingClient()
    context = AppContext.for_project(tmp_path, config_path=str(path), now=lambda: client.now / 1000)
    stores = stores_for_profiles(settings, context.now)
    for currency, store in stores.items():
        store.save_strategy(json_decimal(settings.policies[currency].__dict__), "ACTIVE")
        store.touch_heartbeat()
    authorization = {
        "v4": True,
        "session": context.process_state.supervisor_session,
        "currencies": ["USD"],
        "authorizedAt": context.now(),
        "configDigest": lendingbot.config_sha256(str(path)),
        "controlDigest": restart_control_digest(str(path)),
        "buildId": lendingbot.worker_build_id(),
        "strategies": {"USD": stores["USD"].strategy("ACTIVE")["version_id"]},
    }
    context.process_state.auto_restart_authorization = authorization
    changed = replace(settings.policies["USD"], max_lend_percent=D("80"))
    stores["USD"].save_strategy(json_decimal(changed.__dict__), "ACTIVE")
    lendingbot.mirror_active_strategy_v3(str(path), changed)
    monkeypatch.setattr(lendingbot, "controlled_bot_status", lambda *_args: {"running": True})
    supervisor_tick(str(path), context.status_path, context)
    assert authorization["configDigest"] == lendingbot.config_sha256(str(path))
    assert authorization["strategies"]["USD"] == stores["USD"].strategy("ACTIVE")["version_id"]
    original_control = authorization["controlDigest"]
    path.write_text(path.read_text(encoding="utf-8").replace("fixture-secret", "different-secret"), encoding="utf-8")
    assert restart_control_digest(str(path)) != original_control


def test_settings_autosave_is_currency_scoped_and_preserves_live_usd(tmp_path):
    path, settings = configuration(tmp_path)
    client = FundingClient()
    context = AppContext.for_project(tmp_path, config_path=str(path), client_factory=lambda *_args: client)
    stores = stores_for_profiles(settings)
    stores["USD"].set_mode("LIVE")
    service = V4DashboardService(str(path), context.status_path, context)
    result = service.settings({"currency": "USDT", "enabled": False, "autoTransfer": True})
    assert result["enabled"] is False and result["autoTransfer"] is True
    assert stores["USD"].runtime()["mode"] == "LIVE"
    with pytest.raises(ConfigError, match="先暂停"):
        service.settings({"currency": "USD", "enabled": False, "autoTransfer": True})
    preflight = service.preflight(["USDT"])
    assert preflight["canStart"] is False
    assert "尚未启用" in preflight["checks"][0]["detail"]
    assert "account" not in preflight["summary"]
    assert client.submissions == []


def test_dashboard_pause_is_single_currency_and_stop_pauses_both(tmp_path, monkeypatch):
    path, settings = configuration(tmp_path)
    context = AppContext.for_project(tmp_path, config_path=str(path))
    stores = stores_for_profiles(settings)
    for store in stores.values():
        store.set_mode("LIVE")
    context.process_state.auto_restart_authorization = {"v4": True, "currencies": ["USD", "USDT"]}
    stopped = []
    monkeypatch.setattr(lendingbot, "stop_controlled_bot", lambda *_args, **_kwargs: stopped.append(True))
    service = V4DashboardService(str(path), context.status_path, context)
    service.pause("USDT")
    assert stores["USDT"].runtime()["mode"] == "PAUSED"
    assert stores["USD"].runtime()["mode"] == "LIVE"
    assert context.process_state.auto_restart_authorization["currencies"] == ["USD"]
    assert stopped == []
    stores["USDT"].set_mode("LIVE")
    service.stop()
    assert stopped == [True]
    assert all(store.runtime()["mode"] == "PAUSED" for store in stores.values())
