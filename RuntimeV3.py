import threading
import time

from bitfinex import BitfinexAmbiguousWriteError, BitfinexApiError, currency_to_symbol
from DomainTypes import WriteOutcome, WriteResult
from MarketDataStream import BitfinexMarketDataHub
from StateStore import InsufficientReservedBalance
from ExchangeModels import (
    extract_submitted_offer_id,
    parse_book as parse_book_v3,
    parse_credit_rows as parse_credit_rows_v3,
    parse_funding_stats,
    parse_funding_trade_history as parse_funding_trade_rows_v3,
    parse_funding_trades,
    parse_offer_rows as parse_offer_rows_v3,
    parse_wallet_rows as parse_wallet_rows_v3,
)
from StrategyV3 import (
    D,
    POOLS,
    StrategyPolicyV3,
    build_market_signals_v3,
    build_strategy_plan_v3,
    ceil_rate_tick,
    competitive_rate_for_layer,
    gross_daily_floor,
    json_decimal,
    pool_for_period,
    policy_v3_with_overrides,
    rate_below_floor,
    replay_strategy_v3,
    validate_policy_v3,
)


def _write_result(client, result_method, legacy_method, *args, **kwargs):
    method = getattr(client, result_method, None)
    if method is not None:
        return method(*args, **kwargs)
    try:
        response = getattr(client, legacy_method)(*args, **kwargs)
        return WriteResult(WriteOutcome.CONFIRMED, response=response)
    except BitfinexAmbiguousWriteError as exc:
        return WriteResult(WriteOutcome.UNKNOWN, error=str(exc))
    except BitfinexApiError as exc:
        return WriteResult(WriteOutcome.DEFINITE_REJECT, error=str(exc))


def parse_ledger_rows_v3(rows):
    result = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 9:
            continue
        try:
            result.append(
                {
                    "id": int(row[0]),
                    "currency": str(row[1]).upper(),
                    "wallet": None if row[2] is None else str(row[2]).lower(),
                    "mts": int(row[3]),
                    "amount": D(str(row[5])),
                    "balance": None if row[6] is None else D(str(row[6])),
                    "description": str(row[8] or ""),
                }
            )
        except (TypeError, ValueError, ArithmeticError):
            continue
    return result


