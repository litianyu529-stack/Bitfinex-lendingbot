import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from decimal import Decimal

from WriteRecovery import (
    mode_after_ambiguous_resolution,
    restart_transition,
    unique_unbound_candidate,
)


D = Decimal
RUNTIME_MODES = {"PAUSED", "LIVE", "REPLAY", "SAFE", "APPLYING"}
OPEN_INTENT_STATES = {"PLANNED", "SUBMITTING", "AMBIGUOUS"}


class StateStoreError(Exception):
    pass


class DuplicateIntent(StateStoreError):
    pass


class InsufficientReservedBalance(StateStoreError):
    pass


def _decimal_text(value):
    return format(D(value), "f")


def order_intent_fingerprint(currency, amount, rate, period, offer_type, flags, strategy_version, slice_key):
    payload = "|".join(
        (
            str(currency).upper(),
            _decimal_text(amount),
            _decimal_text(rate),
            str(int(period)),
            str(offer_type).upper(),
            str(int(flags)),
            str(strategy_version),
            str(slice_key),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LendingStateStore:
    def __init__(self, path, clock=time.time):
        self.path = os.path.abspath(path)
        self.clock = clock
        os.makedirs(os.path.dirname(self.path) or os.getcwd(), exist_ok=True)
        self._lock = threading.RLock()
        self._backup_before_schema_migration()
        self._initialize()

    def _now_ms(self):
        return int(self.clock() * 1000)

    def _backup_before_schema_migration(self):
        if not os.path.exists(self.path) or os.path.getsize(self.path) == 0:
            return
        connection = sqlite3.connect(self.path)
        try:
            row = connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        except sqlite3.Error:
            return
        finally:
            connection.close()
        version = 0 if row is None else int(row[0])
        if version >= 4:
            return
        backup_dir = os.path.join(os.path.dirname(self.path), "backups")
        os.makedirs(backup_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_path = os.path.join(backup_dir, f"schema-v{version}-{stamp}.sqlite3")
        suffix = 1
        while os.path.exists(backup_path):
            backup_path = os.path.join(backup_dir, f"schema-v{version}-{stamp}-{suffix}.sqlite3")
            suffix += 1
        source = sqlite3.connect(self.path)
        target = sqlite3.connect(backup_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def read_connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, immediate=False):
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _initialize(self):
        statements = [
            """CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS runtime_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                mode TEXT NOT NULL,
                previous_mode TEXT,
                safe_reason TEXT,
                safe_manual INTEGER NOT NULL DEFAULT 0,
                consistent_syncs INTEGER NOT NULL DEFAULT 0,
                last_consistent_sync_ms INTEGER,
                updated_at_ms INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS mode_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_mode TEXT,
                to_mode TEXT NOT NULL,
                reason TEXT,
                created_at_ms INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS strategy_versions (
                version_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                policy_json TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL,
                activated_at_ms INTEGER
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS one_active_strategy
                ON strategy_versions(status) WHERE status = 'ACTIVE'""",
            """CREATE UNIQUE INDEX IF NOT EXISTS one_pending_strategy
                ON strategy_versions(status) WHERE status = 'PENDING'""",
            """CREATE UNIQUE INDEX IF NOT EXISTS one_draft_strategy
                ON strategy_versions(status) WHERE status = 'DRAFT'""",
            """CREATE TABLE IF NOT EXISTS order_intents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL UNIQUE,
                currency TEXT NOT NULL,
                slice_key TEXT NOT NULL,
                pool TEXT NOT NULL,
                layer TEXT NOT NULL,
                amount TEXT NOT NULL,
                submitted_rate TEXT NOT NULL,
                effective_rate TEXT NOT NULL,
                period INTEGER NOT NULL,
                offer_type TEXT NOT NULL,
                display_type TEXT,
                flags INTEGER NOT NULL DEFAULT 0,
                strategy_version TEXT NOT NULL,
                plan_hash TEXT,
                state TEXT NOT NULL,
                exchange_offer_id INTEGER UNIQUE,
                error_text TEXT,
                created_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS order_intents_state_idx ON order_intents(state)""",
            """CREATE TABLE IF NOT EXISTS offers (
                offer_id INTEGER PRIMARY KEY,
                currency TEXT NOT NULL,
                amount TEXT NOT NULL,
                amount_original TEXT,
                rate TEXT NOT NULL,
                rate_real TEXT,
                period INTEGER NOT NULL,
                offer_type TEXT NOT NULL,
                display_type TEXT,
                flags INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                managed INTEGER NOT NULL DEFAULT 0,
                pool TEXT,
                layer TEXT,
                plan_hash TEXT,
                mts_created INTEGER,
                mts_updated INTEGER,
                last_seen_ms INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS credits (
                credit_id INTEGER PRIMARY KEY,
                currency TEXT NOT NULL,
                amount TEXT NOT NULL,
                rate TEXT NOT NULL,
                rate_real TEXT,
                period INTEGER NOT NULL,
                rate_type TEXT,
                display_type TEXT,
                hidden INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                managed INTEGER NOT NULL DEFAULT 0,
                pool TEXT,
                layer TEXT,
                offer_id INTEGER,
                mts_opening INTEGER,
                mts_updated INTEGER,
                last_seen_ms INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS funding_trades (
                trade_id INTEGER PRIMARY KEY,
                currency TEXT NOT NULL,
                offer_id INTEGER,
                amount TEXT NOT NULL,
                rate TEXT NOT NULL,
                period INTEGER NOT NULL,
                mts INTEGER NOT NULL,
                managed INTEGER NOT NULL DEFAULT 0
            )""",
            """CREATE TABLE IF NOT EXISTS credit_closures (
                credit_id INTEGER PRIMARY KEY,
                currency TEXT NOT NULL,
                amount TEXT NOT NULL,
                rate TEXT NOT NULL,
                period INTEGER NOT NULL,
                rate_type TEXT,
                hidden INTEGER NOT NULL DEFAULT 0,
                managed INTEGER NOT NULL DEFAULT 0,
                pool TEXT,
                opened_at_ms INTEGER,
                closed_at_ms INTEGER NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS credit_closures_time_idx ON credit_closures(closed_at_ms)""",
            """CREATE TABLE IF NOT EXISTS offer_history (
                offer_id INTEGER PRIMARY KEY,
                currency TEXT NOT NULL,
                amount TEXT NOT NULL,
                rate TEXT NOT NULL,
                rate_real TEXT,
                period INTEGER NOT NULL,
                offer_type TEXT NOT NULL,
                flags INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                mts_created INTEGER,
                mts_updated INTEGER,
                managed INTEGER NOT NULL DEFAULT 0
            )""",
            """CREATE TABLE IF NOT EXISTS credit_history (
                credit_id INTEGER PRIMARY KEY,
                currency TEXT NOT NULL,
                amount TEXT NOT NULL,
                rate TEXT NOT NULL,
                rate_real TEXT,
                period INTEGER NOT NULL,
                rate_type TEXT,
                hidden INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                mts_opening INTEGER,
                mts_updated INTEGER,
                managed INTEGER NOT NULL DEFAULT 0
            )""",
            """CREATE TABLE IF NOT EXISTS rate_floor_violations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                credit_id INTEGER NOT NULL,
                pool TEXT NOT NULL,
                floor_rate TEXT NOT NULL,
                observed_rate TEXT NOT NULL,
                started_at_ms INTEGER NOT NULL,
                ended_at_ms INTEGER
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS one_open_floor_violation
                ON rate_floor_violations(credit_id) WHERE ended_at_ms IS NULL""",
            """CREATE TABLE IF NOT EXISTS ledger_entries (
                ledger_id INTEGER PRIMARY KEY,
                currency TEXT NOT NULL,
                wallet TEXT,
                amount TEXT NOT NULL,
                balance TEXT,
                description TEXT,
                category INTEGER,
                mts INTEGER NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS ledger_entries_category_mts_idx
                ON ledger_entries(category, mts)""",
            """CREATE TABLE IF NOT EXISTS income_sync_state (
                currency TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                next_end_ms INTEGER,
                earliest_mts INTEGER,
                last_success_ms INTEGER,
                completed_at_ms INTEGER,
                error TEXT,
                updated_at_ms INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS market_trades (
                trade_id TEXT PRIMARY KEY,
                mts INTEGER NOT NULL,
                amount TEXT NOT NULL,
                rate TEXT NOT NULL,
                period INTEGER NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS market_trades_mts_idx ON market_trades(mts)""",
            """CREATE TABLE IF NOT EXISTS market_bars (
                interval_name TEXT NOT NULL,
                bucket_ms INTEGER NOT NULL,
                weighted_median_rate TEXT NOT NULL,
                q25_rate TEXT NOT NULL,
                q75_rate TEXT NOT NULL,
                volume TEXT NOT NULL,
                trade_count INTEGER NOT NULL,
                PRIMARY KEY (interval_name, bucket_ms)
            )""",
            """CREATE TABLE IF NOT EXISTS reprice_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                offer_id INTEGER,
                intent_id INTEGER,
                reason TEXT NOT NULL,
                old_rate TEXT,
                new_rate TEXT,
                strategy_version TEXT,
                plan_hash TEXT,
                display_type TEXT,
                created_at_ms INTEGER NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS reprice_events_time_idx ON reprice_events(created_at_ms)""",
            """CREATE TABLE IF NOT EXISTS strategy_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                from_version TEXT,
                to_version TEXT,
                plan_hash TEXT,
                reason TEXT,
                created_at_ms INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS account_samples (
                mts INTEGER PRIMARY KEY,
                total_principal TEXT NOT NULL,
                wallet_available TEXT NOT NULL,
                open_offers TEXT NOT NULL,
                active_credits TEXT NOT NULL,
                net_interest_total TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS funding_stats (
                mts INTEGER PRIMARY KEY,
                frr_daily_rate TEXT,
                utilization TEXT,
                payload_json TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS book_snapshots (
                mts INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                book_json TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS legacy_migrations (
                migration_id TEXT PRIMARY KEY,
                source_path TEXT,
                result_json TEXT NOT NULL,
                completed_at_ms INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS strategy_rollouts (
                candidate_version TEXT PRIMARY KEY,
                candidate_share INTEGER NOT NULL,
                stage_started_at_ms INTEGER NOT NULL,
                status TEXT NOT NULL,
                last_reason TEXT
            )""",
        ]
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
        finally:
            connection.close()
        with self.transaction(immediate=True) as connection:
            for statement in statements:
                connection.execute(statement)
            self._migrate_v3_audit_columns(connection)
            self._migrate_v4_columns(connection)
            connection.execute(
                """INSERT INTO schema_meta(key, value) VALUES('schema_version', '4')
                   ON CONFLICT(key) DO UPDATE SET value='4'"""
            )
            connection.execute(
                """INSERT OR IGNORE INTO income_sync_state(
                    currency, status, updated_at_ms
                ) VALUES('USD', 'PENDING', ?)""",
                (self._now_ms(),),
            )
            connection.execute(
                """INSERT OR IGNORE INTO runtime_state(
                    singleton, mode, previous_mode, updated_at_ms
                ) VALUES(1, 'PAUSED', NULL, ?)""",
                (self._now_ms(),),
            )

    @staticmethod
    def _migrate_v3_audit_columns(connection):
        required = {
            "order_intents": {
                "display_type": "TEXT",
                "plan_hash": "TEXT",
            },
            "offers": {
                "display_type": "TEXT",
                "plan_hash": "TEXT",
            },
            "reprice_events": {
                "strategy_version": "TEXT",
                "plan_hash": "TEXT",
                "display_type": "TEXT",
            },
            "credits": {
                "layer": "TEXT",
                "display_type": "TEXT",
            },
        }
        for table, columns in required.items():
            existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        connection.execute(
            """UPDATE order_intents SET display_type = CASE
                   WHEN offer_type = 'LIMIT' THEN 'LIMIT'
                   WHEN offer_type = 'FRRDELTAFIX' THEN 'FRR_DELTA_FIXED'
                   WHEN offer_type = 'FRRDELTAVAR' AND CAST(submitted_rate AS REAL) = 0 THEN 'FRR'
                   WHEN offer_type = 'FRRDELTAVAR' THEN 'FRR_DELTA_VARIABLE'
                   ELSE offer_type END
               WHERE display_type IS NULL OR display_type = ''"""
        )

    @staticmethod
    def _migrate_v4_columns(connection):
        required = {
            "order_intents": {
                "write_phase": "TEXT NOT NULL DEFAULT 'NOT_SENT'",
                "resolution": "TEXT",
                "strategy_variant": "TEXT NOT NULL DEFAULT 'baseline'",
                "request_started_at_ms": "INTEGER",
                "resolved_at_ms": "INTEGER",
            },
            "offers": {
                "strategy_variant": "TEXT NOT NULL DEFAULT 'baseline'",
            },
            "credits": {
                "strategy_variant": "TEXT NOT NULL DEFAULT 'baseline'",
            },
        }
        for table, columns in required.items():
            existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        connection.execute(
            """UPDATE offers SET display_type = CASE
                   WHEN offer_type = 'LIMIT' THEN 'LIMIT'
                   WHEN offer_type = 'FRRDELTAFIX' THEN 'FRR_DELTA_FIXED'
                   WHEN offer_type = 'FRRDELTAVAR' AND CAST(rate AS REAL) = 0 THEN 'FRR'
                   WHEN offer_type = 'FRRDELTAVAR' THEN 'FRR_DELTA_VARIABLE'
                   ELSE offer_type END
               WHERE display_type IS NULL OR display_type = ''"""
        )
        connection.execute(
            """UPDATE credits SET display_type = COALESCE(
                   (SELECT intents.display_type FROM order_intents AS intents
                    WHERE intents.exchange_offer_id = credits.offer_id LIMIT 1),
                   CASE UPPER(COALESCE(rate_type, ''))
                       WHEN 'FIXED' THEN 'LIMIT'
                       WHEN 'LIMIT' THEN 'LIMIT'
                       WHEN 'VAR' THEN 'VARIABLE_UNKNOWN'
                       WHEN 'VARIABLE' THEN 'VARIABLE_UNKNOWN'
                       WHEN 'FRRDELTAVAR' THEN 'VARIABLE_UNKNOWN'
                       ELSE NULL END)
               WHERE display_type IS NULL OR display_type = ''"""
        )

    def runtime(self):
        with self.read_connection() as connection:
            row = connection.execute("SELECT * FROM runtime_state WHERE singleton = 1").fetchone()
        return dict(row)

    def set_mode(self, mode, reason=""):
        mode = str(mode).upper()
        if mode not in RUNTIME_MODES:
            raise StateStoreError(f"unsupported runtime mode: {mode}")
        now = self._now_ms()
        with self.transaction(immediate=True) as connection:
            current = connection.execute("SELECT * FROM runtime_state WHERE singleton = 1").fetchone()
            from_mode = current["mode"]
            if from_mode == "SAFE" and current["safe_manual"] and mode != "SAFE":
                raise StateStoreError("manual SAFE must be resolved through the ambiguous intent workflow")
            if from_mode == "LIVE" and mode == "REPLAY":
                raise StateStoreError("LIVE must transition to PAUSED before REPLAY")
            previous = from_mode if mode == "SAFE" else current["previous_mode"]
            connection.execute(
                """UPDATE runtime_state
                   SET mode = ?, previous_mode = ?, safe_reason = ?, safe_manual = 0,
                       consistent_syncs = 0, last_consistent_sync_ms = NULL, updated_at_ms = ?
                   WHERE singleton = 1""",
                (mode, previous, reason if mode == "SAFE" else None, now),
            )
            connection.execute(
                "INSERT INTO mode_events(from_mode, to_mode, reason, created_at_ms) VALUES(?, ?, ?, ?)",
                (from_mode, mode, reason, now),
            )
        return self.runtime()

    def enter_safe(self, reason, manual=False):
        now = self._now_ms()
        with self.transaction(immediate=True) as connection:
            current = connection.execute("SELECT * FROM runtime_state WHERE singleton = 1").fetchone()
            previous = current["previous_mode"] if current["mode"] == "SAFE" else current["mode"]
            sticky_manual = bool(manual or (current["mode"] == "SAFE" and current["safe_manual"]))
            safe_reason = current["safe_reason"] if sticky_manual and current["safe_manual"] else str(reason)
            connection.execute(
                """UPDATE runtime_state
                   SET mode = 'SAFE', previous_mode = ?, safe_reason = ?, safe_manual = ?,
                       consistent_syncs = 0, last_consistent_sync_ms = NULL, updated_at_ms = ?
                   WHERE singleton = 1""",
                (previous, safe_reason, int(sticky_manual), now),
            )
            if current["mode"] != "SAFE":
                connection.execute(
                    "INSERT INTO mode_events(from_mode, to_mode, reason, created_at_ms) VALUES(?, 'SAFE', ?, ?)",
                    (current["mode"], str(reason), now),
                )
        return self.runtime()

    def record_consistent_sync(self, now_ms=None):
        now = int(now_ms if now_ms is not None else self._now_ms())
        with self.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM runtime_state WHERE singleton = 1").fetchone()
            if row["mode"] != "SAFE" or row["safe_manual"]:
                return dict(row)
            previous_sync = row["last_consistent_sync_ms"]
            count = row["consistent_syncs"]
            if previous_sync is None or now - previous_sync >= 30_000:
                count += 1
                connection.execute(
                    "UPDATE runtime_state SET consistent_syncs = ?, last_consistent_sync_ms = ?, "
                    "updated_at_ms = ? WHERE singleton = 1",
                    (count, now, now),
                )
            if count >= 2:
                target = row["previous_mode"] if row["previous_mode"] in {"LIVE", "PAUSED", "REPLAY"} else "PAUSED"
                connection.execute(
                    """UPDATE runtime_state SET mode = ?, previous_mode = NULL, safe_reason = NULL,
                       safe_manual = 0, consistent_syncs = 0, last_consistent_sync_ms = NULL,
                       updated_at_ms = ? WHERE singleton = 1""",
                    (target, now),
                )
                connection.execute(
                    "INSERT INTO mode_events(from_mode, to_mode, reason, created_at_ms) "
                    "VALUES('SAFE', ?, 'reconciled', ?)",
                    (target, now),
                )
        return self.runtime()

    def save_strategy(self, policy_payload, status="PENDING"):
        status = str(status).upper()
        if status not in {"DRAFT", "PENDING", "ACTIVE", "ARCHIVED"}:
            raise StateStoreError("invalid strategy status")
        serialized = json.dumps(policy_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        version_id = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
        now = self._now_ms()
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM strategy_versions WHERE version_id = ?", (version_id,)
            ).fetchone()
            if existing is not None:
                existing_status = str(existing["status"]).upper()
                if existing_status == "ACTIVE" and status in {"DRAFT", "PENDING", "ACTIVE"}:
                    return version_id
                if existing_status == status:
                    return version_id
            if status in {"DRAFT", "PENDING", "ACTIVE"}:
                connection.execute("UPDATE strategy_versions SET status = 'ARCHIVED' WHERE status = ?", (status,))
            connection.execute(
                """INSERT INTO strategy_versions(version_id, status, policy_json, created_at_ms, activated_at_ms)
                   VALUES(?, ?, ?, ?, ?)
                   ON CONFLICT(version_id) DO UPDATE SET status = excluded.status,
                       policy_json = excluded.policy_json,
                       activated_at_ms = CASE WHEN excluded.status = 'ACTIVE'
                           THEN excluded.activated_at_ms ELSE strategy_versions.activated_at_ms END""",
                (version_id, status, serialized, now, now if status == "ACTIVE" else None),
            )
        return version_id

    def promote_draft_to_pending(self):
        with self.transaction(immediate=True) as connection:
            draft = connection.execute(
                "SELECT * FROM strategy_versions WHERE status = 'DRAFT' ORDER BY created_at_ms DESC LIMIT 1"
            ).fetchone()
            if draft is None:
                raise StateStoreError("no draft strategy")
            connection.execute("UPDATE strategy_versions SET status = 'ARCHIVED' WHERE status = 'PENDING'")
            connection.execute(
                "UPDATE strategy_versions SET status = 'PENDING' WHERE version_id = ?",
                (draft["version_id"],),
            )
        return self.strategy("PENDING")

    def normalize_active_strategy(self, policy_payload, reason="schema normalization"):
        serialized = json.dumps(policy_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        version_id = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
        now = self._now_ms()
        with self.transaction(immediate=True) as connection:
            active = connection.execute(
                "SELECT version_id FROM strategy_versions WHERE status='ACTIVE' ORDER BY activated_at_ms DESC LIMIT 1"
            ).fetchone()
            from_version = None if active is None else active["version_id"]
            if from_version == version_id:
                return version_id
            connection.execute("UPDATE strategy_versions SET status='ARCHIVED' WHERE status='ACTIVE'")
            connection.execute(
                """INSERT INTO strategy_versions(version_id, status, policy_json, created_at_ms, activated_at_ms)
                   VALUES(?, 'ACTIVE', ?, ?, ?)
                   ON CONFLICT(version_id) DO UPDATE SET status='ACTIVE', policy_json=excluded.policy_json,
                       activated_at_ms=excluded.activated_at_ms""",
                (version_id, serialized, now, now),
            )
            connection.execute(
                """INSERT INTO strategy_events(event_type, from_version, to_version, plan_hash, reason, created_at_ms)
                   VALUES('SCHEMA_NORMALIZATION', ?, ?, NULL, ?, ?)""",
                (from_version, version_id, str(reason), now),
            )
        return version_id

    def strategy(self, status="ACTIVE"):
        with self.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM strategy_versions WHERE status = ? ORDER BY created_at_ms DESC LIMIT 1",
                (str(status).upper(),),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["policy"] = json.loads(result.pop("policy_json"))
        return result

    def discard_strategy(self, status="DRAFT"):
        status = str(status).upper()
        if status not in {"DRAFT", "PENDING"}:
            raise StateStoreError("only DRAFT or PENDING can be discarded")
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE strategy_versions SET status='ARCHIVED' WHERE status=?",
                (status,),
            )
        return self.strategy(status)

    def activate_pending_strategy(self, plan_hash=None, reason="pending adjustments confirmed"):
        now = self._now_ms()
        with self.transaction(immediate=True) as connection:
            pending = connection.execute(
                "SELECT * FROM strategy_versions WHERE status = 'PENDING' ORDER BY created_at_ms DESC LIMIT 1"
            ).fetchone()
            if pending is None:
                raise StateStoreError("no pending strategy")
            active = connection.execute("SELECT version_id FROM strategy_versions WHERE status = 'ACTIVE'").fetchone()
            connection.execute("UPDATE strategy_versions SET status = 'ARCHIVED' WHERE status = 'ACTIVE'")
            connection.execute(
                "UPDATE strategy_versions SET status = 'ACTIVE', activated_at_ms = ? WHERE version_id = ?",
                (now, pending["version_id"]),
            )
            connection.execute(
                """INSERT INTO strategy_events(
                    event_type, from_version, to_version, plan_hash, reason, created_at_ms
                ) VALUES('ACTIVATE', ?, ?, ?, ?, ?)""",
                (
                    None if active is None else active["version_id"],
                    pending["version_id"],
                    plan_hash,
                    str(reason),
                    now,
                ),
            )
        return self.strategy("ACTIVE")

    def reserved_amount(self, currency="USD", connection=None):
        owns_connection = connection is None
        connection = connection or self._connect()
        try:
            placeholders = ",".join("?" for _ in OPEN_INTENT_STATES)
            row = connection.execute(
                f"SELECT amount FROM order_intents WHERE currency = ? AND state IN ({placeholders})",
                (currency.upper(), *sorted(OPEN_INTENT_STATES)),
            ).fetchall()
            return sum((D(item["amount"]) for item in row), D("0"))
        finally:
            if owns_connection:
                connection.close()

    def reserve_intent(self, order, wallet_available):
        fingerprint = order.get("fingerprint") or order_intent_fingerprint(
            order.get("currency", "USD"),
            order["amount"],
            order["submitted_rate"],
            order["period"],
            order["offer_type"],
            order.get("flags", 0),
            order["strategy_version"],
            order["slice_key"],
        )
        now = self._now_ms()
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM order_intents WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            if existing is not None:
                return False, dict(existing)
            reserved = self.reserved_amount(order.get("currency", "USD"), connection)
            if reserved + D(order["amount"]) > D(wallet_available):
                raise InsufficientReservedBalance(
                    f"reserved {reserved} plus {order['amount']} exceeds wallet {wallet_available}"
                )
            cursor = connection.execute(
                """INSERT INTO order_intents(
                    fingerprint, currency, slice_key, pool, layer, amount, submitted_rate,
                    effective_rate, period, offer_type, display_type, flags, strategy_version, plan_hash, state,
                    write_phase, strategy_variant, created_at_ms, updated_at_ms
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PLANNED', 'NOT_SENT', ?, ?, ?)""",
                (
                    fingerprint,
                    order.get("currency", "USD").upper(),
                    str(order["slice_key"]),
                    order["pool"],
                    order["layer"],
                    _decimal_text(order["amount"]),
                    _decimal_text(order["submitted_rate"]),
                    _decimal_text(order["effective_rate"]),
                    int(order["period"]),
                    order["offer_type"],
                    order.get("display_type") or order["offer_type"],
                    int(order.get("flags", 0)),
                    str(order["strategy_version"]),
                    order.get("plan_hash"),
                    str(order.get("strategy_variant") or "baseline"),
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM order_intents WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return True, dict(row)

    def _set_intent_state(
        self,
        intent_id,
        state,
        exchange_offer_id=None,
        error_text=None,
        write_phase=None,
        resolution=None,
    ):
        now = self._now_ms()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE order_intents SET state = ?, exchange_offer_id = COALESCE(?, exchange_offer_id),
                   error_text = ?, write_phase = COALESCE(?, write_phase),
                   resolution = COALESCE(?, resolution),
                   resolved_at_ms = CASE WHEN ? IS NULL THEN resolved_at_ms ELSE ? END,
                   updated_at_ms = ? WHERE id = ?""",
                (
                    state,
                    exchange_offer_id,
                    error_text,
                    write_phase,
                    resolution,
                    resolution,
                    now,
                    now,
                    int(intent_id),
                ),
            )
            row = connection.execute("SELECT * FROM order_intents WHERE id = ?", (int(intent_id),)).fetchone()
        if row is None:
            raise StateStoreError("unknown order intent")
        return dict(row)

    def mark_submitting(self, intent_id):
        now = self._now_ms()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE order_intents SET state='SUBMITTING', write_phase='SENT',
                   request_started_at_ms=?, updated_at_ms=? WHERE id=?""",
                (now, now, int(intent_id)),
            )
        return self.intent(intent_id)

    def confirm_intent(self, intent_id, exchange_offer_id):
        return self._set_intent_state(
            intent_id,
            "CONFIRMED",
            int(exchange_offer_id),
            write_phase="CONFIRMED",
            resolution="EXCHANGE_CONFIRMED",
        )

    def mark_ambiguous(self, intent_id, error_text):
        self._set_intent_state(
            intent_id,
            "AMBIGUOUS",
            error_text=str(error_text),
            write_phase="UNKNOWN",
            resolution="MANUAL_REQUIRED",
        )
        self.enter_safe(f"AMBIGUOUS_SUBMIT:{intent_id}", manual=True)
        return self.intent(intent_id)

    def recover_incomplete_writes(self):
        """Close never-sent plans and fail closed on requests that may have reached Bitfinex."""
        now = self._now_ms()
        planned_transition = restart_transition("PLANNED")
        submitting_transition = restart_transition("SUBMITTING")
        with self.transaction(immediate=True) as connection:
            planned = connection.execute(
                """UPDATE order_intents SET state=?, write_phase=?,
                   resolution=?, resolved_at_ms=?, updated_at_ms=?
                   WHERE state='PLANNED'""",
                (
                    planned_transition.state,
                    planned_transition.write_phase,
                    planned_transition.resolution,
                    now,
                    now,
                ),
            ).rowcount
            submitting = connection.execute("""SELECT id FROM order_intents WHERE state='SUBMITTING'""").fetchall()
            if submitting:
                connection.execute(
                    """UPDATE order_intents SET state=?, write_phase=?,
                       resolution=?,
                       error_text='process restarted before exchange confirmation',
                       resolved_at_ms=?, updated_at_ms=? WHERE state='SUBMITTING'""",
                    (
                        submitting_transition.state,
                        submitting_transition.write_phase,
                        submitting_transition.resolution,
                        now,
                        now,
                    ),
                )
        for row in submitting:
            self.enter_safe(f"AMBIGUOUS_SUBMIT:{row['id']}", manual=True)
        return {"closedBeforeSend": planned, "ambiguousAfterSend": len(submitting)}

    def resolve_ambiguous_intent(self, intent_id, exchange_offer_id=None, close=False):
        """Resolve an uncertain write only after an operator has reconciled it.

        Binding requires a concrete exchange id.  Closing means the operator has
        confirmed that no offer exists.  A manual SAFE always returns to PAUSED so
        that resuming LIVE still requires the normal preflight confirmation.
        """
        now = self._now_ms()
        with self.transaction(immediate=True) as connection:
            intent = connection.execute("SELECT * FROM order_intents WHERE id = ?", (int(intent_id),)).fetchone()
            if intent is None or intent["state"] != "AMBIGUOUS":
                raise StateStoreError("intent is not awaiting ambiguous resolution")
            if bool(close) == (exchange_offer_id is not None):
                raise StateStoreError("choose exactly one resolution: bind offer or confirm absent")
            if exchange_offer_id is not None:
                duplicate = connection.execute(
                    "SELECT id FROM order_intents WHERE exchange_offer_id = ? AND id != ?",
                    (int(exchange_offer_id), int(intent_id)),
                ).fetchone()
                if duplicate is not None:
                    raise StateStoreError("exchange offer is already bound to another intent")
                connection.execute(
                    """UPDATE order_intents SET state='CONFIRMED', exchange_offer_id=?,
                       error_text=NULL, write_phase='CONFIRMED', resolution='OPERATOR_BOUND',
                       resolved_at_ms=?, updated_at_ms=? WHERE id=?""",
                    (int(exchange_offer_id), now, now, int(intent_id)),
                )
            else:
                connection.execute(
                    """UPDATE order_intents SET state='CLOSED', error_text='operator confirmed absent',
                       write_phase='NOT_CREATED', resolution='OPERATOR_CONFIRMED_ABSENT',
                       resolved_at_ms=?, updated_at_ms=? WHERE id=?""",
                    (now, now, int(intent_id)),
                )
            unresolved = connection.execute(
                "SELECT COUNT(*) AS count FROM order_intents WHERE state='AMBIGUOUS'"
            ).fetchone()["count"]
            runtime = connection.execute("SELECT * FROM runtime_state WHERE singleton=1").fetchone()
            if mode_after_ambiguous_resolution(unresolved, runtime["mode"], runtime["safe_manual"]) == "PAUSED":
                connection.execute(
                    """UPDATE runtime_state SET mode='PAUSED', previous_mode=NULL,
                       safe_reason=NULL, safe_manual=0, consistent_syncs=0,
                       last_consistent_sync_ms=NULL, updated_at_ms=? WHERE singleton=1""",
                    (now,),
                )
                connection.execute(
                    """INSERT INTO mode_events(from_mode, to_mode, reason, created_at_ms)
                       VALUES('SAFE', 'PAUSED', 'ambiguous intent resolved', ?)""",
                    (now,),
                )
        return {"intent": self.intent(intent_id), "runtime": self.runtime()}

    def close_intent(self, intent_id):
        return self._set_intent_state(intent_id, "CLOSED", resolution="CLOSED")

    def reject_intent(self, intent_id, error_text):
        return self._set_intent_state(
            intent_id,
            "CLOSED",
            error_text=str(error_text),
            write_phase="DEFINITE_REJECT",
            resolution="EXCHANGE_REJECTED",
        )

    def intent(self, intent_id):
        with self.read_connection() as connection:
            row = connection.execute("SELECT * FROM order_intents WHERE id = ?", (int(intent_id),)).fetchone()
        return None if row is None else dict(row)

    def intents(self, states=None):
        query = "SELECT * FROM order_intents"
        params = ()
        if states:
            normalized = [str(state).upper() for state in states]
            query += " WHERE state IN (" + ",".join("?" for _ in normalized) + ")"
            params = tuple(normalized)
        query += " ORDER BY id"
        with self.read_connection() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def reconcile_ambiguous_candidates(self):
        """Bind only a unique authoritative offer/trade match; leave every other case manual."""
        resolved = []
        with self.transaction(immediate=True) as connection:
            intents = connection.execute("SELECT * FROM order_intents WHERE state='AMBIGUOUS' ORDER BY id").fetchall()
            for intent in intents:
                start = int(intent["request_started_at_ms"] or intent["updated_at_ms"] or 0) - 300_000
                candidate_ids = set()
                offers = connection.execute(
                    """SELECT offer_id FROM offers
                       WHERE status != 'CLOSED' AND currency=? AND amount=? AND period=?
                         AND offer_type=? AND flags=? AND COALESCE(mts_created, 0) >= ?""",
                    (
                        intent["currency"],
                        intent["amount"],
                        intent["period"],
                        intent["offer_type"],
                        intent["flags"],
                        start,
                    ),
                ).fetchall()
                candidate_ids.update(int(row["offer_id"]) for row in offers)
                trades = connection.execute(
                    """SELECT DISTINCT offer_id FROM funding_trades
                       WHERE currency=? AND amount=? AND period=? AND mts >= ?""",
                    (intent["currency"], intent["amount"], intent["period"], start),
                ).fetchall()
                candidate_ids.update(int(row["offer_id"]) for row in trades if row["offer_id"] is not None)
                bound = {
                    int(row["exchange_offer_id"])
                    for row in connection.execute(
                        "SELECT exchange_offer_id FROM order_intents WHERE exchange_offer_id IS NOT NULL AND id != ?",
                        (intent["id"],),
                    ).fetchall()
                }
                offer_id = unique_unbound_candidate(candidate_ids, bound)
                if offer_id is None:
                    continue
                now = self._now_ms()
                connection.execute(
                    """UPDATE order_intents SET state='CONFIRMED', exchange_offer_id=?,
                       error_text=NULL, write_phase='CONFIRMED', resolution='AUTO_UNIQUE_MATCH',
                       resolved_at_ms=?, updated_at_ms=? WHERE id=?""",
                    (offer_id, now, now, intent["id"]),
                )
                connection.execute(
                    """UPDATE offers SET managed=1, pool=COALESCE(pool, ?), layer=COALESCE(layer, ?),
                       plan_hash=COALESCE(plan_hash, ?), strategy_variant=? WHERE offer_id=?""",
                    (
                        intent["pool"],
                        intent["layer"],
                        intent["plan_hash"],
                        intent["strategy_variant"],
                        offer_id,
                    ),
                )
                resolved.append({"intentId": intent["id"], "offerId": offer_id})
            if resolved:
                unresolved = connection.execute(
                    "SELECT COUNT(*) AS count FROM order_intents WHERE state='AMBIGUOUS'"
                ).fetchone()["count"]
                runtime = connection.execute("SELECT * FROM runtime_state WHERE singleton=1").fetchone()
                if mode_after_ambiguous_resolution(unresolved, runtime["mode"], runtime["safe_manual"]) == "PAUSED":
                    now = self._now_ms()
                    connection.execute(
                        """UPDATE runtime_state SET mode='PAUSED', previous_mode=NULL,
                           safe_reason=NULL, safe_manual=0, consistent_syncs=0,
                           last_consistent_sync_ms=NULL, updated_at_ms=? WHERE singleton=1""",
                        (now,),
                    )
                    connection.execute(
                        """INSERT INTO mode_events(from_mode, to_mode, reason, created_at_ms)
                           VALUES('SAFE', 'PAUSED', 'ambiguous intent uniquely reconciled', ?)""",
                        (now,),
                    )
        return resolved

    def record_legacy_migration(self, migration_id, source_path, result):
        now = self._now_ms()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT OR IGNORE INTO legacy_migrations(
                       migration_id, source_path, result_json, completed_at_ms
                   ) VALUES(?, ?, ?, ?)""",
                (
                    str(migration_id),
                    os.path.abspath(source_path) if source_path else None,
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
        return self.legacy_migration(migration_id)

    def legacy_migration(self, migration_id):
        with self.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM legacy_migrations WHERE migration_id=?", (str(migration_id),)
            ).fetchone()
        return None if row is None else {**dict(row), "result": json.loads(row["result_json"])}

    def set_rollout(self, candidate_version, candidate_share, status="ACTIVE", reason=""):
        share = int(candidate_share)
        if share not in {0, 10, 25, 50, 100}:
            raise StateStoreError("candidate share must be one of 0, 10, 25, 50, 100")
        now = self._now_ms()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO strategy_rollouts(
                       candidate_version, candidate_share, stage_started_at_ms, status, last_reason
                   ) VALUES(?, ?, ?, ?, ?)
                   ON CONFLICT(candidate_version) DO UPDATE SET
                       candidate_share=excluded.candidate_share,
                       stage_started_at_ms=excluded.stage_started_at_ms,
                       status=excluded.status, last_reason=excluded.last_reason""",
                (str(candidate_version), share, now, str(status), str(reason)),
            )
        return self.rollout(candidate_version)

    def rollout(self, candidate_version):
        with self.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM strategy_rollouts WHERE candidate_version=?",
                (str(candidate_version),),
            ).fetchone()
        return None if row is None else dict(row)

    def replenishment_slice_key(self, base_key):
        base_key = str(base_key)
        with self.read_connection() as connection:
            rows = connection.execute(
                """SELECT slice_key, state FROM order_intents
                   WHERE slice_key = ? OR slice_key LIKE ? ORDER BY id""",
                (base_key, base_key + ":r%"),
            ).fetchall()
        if not rows:
            return base_key
        latest = rows[-1]
        if latest["state"] != "CLOSED":
            return latest["slice_key"]
        return f"{base_key}:r{len(rows)}"

    def reconcile_offers(self, offers, seen_at_ms=None):
        seen_at = int(seen_at_ms if seen_at_ms is not None else self._now_ms())
        ids = []
        with self.transaction(immediate=True) as connection:
            for offer in offers or []:
                offer_id = int(offer["id"])
                ids.append(offer_id)
                intent = connection.execute(
                    "SELECT * FROM order_intents WHERE exchange_offer_id = ?", (offer_id,)
                ).fetchone()
                managed = bool(offer.get("managed", False) or intent is not None)
                pool = offer.get("pool") or (intent["pool"] if intent else None)
                layer = offer.get("layer") or (intent["layer"] if intent else None)
                display_type = offer.get("display_type") or (intent["display_type"] if intent else None)
                if not display_type:
                    raw_type = str(offer.get("offer_type", "LIMIT")).upper()
                    if raw_type == "FRRDELTAVAR":
                        display_type = "FRR" if D(offer.get("rate", 0)) == 0 else "FRR_DELTA_VARIABLE"
                    elif raw_type == "FRRDELTAFIX":
                        display_type = "FRR_DELTA_FIXED"
                    else:
                        display_type = raw_type
                plan_hash = offer.get("plan_hash") or (intent["plan_hash"] if intent else None)
                strategy_variant = offer.get("strategy_variant") or (
                    intent["strategy_variant"] if intent else "baseline"
                )
                connection.execute(
                    """INSERT INTO offers(
                        offer_id, currency, amount, amount_original, rate, rate_real, period,
                        offer_type, display_type, flags, status, managed, pool, layer, plan_hash,
                        strategy_variant, mts_created, mts_updated, last_seen_ms
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(offer_id) DO UPDATE SET amount=excluded.amount, rate=excluded.rate,
                        rate_real=excluded.rate_real, status=excluded.status, flags=excluded.flags,
                        managed=MAX(offers.managed, excluded.managed), pool=COALESCE(offers.pool, excluded.pool),
                        layer=COALESCE(offers.layer, excluded.layer),
                        display_type=COALESCE(offers.display_type, excluded.display_type),
                        plan_hash=COALESCE(offers.plan_hash, excluded.plan_hash), mts_updated=excluded.mts_updated,
                        strategy_variant=CASE WHEN excluded.managed=1 THEN excluded.strategy_variant
                                              ELSE offers.strategy_variant END,
                        last_seen_ms=excluded.last_seen_ms""",
                    (
                        offer_id,
                        str(offer.get("currency", "USD")).upper(),
                        _decimal_text(offer["amount"]),
                        _decimal_text(offer.get("amount_original", offer["amount"])),
                        _decimal_text(offer["rate"]),
                        None if offer.get("rate_real") is None else _decimal_text(offer["rate_real"]),
                        int(offer["period"]),
                        offer.get("offer_type", "LIMIT"),
                        display_type,
                        int(offer.get("flags", 0)),
                        offer.get("status", "ACTIVE"),
                        int(managed),
                        pool,
                        layer,
                        plan_hash,
                        strategy_variant,
                        offer.get("mts_created"),
                        offer.get("mts_updated"),
                        seen_at,
                    ),
                )
            if ids:
                placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    f"UPDATE offers SET status='CLOSED' WHERE status != 'CLOSED' AND offer_id NOT IN ({placeholders})",
                    tuple(ids),
                )
                connection.execute(
                    f"""UPDATE order_intents SET state='CLOSED', updated_at_ms=?
                        WHERE state='CONFIRMED' AND exchange_offer_id NOT IN ({placeholders})""",
                    (seen_at, *ids),
                )
            else:
                connection.execute("UPDATE offers SET status='CLOSED' WHERE status != 'CLOSED'")
                connection.execute(
                    "UPDATE order_intents SET state='CLOSED', updated_at_ms=? WHERE state='CONFIRMED'",
                    (seen_at,),
                )
        return self.offers(active_only=True)

    def offers(self, active_only=False):
        query = "SELECT * FROM offers" + (" WHERE status != 'CLOSED'" if active_only else "") + " ORDER BY offer_id"
        with self.read_connection() as connection:
            return [dict(row) for row in connection.execute(query).fetchall()]

    def record_reprice(
        self,
        offer_id,
        reason,
        old_rate=None,
        new_rate=None,
        intent_id=None,
        created_at_ms=None,
        strategy_version=None,
        plan_hash=None,
        display_type=None,
    ):
        created = int(created_at_ms if created_at_ms is not None else self._now_ms())
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO reprice_events(
                    offer_id, intent_id, reason, old_rate, new_rate,
                    strategy_version, plan_hash, display_type, created_at_ms
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    None if offer_id is None else int(offer_id),
                    None if intent_id is None else int(intent_id),
                    str(reason),
                    None if old_rate is None else _decimal_text(old_rate),
                    None if new_rate is None else _decimal_text(new_rate),
                    None if strategy_version is None else str(strategy_version),
                    plan_hash,
                    display_type,
                    created,
                ),
            )

    def reprice_count_since(self, since_ms):
        with self.read_connection() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM reprice_events WHERE created_at_ms >= ?", (int(since_ms),)
                ).fetchone()["count"]
            )

    def last_reprice_for_family(self, pool, layer):
        with self.read_connection() as connection:
            row = connection.execute(
                """SELECT MAX(events.created_at_ms) AS last_reprice
                   FROM reprice_events AS events
                   JOIN offers ON offers.offer_id = events.offer_id
                   WHERE offers.pool = ? AND offers.layer = ?""",
                (str(pool), str(layer)),
            ).fetchone()
        return None if row is None else row["last_reprice"]

    def reconcile_credits(self, credits, seen_at_ms=None):
        seen_at = int(seen_at_ms if seen_at_ms is not None else self._now_ms())
        ids = []
        with self.transaction(immediate=True) as connection:
            for credit in credits or []:
                credit_id = int(credit["id"])
                ids.append(credit_id)
                offer_id = credit.get("offer_id")
                managed = bool(credit.get("managed", False))
                inferred_pool = credit.get("pool")
                inferred_layer = credit.get("layer")
                inferred_display_type = credit.get("display_type")
                inferred_variant = credit.get("strategy_variant") or "baseline"
                if offer_id is None and credit.get("mts_opening"):
                    candidates = connection.execute(
                        """SELECT trades.offer_id, trades.amount, intents.pool, intents.layer,
                                  intents.display_type, intents.strategy_variant
                           FROM funding_trades AS trades
                           JOIN order_intents AS intents ON intents.exchange_offer_id = trades.offer_id
                           WHERE trades.period = ? AND ABS(trades.mts - ?) <= 300000
                           ORDER BY ABS(trades.mts - ?)""",
                        (int(credit["period"]), int(credit["mts_opening"]), int(credit["mts_opening"])),
                    ).fetchall()
                    for candidate in candidates:
                        if abs(D(candidate["amount"]) - D(credit["amount"])) <= D("0.00000001"):
                            offer_id = candidate["offer_id"]
                            inferred_pool = candidate["pool"]
                            inferred_layer = candidate["layer"]
                            inferred_display_type = candidate["display_type"]
                            inferred_variant = candidate["strategy_variant"]
                            break
                if offer_id is not None:
                    intent = connection.execute(
                        "SELECT display_type, strategy_variant FROM order_intents WHERE exchange_offer_id = ?",
                        (int(offer_id),),
                    ).fetchone()
                    managed = managed or intent is not None
                    if intent is not None:
                        inferred_display_type = inferred_display_type or intent["display_type"]
                        inferred_variant = intent["strategy_variant"]
                raw_rate_type = str(credit.get("rate_type") or "").upper()
                if not inferred_display_type:
                    if raw_rate_type in {"FIXED", "LIMIT"}:
                        inferred_display_type = "LIMIT"
                    elif raw_rate_type in {"VAR", "VARIABLE", "FRRDELTAVAR"}:
                        inferred_display_type = "VARIABLE_UNKNOWN"
                connection.execute(
                    """INSERT INTO credits(
                        credit_id, currency, amount, rate, rate_real, period, rate_type, display_type, hidden,
                        status, managed, pool, layer, offer_id, strategy_variant,
                        mts_opening, mts_updated, last_seen_ms
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(credit_id) DO UPDATE SET amount=excluded.amount, rate=excluded.rate,
                        rate_real=excluded.rate_real, status=excluded.status,
                        managed=MAX(credits.managed, excluded.managed), last_seen_ms=excluded.last_seen_ms,
                        pool=COALESCE(credits.pool, excluded.pool),
                        layer=COALESCE(credits.layer, excluded.layer),
                        display_type=COALESCE(credits.display_type, excluded.display_type),
                        offer_id=COALESCE(credits.offer_id, excluded.offer_id),
                        strategy_variant=CASE WHEN excluded.managed=1 THEN excluded.strategy_variant
                                              ELSE credits.strategy_variant END,
                        mts_updated=excluded.mts_updated""",
                    (
                        credit_id,
                        str(credit.get("currency", "USD")).upper(),
                        _decimal_text(credit["amount"]),
                        _decimal_text(credit["rate"]),
                        None if credit.get("rate_real") is None else _decimal_text(credit["rate_real"]),
                        int(credit["period"]),
                        credit.get("rate_type"),
                        inferred_display_type,
                        int(bool(credit.get("hidden", False))),
                        credit.get("status", "ACTIVE"),
                        int(managed),
                        inferred_pool,
                        inferred_layer,
                        offer_id,
                        inferred_variant,
                        credit.get("mts_opening"),
                        credit.get("mts_updated"),
                        seen_at,
                    ),
                )
            if ids:
                placeholders = ",".join("?" for _ in ids)
                closing = connection.execute(
                    f"SELECT * FROM credits WHERE status != 'CLOSED' AND credit_id NOT IN ({placeholders})",
                    tuple(ids),
                ).fetchall()
                for row in closing:
                    connection.execute(
                        """INSERT OR IGNORE INTO credit_closures(
                            credit_id, currency, amount, rate, period, rate_type, hidden,
                            managed, pool, opened_at_ms, closed_at_ms
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            row["credit_id"],
                            row["currency"],
                            row["amount"],
                            row["rate_real"] or row["rate"],
                            row["period"],
                            row["rate_type"],
                            row["hidden"],
                            row["managed"],
                            row["pool"],
                            row["mts_opening"],
                            seen_at,
                        ),
                    )
                connection.execute(
                    "UPDATE credits SET status='CLOSED' WHERE status != 'CLOSED' "
                    f"AND credit_id NOT IN ({placeholders})",
                    tuple(ids),
                )
            else:
                closing = connection.execute("SELECT * FROM credits WHERE status != 'CLOSED'").fetchall()
                for row in closing:
                    connection.execute(
                        """INSERT OR IGNORE INTO credit_closures(
                            credit_id, currency, amount, rate, period, rate_type, hidden,
                            managed, pool, opened_at_ms, closed_at_ms
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            row["credit_id"],
                            row["currency"],
                            row["amount"],
                            row["rate_real"] or row["rate"],
                            row["period"],
                            row["rate_type"],
                            row["hidden"],
                            row["managed"],
                            row["pool"],
                            row["mts_opening"],
                            seen_at,
                        ),
                    )
                connection.execute("UPDATE credits SET status='CLOSED' WHERE status != 'CLOSED'")

    def credits(self, active_only=False):
        query = "SELECT * FROM credits" + (" WHERE status != 'CLOSED'" if active_only else "") + " ORDER BY credit_id"
        with self.read_connection() as connection:
            return [dict(row) for row in connection.execute(query).fetchall()]

    def import_legacy_managed_offers(self, path):
        if not path or not os.path.exists(path):
            return 0
        try:
            with open(path, "r", encoding="utf-8") as source:
                payload = json.load(source)
        except (OSError, ValueError):
            return 0
        imported = 0
        now = self._now_ms()
        with self.transaction(immediate=True) as connection:
            for raw_id, metadata in (payload.get("offers") or {}).items():
                try:
                    offer_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                bucket = str(metadata.get("bucket") or "balanced")
                pool = "long" if bucket == "long" else "short"
                layer = "quick" if bucket == "fast" else "balanced" if bucket == "balanced" else "high"
                connection.execute(
                    """INSERT INTO offers(
                        offer_id, currency, amount, rate, period, offer_type, flags, status,
                        managed, pool, layer, mts_created, last_seen_ms
                    ) VALUES(?, ?, '0', '0', 2, ?, 0, 'LEGACY_PENDING_SYNC', 1, ?, ?, ?, ?)
                    ON CONFLICT(offer_id) DO UPDATE SET managed=1, pool=COALESCE(offers.pool, excluded.pool),
                        layer=COALESCE(offers.layer, excluded.layer)""",
                    (
                        offer_id,
                        str(metadata.get("currency", "USD")).upper(),
                        metadata.get("offerType", "LIMIT"),
                        pool,
                        layer,
                        metadata.get("createdAt"),
                        now,
                    ),
                )
                imported += 1
        return imported

    def record_rate_floor_violations(self, violations, now_ms=None):
        now = int(now_ms if now_ms is not None else self._now_ms())
        active = {int(row["credit_id"]): row for row in violations or []}
        with self.transaction(immediate=True) as connection:
            open_rows = {
                int(row["credit_id"]): row
                for row in connection.execute(
                    "SELECT * FROM rate_floor_violations WHERE ended_at_ms IS NULL"
                ).fetchall()
            }
            for credit_id, row in active.items():
                if credit_id not in open_rows:
                    connection.execute(
                        """INSERT INTO rate_floor_violations(
                            credit_id, pool, floor_rate, observed_rate, started_at_ms
                        ) VALUES(?, ?, ?, ?, ?)""",
                        (
                            credit_id,
                            str(row["pool"]),
                            _decimal_text(row["floor_rate"]),
                            _decimal_text(row["observed_rate"]),
                            now,
                        ),
                    )
                else:
                    connection.execute(
                        """UPDATE rate_floor_violations SET observed_rate=?
                           WHERE credit_id=? AND ended_at_ms IS NULL""",
                        (_decimal_text(row["observed_rate"]), credit_id),
                    )
            closed_ids = [credit_id for credit_id in open_rows if credit_id not in active]
            if closed_ids:
                placeholders = ",".join("?" for _ in closed_ids)
                connection.execute(
                    "UPDATE rate_floor_violations SET ended_at_ms=? "
                    "WHERE ended_at_ms IS NULL "
                    f"AND credit_id IN ({placeholders})",
                    (now, *closed_ids),
                )
        return len(active)

    def upsert_offer_history(self, offers):
        with self.transaction(immediate=True) as connection:
            for offer in offers or []:
                managed = (
                    connection.execute(
                        "SELECT 1 FROM order_intents WHERE exchange_offer_id=?", (int(offer["id"]),)
                    ).fetchone()
                    is not None
                )
                connection.execute(
                    """INSERT OR REPLACE INTO offer_history(
                        offer_id, currency, amount, rate, rate_real, period, offer_type,
                        flags, status, mts_created, mts_updated, managed
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        int(offer["id"]),
                        str(offer.get("currency", "USD")).upper(),
                        _decimal_text(offer["amount"]),
                        _decimal_text(offer["rate"]),
                        None if offer.get("rate_real") is None else _decimal_text(offer["rate_real"]),
                        int(offer["period"]),
                        str(offer.get("offer_type", "LIMIT")),
                        int(offer.get("flags", 0)),
                        str(offer.get("status", "UNKNOWN")),
                        offer.get("mts_created"),
                        offer.get("mts_updated"),
                        int(managed),
                    ),
                )

    def upsert_credit_history(self, credits):
        with self.transaction(immediate=True) as connection:
            for credit in credits or []:
                managed = False
                opening = credit.get("mts_opening")
                if opening:
                    matches = connection.execute(
                        """SELECT 1 FROM funding_trades AS trades
                           JOIN order_intents AS intents ON intents.exchange_offer_id=trades.offer_id
                           WHERE trades.period=? AND ABS(trades.mts-?)<=300000 AND trades.amount=? LIMIT 1""",
                        (int(credit["period"]), int(opening), _decimal_text(credit["amount"])),
                    ).fetchone()
                    managed = matches is not None
                connection.execute(
                    """INSERT OR REPLACE INTO credit_history(
                        credit_id, currency, amount, rate, rate_real, period, rate_type,
                        hidden, status, mts_opening, mts_updated, managed
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        int(credit["id"]),
                        str(credit.get("currency", "USD")).upper(),
                        _decimal_text(credit["amount"]),
                        _decimal_text(credit["rate"]),
                        None if credit.get("rate_real") is None else _decimal_text(credit["rate_real"]),
                        int(credit["period"]),
                        credit.get("rate_type"),
                        int(bool(credit.get("hidden"))),
                        str(credit.get("status", "UNKNOWN")),
                        opening,
                        credit.get("mts_updated"),
                        int(managed),
                    ),
                )

    def upsert_market_trades(self, trades):
        with self.transaction(immediate=True) as connection:
            for trade in trades or []:
                connection.execute(
                    """INSERT OR IGNORE INTO market_trades(trade_id, mts, amount, rate, period)
                       VALUES(?, ?, ?, ?, ?)""",
                    (
                        str(trade["id"]),
                        int(trade["mts"]),
                        _decimal_text(abs(D(trade["amount"]))),
                        _decimal_text(trade["rate"]),
                        int(trade["period"]),
                    ),
                )

    def upsert_funding_stats(self, stats):
        with self.transaction(immediate=True) as connection:
            for row in stats or []:
                mts = int(row.get("mts") or row.get("timestamp") or 0)
                if mts <= 0:
                    continue
                connection.execute(
                    """INSERT INTO funding_stats(mts, frr_daily_rate, utilization, payload_json)
                       VALUES(?, ?, ?, ?)
                       ON CONFLICT(mts) DO UPDATE SET
                           frr_daily_rate=excluded.frr_daily_rate,
                           utilization=excluded.utilization,
                           payload_json=excluded.payload_json""",
                    (
                        mts,
                        None if row.get("frr_daily_rate") is None else _decimal_text(row["frr_daily_rate"]),
                        None if row.get("utilization") is None else _decimal_text(row["utilization"]),
                        json.dumps(row, ensure_ascii=False, sort_keys=True, default=str),
                    ),
                )

    def funding_stats(self, start_ms=0, end_ms=None):
        query = "SELECT payload_json FROM funding_stats WHERE mts >= ?"
        params = [int(start_ms)]
        if end_ms is not None:
            query += " AND mts <= ?"
            params.append(int(end_ms))
        query += " ORDER BY mts"
        with self.read_connection() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def record_book_snapshot(self, book, source="REST", now_ms=None):
        now = int(now_ms if now_ms is not None else self._now_ms())
        bucket = now - (now % 60_000)
        payload = json.dumps(book or [], ensure_ascii=False, sort_keys=True, default=str)
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT OR REPLACE INTO book_snapshots(mts, source, book_json)
                   VALUES(?, ?, ?)""",
                (bucket, str(source), payload),
            )

    def book_snapshots(self, start_ms=0, end_ms=None):
        query = "SELECT * FROM book_snapshots WHERE mts >= ?"
        params = [int(start_ms)]
        if end_ms is not None:
            query += " AND mts <= ?"
            params.append(int(end_ms))
        query += " ORDER BY mts"
        with self.read_connection() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [{**dict(row), "book": json.loads(row["book_json"])} for row in rows]

    def record_market_bars(self, windows, now_ms=None):
        now = int(now_ms if now_ms is not None else self._now_ms())
        bucket = now - (now % 60_000)
        with self.transaction(immediate=True) as connection:
            for interval_name, values in (windows or {}).items():
                connection.execute(
                    """INSERT OR REPLACE INTO market_bars(
                        interval_name, bucket_ms, weighted_median_rate, q25_rate,
                        q75_rate, volume, trade_count
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(interval_name),
                        bucket,
                        _decimal_text(values.get("median", 0)),
                        _decimal_text(values.get("q25", 0)),
                        _decimal_text(values.get("q75", 0)),
                        _decimal_text(values.get("volume", 0)),
                        int(values.get("count", 0)),
                    ),
                )

    def market_trades(self, start_ms=0, end_ms=None):
        query = "SELECT * FROM market_trades WHERE mts >= ?"
        params = [int(start_ms)]
        if end_ms is not None:
            query += " AND mts <= ?"
            params.append(int(end_ms))
        query += " ORDER BY mts"
        with self.read_connection() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [
            {
                "id": row["trade_id"],
                "mts": row["mts"],
                "amount": D(row["amount"]),
                "rate": D(row["rate"]),
                "period": row["period"],
            }
            for row in rows
        ]

    def prune_market_data(self, retention_days=90, now_ms=None):
        now = int(now_ms if now_ms is not None else self._now_ms())
        threshold = now - int(retention_days) * 86_400_000
        with self.transaction(immediate=True) as connection:
            trade_count = connection.execute("DELETE FROM market_trades WHERE mts < ?", (threshold,)).rowcount
            bar_count = connection.execute("DELETE FROM market_bars WHERE bucket_ms < ?", (threshold,)).rowcount
        return {"trades": trade_count, "bars": bar_count}

    def record_account_sample(self, total, wallet, offers, credits, net_interest, mts=None):
        mts = int(mts if mts is not None else self._now_ms())
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT OR REPLACE INTO account_samples(
                    mts, total_principal, wallet_available, open_offers, active_credits, net_interest_total
                ) VALUES(?, ?, ?, ?, ?, ?)""",
                (mts, *(_decimal_text(value) for value in (total, wallet, offers, credits, net_interest))),
            )

    def upsert_income_ledgers(self, rows, category=28):
        """Store authoritative interest ledger rows, keyed by Bitfinex ledger ID."""
        with self.transaction(immediate=True) as connection:
            for row in rows or []:
                connection.execute(
                    """INSERT INTO ledger_entries(
                        ledger_id, currency, wallet, amount, balance, description, category, mts
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(ledger_id) DO UPDATE SET
                        currency=excluded.currency, wallet=excluded.wallet,
                        amount=excluded.amount, balance=excluded.balance,
                        description=excluded.description, category=excluded.category,
                        mts=excluded.mts""",
                    (
                        int(row["id"]),
                        str(row.get("currency") or "USD").upper(),
                        str(row.get("wallet") or "").lower() or None,
                        _decimal_text(row["amount"]),
                        None if row.get("balance") is None else _decimal_text(row["balance"]),
                        str(row.get("description") or ""),
                        int(category),
                        int(row["mts"]),
                    ),
                )

    def income_sync_state(self, currency="USD"):
        currency = str(currency).upper()
        with self.read_connection() as connection:
            row = connection.execute("SELECT * FROM income_sync_state WHERE currency = ?", (currency,)).fetchone()
        if row is None:
            return {
                "currency": currency,
                "status": "PENDING",
                "next_end_ms": None,
                "earliest_mts": None,
                "last_success_ms": None,
                "completed_at_ms": None,
                "error": None,
                "updated_at_ms": None,
            }
        return dict(row)

    def update_income_sync_state(self, currency="USD", **values):
        allowed = {
            "status",
            "next_end_ms",
            "earliest_mts",
            "last_success_ms",
            "completed_at_ms",
            "error",
        }
        updates = {key: value for key, value in values.items() if key in allowed}
        if not updates:
            return self.income_sync_state(currency)
        currency = str(currency).upper()
        now = self._now_ms()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO income_sync_state(currency, status, updated_at_ms) VALUES(?, 'PENDING', ?)",
                (currency, now),
            )
            connection.execute(
                f"UPDATE income_sync_state SET {assignments}, updated_at_ms = ? WHERE currency = ?",
                (*updates.values(), now, currency),
            )
        return self.income_sync_state(currency)

    def realized_income(self, currency="USD", start_ms=None, end_ms=None):
        clauses = ["currency = ?", "wallet = 'funding'", "category = 28", "CAST(amount AS REAL) > 0"]
        params = [str(currency).upper()]
        if start_ms is not None:
            clauses.append("mts >= ?")
            params.append(int(start_ms))
        if end_ms is not None:
            clauses.append("mts <= ?")
            params.append(int(end_ms))
        with self.read_connection() as connection:
            rows = connection.execute(
                f"SELECT amount FROM ledger_entries WHERE {' AND '.join(clauses)}", tuple(params)
            ).fetchall()
        return sum((D(row["amount"]) for row in rows), D("0"))

    def realized_income_summary(self, currency="USD", now_ms=None):
        now = int(now_ms if now_ms is not None else self._now_ms())
        local = time.localtime(now / 1000)
        midnight = int(
            time.mktime(
                (
                    local.tm_year,
                    local.tm_mon,
                    local.tm_mday,
                    0,
                    0,
                    0,
                    local.tm_wday,
                    local.tm_yday,
                    local.tm_isdst,
                )
            )
            * 1000
        )
        return {
            "today": _decimal_text(self.realized_income(currency, midnight, now)),
            "thirtyDays": _decimal_text(self.realized_income(currency, now - 30 * 86_400_000, now)),
            "lifetime": _decimal_text(self.realized_income(currency, None, now)),
            "currency": str(currency).upper(),
        }

    def income_history_sync_payload(self, currency="USD"):
        state = self.income_sync_state(currency)
        return {
            "status": state["status"],
            "earliestMts": state["earliest_mts"],
            "lastSuccess": state["last_success_ms"],
            "error": state["error"],
        }

    def statistics(self, window_days=None, now_ms=None):
        now = int(now_ms if now_ms is not None else self._now_ms())
        start = 0 if window_days is None else now - int(window_days) * 86_400_000
        with self.read_connection() as connection:
            rows = connection.execute("SELECT * FROM account_samples WHERE mts >= ? ORDER BY mts", (start,)).fetchall()
            reprices = connection.execute(
                "SELECT COUNT(*) AS count FROM reprice_events WHERE created_at_ms >= ?", (start,)
            ).fetchone()["count"]
            waits = connection.execute(
                """SELECT created_at_ms, updated_at_ms FROM order_intents
                   WHERE state IN ('CONFIRMED', 'CLOSED') AND exchange_offer_id IS NOT NULL
                   AND updated_at_ms >= ?""",
                (start,),
            ).fetchall()
            closures = connection.execute(
                "SELECT * FROM credit_closures WHERE closed_at_ms >= ? ORDER BY closed_at_ms",
                (start,),
            ).fetchall()
        wait_ms = [max(0, row["updated_at_ms"] - row["created_at_ms"]) for row in waits]
        attributed = {}
        early = 0
        for row in closures:
            opened = row["opened_at_ms"] or row["closed_at_ms"]
            elapsed_days = D(max(0, row["closed_at_ms"] - opened)) / D("86400000")
            contractual_days = D(max(1, row["period"]))
            if elapsed_days + D("0.000001") < contractual_days:
                early += 1
            fee = D("0.18") if row["hidden"] else D("0.15")
            earned = D(row["amount"]) * D(row["rate"]) * elapsed_days * (D("1") - fee)
            pool = row["pool"] if row["managed"] and row["pool"] else "EXTERNAL"
            order_type = row["rate_type"] if row["managed"] and row["rate_type"] else "EXTERNAL"
            key = f"{pool}:{order_type}"
            attributed[key] = attributed.get(key, D("0")) + earned
        details = {
            "averageWaitSeconds": _decimal_text(D(sum(wait_ms)) / D(len(wait_ms)) / D("1000")) if wait_ms else "0",
            "earlyReturnPercent": _decimal_text(D(early) / D(len(closures)) * D("100")) if closures else "0",
            "closedCreditCount": len(closures),
            "returnsByPoolAndType": {key: _decimal_text(value) for key, value in sorted(attributed.items())},
        }
        ledger_interest = self.realized_income("USD", None if window_days is None else start, now)
        if not rows:
            return {
                "windowDays": window_days,
                "utilizationPercent": "0",
                "netInterest": _decimal_text(ledger_interest),
                "actualNetAprPercent": "0",
                "idlePrincipalTime": "0",
                "repriceCount": reprices,
                **details,
            }
        utilized_time = D("0")
        principal_time = D("0")
        idle_time = D("0")
        for index, row in enumerate(rows):
            end = rows[index + 1]["mts"] if index + 1 < len(rows) else now
            duration_days = D(max(0, end - row["mts"])) / D("86400000")
            total = D(row["total_principal"])
            utilized = D(row["active_credits"])
            principal_time += total * duration_days
            utilized_time += utilized * duration_days
            idle_time += max(D("0"), total - utilized) * duration_days
        # APR is estimated only for the period where principal samples exist.
        # The displayed realized income can cover a wider ledger interval.
        apr_interest = self.realized_income("USD", max(start, int(rows[0]["mts"])), now)
        utilization = D("0") if principal_time <= 0 else utilized_time / principal_time
        apr = D("0") if principal_time <= 0 else apr_interest / principal_time * D("365")
        return {
            "windowDays": window_days,
            "utilizationPercent": _decimal_text(utilization * D("100")),
            "netInterest": _decimal_text(ledger_interest),
            "actualNetAprPercent": _decimal_text(apr * D("100")),
            "idlePrincipalTime": _decimal_text(idle_time),
            "repriceCount": reprices,
            **details,
        }
