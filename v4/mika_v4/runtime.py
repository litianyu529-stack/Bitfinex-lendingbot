from __future__ import annotations

import json
import signal
import threading
import time
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from .bitfinex import BitfinexClient, BitfinexError
from .config import V4Settings, atomic_write, policy_payload
from .domain import AccountSnapshot, AllocationPlan, RuntimeMode, StrategyStatus
from .execution import ExecutionBlocked, SafeExecutor
from .locks import CrossVersionLiveLock, LiveLockError, ProcessLock
from .market import MarketBuffer, PublicMarketStream, build_market_snapshot
from .store import V4Store, _encode
from .strategy import bottom_rung_triggered, build_plan, fast_shift_allowed, floor_stale, gross_daily_floor


D = Decimal


def parse_book(rows: list[list[Any]]) -> list[dict[str, Any]]:
    return [
        {"rate": D(str(row[0])), "period": int(row[1]), "count": int(row[2]), "amount": D(str(row[3]))}
        for row in rows
        if isinstance(row, list) and len(row) >= 4
    ]


def parse_trades(rows: list[list[Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(row[0]),
            "mts": int(row[1]),
            "amount": abs(D(str(row[2]))),
            "rate": D(str(row[3])),
            "period": int(row[4]),
        }
        for row in rows
        if isinstance(row, list) and len(row) >= 5
    ]


class LendingRuntime:
    def __init__(
        self, settings: V4Settings, client: BitfinexClient | None = None, store: V4Store | None = None
    ) -> None:
        self.settings = settings
        self.policy = settings.policy
        self.store = store or V4Store(settings.state_db)
        self.client = client or BitfinexClient(
            settings.api_key,
            settings.api_secret,
            auth_limit_per_minute=settings.policy.max_authenticated_requests_per_minute,
        )
        self.executor = SafeExecutor(self.client, self.store, self.policy)
        self.market_buffer = MarketBuffer()
        self.market_stream = PublicMarketStream(self.market_buffer, self._log_market)
        self.live_lock = CrossVersionLiveLock(settings.repository_root, "v4")
        self.worker_lock = ProcessLock(settings.state_db.parent / "lendingbot-v4-worker.lock", "v4-worker")
        self._stop = threading.Event()
        self.last_account: AccountSnapshot | None = None
        self.last_market = None
        self.last_plan: AllocationPlan | None = None
        self.last_action = "STARTING"
        self._last_full_ms = 0

    def _log_market(self, message: str) -> None:
        self.store.record_event("WARNING", "MARKET_STREAM", {"message": message})

    def request_stop(self, *_: object) -> None:
        self._stop.set()

    def bootstrap_market(self) -> None:
        now = int(time.time() * 1000)
        book = parse_book(self.client.funding_book("fUSD", 250))
        trades = parse_trades(self.client.funding_trades("fUSD", now - 24 * 60 * 60_000, 10_000, sort=-1))
        self.market_buffer.replace_book(book, now)
        self.market_buffer.replace_trades(trades)

    def _snapshot_market(self, refresh_rest: bool = False):
        if refresh_rest:
            try:
                now = int(time.time() * 1000)
                self.market_buffer.replace_book(parse_book(self.client.funding_book("fUSD", 250)), now)
                historical = parse_trades(self.client.funding_trades("fUSD", now - 24 * 60 * 60_000, 10_000, sort=-1))
                if historical:
                    self.market_buffer.replace_trades(historical)
            except BitfinexError as exc:
                self.store.record_event("WARNING", "REST_MARKET_FAILED", {"error": str(exc)})
        book, trades, updated = self.market_buffer.snapshot()
        return build_market_snapshot(book, trades, self.policy, last_update_ms=updated)

    def _sync_grid(self, account: AccountSnapshot) -> None:
        self.executor.sync_account(account)
        self.store.record_account_sample(account)

    def _deployable(self, account: AccountSnapshot, target_pool: str | None = None) -> tuple[D, D, D]:
        managed_ids = self.store.managed_offer_ids()
        target_ids = managed_ids if target_pool is None else self.store.managed_offer_ids({target_pool})
        managed_open = sum((offer.amount for offer in account.offers if offer.offer_id in target_ids), D("0"))
        deployable = account.wallet_available + managed_open
        # Bitfinex funding-wallet balance is the authoritative total; its
        # available field plus open offers/credits are only reconciliation parts.
        total = account.wallet_total
        committed = (
            sum((offer.amount for offer in account.offers if offer.offer_id not in target_ids), D("0"))
            + sum((credit.amount for credit in account.credits), D("0"))
            + sum((loan.amount for loan in account.loans), D("0"))
        )
        return deployable, total, committed

    def _small_idle_target(self, account: AccountSnapshot) -> str | None:
        if not self.policy.idle_merge_trigger <= account.wallet_available < D("150"):
            return None
        active_pools = {row["pool"] for row in self.store.active_rungs() if row["offer_id"] is not None}
        return "short" if "short" in active_pools else "medium" if "medium" in active_pools else None

    def _bottom_trigger(self, account: AccountSnapshot) -> tuple[str | None, D | None]:
        open_by_id = {offer.offer_id: offer for offer in account.offers}
        candidates = [
            row
            for row in self.store.active_rungs()
            if row["pool"] in {"short", "medium"} and int(row["rung_index"]) == 0
        ]
        for row in candidates:
            offer = open_by_id.get(row["offer_id"])
            remaining = D("0") if offer is None else offer.amount
            if bottom_rung_triggered(D(row["amount_original"]), remaining, self.policy.partial_fill_trigger_percent):
                group_anchor = None
                with self.store.connect() as db:
                    group = db.execute(
                        "SELECT anchor FROM grid_groups WHERE group_id=? AND generation=?",
                        (row["group_id"], row["generation"]),
                    ).fetchone()
                    if group:
                        group_anchor = D(group[0])
                return str(row["pool"]), group_anchor
        return None, None

    def _floor_stale_trigger(self, now_ms: int) -> str | None:
        for row in self.store.active_rungs():
            floor = gross_daily_floor(self.policy.floor_apr_percent(row["pool"]), self.policy.normal_fee_percent)
            if D(row["rate"]) <= floor:
                self.store.mark_floor_reached(row["offer_key"], now_ms)
            if floor_stale(
                row["pool"],
                D(row["rate"]),
                floor,
                row["floor_reached_at_ms"],
                now_ms,
                self.policy,
            ):
                return str(row["pool"])
        return None

    def cycle(self, *, force_full: bool = False) -> StrategyStatus:
        now = int(time.time() * 1000)
        try:
            account = self.client.account_snapshot(self.policy.currency)
        except BitfinexError as exc:
            self.store.enter_safe(f"account synchronization failed: {exc}")
            self.last_action = "SAFE_ACCOUNT_SYNC"
            return self.status()
        self.last_account = account
        if not account.authoritative:
            self.store.enter_safe("account available balance is not authoritative")
            self.last_action = "SAFE_ACCOUNT_UNKNOWN"
            self._write_status()
            return self.status()
        self._sync_grid(account)
        mode = self.store.mode()
        if mode != RuntimeMode.SAFE and self.store.unresolved_intents():
            self.store.enter_safe("unfinished execution intent recovered at startup")
            mode = RuntimeMode.SAFE
        if mode == RuntimeMode.SAFE:
            self.store.record_consistent_snapshot(account.as_of_ms)
            self.last_action = "SAFE_RECOVERY_CHECK"
            self._write_status()
            return self.status()

        full_due = force_full or now - self._last_full_ms >= self.policy.full_replan_seconds * 1000
        market = self._snapshot_market(refresh_rest=full_due)
        self.last_market = market
        if not market.fresh or market.valid_components < 2:
            if mode == RuntimeMode.LIVE:
                self.store.enter_safe("market data is stale or has fewer than two valid anchor signals")
            self.last_action = "NO_WRITE_STALE_MARKET"
            self._write_status()
            return self.status()

        triggered_pool, old_anchor = self._bottom_trigger(account)
        stale_pool = self._floor_stale_trigger(now)
        fast_replan = bool(triggered_pool and old_anchor is not None and fast_shift_allowed(old_anchor, market))
        if triggered_pool and not fast_replan and not full_due:
            self.last_action = "RISING_MARKET_HOLD"
            self._write_status()
            return self.status()
        if not (full_due or fast_replan or stale_pool):
            self.last_action = "FAST_SYNC_OK"
            self._write_status()
            return self.status()

        small_idle_target = None if (triggered_pool or fast_replan or stale_pool) else self._small_idle_target(account)
        target_pool = (
            triggered_pool if fast_replan else stale_pool if stale_pool in {"short", "medium"} else small_idle_target
        )
        deployable, total, committed = self._deployable(account, target_pool)
        plan = build_plan(
            self.policy,
            market,
            deployable,
            total,
            self.store.planner_state(),
            target_pool=target_pool,
            existing_committed=committed,
        )
        self.last_plan = plan
        reason = (
            "BOTTOM_RUNG_SHIFT"
            if fast_replan
            else "FLOOR_STALE"
            if stale_pool
            else f"IDLE_MERGE_{small_idle_target.upper()}"
            if small_idle_target
            else "FULL_REPLAN"
        )
        try:
            targets = {target_pool} if target_pool else {"long"} if stale_pool == "long" else None
            self.last_action = self.executor.reconcile(plan, account, reason, targets)
        except ExecutionBlocked as exc:
            self.last_action = f"BLOCKED: {exc}"
        self._last_full_ms = now if full_due else self._last_full_ms
        self._write_status()
        return self.status()

    def status(self) -> StrategyStatus:
        return StrategyStatus(
            mode=self.store.mode(),
            market=self.last_market,
            account=self.last_account,
            plan=self.last_plan,
            safe_reason=self.store.safe_reason(),
        )

    def status_payload(self) -> dict[str, Any]:
        payload = self.store.status_payload()
        payload.update(
            {
                "version": "0.4.0",
                "strategy": "V4",
                "last_action": self.last_action,
                "policy": policy_payload(self.policy),
                "floor_daily_rates": {
                    pool: gross_daily_floor(self.policy.floor_apr_percent(pool), self.policy.normal_fee_percent)
                    for pool in ("short", "medium", "long")
                },
                "market": asdict(self.last_market) if self.last_market else None,
                "account": asdict(self.last_account) if self.last_account else None,
                "plan": asdict(self.last_plan) if self.last_plan else None,
            }
        )
        return json.loads(json.dumps(payload, default=_encode, ensure_ascii=False))

    def _write_status(self) -> None:
        atomic_write(self.settings.status_file, json.dumps(self.status_payload(), ensure_ascii=False, indent=2))

    def run(self) -> None:
        try:
            self.worker_lock.acquire()
        except LiveLockError as exc:
            self.store.record_event("ERROR", "WORKER_LOCK_REJECTED", {"error": str(exc)})
            self.last_action = "WORKER_LOCK_REJECTED"
            self._write_status()
            return
        self.store.close_unsent_planned_intents()
        mode = self.store.mode()
        if mode == RuntimeMode.LIVE:
            try:
                self.live_lock.acquire()
            except LiveLockError as exc:
                self.store.enter_safe(str(exc))
                self.last_action = "SAFE_LIVE_LOCK"
                self._write_status()
                self.worker_lock.release()
                return
        try:
            self.bootstrap_market()
        except BitfinexError as exc:
            self.store.record_event("WARNING", "MARKET_BOOTSTRAP_FAILED", {"error": str(exc)})
        self.market_stream.start()
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self.request_stop)
            signal.signal(signal.SIGTERM, self.request_stop)
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                self.cycle()
                wait = max(0.1, self.policy.fast_sync_seconds - (time.monotonic() - started))
                self._stop.wait(wait)
        finally:
            self.market_stream.stop()
            self.live_lock.release()
            self.worker_lock.release()

    def enable_live(self, confirmation: str) -> None:
        if confirmation != "ENABLE V4 LIVE":
            raise ValueError("LIVE confirmation must exactly equal: ENABLE V4 LIVE")
        if not self.settings.api_key or not self.settings.api_secret:
            raise ValueError("API credentials are required for LIVE")
        if self.store.unresolved_intents():
            raise ValueError("unresolved execution intents block LIVE")
        from .validation import shadow_audit

        audit = shadow_audit(self.store, self.policy.shadow_days, self.policy)
        if not audit["ready_for_manual_review"]:
            raise ValueError("七天 SHADOW 安全审计尚未达标")
        validation = self.store.latest_validation()
        if not validation or not validation["passed"] or validation["evidence_end_ms"] < audit["end_ms"]:
            raise ValueError("需要覆盖当前 SHADOW 期末的已通过验证报告")
        try:
            self.live_lock.acquire()
        except LiveLockError:
            raise
        self.store.set_mode(RuntimeMode.LIVE)

    def disable_live(self, mode: RuntimeMode = RuntimeMode.SHADOW) -> None:
        self.store.set_mode(mode)
        self.live_lock.release()

    def adopt_external(self, offer_ids: list[int], confirmations: dict[str, str]) -> int:
        if self.store.mode() != RuntimeMode.PAUSED:
            raise ValueError("外部订单接管只允许在 PAUSED 模式执行")
        if not self.policy.adopt_external_offers:
            raise ValueError("配置未开放外部订单接管")
        if self.last_account is None or not self.last_account.authoritative:
            raise ValueError("需要最新权威账户快照")
        managed = self.store.managed_offer_ids()
        available = {item.offer_id: item for item in self.last_account.offers if item.offer_id not in managed}
        selected = []
        allowed_periods = set(self.policy.short_periods + self.policy.medium_periods + (self.policy.long_period,))
        for offer_id in sorted(set(int(item) for item in offer_ids)):
            if confirmations.get(str(offer_id)) != f"ADOPT {offer_id}":
                raise ValueError(f"订单 {offer_id} 缺少逐笔确认")
            offer = available.get(offer_id)
            if offer is None:
                raise ValueError(f"订单 {offer_id} 不存在或已由 V4 管理")
            if offer.currency != "USD" or offer.offer_type != "LIMIT" or offer.flags != 0 or offer.hidden:
                raise ValueError(f"订单 {offer_id} 不是可见 fUSD LIMIT")
            if offer.period not in allowed_periods:
                raise ValueError(f"订单 {offer_id} 的期限不受 V4 支持")
            selected.append(offer)
        return self.store.adopt_offers(selected, self.last_account.as_of_ms)
