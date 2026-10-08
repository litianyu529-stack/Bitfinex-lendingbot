"""V4 coordinator: independent currency runtimes with one account write gate."""

import sqlite3
import threading
import time
from decimal import Decimal

from bitfinex import BitfinexApiError
from Currency import funding_sizing, normalize_currency, usdt_minimum
from DomainTypes import WriteOutcome, WriteResult
from MarketDataStream import BitfinexMarketDataHub
from RuntimeV3 import LendingRuntimeV3
from Configuration import strategy_v3_from_record


class FundingWriteGate:
    def __init__(self, client, stores, clock=time.time):
        self.client = client
        self.stores = stores
        self.clock = clock
        self.lock = threading.RLock()
        self.global_block = False
        self.global_error = None
        self.bid = None
        self.bid_at = None
        self.require_wallet_write = False

    def probe_permissions(self):
        from lendingbot import parse_key_permissions, permission_enabled

        permissions = parse_key_permissions(self.client.key_permissions())
        required = [("wallets", "read"), ("funding", "read"), ("funding", "write")]
        if self.require_wallet_write:
            required.append(("wallets", "write"))
        if (
            not all(permission_enabled(permissions, scope, access) for scope, access in required)
            or permission_enabled(permissions, "withdraw", "write")
            or permission_enabled(permissions, "ui_withdraw", "write")
        ):
            raise BitfinexApiError("account permissions do not satisfy LIVE requirements", category="AUTH_PERMISSION")

    def minimum(self, currency):
        if currency == "USD":
            return Decimal("150")
        with self.lock:
            now = self.clock()
            if self.bid_at is None or now - self.bid_at >= 30:
                try:
                    bid = self.client.ticker("tUSTUSD")[0]
                    minimum = usdt_minimum(bid)
                except Exception as exc:
                    self.bid_at = None
                    raise BitfinexApiError(str(exc), category="USDT_FX_STALE", retryable=True) from exc
                self.bid = bid
                self.bid_at = self.clock()
                return minimum
            if self.clock() - self.bid_at > 60:
                raise BitfinexApiError("USDT/USD quote is stale", category="USDT_FX_STALE", retryable=True)
            return usdt_minimum(self.bid)

    def client_for(self, currency):
        return CurrencyFundingClient(self, currency)


class CurrencyFundingClient:
    """Only the coordinator supplies authenticated clients to currency runtimes."""

    def __init__(self, gate, currency):
        self.gate = gate
        self.currency = currency

    def __getattr__(self, name):
        value = getattr(self.gate.client, name)
        if not callable(value):
            return value

        def call(*args, **kwargs):
            try:
                return value(*args, **kwargs)
            except BitfinexApiError as exc:
                if exc.category == "AUTH_PERMISSION":
                    self.gate.global_block = True
                    self.gate.global_error = exc
                raise

        return call

    def _write(self, method, *args, amount=None, **kwargs):
        with self.gate.lock:
            if self.gate.global_block or self.gate.stores[self.currency].runtime()["mode"] != "LIVE":
                return WriteResult(WriteOutcome.DEFINITE_REJECT, error="funding writes are paused", category="PAUSED")
            if amount is not None:
                try:
                    minimum = self.gate.minimum(self.currency)
                except BitfinexApiError as exc:
                    # No submission happened. Do not create an ambiguous intent for a failed quote read.
                    return WriteResult(WriteOutcome.DEFINITE_REJECT, error=str(exc), category=exc.category)
                if Decimal(str(amount)) < minimum:
                    return WriteResult(
                        WriteOutcome.DEFINITE_REJECT,
                        error="amount below current funding minimum",
                        category="FUNDING_MINIMUM_CHANGED",
                    )
                attempts = sum(
                    store.submission_attempt_count_since(int(self.gate.clock() * 1000) - 60_000, currency)
                    for currency, store in self.gate.stores.items()
                )
                # The current intent was durably reserved before entering this gate.
                if attempts > 60:
                    return WriteResult(
                        WriteOutcome.DEFINITE_REJECT,
                        error="account submission budget exhausted",
                        category="ACCOUNT_SUBMISSION_BUDGET",
                    )
            result = getattr(self.gate.client, method)(*args, **kwargs)
            if result.category == "AUTH_PERMISSION":
                self.gate.global_block = True
                self.gate.global_error = BitfinexApiError(result.error, category=result.category)
            return result

    def submit_funding_offer_result(self, symbol, amount, *args, **kwargs):
        if normalize_currency(symbol) != self.currency:
            raise ValueError("funding submit currency does not match runtime")
        return self._write("submit_funding_offer_result", symbol, amount, *args, amount=amount, **kwargs)

    def cancel_funding_offer_result(self, offer_id):
        offer = next(
            (
                row
                for row in self.gate.stores[self.currency].offers(active_only=True)
                if int(row["offer_id"]) == int(offer_id)
            ),
            None,
        )
        if offer is None or not offer["managed"]:
            raise ValueError("cannot cancel an offer without currency-specific ownership")
        return self._write("cancel_funding_offer_result", offer_id)

    def transfer_between_wallets_result(self, source, destination, currency, amount):
        if normalize_currency(currency) != self.currency:
            raise ValueError("wallet transfer currency does not match runtime")
        return self._write("transfer_between_wallets_result", source, destination, currency, amount)


