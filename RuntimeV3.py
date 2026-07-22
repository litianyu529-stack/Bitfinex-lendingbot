import threading
import time
from decimal import Decimal

from bitfinex import BitfinexAmbiguousWriteError, BitfinexApiError, currency_to_symbol
from MarketDataStream import BitfinexMarketDataHub
from StateStore import InsufficientReservedBalance, LendingStateStore
from StrategyEngine import extract_submitted_offer_id, parse_funding_stats, parse_funding_trades
from StrategyV3 import (
    D,
    POOLS,
    StrategyPolicyV3,
    build_market_signals_v3,
    build_strategy_plan_v3,
    gross_daily_floor,
    json_decimal,
    net_apr_from_daily,
    pool_for_period,
    policy_v3_with_overrides,
    replay_strategy_v3,
    validate_policy_v3,
)


def parse_book_v3(rows):
    result = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 4:
            continue
        try:
            result.append({
                "rate": D(str(row[0])),
                "period": int(row[1]),
                "count": int(row[2]),
                "amount": D(str(row[3])),
            })
        except (TypeError, ValueError, ArithmeticError):
            continue
    return result


def parse_wallet_rows_v3(rows):
    result = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        wallet_type = str(row[0]).lower()
        currency = str(row[1]).upper()
        if currency != "USD":
            continue
        balance = D(str(row[2]))
        available = balance if len(row) < 5 or row[4] is None else D(str(row[4]))
        result.append({
            "wallet_type": wallet_type,
            "currency": currency,
            "balance": balance,
            "available": available,
            "unsettled_interest": D(str(row[3] or 0)) if len(row) > 3 else D("0"),
        })
    return result


def parse_offer_rows_v3(rows):
    result = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 16:
            continue
        try:
            result.append({
                "id": int(row[0]),
                "currency": "USD",
                "mts_created": int(row[2] or 0),
                "mts_updated": int(row[3] or 0),
                "amount": abs(D(str(row[4]))),
                "amount_original": abs(D(str(row[5]))),
                "offer_type": str(row[6]),
                "flags": int(row[9] or 0),
                "status": str(row[10]),
                "rate": D(str(row[14])),
                "period": int(row[15]),
                "hidden": bool(row[17]) if len(row) > 17 else bool(int(row[9] or 0) & 64),
                "rate_real": D(str(row[20])) if len(row) > 20 and row[20] is not None else None,
            })
        except (TypeError, ValueError, ArithmeticError):
            continue
    return result


def parse_credit_rows_v3(rows):
    result = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 13:
            continue
        try:
            result.append({
                "id": int(row[0]),
                "currency": "USD",
                "mts_created": int(row[3] or 0),
                "mts_updated": int(row[4] or 0),
                "amount": abs(D(str(row[5]))),
                "status": str(row[7]),
                "rate_type": str(row[8]) if len(row) > 8 and row[8] is not None else None,
                "rate": D(str(row[11])),
                "period": int(row[12]),
                "mts_opening": int(row[13] or 0) if len(row) > 13 else 0,
                "hidden": bool(row[16]) if len(row) > 16 else False,
                "rate_real": D(str(row[19])) if len(row) > 19 and row[19] is not None else None,
            })
        except (TypeError, ValueError, ArithmeticError):
            continue
    return result


def parse_funding_trade_rows_v3(rows):
    result = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            continue
        try:
            result.append({
                "id": int(row[0]),
                "currency": "USD",
                "mts": int(row[2]),
                "offer_id": int(row[3]),
                "amount": abs(D(str(row[4]))),
                "rate": D(str(row[5])),
                "period": int(row[6]),
            })
        except (TypeError, ValueError, ArithmeticError):
            continue
    return result


def parse_ledger_rows_v3(rows):
    result = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 9:
            continue
        try:
            result.append({
                "id": int(row[0]),
                "currency": str(row[1]).upper(),
                "wallet": None if row[2] is None else str(row[2]).lower(),
                "mts": int(row[3]),
                "amount": D(str(row[5])),
                "balance": None if row[6] is None else D(str(row[6])),
                "description": str(row[8] or ""),
            })
        except (TypeError, ValueError, ArithmeticError):
            continue
    return result


