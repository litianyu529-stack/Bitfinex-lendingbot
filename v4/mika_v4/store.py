from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .domain import (
    AllocationPlan,
    IntentState,
    LongGateState,
    PeriodChoice,
    PlannedOffer,
    PlannerState,
    RuntimeMode,
)


D = Decimal
SCHEMA_VERSION = 1


def _encode(value: Any) -> Any:
    if isinstance(value, D):
        return format(value, "f")
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"cannot encode {type(value)!r}")


def plan_to_json(plan: AllocationPlan) -> str:
    return json.dumps(asdict(plan), default=_encode, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def plan_from_json(raw: str) -> AllocationPlan:
    data = json.loads(raw)
    state = data["state"]
    planner = PlannerState(
        short_period=PeriodChoice(**state["short_period"]),
        medium_period=PeriodChoice(**state["medium_period"]),
        long_gate=LongGateState(**state["long_gate"]),
    )
    orders = tuple(
        PlannedOffer(
            key=item["key"],
            pool=item["pool"],
            rung_index=int(item["rung_index"]),
            amount=D(item["amount"]),
            rate=D(item["rate"]),
            period=int(item["period"]),
            group_generation=int(item.get("group_generation", 1)),
        )
        for item in data["orders"]
    )
    return AllocationPlan(
        as_of_ms=int(data["as_of_ms"]),
        anchor=D(data["anchor"]),
        step=D(data["step"]),
        deployable=D(data["deployable"]),
        planned_amount=D(data["planned_amount"]),
        idle_amount=D(data["idle_amount"]),
        long_tier=int(data["long_tier"]),
        orders=orders,
        state=planner,
        reasons=tuple(data.get("reasons", [])),
    )


class V4Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS schema_info(version INTEGER NOT NULL);
                INSERT INTO schema_info(version) SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_info);
                CREATE TABLE IF NOT EXISTS runtime_state(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), mode TEXT NOT NULL,
                    previous_mode TEXT NOT NULL, safe_reason TEXT, consistent_syncs INTEGER NOT NULL DEFAULT 0,
                    last_authoritative_ms INTEGER, updated_at_ms INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO runtime_state VALUES(1,'SHADOW','SHADOW',NULL,0,NULL,0);
                CREATE TABLE IF NOT EXISTS planner_state(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL, updated_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS grid_groups(
                    group_id TEXT NOT NULL, generation INTEGER NOT NULL, pool TEXT NOT NULL,
                    anchor TEXT NOT NULL, step TEXT NOT NULL, period INTEGER NOT NULL, status TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL, rebuilt_at_ms INTEGER NOT NULL, rebuild_reason TEXT,
                    PRIMARY KEY(group_id,generation)
                );
                CREATE TABLE IF NOT EXISTS grid_rungs(
                    offer_key TEXT PRIMARY KEY, group_id TEXT NOT NULL, generation INTEGER NOT NULL,
                    pool TEXT NOT NULL, rung_index INTEGER NOT NULL, offer_id INTEGER UNIQUE,
                    amount_original TEXT NOT NULL, amount_remaining TEXT NOT NULL, rate TEXT NOT NULL,
                    period INTEGER NOT NULL, status TEXT NOT NULL, floor_reached_at_ms INTEGER,
                    last_fill_ms INTEGER, updated_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_intents(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL UNIQUE, action TEXT NOT NULL,
                    offer_key TEXT, offer_id INTEGER, amount TEXT, rate TEXT, period INTEGER,
                    state TEXT NOT NULL, error TEXT, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_plan(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    phase TEXT NOT NULL, reason TEXT, pools TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rebuild_events(pool TEXT NOT NULL, mts INTEGER NOT NULL, reason TEXT);
                CREATE INDEX IF NOT EXISTS ix_rebuild_events ON rebuild_events(pool,mts);
                CREATE TABLE IF NOT EXISTS confirmations(
                    kind TEXT NOT NULL, candidate TEXT NOT NULL, count INTEGER NOT NULL, mts INTEGER NOT NULL,
                    PRIMARY KEY(kind)
                );
                CREATE TABLE IF NOT EXISTS market_confirmations(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, mts INTEGER NOT NULL, kind TEXT NOT NULL,
                    current_value TEXT, candidate_value TEXT, confirmations INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, mts INTEGER NOT NULL, level TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shadow_plans(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, mts INTEGER NOT NULL, fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL, UNIQUE(mts,fingerprint)
                );
                CREATE TABLE IF NOT EXISTS market_trades(
                    trade_id INTEGER PRIMARY KEY, mts INTEGER NOT NULL, amount TEXT NOT NULL,
                    rate TEXT NOT NULL, period INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'v4'
                );
                CREATE TABLE IF NOT EXISTS funding_stats(
                    mts INTEGER PRIMARY KEY, value TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'v4'
                );
                CREATE TABLE IF NOT EXISTS book_snapshots(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, mts INTEGER NOT NULL, rate TEXT NOT NULL,
                    period INTEGER NOT NULL, count INTEGER NOT NULL, amount TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'v4'
                );
                CREATE TABLE IF NOT EXISTS account_samples(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, mts INTEGER NOT NULL, wallet_available TEXT NOT NULL,
                    wallet_total TEXT NOT NULL, offers_total TEXT NOT NULL, credits_total TEXT NOT NULL,
                    loans_total TEXT NOT NULL, authoritative INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS imports(
                    source_path TEXT PRIMARY KEY, source_fingerprint TEXT NOT NULL, imported_at_ms INTEGER NOT NULL,
                    rows_imported INTEGER NOT NULL, note TEXT
                );
                CREATE TABLE IF NOT EXISTS validation_reports(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, mts INTEGER NOT NULL, evidence_end_ms INTEGER NOT NULL,
                    passed INTEGER NOT NULL, payload TEXT NOT NULL
                );
            """)

    @staticmethod
    def fingerprint(plan: AllocationPlan) -> str:
        import hashlib

        structural = [
            {
                "pool": order.pool,
                "rung": order.rung_index,
                "amount": format(order.amount, "f"),
                "rate": format(order.rate, "f"),
                "period": order.period,
            }
            for order in sorted(plan.orders, key=lambda item: (item.pool, item.rung_index))
        ]
        payload = json.dumps(structural, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def mode(self) -> RuntimeMode:
        with self.connect() as db:
            return RuntimeMode(db.execute("SELECT mode FROM runtime_state WHERE singleton=1").fetchone()[0])

    def set_mode(self, mode: RuntimeMode) -> None:
        if mode == RuntimeMode.SAFE:
            raise ValueError("use enter_safe")
        now = int(time.time() * 1000)
        with self.connect() as db:
            db.execute(
                "UPDATE runtime_state SET mode=?, previous_mode=?, safe_reason=NULL, consistent_syncs=0, updated_at_ms=? WHERE singleton=1",
                (mode.value, mode.value, now),
            )

    def enter_safe(self, reason: str) -> None:
        now = int(time.time() * 1000)
        with self.connect() as db:
            row = db.execute("SELECT mode,previous_mode FROM runtime_state WHERE singleton=1").fetchone()
            previous = row[0] if row[0] != RuntimeMode.SAFE.value else row[1]
            db.execute(
                "UPDATE runtime_state SET mode='SAFE', previous_mode=?, safe_reason=?, consistent_syncs=0, last_authoritative_ms=NULL, updated_at_ms=? WHERE singleton=1",
                (previous, reason, now),
            )
            self._event(db, "ERROR", "SAFE_ENTERED", {"reason": reason})

    def safe_reason(self) -> str | None:
        with self.connect() as db:
            return db.execute("SELECT safe_reason FROM runtime_state WHERE singleton=1").fetchone()[0]

    def record_consistent_snapshot(self, as_of_ms: int, minimum_gap_ms: int = 30_000) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT mode,previous_mode,consistent_syncs,last_authoritative_ms FROM runtime_state WHERE singleton=1"
            ).fetchone()
            if row[0] != RuntimeMode.SAFE.value:
                return False
            last = row[3]
            count = int(row[2])
            if last is None or int(as_of_ms) - int(last) >= minimum_gap_ms:
                count += 1
                db.execute(
                    "UPDATE runtime_state SET consistent_syncs=?,last_authoritative_ms=?,updated_at_ms=? WHERE singleton=1",
                    (count, int(as_of_ms), int(time.time() * 1000)),
                )
            if count < 2:
                return False
            unresolved = db.execute(
                "SELECT COUNT(*) FROM execution_intents WHERE state IN ('SUBMITTING','AMBIGUOUS')"
            ).fetchone()[0]
            if unresolved:
                return False
            restored = row[1] if row[1] != RuntimeMode.LIVE.value else RuntimeMode.PAUSED.value
            db.execute(
                "UPDATE runtime_state SET mode=?,previous_mode=?,safe_reason=NULL,consistent_syncs=0,updated_at_ms=? WHERE singleton=1",
                (restored, restored, int(time.time() * 1000)),
            )
            self._event(db, "INFO", "SAFE_RECOVERED", {"mode": restored})
            return True

    def planner_state(self) -> PlannerState:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM planner_state WHERE singleton=1").fetchone()
        if not row:
            return PlannerState()
        data = json.loads(row[0])
        return PlannerState(
            short_period=PeriodChoice(**data["short_period"]),
            medium_period=PeriodChoice(**data["medium_period"]),
            long_gate=LongGateState(**data["long_gate"]),
        )

    def save_planner_state(self, state: PlannerState) -> None:
        payload = json.dumps(asdict(state), separators=(",", ":"), sort_keys=True)
        now = int(time.time() * 1000)
        with self.connect() as db:
            db.execute(
                "INSERT INTO planner_state VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET payload=excluded.payload,updated_at_ms=excluded.updated_at_ms",
                (payload, now),
            )
            rows = (
                (
                    "short_period",
                    state.short_period.current,
                    state.short_period.candidate,
                    state.short_period.confirmations,
                ),
                (
                    "medium_period",
                    state.medium_period.current,
                    state.medium_period.candidate,
                    state.medium_period.confirmations,
                ),
                ("long_tier", state.long_gate.tier, state.long_gate.candidate_tier, state.long_gate.confirmations),
            )
            db.executemany(
                "INSERT INTO market_confirmations(mts,kind,current_value,candidate_value,confirmations) VALUES(?,?,?,?,?)",
                [(now, kind, current, candidate, count) for kind, current, candidate, count in rows],
            )

    def active_rungs(self) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM grid_rungs WHERE status NOT IN ('CLOSED','REJECTED') ORDER BY pool,rung_index"
                )
            )

    def current_offer_shape(self, pools: set[str] | None = None) -> list[dict[str, Any]]:
        return [
            {
                "pool": row["pool"],
                "rung": int(row["rung_index"]),
                "amount": format(D(row["amount_remaining"]), "f"),
                "rate": format(D(row["rate"]), "f"),
                "period": int(row["period"]),
            }
            for row in self.active_rungs()
            if row["offer_id"] is not None and (pools is None or row["pool"] in pools)
        ]

    def managed_offer_ids(self, pools: set[str] | None = None) -> set[int]:
        return {
            int(row["offer_id"])
            for row in self.active_rungs()
            if row["offer_id"] is not None and (pools is None or row["pool"] in pools)
        }

    def save_plan_rungs(self, plan: AllocationPlan, reason: str) -> None:
        now = int(time.time() * 1000)
        by_pool: dict[str, list[PlannedOffer]] = {}
        for offer in plan.orders:
            by_pool.setdefault(offer.pool, []).append(offer)
        with self.connect() as db:
            for pool, offers in by_pool.items():
                group_id = offers[0].key.split(":", 1)[0]
                generation_row = db.execute(
                    "SELECT COALESCE(MAX(generation),0) FROM grid_groups WHERE pool=?", (pool,)
                ).fetchone()
                generation = int(generation_row[0]) + 1
                db.execute(
                    "INSERT INTO grid_groups VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        group_id,
                        generation,
                        pool,
                        format(plan.anchor, "f"),
                        format(plan.step, "f"),
                        offers[0].period,
                        "ACTIVE",
                        now,
                        now,
                        reason,
                    ),
                )
                db.execute("INSERT INTO rebuild_events VALUES(?,?,?)", (pool, now, reason))
                for offer in offers:
                    db.execute(
                        "INSERT OR IGNORE INTO grid_rungs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            offer.key,
                            group_id,
                            generation,
                            pool,
                            offer.rung_index,
                            None,
                            format(offer.amount, "f"),
                            format(offer.amount, "f"),
                            format(offer.rate, "f"),
                            offer.period,
                            "PLANNED",
                            None,
                            None,
                            now,
                        ),
                    )

    def update_rung_offer(self, offer_key: str, offer_id: int, status: str = "OPEN") -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE grid_rungs SET offer_id=?,status=?,updated_at_ms=? WHERE offer_key=?",
                (int(offer_id), status, int(time.time() * 1000), offer_key),
            )

    def mark_rung_rejected(self, offer_key: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE grid_rungs SET status='REJECTED',updated_at_ms=? WHERE offer_key=?",
                (int(time.time() * 1000), offer_key),
            )

    def update_rung_snapshot(self, offer_id: int, remaining: D, status: str, as_of_ms: int) -> None:
        with self.connect() as db:
            row = db.execute("SELECT amount_remaining FROM grid_rungs WHERE offer_id=?", (offer_id,)).fetchone()
            if not row:
                return
            fill_ms = as_of_ms if D(str(row[0])) > D(remaining) else None
            db.execute(
                "UPDATE grid_rungs SET amount_remaining=?,status=?,last_fill_ms=COALESCE(?,last_fill_ms),updated_at_ms=? WHERE offer_id=?",
                (format(D(remaining), "f"), status, fill_ms, as_of_ms, offer_id),
            )

    def mark_floor_reached(self, offer_key: str, as_of_ms: int) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE grid_rungs SET floor_reached_at_ms=COALESCE(floor_reached_at_ms,?),updated_at_ms=? WHERE offer_key=?",
                (as_of_ms, as_of_ms, offer_key),
            )

    def close_missing_rungs(self, open_offer_ids: set[int], as_of_ms: int) -> None:
        with self.connect() as db:
            rows = db.execute(
                "SELECT offer_key,offer_id FROM grid_rungs WHERE offer_id IS NOT NULL AND status NOT IN ('CLOSED','REJECTED')"
            ).fetchall()
            for row in rows:
                if int(row["offer_id"]) not in open_offer_ids:
                    db.execute(
                        "UPDATE grid_rungs SET amount_remaining='0',status='CLOSED',updated_at_ms=? WHERE offer_key=?",
                        (as_of_ms, row["offer_key"]),
                    )

    def rebuild_count(self, pool: str, now_ms: int) -> int:
        with self.connect() as db:
            return int(
                db.execute(
                    "SELECT COUNT(*) FROM rebuild_events WHERE pool=? AND mts>=?", (pool, now_ms - 3_600_000)
                ).fetchone()[0]
            )

    def last_rebuild_ms(self, pool: str) -> int:
        with self.connect() as db:
            return int(
                db.execute("SELECT COALESCE(MAX(mts),0) FROM rebuild_events WHERE pool=?", (pool,)).fetchone()[0]
            )

    def create_intent(
        self,
        fingerprint: str,
        action: str,
        *,
        offer_key: str | None = None,
        offer_id: int | None = None,
        amount: D | None = None,
        rate: D | None = None,
        period: int | None = None,
    ) -> bool:
        now = int(time.time() * 1000)
        with self.connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO execution_intents(fingerprint,action,offer_key,offer_id,amount,rate,period,state,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    fingerprint,
                    action,
                    offer_key,
                    offer_id,
                    None if amount is None else format(amount, "f"),
                    None if rate is None else format(rate, "f"),
                    period,
                    IntentState.PLANNED.value,
                    now,
                    now,
                ),
            )
            return cursor.rowcount == 1

    def set_intent_state(self, fingerprint: str, state: IntentState, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE execution_intents SET state=?,error=?,updated_at_ms=? WHERE fingerprint=?",
                (state.value, error, int(time.time() * 1000), fingerprint),
            )

    def unresolved_intents(self) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(
                db.execute("SELECT * FROM execution_intents WHERE state IN ('PLANNED','SUBMITTING','AMBIGUOUS')")
            )

    def close_unsent_planned_intents(self) -> int:
        now = int(time.time() * 1000)
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE execution_intents SET state='CLOSED',error='process stopped before send',updated_at_ms=? "
                "WHERE state='PLANNED'",
                (now,),
            )
            if cursor.rowcount:
                self._event(db, "WARNING", "UNSENT_INTENTS_CLOSED", {"count": cursor.rowcount})
            return cursor.rowcount

    def set_pending_plan(self, plan: AllocationPlan, phase: str, reason: str, pools: set[str]) -> None:
        now = int(time.time() * 1000)
        with self.connect() as db:
            db.execute(
                "INSERT INTO pending_plan VALUES(1,?,?,?,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET payload=excluded.payload,fingerprint=excluded.fingerprint,phase=excluded.phase,reason=excluded.reason,pools=excluded.pools,updated_at_ms=excluded.updated_at_ms",
                (plan_to_json(plan), self.fingerprint(plan), phase, reason, json.dumps(sorted(pools)), now, now),
            )

    def update_pending_phase(self, phase: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE pending_plan SET phase=?,updated_at_ms=? WHERE singleton=1",
                (phase, int(time.time() * 1000)),
            )

    def get_pending_plan(self) -> tuple[AllocationPlan, str, str, set[str]] | None:
        with self.connect() as db:
            row = db.execute("SELECT payload,phase,reason,pools FROM pending_plan WHERE singleton=1").fetchone()
        return None if row is None else (plan_from_json(row[0]), row[1], row[2] or "", set(json.loads(row[3])))

    def clear_pending_plan(self) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM pending_plan WHERE singleton=1")

    def record_shadow_plan(self, plan: AllocationPlan) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO shadow_plans(mts,fingerprint,payload) VALUES(?,?,?)",
                (plan.as_of_ms, self.fingerprint(plan), plan_to_json(plan)),
            )

    def record_account_sample(self, snapshot: Any) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO account_samples(mts,wallet_available,wallet_total,offers_total,credits_total,loans_total,authoritative) VALUES(?,?,?,?,?,?,?)",
                (
                    snapshot.as_of_ms,
                    format(snapshot.wallet_available, "f"),
                    format(snapshot.wallet_total, "f"),
                    format(sum((x.amount for x in snapshot.offers), D("0")), "f"),
                    format(sum((x.amount for x in snapshot.credits), D("0")), "f"),
                    format(sum((x.amount for x in snapshot.loans), D("0")), "f"),
                    int(snapshot.authoritative),
                ),
            )

    def reconcile_ambiguous(self, snapshot: Any) -> None:
        """Resolve unknown writes only after the same authoritative evidence is seen twice."""
        if not snapshot.authoritative:
            return
        offers = {int(item.offer_id): item for item in snapshot.offers}
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM execution_intents WHERE state IN ('SUBMITTING','AMBIGUOUS') ORDER BY id"
            ).fetchall()
            for row in rows:
                candidate: str | None = None
                if row["action"] == "CANCEL" and row["offer_id"] is not None:
                    if int(row["offer_id"]) not in offers:
                        candidate = "ABSENT"
                elif row["action"] == "SUBMIT":
                    matching = [
                        item
                        for item in snapshot.offers
                        if D(item.amount_original) == D(row["amount"])
                        and D(item.rate) == D(row["rate"])
                        and int(item.period) == int(row["period"])
                        and int(item.mts_created) >= int(row["created_at_ms"]) - 5_000
                    ]
                    if len(matching) == 1:
                        candidate = str(matching[0].offer_id)
                kind = f"ambiguous:{row['fingerprint']}"
                if candidate is None:
                    db.execute("DELETE FROM confirmations WHERE kind=?", (kind,))
                    continue
                existing = db.execute("SELECT candidate,count,mts FROM confirmations WHERE kind=?", (kind,)).fetchone()
                if existing and existing[0] == candidate and snapshot.as_of_ms - int(existing[2]) >= 30_000:
                    count = int(existing[1]) + 1
                elif existing and existing[0] == candidate:
                    count = int(existing[1])
                else:
                    count = 1
                db.execute(
                    "INSERT INTO confirmations(kind,candidate,count,mts) VALUES(?,?,?,?) ON CONFLICT(kind) DO UPDATE SET candidate=excluded.candidate,count=excluded.count,mts=excluded.mts",
                    (kind, candidate, count, snapshot.as_of_ms),
                )
                if count < 2:
                    continue
                db.execute(
                    "UPDATE execution_intents SET state='CONFIRMED',error=NULL,updated_at_ms=? WHERE id=?",
                    (snapshot.as_of_ms, row["id"]),
                )
                if row["action"] == "SUBMIT" and row["offer_key"]:
                    db.execute(
                        "UPDATE grid_rungs SET offer_id=?,status='OPEN',updated_at_ms=? WHERE offer_key=?",
                        (int(candidate), snapshot.as_of_ms, row["offer_key"]),
                    )
                db.execute("DELETE FROM confirmations WHERE kind=?", (kind,))

    def record_event(self, level: str, kind: str, payload: dict[str, Any]) -> None:
        with self.connect() as db:
            self._event(db, level, kind, payload)

    def record_validation_report(self, passed: bool, payload: str, evidence_end_ms: int) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO validation_reports(mts,evidence_end_ms,passed,payload) VALUES(?,?,?,?)",
                (int(time.time() * 1000), int(evidence_end_ms), int(passed), payload),
            )

    def latest_validation(self) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM validation_reports ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def adopt_offers(self, offers: list[Any], as_of_ms: int) -> int:
        adopted = 0
        by_pool: dict[tuple[str, int], list[Any]] = {}
        for offer in offers:
            pool = "short" if offer.period <= 7 else "medium" if offer.period <= 30 else "long"
            by_pool.setdefault((pool, offer.period), []).append(offer)
        with self.connect() as db:
            for (pool, period), selected in by_pool.items():
                group_id = f"adopted-{pool}-{period}-{as_of_ms}"
                generation = int(
                    db.execute(
                        "SELECT COALESCE(MAX(generation),0)+1 FROM grid_groups WHERE pool=?", (pool,)
                    ).fetchone()[0]
                )
                anchor = min((item.rate for item in selected), default=D("0"))
                db.execute(
                    "INSERT INTO grid_groups VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        group_id,
                        generation,
                        pool,
                        format(anchor, "f"),
                        "0",
                        period,
                        "ACTIVE",
                        as_of_ms,
                        as_of_ms,
                        "EXPLICIT_EXTERNAL_ADOPTION",
                    ),
                )
                for index, offer in enumerate(sorted(selected, key=lambda item: (item.rate, item.offer_id))):
                    key = f"adopted:{offer.offer_id}"
                    cursor = db.execute(
                        "INSERT OR IGNORE INTO grid_rungs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            key,
                            group_id,
                            generation,
                            pool,
                            index,
                            offer.offer_id,
                            format(offer.amount_original, "f"),
                            format(offer.amount, "f"),
                            format(offer.rate, "f"),
                            offer.period,
                            "OPEN",
                            None,
                            None,
                            as_of_ms,
                        ),
                    )
                    adopted += cursor.rowcount
            self._event(db, "WARNING", "EXTERNAL_OFFERS_ADOPTED", {"offer_ids": [item.offer_id for item in offers]})
        return adopted

    @staticmethod
    def _event(db: sqlite3.Connection, level: str, kind: str, payload: dict[str, Any]) -> None:
        db.execute(
            "INSERT INTO events(mts,level,kind,payload) VALUES(?,?,?,?)",
            (int(time.time() * 1000), level, kind, json.dumps(payload, default=_encode, ensure_ascii=False)),
        )

    def status_payload(self) -> dict[str, Any]:
        with self.connect() as db:
            state = dict(db.execute("SELECT * FROM runtime_state WHERE singleton=1").fetchone())
            active = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM grid_rungs WHERE status NOT IN ('CLOSED','REJECTED') ORDER BY pool,rung_index"
                )
            ]
            events = [dict(row) for row in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 30")]
            pending = db.execute(
                "SELECT fingerprint,phase,reason,pools,created_at_ms,updated_at_ms FROM pending_plan WHERE singleton=1"
            ).fetchone()
        return {
            "runtime": state,
            "active_rungs": active,
            "events": events,
            "pending_plan": dict(pending) if pending else None,
            "validation": self.latest_validation(),
        }