class LendingRuntimeV3:
    def __init__(
        self,
        client,
        policy,
        store,
        log=None,
        hub=None,
        auto_transfer_wallets=(),
        on_policy_activated=None,
        clock=time.time,
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
        self.auto_transfer_wallets = tuple(str(item).lower() for item in auto_transfer_wallets)
        self.on_policy_activated = on_policy_activated
        self.clock = clock
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
        recovery = self.store.recover_incomplete_writes()
        if recovery["ambiguousAfterSend"]:
            self._log(f"检测到 {recovery['ambiguousAfterSend']} 个进程中断时未确认的写入；已进入人工 SAFE。")
        if self.store.strategy("ACTIVE") is None:
            self.store.save_strategy(json_decimal(self.policy.__dict__), status="ACTIVE")
        else:
            self.policy = validate_policy_v3(
                policy_v3_with_overrides(StrategyPolicyV3(), self.store.strategy("ACTIVE")["policy"])
            )
        self._apply_policy_runtime_settings()
        self.sync_rest(include_history=True)
        auto_resolved = self.store.reconcile_ambiguous_candidates()
        if auto_resolved:
            self._log(f"已通过唯一 Bitfinex 记录自动恢复 {len(auto_resolved)} 个未知写入。")
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
        now = int(now_ms if now_ms is not None else self.clock() * 1000)
        limit = min(2500, max(1, int(page_limit)))
        state = self.store.income_sync_state("USD")
        if state["status"] == "COMPLETE":
            start = max(0, int(state.get("last_success_ms") or now) - 60_000)
            rows = self.client.ledgers("USD", start=start, end=now, limit=limit, wallet="funding", category=28)
            parsed = [
                row for row in parse_ledger_rows_v3(rows) if row["currency"] == "USD" and row["wallet"] == "funding"
            ]
            self.store.upsert_income_ledgers(parsed, category=28)
            earliest = state.get("earliest_mts")
            if parsed:
                earliest = min([row["mts"] for row in parsed] + ([earliest] if earliest is not None else []))
            return self.store.update_income_sync_state(
                "USD",
                status="COMPLETE",
                earliest_mts=earliest,
                last_success_ms=now,
                error=None,
            )

        end = int(state.get("next_end_ms") or now)
        self.store.update_income_sync_state("USD", status="BACKFILLING", error=None)
        rows = self.client.ledgers("USD", end=end, limit=limit, wallet="funding", category=28)
        parsed = [row for row in parse_ledger_rows_v3(rows) if row["currency"] == "USD" and row["wallet"] == "funding"]
        self.store.upsert_income_ledgers(parsed, category=28)
        earliest = state.get("earliest_mts")
        if parsed:
            page_earliest = min(row["mts"] for row in parsed)
            earliest = page_earliest if earliest is None else min(int(earliest), page_earliest)
        completed = len(rows or []) < limit
        if completed:
            return self.store.update_income_sync_state(
                "USD",
                status="COMPLETE",
                next_end_ms=None,
                earliest_mts=earliest,
                last_success_ms=now,
                completed_at_ms=now,
                error=None,
            )
        if not parsed:
            raise BitfinexApiError("income history page contained no usable USD funding ledgers")
        next_end = min(row["mts"] for row in parsed) - 1
        previous_end = state.get("next_end_ms")
        if previous_end is not None and next_end >= int(previous_end):
            raise BitfinexApiError("income history cursor did not move backwards")
        return self.store.update_income_sync_state(
            "USD",
            status="BACKFILLING",
            next_end_ms=next_end,
            earliest_mts=earliest,
            last_success_ms=now,
            error=None,
        )

    def sync_rest(self, include_history=False, now_ms=None):
        now = int(now_ms if now_ms is not None else self.clock() * 1000)
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
                result = _write_result(
                    self.client,
                    "transfer_between_wallets_result",
                    "transfer_between_wallets",
                    wallet["wallet_type"],
                    "funding",
                    "USD",
                    format(wallet["available"], "f"),
                )
                if result.outcome == WriteOutcome.CONFIRMED:
                    transferred = True
                    self._log(f"USD 已从 {wallet['wallet_type']} 钱包自动转入 funding：{result.response}")
                elif result.outcome == WriteOutcome.UNKNOWN:
                    # A complete wallet snapshot makes retrying this sweep
                    # idempotent: an already-transferred source balance is zero.
                    self.store.enter_safe("AMBIGUOUS_WALLET_TRANSFER")
                    raise BitfinexAmbiguousWriteError(result.error)
                else:
                    self._log(f"USD 自动转入被明确拒绝：{result.error}")
            if transferred:
                raw_wallets = self.client.wallets()
        raw_offers = self.client.active_funding_offers(symbol)
        raw_credits = self.client.active_funding_credits(symbol)
        book = parse_book_v3(raw_book)
        trades = parse_funding_trades(raw_trades)
        self._stats = parse_funding_stats(raw_stats)
        offers = parse_offer_rows_v3(raw_offers)
        credits = parse_credit_rows_v3(raw_credits)
        previously_managed = {int(row["offer_id"]) for row in self.store.offers(active_only=True) if row["managed"]}
        managed_ids = {int(row["offer_id"]) for row in self.store.offers() if row["managed"]}
        for offer in offers:
            offer["managed"] = offer["id"] in managed_ids
            offer["pool"] = pool_for_period(offer["period"])
            offer["display_type"] = self._offer_display_type(offer)
        active_offer_ids = {int(row["id"]) for row in offers}
        known_credit_ids = {int(row["credit_id"]) for row in self.store.credits()}
        if (previously_managed - active_offer_ids) and any(int(row["id"]) not in known_credit_ids for row in credits):
            try:
                recent_rows = self.client.funding_trades_history(
                    symbol, start=now - 600_000, end=now, limit=250, sort=1
                )
                self._store_funding_trade_history(parse_funding_trade_rows_v3(recent_rows))
            except (BitfinexApiError, AttributeError) as exc:
                self._log(f"成交归属即时同步失败，将保持待归属并安全重试：{exc}")
        self.store.reconcile_offers(offers, now)
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
        self.store.upsert_funding_stats(self._stats)
        self.store.record_book_snapshot(book, source="REST", now_ms=now)
        self.hub.apply_rest_snapshot(
            book=book,
            trades=trades,
            wallets=raw_wallets,
            offers=offers,
            credits=credits,
            synced_at_ms=now,
        )
        self._last_rest_sync_ms = now
        ambiguous = self.store.intents(states={"AMBIGUOUS"})
        history_complete = self.sync_ambiguous_write_history(ambiguous, now) if ambiguous else True
        if include_history or now - self._last_history_sync_ms >= 900_000:
            self.sync_history(now)
        # Reconcile write uncertainty only after the complete account snapshot is
        # stored. A unique match is safe even if history was temporarily
        # unavailable; confirming absence requires authoritative trade history.
        resolved_submits = self.store.reconcile_ambiguous_candidates(
            confirm_absent=history_complete,
            now_ms=now,
        )
        if resolved_submits:
            # The first reconciliation pass happened before the uncertain intent
            # was bound. Refresh ownership in both durable state and this cycle's
            # in-memory snapshot before LIVE planning resumes.
            self.store.reconcile_offers(offers, now)
            self.store.reconcile_credits(credits, now)
            stored_offers = {int(row["offer_id"]): row for row in self.store.offers(active_only=True)}
            for offer in offers:
                stored = stored_offers.get(int(offer["id"]))
                if stored:
                    offer["managed"] = bool(stored["managed"])
                    offer["pool"] = stored.get("pool") or offer.get("pool")
                    offer["layer"] = stored.get("layer")
                    offer["display_type"] = stored.get("display_type") or offer.get("display_type")
            stored_credits = {int(row["credit_id"]): row for row in self.store.credits(active_only=True)}
            for credit in credits:
                stored = stored_credits.get(int(credit["id"]))
                if stored:
                    credit["managed"] = bool(stored["managed"])
                    credit["pool"] = stored.get("pool") or credit.get("pool")
                    credit["layer"] = stored.get("layer")
                    credit["display_type"] = stored.get("display_type") or credit.get("display_type")
            self.hub.apply_rest_snapshot(offers=offers, credits=credits, synced_at_ms=now)
        current = self.store.runtime()
        if current["mode"] == "SAFE" and str(current.get("safe_reason") or "").startswith("AMBIGUOUS_CANCEL:"):
            self.store.observe_ambiguous_cancel(active_offer_ids, now)
        else:
            # Transient data/transport/runtime failures recover after two complete
            # snapshots. Manual ambiguous submits are handled only above.
            self.store.record_consistent_sync(now)
        return self.hub.snapshot(now)

    def sync_ambiguous_write_history(self, intents, now_ms=None):
        """Fetch the narrow authoritative history window for uncertain submits.

        The regular 90-day history query is capped and can omit recent records on
        busy accounts. Recovery therefore queries each request's own time window.
        """
        now = int(now_ms if now_ms is not None else self.clock() * 1000)
        symbol = currency_to_symbol("USD")
        complete = True
        for intent in intents:
            request_ms = int(intent.get("request_started_at_ms") or intent.get("updated_at_ms") or now)
            start = request_ms - 300_000
            end = min(now, request_ms + 600_000)
            try:
                funding_rows = self.client.funding_trades_history(symbol, start=start, end=end, limit=2500, sort=1)
                offer_rows = self.client.funding_offers_history(symbol, start=start, end=end, limit=2500)
            except (BitfinexApiError, AttributeError) as exc:
                complete = False
                self._log(f"未知挂单对账历史同步失败，将保持 SAFE 并重试：{exc}")
                continue
            self._store_funding_trade_history(parse_funding_trade_rows_v3(funding_rows))
            self.store.upsert_offer_history(parse_offer_rows_v3(offer_rows))
            if now - request_ms < 60_000:
                complete = False
        return complete

    def _store_funding_trade_history(self, parsed_funding):
        with self.store.transaction(immediate=True) as connection:
            for row in parsed_funding:
                managed = (
                    connection.execute(
                        "SELECT 1 FROM order_intents WHERE exchange_offer_id = ?", (row["offer_id"],)
                    ).fetchone()
                    is not None
                )
                connection.execute(
                    """INSERT OR REPLACE INTO funding_trades(
                        trade_id, currency, offer_id, amount, rate, period, mts, managed
                    ) VALUES(?, 'USD', ?, ?, ?, ?, ?, ?)""",
                    (
                        row["id"],
                        row["offer_id"],
                        format(row["amount"], "f"),
                        format(row["rate"], "f"),
                        row["period"],
                        row["mts"],
                        int(managed),
                    ),
                )

    def sync_history(self, now_ms=None):
        now = int(now_ms if now_ms is not None else self.clock() * 1000)
        symbol = currency_to_symbol("USD")
        start = now - 90 * 86_400_000
        try:
            funding_rows = self.client.funding_trades_history(symbol, start=start, end=now, limit=2500, sort=1)
        except BitfinexApiError:
            funding_rows = []
            funding_history_complete = False
        else:
            funding_history_complete = True
        self._store_funding_trade_history(parse_funding_trade_rows_v3(funding_rows))
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
        return funding_history_complete

    @staticmethod
    def _account(snapshot):
        wallet = sum(
            (
                D(row["available"])
                for row in snapshot["wallets"]
                if row.get("wallet_type") == "funding" and row.get("currency") == "USD"
            ),
            D("0"),
        )
        offer_total = sum((D(row["amount"]) for row in snapshot["offers"] if row.get("currency") == "USD"), D("0"))
        credit_total = sum((D(row["amount"]) for row in snapshot["credits"] if row.get("currency") == "USD"), D("0"))
        exposure = {pool: D("0") for pool in POOLS}
        managed_offer_exposure = {pool: D("0") for pool in POOLS}
        exposure_by_layer = {layer: D("0") for layer in ("quick", "balanced", "high")}
        managed_offer_layer_exposure = {layer: D("0") for layer in ("quick", "balanced", "high")}
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
                if row in snapshot["offers"]:
                    if pool in managed_offer_exposure:
                        managed_offer_exposure[pool] += amount
                    if layer in managed_offer_layer_exposure:
                        managed_offer_layer_exposure[layer] += amount
        return {
            "wallet": wallet,
            "offers": offer_total,
            "credits": credit_total,
            "total": wallet + offer_total + credit_total,
            "exposure": exposure,
            "exposureByLayer": exposure_by_layer,
            "managedOfferExposure": managed_offer_exposure,
            "managedOfferLayerExposure": managed_offer_layer_exposure,
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
            offer_exposure_by_pool=account.get("managedOfferExposure"),
            offer_exposure_by_layer=account.get("managedOfferLayerExposure"),
        )

    def _net_interest_total(self):
        return self.store.realized_income("USD")

    def _record_variable_floor_violations(self, now_ms):
        violations = []
        for credit in self.store.credits(active_only=True):
            if not credit["managed"] or str(credit.get("rate_type") or "").upper() not in {
                "VAR",
                "VARIABLE",
                "FRRDELTAVAR",
            }:
                continue
            pool = credit.get("pool") or pool_for_period(credit["period"])
            if pool not in POOLS or self.policy.floor_apr(pool) is None:
                continue
            fee = self.policy.hidden_fee_rate if credit.get("hidden") else self.policy.normal_fee_rate
            floor_rate = gross_daily_floor(self.policy.floor_apr(pool), fee)
            observed = D(credit.get("rate_real") or credit["rate"])
            if rate_below_floor(observed, floor_rate):
                violations.append(
                    {
                        "credit_id": credit["credit_id"],
                        "pool": pool,
                        "floor_rate": floor_rate,
                        "observed_rate": observed,
                    }
                )
        self.store.record_rate_floor_violations(violations, now_ms)

    def _repricing_status(self, signals, now_ms):
        rows = []
        for offer in self.store.offers(active_only=True):
            offer_id = int(offer["offer_id"])
            chain = self.store.reprice_chain_for_offer(offer_id)
            pool = offer.get("pool") or pool_for_period(offer["period"])
            layer = offer.get("layer") or "balanced"
            if chain is None or pool not in POOLS or self.policy.floor_apr(pool) is None:
                continue
            stage = int(chain.get("current_stage") or 0)
            stages = self.policy.reprice_stages(pool)
            next_stage = stage + 1 if stage < len(stages) else None
            next_at = (
                int(chain["started_at_ms"]) + int(stages[next_stage - 1]) * 60_000
                if next_stage is not None
                else None
            )
            hidden = bool(int(offer.get("flags") or 0) & 64)
            fee = self.policy.hidden_fee_rate if hidden else self.policy.normal_fee_rate
            floor_rate = ceil_rate_tick(gross_daily_floor(self.policy.floor_apr(pool), fee))
            benchmark = competitive_rate_for_layer(layer, signals, floor_rate)
            next_target = None
            if next_stage is not None:
                fraction = (D("1") / D("3"), D("2") / D("3"), D("1"))[next_stage - 1]
                origin_rate = D(chain["origin_rate"])
                next_target = ceil_rate_tick(
                    max(floor_rate, origin_rate - max(D("0"), origin_rate - benchmark) * fraction)
                )
            rows.append(
                {
                    "offerId": offer_id,
                    "pool": pool,
                    "layer": layer,
                    "stage": stage,
                    "elapsedMinutes": max(D("0"), D(now_ms - int(chain["started_at_ms"])) / D("60000")),
                    "nextStageAtMs": next_at,
                    "nextTargetRate": next_target,
                    "benchmarkRate": benchmark,
                    "floorRate": floor_rate,
                    "pendingAction": chain.get("pending_action"),
                }
            )
        return json_decimal(rows)

    def _strategy_status(self, snapshot, signals, result=None):
        account = self._account(snapshot)
        runtime = self.store.runtime()
        realized_income = self.store.realized_income_summary("USD")
        income_sync = self.store.income_history_sync_payload("USD")
        now = int(snapshot.get("now") or self.clock() * 1000)
        repricing = self._repricing_status(signals, now)
        repricing_by_offer = {int(row["offerId"]): row for row in repricing}
        open_offers = json_decimal(snapshot["offers"])
        for offer in open_offers:
            offer_id = int(offer.get("id") or offer.get("offer_id") or 0)
            if offer_id in repricing_by_offer:
                offer["repriceState"] = repricing_by_offer[offer_id]
        strategy_payload = None if result is None else json_decimal(result)
        if strategy_payload is not None:
            strategy_payload["repricing"] = repricing
        status = {
            "schemaVersion": 3,
            "operationMode": runtime["mode"],
            "runtime": runtime,
            "marketData": {
                key: value
                for key, value in snapshot.items()
                if key not in {"book", "trades", "wallets", "offers", "credits", "fundingTrades"}
            },
            "market": json_decimal(signals),
            "account": json_decimal(account),
            "openOffers": open_offers,
            "credits": json_decimal(snapshot["credits"]),
            "realizedIncome": realized_income,
            "incomeHistorySync": income_sync,
            "strategyV3": strategy_payload,
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
        for row in plan_result["plan"]:
            base_slice_key = f"{strategy_version}:{row['pool']}:{row['layer']}:{row['slice_index']}"
            pending_reprice = self.store.pending_reprice_for_base(base_slice_key, strategy_version)
            submit_row = self._apply_pending_reprice(row, pending_reprice)
            if submit_row["amount"] > remaining:
                break
            order = {
                **submit_row,
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
            result = _write_result(
                self.client,
                "submit_funding_offer_result",
                "submit_funding_offer",
                "fUSD",
                format(submit_row["amount"], "f"),
                format(submit_row["submitted_rate"], "f"),
                submit_row["period"],
                submit_row["offer_type"],
                flags=submit_row["flags"],
            )
            if result.outcome == WriteOutcome.CONFIRMED:
                response = result.response
                offer_id = extract_submitted_offer_id(response)
                if offer_id is None:
                    self.store.mark_ambiguous(intent["id"], "successful notification omitted offer id")
                    break
                self.store.confirm_intent(intent["id"], offer_id)
                self.store.bind_reprice_replacement(
                    base_slice_key,
                    strategy_version,
                    offer_id,
                    submit_row["effective_rate"],
                )
                remaining -= submit_row["amount"]
                submitted.append({"intentId": intent["id"], "offerId": offer_id, **submit_row})
            elif result.outcome == WriteOutcome.UNKNOWN:
                self.store.mark_ambiguous(intent["id"], result.error)
                break
            else:
                self.store.reject_intent(intent["id"], result.error)
                self._log(f"USD v3 挂单被明确拒绝：{result.error}")
        return submitted

    @staticmethod
    def _apply_pending_reprice(row, pending):
        if not pending or pending.get("pending_target_rate") is None:
            return dict(row)
        adjusted = dict(row)
        floor_rate = ceil_rate_tick(adjusted.get("gross_daily_floor") or 0)
        desired = ceil_rate_tick(max(floor_rate, D(pending["pending_target_rate"])))
        display_type = str(adjusted.get("display_type") or adjusted.get("offer_type") or "LIMIT").upper()
        if display_type == "LIMIT":
            adjusted["submitted_rate"] = desired
            adjusted["effective_rate"] = desired
            adjusted["target_rate"] = desired
        elif display_type in {"FRR_DELTA_FIXED", "FRR_DELTA_VARIABLE"}:
            frr = max(D("0"), D(adjusted["effective_rate"]) - D(adjusted["submitted_rate"]))
            offset = ceil_rate_tick(max(D("0"), desired - frr))
            adjusted["submitted_rate"] = offset
            adjusted["effective_rate"] = frr + offset
            adjusted["target_rate"] = desired
        return adjusted

    def _cancel_reprice_candidates(self, snapshot, plan_result, now_ms=None, strategy_version=None):
        now = int(now_ms if now_ms is not None else self.clock() * 1000)
        signals = snapshot.get("market", {})
        target_by_key = {}
        for row in plan_result["plan"]:
            target_by_key.setdefault((row["pool"], row["layer"]), row)
        canceled = []
        for offer in self.store.offers(active_only=True):
            if not offer["managed"] or offer["currency"] != "USD":
                continue
            offer_id = int(offer["offer_id"])
            if offer_id in self._pending_cancel_requested:
                continue
            pool = offer.get("pool") or pool_for_period(offer["period"])
            layer = offer.get("layer") or "balanced"
            if pool not in POOLS:
                continue
            target = target_by_key.get((pool, layer))
            chain = self.store.ensure_reprice_chain(offer, strategy_version, now)
            if chain is None or chain.get("pending_action"):
                continue

            old_rate = D(offer.get("rate_real") or offer["rate"])
            hidden = bool(int(offer.get("flags") or 0) & 64)
            fee = self.policy.hidden_fee_rate if hidden else self.policy.normal_fee_rate
            floor_rate = ceil_rate_tick(gross_daily_floor(self.policy.floor_apr(pool), fee))
            benchmark = competitive_rate_for_layer(layer, signals, floor_rate)
            if target is None:
                target = {
                    "display_type": str(offer.get("display_type") or self._offer_display_type(offer)).upper(),
                    "period": int(offer["period"]),
                    "flags": int(offer.get("flags") or 0),
                    "effective_rate": benchmark,
                }
            threshold = max(self.policy.minimum_rate_change, D(signals.get("trend_threshold") or 0))
            display_type = str(offer.get("display_type") or self._offer_display_type(offer)).upper()
            compatible_shape = (
                display_type == str(target["display_type"]).upper()
                and int(offer["period"]) == int(target["period"])
                and int(offer.get("flags") or 0) == int(target["flags"])
            )

            action = None
            reason = None
            stage = None
            desired_rate = None
            offer_age = now - int(offer.get("mts_created") or now)
            if not compatible_shape:
                if offer_age < self.policy.minimum_offer_minutes * 60_000:
                    continue
                action = "SHAPE_CHANGE"
                reason = "shape_change"
                desired_rate = ceil_rate_tick(max(floor_rate, D(target["effective_rate"])))
            elif benchmark - old_rate >= threshold:
                if offer_age < self.policy.minimum_offer_minutes * 60_000:
                    continue
                action = "MARKET_RISE"
                reason = "market_rise"
                desired_rate = benchmark
            else:
                next_stage = int(chain.get("current_stage") or 0) + 1
                stages = self.policy.reprice_stages(pool)
                if next_stage > len(stages):
                    continue
                due_at = int(chain["started_at_ms"]) + int(stages[next_stage - 1]) * 60_000
                if now < due_at:
                    continue
                fraction = (D("1") / D("3"), D("2") / D("3"), D("1"))[next_stage - 1]
                origin_rate = D(chain["origin_rate"])
                gap = max(D("0"), origin_rate - benchmark)
                desired_rate = ceil_rate_tick(max(floor_rate, origin_rate - gap * fraction))
                if (
                    display_type == "FRR"
                    or old_rate <= desired_rate
                    or old_rate - desired_rate < threshold
                ):
                    self.store.complete_reprice_stage(chain["chain_key"], next_stage, now)
                    self.store.record_ownership_event(
                        "REPRICE_STAGE_SKIPPED",
                        offer_id=offer_id,
                        details={
                            "stage": next_stage,
                            "benchmarkRate": format(benchmark, "f"),
                            "targetRate": format(desired_rate, "f"),
                        },
                    )
                    continue
                action = "AGE_STAGE"
                stage = next_stage
                reason = f"age_stage_{stage}"

            if self.store.reprice_count_since(now - 3_600_000) >= self.policy.max_reprices_per_hour:
                continue
            last_family_reprice = self.store.last_reprice_for_family(pool, layer)
            if (
                last_family_reprice is not None
                and now - int(last_family_reprice) < self.policy.reprice_cooldown_minutes * 60_000
            ):
                continue
            write = _write_result(self.client, "cancel_funding_offer_result", "cancel_funding_offer", offer_id)
            if write.outcome == WriteOutcome.UNKNOWN:
                self.store.record_ownership_event(
                    "CANCEL_UNKNOWN",
                    offer_id=offer_id,
                    details={"reason": reason, "error": write.error},
                )
                self.store.enter_safe(f"AMBIGUOUS_CANCEL:{offer_id}", manual=True)
                self._log(write.error)
                break
            if write.outcome == WriteOutcome.DEFINITE_REJECT:
                self.store.record_ownership_event(
                    "CANCEL_REJECTED",
                    offer_id=offer_id,
                    details={"reason": reason, "error": write.error},
                )
                self._log(f"撤销 Offer {offer_id} 被明确拒绝：{write.error}")
                continue
            self.store.mark_reprice_pending(
                chain["chain_key"],
                action,
                desired_rate,
                stage=stage,
                now_ms=now,
            )
            self.store.record_reprice(
                offer_id,
                reason,
                old_rate,
                desired_rate,
                strategy_version=strategy_version,
                plan_hash=plan_result.get("plan_hash"),
                display_type=target.get("display_type"),
                chain_key=chain["chain_key"],
                stage=stage,
                benchmark_rate=benchmark,
                floor_rate=floor_rate,
            )
            canceled.append(offer_id)
            self._pending_cancel_requested.add(offer_id)
            self.store.record_ownership_event(
                "CANCEL_CONFIRMED", offer_id=offer_id, details={"reason": reason}
            )
            if action == "AGE_STAGE":
                self._log(
                    f"挂单 {offer_id} 累计等待达到第 {stage} 阶段，"
                    f"利率由 {format(old_rate, 'f')} 调整为 {format(desired_rate, 'f')}。"
                )
            elif action == "MARKET_RISE":
                self._log(
                    f"挂单 {offer_id} 检测到市场利率明显上涨，"
                    f"将由 {format(old_rate, 'f')} 追价至 {format(desired_rate, 'f')}。"
                )
            else:
                self._log(f"挂单 {offer_id} 的期限或类型需要按当前策略调整。")
        return canceled

    @staticmethod
    def ratio_rebalance_candidates(offers, plan_result, policy, now_ms, pending_ids=None):
        pending_ids = set(pending_ids or ())
        tolerance = D(plan_result.get("ratio_tolerance") or policy.min_order_amount)
        deviations = plan_result.get("deviation_amounts") or {}
        candidates = []
        for pool in POOLS:
            excess = D(deviations.get(pool, 0)) - tolerance
            if excess <= 0:
                continue
            pool_offers = [
                row
                for row in offers
                if row.get("managed")
                and row.get("currency") == "USD"
                and (row.get("pool") or pool_for_period(row["period"])) == pool
                and int(row.get("offer_id") or row.get("id")) not in pending_ids
                and now_ms - int(row.get("mts_created") or now_ms) >= policy.minimum_offer_minutes * 60_000
            ]
            pool_offers.sort(key=lambda row: (D(row.get("rate_real") or row["rate"]), int(row.get("mts_created") or 0)))
            removed = D("0")
            for offer in pool_offers:
                candidates.append(offer)
                removed += D(offer["amount"])
                if removed >= excess:
                    break
        return candidates

    def _cancel_ratio_rebalance(self, plan_result, now_ms, strategy_version):
        if self.store.reprice_count_since(now_ms - 3_600_000) >= self.policy.max_reprices_per_hour:
            return []
        candidates = self.ratio_rebalance_candidates(
            self.store.offers(active_only=True), plan_result, self.policy, now_ms, self._pending_cancel_requested
        )
        canceled = []
        remaining_limit = max(0, self.policy.max_reprices_per_hour - self.store.reprice_count_since(now_ms - 3_600_000))
        for offer in candidates[: min(10, remaining_limit)]:
            offer_id = int(offer["offer_id"])
            write = _write_result(self.client, "cancel_funding_offer_result", "cancel_funding_offer", offer_id)
            if write.outcome == WriteOutcome.UNKNOWN:
                self.store.record_ownership_event(
                    "CANCEL_UNKNOWN",
                    offer_id=offer_id,
                    details={"reason": "ratio_rebalance", "error": write.error},
                )
                self.store.enter_safe(f"AMBIGUOUS_CANCEL:{offer_id}", manual=True)
                self._log(f"撤销 Offer {offer_id} 结果未知，将自动对账后重试：{write.error}")
                break
            if write.outcome == WriteOutcome.DEFINITE_REJECT:
                self.store.record_ownership_event(
                    "CANCEL_REJECTED", offer_id=offer_id, details={"reason": "ratio_rebalance", "error": write.error}
                )
                continue
            self.store.record_reprice(
                offer_id,
                "ratio_rebalance",
                offer.get("rate_real") or offer["rate"],
                None,
                created_at_ms=now_ms,
                strategy_version=strategy_version,
                plan_hash=plan_result.get("plan_hash"),
                display_type=offer.get("display_type"),
            )
            self.store.record_ownership_event(
                "CANCEL_CONFIRMED", offer_id=offer_id, details={"reason": "ratio_rebalance"}
            )
            self._pending_cancel_requested.add(offer_id)
            canceled.append(offer_id)
        return canceled

    def _pending_adjustments(self, snapshot, account, pending_policy, pending_result, now_ms):
        """Return hard incompatibilities that must disappear before activation.

        These checks intentionally do not use the ordinary reprice age/cooldown.
        A PENDING policy is a safety boundary, not a market-timing suggestion.
        """
        adjustments = []
        managed_offers = [
            row for row in self.store.offers(active_only=True) if row["managed"] and row["currency"] == "USD"
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
            if floor_apr is not None and rate_below_floor(effective, gross_daily_floor(floor_apr, fee)):
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
        self,
        adjustments,
        now_ms,
        reason_prefix="policy",
        strategy_version=None,
        plan_hash=None,
    ):
        canceled = []
        for offer, target, reason in adjustments[:10]:
            offer_id = int(offer["offer_id"])
            if offer_id in self._pending_cancel_requested:
                continue
            write = _write_result(self.client, "cancel_funding_offer_result", "cancel_funding_offer", offer_id)
            if write.outcome == WriteOutcome.UNKNOWN:
                self.store.record_ownership_event(
                    "CANCEL_UNKNOWN",
                    offer_id=offer_id,
                    details={"reason": f"{reason_prefix}:{reason}", "error": write.error},
                )
                self.store.enter_safe(f"AMBIGUOUS_CANCEL:{offer_id}", manual=True)
                self._log(f"撤销 Offer {offer_id} 结果未知，将自动对账后重试：{write.error}")
                break
            if write.outcome == WriteOutcome.DEFINITE_REJECT:
                self.store.record_ownership_event(
                    "CANCEL_REJECTED",
                    offer_id=offer_id,
                    details={"reason": f"{reason_prefix}:{reason}", "error": write.error},
                )
                self._log(f"撤销 Offer {offer_id} 被明确拒绝：{write.error}")
                continue
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
            self.store.record_ownership_event(
                "CANCEL_CONFIRMED", offer_id=offer_id, details={"reason": f"{reason_prefix}:{reason}"}
            )
            self._log(f"挂单 {offer_id} 正在执行策略强制调整：{reason_prefix}:{reason}。")
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
            adjustments,
            now_ms,
            "pending_strategy",
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
        now = int(now_ms if now_ms is not None else self.clock() * 1000)
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
                    hard_adjustments,
                    now,
                    "active_policy",
                    strategy_version=version,
                    plan_hash=result.get("plan_hash"),
                )
                if not hard_adjustments:
                    canceled = self._cancel_ratio_rebalance(result, now, version)
                    result["rebalance_cancellations"] = canceled
                    result["rebalanceCancellations"] = canceled
                    if not canceled:
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
            result = replay_strategy_v3(
                self.policy, snapshot["trades"], self._stats, account["total"], snapshot["book"], now
            )
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