class LendingRuntimeV3:
    def __init__(
        self, client, policy, store, log=None, hub=None, legacy_state_path=None,
        auto_transfer_wallets=(), on_policy_activated=None,
    ):
        self.client = client
        self.policy = validate_policy_v3(policy)
        self.store = store
        self.log = log
        self.hub = hub or BitfinexMarketDataHub(
            client.api_key,
            client.api_secret,
            symbol="fUSD",
            store=store,
            fallback_seconds=policy.ws_fallback_seconds,
            rest_stale_seconds=policy.rest_stale_seconds,
        )
        self.legacy_state_path = legacy_state_path
        self.auto_transfer_wallets = tuple(str(item).lower() for item in auto_transfer_wallets)
        self.on_policy_activated = on_policy_activated
        self._last_rest_sync_ms = 0
        self._last_history_sync_ms = 0
        self._stats = []
        self._bootstrapped = False
        self._income_sync_stop = threading.Event()
        self._income_sync_thread = None
        self._pending_cancel_requested = set()

    def _log(self, message):
        if self.log is not None:
            self.log.log(message)

    @staticmethod
    def _offer_display_type(offer):
        display = str(offer.get("display_type") or "").upper()
        if display:
            return display
        offer_type = str(offer.get("offer_type") or "LIMIT").upper()
        if offer_type == "FRRDELTAFIX":
            return "FRR_DELTA_FIXED"
        if offer_type == "FRRDELTAVAR":
            return "FRR" if D(offer.get("rate") or 0) == 0 else "FRR_DELTA_VARIABLE"
        return offer_type

    @staticmethod
    def _display_type_enabled(policy, display_type):
        return {
            "LIMIT": policy.enable_limit,
            "FRR": policy.enable_frr,
            "FRR_DELTA_FIXED": policy.enable_frr_delta_fixed,
            "FRR_DELTA_VARIABLE": policy.enable_frr_delta_variable,
        }.get(str(display_type).upper(), False)

    def _apply_policy_runtime_settings(self):
        self.hub.fallback_ms = int(self.policy.ws_fallback_seconds) * 1000
        self.hub.rest_stale_ms = int(self.policy.rest_stale_seconds) * 1000

    def bootstrap(self, start_websocket=True):
        if self._bootstrapped:
            return
        if self.legacy_state_path:
            imported = self.store.import_legacy_managed_offers(self.legacy_state_path)
            if imported:
                self._log(f"v3 已导入 {imported} 个旧版托管挂单标记。")
        if self.store.strategy("ACTIVE") is None:
            self.store.save_strategy(json_decimal(self.policy.__dict__), status="ACTIVE")
        else:
            self.policy = validate_policy_v3(policy_v3_with_overrides(StrategyPolicyV3(), self.store.strategy("ACTIVE")["policy"]))
        self._apply_policy_runtime_settings()
        self.sync_rest(include_history=True)
        if start_websocket:
            self.hub.start()
        self.start_income_history_sync()
        self._bootstrapped = True

    def shutdown(self):
        self._income_sync_stop.set()
        if self._income_sync_thread is not None:
            self._income_sync_thread.join(timeout=5)
            self._income_sync_thread = None
        self.hub.stop()

    def start_income_history_sync(self):
        if self._income_sync_thread is not None and self._income_sync_thread.is_alive():
            return
        self._income_sync_stop.clear()
        self._income_sync_thread = threading.Thread(
            target=self._income_history_worker,
            name="bitfinex-income-history",
            daemon=True,
        )
        self._income_sync_thread.start()

    def _income_history_worker(self):
        """Backfill ledgers without making a statistics outage a trading outage."""
        while not self._income_sync_stop.is_set():
            try:
                state = self.store.income_sync_state("USD")
                self.sync_income_history_once()
                updated = self.store.income_sync_state("USD")
                if updated["status"] == "COMPLETE":
                    wait_seconds = 900
                elif state.get("next_end_ms") == updated.get("next_end_ms"):
                    wait_seconds = 60
                else:
                    # Stay below the documented authenticated endpoint rate limit.
                    wait_seconds = 0.75
            except Exception as exc:
                self.store.update_income_sync_state("USD", status="ERROR", error=str(exc)[:500])
                self._log(f"USD 历史收益同步失败（不影响交易）：{exc}")
                wait_seconds = 60
            self._income_sync_stop.wait(wait_seconds)

    def sync_income_history_once(self, now_ms=None, page_limit=2500):
        """Fetch one historical page or one incremental page; safe to resume."""
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        limit = min(2500, max(1, int(page_limit)))
        state = self.store.income_sync_state("USD")
        if state["status"] == "COMPLETE":
            start = max(0, int(state.get("last_success_ms") or now) - 60_000)
            rows = self.client.ledgers(
                "USD", start=start, end=now, limit=limit, wallet="funding", category=28
            )
            parsed = [
                row for row in parse_ledger_rows_v3(rows)
                if row["currency"] == "USD" and row["wallet"] == "funding"
            ]
            self.store.upsert_income_ledgers(parsed, category=28)
            earliest = state.get("earliest_mts")
            if parsed:
                earliest = min([row["mts"] for row in parsed] + ([earliest] if earliest is not None else []))
            return self.store.update_income_sync_state(
                "USD", status="COMPLETE", earliest_mts=earliest,
                last_success_ms=now, error=None,
            )

        end = int(state.get("next_end_ms") or now)
        self.store.update_income_sync_state("USD", status="BACKFILLING", error=None)
        rows = self.client.ledgers(
            "USD", end=end, limit=limit, wallet="funding", category=28
        )
        parsed = [
            row for row in parse_ledger_rows_v3(rows)
            if row["currency"] == "USD" and row["wallet"] == "funding"
        ]
        self.store.upsert_income_ledgers(parsed, category=28)
        earliest = state.get("earliest_mts")
        if parsed:
            page_earliest = min(row["mts"] for row in parsed)
            earliest = page_earliest if earliest is None else min(int(earliest), page_earliest)
        completed = len(rows or []) < limit
        if completed:
            return self.store.update_income_sync_state(
                "USD", status="COMPLETE", next_end_ms=None,
                earliest_mts=earliest, last_success_ms=now,
                completed_at_ms=now, error=None,
            )
        if not parsed:
            raise BitfinexApiError("income history page contained no usable USD funding ledgers")
        next_end = min(row["mts"] for row in parsed) - 1
        previous_end = state.get("next_end_ms")
        if previous_end is not None and next_end >= int(previous_end):
            raise BitfinexApiError("income history cursor did not move backwards")
        return self.store.update_income_sync_state(
            "USD", status="BACKFILLING", next_end_ms=next_end,
            earliest_mts=earliest, last_success_ms=now, error=None,
        )

    def sync_rest(self, include_history=False, now_ms=None):
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        symbol = currency_to_symbol("USD")
        raw_book = self.client.funding_book(symbol, 250)
        raw_trades = self.client.funding_trades(symbol, start=now - 7 * 86_400_000, end=now, limit=10000, sort=1)
        raw_stats = self.client.funding_stats(symbol, start=now - 7 * 86_400_000, end=now, limit=250)
        raw_wallets = self.client.wallets()
        if self.auto_transfer_wallets and self.store.runtime()["mode"] == "LIVE":
            transferred = False
            for wallet in parse_wallet_rows_v3(raw_wallets):
                if wallet["wallet_type"] not in self.auto_transfer_wallets or wallet["available"] <= 0:
                    continue
                try:
                    response = self.client.transfer_between_wallets(
                        wallet["wallet_type"], "funding", "USD", format(wallet["available"], "f")
                    )
                    transferred = True
                    self._log(f"USD 已从 {wallet['wallet_type']} 钱包自动转入 funding：{response}")
                except BitfinexAmbiguousWriteError as exc:
                    self.store.enter_safe("AMBIGUOUS_WALLET_TRANSFER", manual=True)
                    raise exc
                except BitfinexApiError as exc:
                    self._log(f"USD 自动转入被明确拒绝：{exc}")
            if transferred:
                raw_wallets = self.client.wallets()
        raw_offers = self.client.active_funding_offers(symbol)
        raw_credits = self.client.active_funding_credits(symbol)
        book = parse_book_v3(raw_book)
        trades = parse_funding_trades(raw_trades)
        self._stats = parse_funding_stats(raw_stats)
        wallets = parse_wallet_rows_v3(raw_wallets)
        offers = parse_offer_rows_v3(raw_offers)
        credits = parse_credit_rows_v3(raw_credits)
        managed_ids = {int(row["offer_id"]) for row in self.store.offers() if row["managed"]}
        for offer in offers:
            offer["managed"] = offer["id"] in managed_ids
            offer["pool"] = pool_for_period(offer["period"])
            offer["display_type"] = self._offer_display_type(offer)
        self.store.reconcile_offers(offers, now)
        active_offer_ids = {int(row["id"]) for row in offers}
        self._pending_cancel_requested.intersection_update(active_offer_ids)
        self.store.reconcile_credits(credits, now)
        stored_credits = {int(row["credit_id"]): row for row in self.store.credits(active_only=True)}
        for credit in credits:
            stored = stored_credits.get(int(credit["id"]))
            if stored:
                credit["managed"] = bool(stored["managed"])
                credit["pool"] = stored.get("pool") or pool_for_period(credit["period"])
                credit["layer"] = stored.get("layer")
                credit["display_type"] = stored.get("display_type")
        self.store.upsert_market_trades(trades)
        self.hub.apply_rest_snapshot(
            book=book,
            trades=trades,
            wallets=raw_wallets,
            offers=offers,
            credits=credits,
            synced_at_ms=now,
        )
        self._last_rest_sync_ms = now
        # A complete REST account snapshot is authoritative for reconciliation.
        # StateStore enforces the two samples / 30 second recovery rule.
        self.store.record_consistent_sync(now)
        if include_history or now - self._last_history_sync_ms >= 900_000:
            self.sync_history(now)
        return self.hub.snapshot(now)

    def sync_history(self, now_ms=None):
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        symbol = currency_to_symbol("USD")
        start = now - 90 * 86_400_000
        try:
            funding_rows = self.client.funding_trades_history(symbol, start=start, end=now, limit=2500, sort=1)
        except BitfinexApiError:
            funding_rows = []
        parsed_funding = parse_funding_trade_rows_v3(funding_rows)
        with self.store.transaction(immediate=True) as connection:
            for row in parsed_funding:
                managed = connection.execute(
                    "SELECT 1 FROM order_intents WHERE exchange_offer_id = ?", (row["offer_id"],)
                ).fetchone() is not None
                connection.execute(
                    """INSERT OR REPLACE INTO funding_trades(
                        trade_id, currency, offer_id, amount, rate, period, mts, managed
                    ) VALUES(?, 'USD', ?, ?, ?, ?, ?, ?)""",
                    (
                        row["id"], row["offer_id"], format(row["amount"], "f"), format(row["rate"], "f"),
                        row["period"], row["mts"], int(managed),
                    ),
                )
        try:
            offer_rows = self.client.funding_offers_history(symbol, start=start, end=now, limit=2500)
        except (BitfinexApiError, AttributeError):
            offer_rows = []
        try:
            credit_rows = self.client.funding_credits_history(symbol, start=start, end=now, limit=2500)
        except (BitfinexApiError, AttributeError):
            credit_rows = []
        self.store.upsert_offer_history(parse_offer_rows_v3(offer_rows))
        self.store.upsert_credit_history(parse_credit_rows_v3(credit_rows))
        self.store.prune_market_data(self.policy.market_retention_days, now)
        self._last_history_sync_ms = now

    @staticmethod
    def _account(snapshot):
        wallet = sum((D(row["available"]) for row in snapshot["wallets"] if row.get("wallet_type") == "funding" and row.get("currency") == "USD"), D("0"))
        offer_total = sum((D(row["amount"]) for row in snapshot["offers"] if row.get("currency") == "USD"), D("0"))
        credit_total = sum((D(row["amount"]) for row in snapshot["credits"] if row.get("currency") == "USD"), D("0"))
        exposure = {pool: D("0") for pool in POOLS}
        exposure_by_layer = {layer: D("0") for layer in ("quick", "balanced", "high")}
        variable = D("0")
        hidden = D("0")
        for row in [*snapshot["offers"], *snapshot["credits"]]:
            pool = row.get("pool") or pool_for_period(row.get("period", 0))
            if pool in exposure:
                exposure[pool] += D(row["amount"])
            amount = D(row["amount"])
            raw_type = str(row.get("display_type") or row.get("rate_type") or row.get("offer_type") or "").upper()
            if raw_type in {"FRR", "FRR_DELTA_VARIABLE", "FRRDELTAVAR", "VAR", "VARIABLE", "VARIABLE_UNKNOWN"}:
                variable += amount
            if bool(row.get("hidden")) or int(row.get("flags") or 0) & 64:
                hidden += amount
            if row.get("managed"):
                layer = row.get("layer") or "balanced"
                if layer in exposure_by_layer:
                    exposure_by_layer[layer] += amount
        return {
            "wallet": wallet,
            "offers": offer_total,
            "credits": credit_total,
            "total": wallet + offer_total + credit_total,
            "exposure": exposure,
            "exposureByLayer": exposure_by_layer,
            "existingExposure": {
                "total": offer_total + credit_total,
                "variable": variable,
                "hidden": hidden,
            },
        }

    @staticmethod
    def _build_plan(account, policy, signals, strategy_version):
        return build_strategy_plan_v3(
            account["total"],
            account["wallet"],
            account["exposure"],
            policy,
            signals,
            strategy_version,
            existing_exposure=account.get("existingExposure"),
            exposure_by_layer=account.get("exposureByLayer"),
        )

    def _net_interest_total(self):
        return self.store.realized_income("USD")

    def _record_variable_floor_violations(self, now_ms):
        violations = []
        for credit in self.store.credits(active_only=True):
            if not credit["managed"] or str(credit.get("rate_type") or "").upper() not in {"VAR", "VARIABLE", "FRRDELTAVAR"}:
                continue
            pool = credit.get("pool") or pool_for_period(credit["period"])
            if pool not in POOLS or self.policy.floor_apr(pool) is None:
                continue
            fee = self.policy.hidden_fee_rate if credit.get("hidden") else self.policy.normal_fee_rate
            floor_rate = gross_daily_floor(self.policy.floor_apr(pool), fee)
            observed = D(credit.get("rate_real") or credit["rate"])
            if observed < floor_rate:
                violations.append({
                    "credit_id": credit["credit_id"],
                    "pool": pool,
                    "floor_rate": floor_rate,
                    "observed_rate": observed,
                })
        self.store.record_rate_floor_violations(violations, now_ms)

    def _strategy_status(self, snapshot, signals, result=None):
        account = self._account(snapshot)
        runtime = self.store.runtime()
        realized_income = self.store.realized_income_summary("USD")
        income_sync = self.store.income_history_sync_payload("USD")
        status = {
            "schemaVersion": 3,
            "operationMode": runtime["mode"],
            "runtime": runtime,
            "marketData": {key: value for key, value in snapshot.items() if key not in {"book", "trades", "wallets", "offers", "credits", "fundingTrades"}},
            "market": json_decimal(signals),
            "account": json_decimal(account),
            "openOffers": json_decimal(snapshot["offers"]),
            "credits": json_decimal(snapshot["credits"]),
            "realizedIncome": realized_income,
            "incomeHistorySync": income_sync,
            "strategyV3": None if result is None else json_decimal(result),
            "activeStrategy": self.store.strategy("ACTIVE"),
            "draftStrategy": self.store.strategy("DRAFT"),
            "pendingStrategy": self.store.strategy("PENDING"),
            "statistics": {
                "1d": self.store.statistics(1),
                "7d": self.store.statistics(7),
                "30d": self.store.statistics(30),
                "90d": self.store.statistics(90),
                "all": self.store.statistics(None),
            },
        }
        if self.log is not None:
            for key, value in status.items():
                if key not in {"openOffers", "credits"}:
                    self.log.updateMetaValue(key, value)
            self.log.updateMetaValue("openOffers", status["openOffers"])
            self.log.updateMetaValue("credits", status["credits"])
            if runtime["mode"] == "SAFE":
                self.log.refreshStatus(f"SAFE：{runtime.get('safe_reason') or '策略已安全暂停'}")
            else:
                self.log.refreshStatus(f"V3 {runtime['mode']} 状态已同步。")
        return status

    def _submit_plan(self, plan_result, wallet_available, strategy_version):
        submitted = []
        remaining = D(wallet_available)
        for index, row in enumerate(plan_result["plan"]):
            if row["amount"] > remaining:
                break
            base_slice_key = f"{strategy_version}:{row['pool']}:{row['layer']}:{row['slice_index']}"
            order = {
                **row,
                "currency": "USD",
                "slice_key": self.store.replenishment_slice_key(base_slice_key),
                "strategy_version": strategy_version,
            }
            try:
                created, intent = self.store.reserve_intent(order, remaining)
            except InsufficientReservedBalance:
                break
            if not created:
                continue
            self.store.mark_submitting(intent["id"])
            try:
                response = self.client.submit_funding_offer(
                    "fUSD",
                    format(row["amount"], "f"),
                    format(row["submitted_rate"], "f"),
                    row["period"],
                    row["offer_type"],
                    flags=row["flags"],
                )
                offer_id = extract_submitted_offer_id(response)
                if offer_id is None:
                    self.store.mark_ambiguous(intent["id"], "successful notification omitted offer id")
                    break
                self.store.confirm_intent(intent["id"], offer_id)
                remaining -= row["amount"]
                submitted.append({"intentId": intent["id"], "offerId": offer_id, **row})
            except BitfinexAmbiguousWriteError as exc:
                self.store.mark_ambiguous(intent["id"], exc)
                break
            except BitfinexApiError as exc:
                self.store.reject_intent(intent["id"], exc)
                self._log(f"USD v3 挂单被明确拒绝：{exc}")
        return submitted

    def _cancel_reprice_candidates(self, snapshot, plan_result, now_ms=None, strategy_version=None):
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        if self.store.reprice_count_since(now - 3_600_000) >= self.policy.max_reprices_per_hour:
            return []
        target_by_key = {}
        for row in plan_result["plan"]:
            target_by_key.setdefault((row["pool"], row["layer"]), row)
        canceled = []
        for offer in self.store.offers(active_only=True):
            if not offer["managed"] or offer["currency"] != "USD":
                continue
            age = now - int(offer.get("mts_created") or now)
            if age < self.policy.minimum_offer_minutes * 60_000:
                continue
            last_family_reprice = self.store.last_reprice_for_family(offer.get("pool"), offer.get("layer"))
            if last_family_reprice is not None and now - int(last_family_reprice) < self.policy.reprice_cooldown_minutes * 60_000:
                continue
            target = target_by_key.get((offer.get("pool"), offer.get("layer")))
            if target is None:
                continue
            old_rate = D(offer.get("rate_real") or offer["rate"])
            threshold = max(self.policy.minimum_rate_change, D(snapshot.get("market", {}).get("trend_threshold", 0)))
            compatible_shape = (
                str(offer.get("display_type") or self._offer_display_type(offer)).upper() == target["display_type"]
                and int(offer["period"]) == int(target["period"])
                and int(offer.get("flags") or 0) == int(target["flags"])
            )
            if compatible_shape and abs(old_rate - target["effective_rate"]) < threshold:
                continue
            try:
                self.client.cancel_funding_offer(offer["offer_id"])
            except BitfinexAmbiguousWriteError as exc:
                self.store.enter_safe(f"AMBIGUOUS_CANCEL:{offer['offer_id']}", manual=True)
                self._log(str(exc))
                break
            self.store.record_reprice(
                offer["offer_id"], "market_change", old_rate, target["effective_rate"],
                strategy_version=strategy_version,
                plan_hash=plan_result.get("plan_hash"),
                display_type=target.get("display_type"),
            )
            canceled.append(offer["offer_id"])
            if self.store.reprice_count_since(now - 3_600_000) >= self.policy.max_reprices_per_hour:
                break
        return canceled

    def _pending_adjustments(self, snapshot, account, pending_policy, pending_result, now_ms):
        """Return hard incompatibilities that must disappear before activation.

        These checks intentionally do not use the ordinary reprice age/cooldown.
        A PENDING policy is a safety boundary, not a market-timing suggestion.
        """
        adjustments = []
        managed_offers = [
            row for row in self.store.offers(active_only=True)
            if row["managed"] and row["currency"] == "USD"
        ]
        cap_excess = max(
            D("0"),
            D(account["existingExposure"]["total"]) - D(pending_result["funding_cap"]),
        )
        for offer in managed_offers:
            pool = offer.get("pool") or pool_for_period(offer["period"])
            display_type = offer.get("display_type") or self._offer_display_type(offer)
            is_hidden = bool(int(offer.get("flags") or 0) & 64)
            fee = pending_policy.hidden_fee_rate if is_hidden else pending_policy.normal_fee_rate
            floor_apr = pending_policy.floor_apr(pool) if pool in POOLS else None
            effective = D(offer.get("rate_real") or offer["rate"])
            reasons = []
            if not self._display_type_enabled(pending_policy, display_type):
                reasons.append("disabled_type")
            if is_hidden and not pending_policy.enable_hidden:
                reasons.append("hidden_disabled")
            if pool not in POOLS or int(offer["period"]) not in pending_policy.periods(pool):
                reasons.append("period_not_allowed")
            if floor_apr is not None and effective < gross_daily_floor(floor_apr, fee):
                reasons.append("below_new_floor")
            if reasons:
                adjustments.append((offer, None, "+".join(reasons)))

        if cap_excess > 0:
            already = {int(item[0]["offer_id"]) for item in adjustments}
            for offer in sorted(managed_offers, key=lambda row: D(row["amount"]), reverse=True):
                if cap_excess <= 0:
                    break
                if int(offer["offer_id"]) in already:
                    cap_excess -= D(offer["amount"])
                    continue
                adjustments.append((offer, None, "funding_cap_exceeded"))
                cap_excess -= D(offer["amount"])
        return adjustments

    def _cancel_hard_adjustments(
        self, adjustments, now_ms, reason_prefix="policy",
        strategy_version=None, plan_hash=None,
    ):
        canceled = []
        for offer, target, reason in adjustments[:10]:
            offer_id = int(offer["offer_id"])
            if offer_id in self._pending_cancel_requested:
                continue
            try:
                self.client.cancel_funding_offer(offer_id)
            except BitfinexAmbiguousWriteError:
                self.store.enter_safe(f"AMBIGUOUS_CANCEL:{offer_id}", manual=True)
                break
            self.store.record_reprice(
                offer_id,
                f"{reason_prefix}:{reason}",
                offer.get("rate_real") or offer["rate"],
                None if target is None else target["effective_rate"],
                created_at_ms=now_ms,
                strategy_version=strategy_version,
                plan_hash=plan_hash,
                display_type=(offer.get("display_type") or self._offer_display_type(offer)),
            )
            self._pending_cancel_requested.add(offer_id)
            canceled.append(offer_id)
        return canceled

    def _advance_pending_strategy(self, snapshot, account, signals, now_ms):
        pending = self.store.strategy("PENDING")
        if pending is None:
            return None
        pending_policy = validate_policy_v3(
            policy_v3_with_overrides(StrategyPolicyV3(), pending["policy"]),
            require_live_floors=True,
        )
        pending_result = self._build_plan(account, pending_policy, signals, pending["version_id"])
        adjustments = self._pending_adjustments(snapshot, account, pending_policy, pending_result, now_ms)
        if not adjustments:
            self.store.activate_pending_strategy(
                plan_hash=pending_result.get("plan_hash"),
                reason="incompatible offers confirmed absent",
            )
            self.policy = pending_policy
            self._apply_policy_runtime_settings()
            if self.on_policy_activated is not None:
                try:
                    self.on_policy_activated(pending_policy, pending["version_id"])
                except Exception as exc:
                    self._log(f"V3 ACTIVE 已切换，但配置镜像写入失败：{exc}")
            self._log(f"v3 策略 {pending['version_id']} 已完成调整并生效。")
            return {"activated": pending["version_id"], "pending": False}
        canceled = self._cancel_hard_adjustments(
            adjustments, now_ms, "pending_strategy",
            strategy_version=pending["version_id"],
            plan_hash=pending_result.get("plan_hash"),
        )
        return {
            "pending": True,
            "versionId": pending["version_id"],
            "remainingAdjustments": max(0, len(adjustments) - len(canceled)),
            "canceled": canceled,
        }

    def cycle(self, now_ms=None):
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        if not self._bootstrapped:
            self.bootstrap(start_websocket=True)
        if now - self._last_rest_sync_ms >= self.policy.rest_stale_seconds * 1000:
            try:
                snapshot = self.sync_rest(now_ms=now)
            except BitfinexApiError as exc:
                snapshot = self.hub.snapshot(now)
                self._log(f"v3 REST 同步失败：{exc}")
        else:
            snapshot = self.hub.snapshot(now)
        if snapshot["safeRequired"]:
            self.store.enter_safe("MARKET_DATA_STALE")
        account = self._account(snapshot)
        signals = build_market_signals_v3(snapshot["book"], snapshot["trades"], self._stats, self.policy, now)
        self.store.record_market_bars(signals.get("windows"), now)
        self._record_variable_floor_violations(now)
        runtime = self.store.runtime()
        if runtime["mode"] == "LIVE":
            validate_policy_v3(self.policy, require_live_floors=True)
            pending_status = self._advance_pending_strategy(snapshot, account, signals, now)
            if pending_status and pending_status.get("pending"):
                pending = self.store.strategy("PENDING")
                pending_policy = validate_policy_v3(
                    policy_v3_with_overrides(StrategyPolicyV3(), pending["policy"]),
                    require_live_floors=True,
                )
                result = self._build_plan(account, pending_policy, signals, pending["version_id"])
                result["pendingStrategy"] = pending_status
                result["submitted"] = []
                canceled = pending_status.get("canceled", [])
            else:
                # Activation can happen above. Always reload ACTIVE and rebuild;
                # never submit the plan produced by the previous policy.
                strategy = self.store.strategy("ACTIVE")
                if strategy is not None:
                    self.policy = validate_policy_v3(
                        policy_v3_with_overrides(StrategyPolicyV3(), strategy["policy"]),
                        require_live_floors=True,
                    )
                    self._apply_policy_runtime_settings()
                version = strategy["version_id"] if strategy else "3"
                signals = build_market_signals_v3(snapshot["book"], snapshot["trades"], self._stats, self.policy, now)
                result = self._build_plan(account, self.policy, signals, version)
                hard_adjustments = self._pending_adjustments(snapshot, account, self.policy, result, now)
                canceled = self._cancel_hard_adjustments(
                    hard_adjustments, now, "active_policy",
                    strategy_version=version,
                    plan_hash=result.get("plan_hash"),
                )
                if not hard_adjustments:
                    canceled = self._cancel_reprice_candidates(
                        {"market": signals}, result, now, strategy_version=version
                    )
                if not canceled and self.store.runtime()["mode"] == "LIVE":
                    # Requested hard cancellations remain blocking until a fresh
                    # authoritative account snapshot confirms the offers vanished.
                    if hard_adjustments:
                        result["submitted"] = []
                    else:
                        result["submitted"] = self._submit_plan(result, account["wallet"], version)
                else:
                    result["submitted"] = []
            result["canceledForReprice"] = canceled
        elif runtime["mode"] == "REPLAY":
            result = replay_strategy_v3(self.policy, snapshot["trades"], self._stats, account["total"], snapshot["book"], now)
        else:
            strategy = self.store.strategy("ACTIVE")
            version = strategy["version_id"] if strategy else "3"
            result = self._build_plan(account, self.policy, signals, version)
        self.store.record_account_sample(
            account["total"], account["wallet"], account["offers"], account["credits"], self._net_interest_total(), now
        )
        status = self._strategy_status(snapshot, signals, result)
        if self.log is not None:
            self.log.persistStatus()
        return status