class V4Coordinator:
    def __init__(self, client, stores, policies, settings, on_policy_activated=None, clock=time.time):
        self.clock = clock
        self.stores = stores
        self.gate = FundingWriteGate(client, stores, clock)
        self.gate.require_wallet_write = bool(settings.transferable_currencies)
        self.runtimes = {}
        self.statuses = {}
        self.auth_hub = BitfinexMarketDataHub(
            client.api_key, client.api_secret, enable_public=False, auth_symbols=("fUSD", "fUST")
        )
        for currency, store in stores.items():
            policy = policies[currency]
            hub = BitfinexMarketDataHub(
                client.api_key,
                client.api_secret,
                symbol="fUST" if currency == "USDT" else "fUSD",
                store=store,
                fallback_seconds=policy.ws_fallback_seconds,
                rest_stale_seconds=policy.rest_stale_seconds,
                enable_auth=False,
            )
            # Public connections stay separate; authenticated account events are fanned out below.
            self.runtimes[currency] = LendingRuntimeV3(
                self.gate.client_for(currency),
                policy,
                store,
                hub=hub,
                clock=clock,
                auto_transfer_wallets=settings.transfer_from_wallets
                if currency in settings.transferable_currencies
                else (),
                on_policy_activated=on_policy_activated,
            )
        for store in stores.values():
            if str(store.recovery_status().get("category") or "").startswith("GLOBAL_"):
                self.gate.global_block = True

    def _pause(self, store, exc, category):
        runtime = store.runtime()
        recovery = store.recovery_status()
        target = recovery.get("targetMode") or runtime["mode"]
        if runtime["mode"] != "PAUSED":
            store.set_mode("PAUSED", f"AUTO_RECOVERY:{category}")
        store.begin_recovery(category, str(exc), origin_mode=target, target_mode=target)
        store.record_recovery_failure(str(exc), category)

    def pause_all(self, exc):
        self.gate.global_block = True
        self.gate.global_error = None
        category = "GLOBAL_DATABASE" if isinstance(exc, sqlite3.Error) else "GLOBAL_ACCOUNT"
        for store in self.stores.values():
            try:
                self._pause(store, exc, category)
            except sqlite3.Error:
                pass

    def _fanout_account(self):
        source = self.auth_hub
        with source._lock:
            for runtime in self.runtimes.values():
                target = runtime.hub
                with target._lock:
                    for name in ("_wallets", "_offers", "_credits", "_loans"):
                        # A reconnect clears readiness, but must not replace fresh REST fallback
                        # with a partial authenticated snapshot.
                        readiness = {
                            "_wallets": "_wallet_snapshot_ready",
                            "_offers": "_offers_snapshot_ready",
                            "_credits": "_credits_snapshot_ready",
                            "_loans": "_loans_snapshot_ready",
                        }[name]
                        if getattr(source, readiness):
                            rows = {
                                key: dict(row)
                                for key, row in getattr(source, name).items()
                                if row and normalize_currency(row.get("currency")) == runtime.currency
                            }
                            if name in {"_offers", "_credits", "_loans"}:
                                persisted = runtime.store.offers() if name == "_offers" else runtime.store.credits()
                                id_field = "offer_id" if name == "_offers" else "credit_id"
                                owned = {int(row[id_field]): row for row in persisted}
                                for key, row in rows.items():
                                    metadata = owned.get(int(key), {})
                                    for field in ("managed", "pool", "layer", "display_type"):
                                        if field in metadata:
                                            row[field] = metadata[field]
                            setattr(target, name, rows)
                    for name in (
                        "_auth_connected",
                        "_auth_generation",
                        "_auth_last_message_ms",
                        "_auth_disconnected_since_ms",
                        "_wallet_snapshot_ready",
                        "_offers_snapshot_ready",
                        "_credits_snapshot_ready",
                        "_loans_snapshot_ready",
                    ):
                        setattr(target, name, getattr(source, name))
                    target._funding_trades = source._funding_trades.copy()

    def cycle(self):
        try:
            self._fanout_account()
        except Exception as exc:
            self.pause_all(exc)
            return self.statuses
        for currency, runtime in self.runtimes.items():
            try:
                runtime.store.touch_heartbeat()
                active = runtime.store.strategy("ACTIVE")
                if active is not None:
                    runtime.policy = strategy_v3_from_record(active)
                    runtime._apply_policy_runtime_settings()
                if (
                    runtime.store.runtime()["mode"] != "LIVE"
                    and not runtime.store.recovery_status()["active"]
                    and not runtime._bootstrapped
                ):
                    continue
                minimum = self.gate.minimum(currency)
                with funding_sizing(minimum):
                    status = runtime.cycle()
                status.update(
                    currency=currency,
                    minimumOrderAmount=str(minimum),
                    version="4.0.0",
                    releaseComparison=runtime.store.release_comparison_v4(),
                    last_update=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.clock())),
                )
                self.statuses[currency] = status
            except sqlite3.Error as exc:
                self.pause_all(exc)
                break
            except Exception as exc:
                if getattr(exc, "category", "") == "AUTH_PERMISSION":
                    self.pause_all(exc)
                else:
                    self._pause(runtime.store, exc, getattr(exc, "category", "CURRENCY_RUNTIME"))
                self.statuses[currency] = {
                    **self.statuses.get(currency, {}),
                    "schemaVersion": 3,
                    "currency": currency,
                    "snapshotAvailable": False,
                    "last_update": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.clock())),
                }
            if self.gate.global_error is not None:
                self.pause_all(self.gate.global_error)
                break
        if self.gate.global_block:
            # Only a fresh account permission probe plus every durable recovery barrier
            # can open the account gate. A paused currency cannot unlock another one.
            try:
                if all(not store.recovery_status()["active"] for store in self.stores.values()):
                    self.gate.probe_permissions()
                    self.gate.global_block = False
            except Exception:
                pass
        return self.statuses

    def start(self):
        self.auth_hub.start()

    def shutdown(self):
        self.auth_hub.stop()
        for runtime in self.runtimes.values():
            runtime.shutdown()
