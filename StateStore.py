import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from decimal import Decimal

from WriteRecovery import can_clear_ambiguous_pause, restart_transition, unique_unbound_candidate
from Recovery import (
    RECOVERY_MINIMUM_GAP_MS,
    RECOVERY_REQUIRED_SNAPSHOTS,
    recovery_category_for_reason,
    recovery_delay_seconds,
)


D = Decimal
RUNTIME_MODES = {"PAUSED", "LIVE", "REPLAY", "APPLYING"}
OPEN_INTENT_STATES = {"PLANNED", "SUBMITTING", "AMBIGUOUS"}
AUTO_RECOVERABLE_PAUSE_REASONS = {
    "MARKET_DATA_STALE",
    "ACCOUNT_AVAILABLE_BALANCE_UNKNOWN",
    "ACCOUNT_RECONCILIATION_MISMATCH",
    "AMBIGUOUS_WALLET_TRANSFER",
}


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


def base_slice_key(slice_key):
    value = str(slice_key)
    prefix, marker, suffix = value.rpartition(":r")
    return prefix if marker and suffix.isdigit() else value


def slice_pool_layer(slice_key):
    parts = base_slice_key(slice_key).split(":")
    if len(parts) >= 4 and parts[-3] in {"short", "medium", "long"} and parts[-2] in {"quick", "balanced", "high"}:
        return parts[-3], parts[-2]
    return None, None


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
        if version >= 13:
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
            """CREATE TABLE IF NOT EXISTS recovery_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                active INTEGER NOT NULL DEFAULT 0,
                category TEXT,
                reason TEXT,
                origin_mode TEXT,
                target_mode TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                successful_snapshots INTEGER NOT NULL DEFAULT 0,
                required_snapshots INTEGER NOT NULL DEFAULT 2,
                started_at_ms INTEGER,
                last_probe_at_ms INTEGER,
                next_probe_at_ms INTEGER,
                last_error TEXT,
                heartbeat_at_ms INTEGER,
                manual_required INTEGER NOT NULL DEFAULT 0,
                resume_pending_cycle INTEGER NOT NULL DEFAULT 0
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
                amount_original TEXT,
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
                chain_key TEXT,
                stage INTEGER,
                benchmark_rate TEXT,
                floor_rate TEXT,
                created_at_ms INTEGER NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS reprice_events_time_idx ON reprice_events(created_at_ms)""",
            """CREATE TABLE IF NOT EXISTS reprice_chains (
                chain_key TEXT PRIMARY KEY,
                strategy_version TEXT NOT NULL,
                base_slice_key TEXT NOT NULL,
                pool TEXT NOT NULL,
                layer TEXT NOT NULL,
                origin_rate TEXT NOT NULL,
                started_at_ms INTEGER NOT NULL,
                current_stage INTEGER NOT NULL DEFAULT 0,
                current_offer_id INTEGER,
                last_reprice_at_ms INTEGER,
                pending_action TEXT,
                pending_target_rate TEXT,
                market_anchor_rate TEXT,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                updated_at_ms INTEGER NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS reprice_chains_offer_idx
                ON reprice_chains(current_offer_id)""",
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
            """CREATE TABLE IF NOT EXISTS ownership_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                offer_id INTEGER,
                credit_id INTEGER,
                event_type TEXT NOT NULL,
                details_json TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS ownership_events_time_idx
                ON ownership_events(created_at_ms)""",
            """CREATE TABLE IF NOT EXISTS period_selection_state (
                strategy_version TEXT NOT NULL,
                pool TEXT NOT NULL,
                selected_period INTEGER,
                selected_since_ms INTEGER,
                scores_json TEXT NOT NULL,
                updated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (strategy_version, pool)
            )""",
            """CREATE TABLE IF NOT EXISTS demand_confirmation_state (
                strategy_version TEXT NOT NULL,
                scope TEXT NOT NULL,
                signal_key TEXT NOT NULL,
                consecutive_cycles INTEGER NOT NULL DEFAULT 0,
                confirmed INTEGER NOT NULL DEFAULT 0,
                last_cycle INTEGER,
                last_share TEXT,
                updated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (strategy_version, scope, signal_key)
            )""",
            """CREATE TABLE IF NOT EXISTS consolidation_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                state TEXT NOT NULL DEFAULT 'IDLE',
                offer_id INTEGER,
                captured_wallet TEXT,
                captured_offer_amount TEXT,
                target_period INTEGER,
                strategy_version TEXT,
                started_at_ms INTEGER,
                updated_at_ms INTEGER NOT NULL,
                last_error TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS external_takeover_state (
                offer_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                snapshot_digest TEXT,
                first_seen_ms INTEGER NOT NULL,
                confirmed_at_ms INTEGER,
                updated_at_ms INTEGER NOT NULL,
                last_error TEXT
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
            self._migrate_v5_columns(connection)
            self._migrate_v6_reprice_columns(connection)
            self._migrate_v7_reprice_chain_columns(connection)
            self._migrate_v8_recovery_columns(connection)
            self._migrate_v9_period_selection_columns(connection)
            self._migrate_v10_recovery_state(connection)
            self._migrate_v11_allocation_state(connection)
            self._migrate_v12_single_paused_mode(connection)
            self._migrate_v13_unattended_recovery(connection)
            connection.execute(
                """INSERT INTO schema_meta(key, value) VALUES('schema_version', '13')
                   ON CONFLICT(key) DO UPDATE SET value='13'"""
            )
            connection.execute(
                """INSERT OR IGNORE INTO income_sync_state(
                    currency, status, updated_at_ms
                ) VALUES('USD', 'PENDING', ?)""",
                (self._now_ms(),),
            )
            connection.execute(
                """INSERT OR IGNORE INTO consolidation_state(singleton, state, updated_at_ms)
                   VALUES(1, 'IDLE', ?)""",
                (self._now_ms(),),
            )
            connection.execute(
                """INSERT OR IGNORE INTO recovery_state(
                       singleton, active, required_snapshots, manual_required, resume_pending_cycle
                   ) VALUES(1, 0, 2, 0, 0)"""
            )
            connection.execute(
                """INSERT OR IGNORE INTO runtime_state(
                    singleton, mode, previous_mode, updated_at_ms
                ) VALUES(1, 'PAUSED', NULL, ?)""",
                (self._now_ms(),),
            )

    @staticmethod
    def _migrate_v9_period_selection_columns(connection):
        existing = {row["name"] for row in connection.execute("PRAGMA table_info(period_selection_state)")}
        for name, definition in {
            "challenger_period": "INTEGER",
            "challenger_since_ms": "INTEGER",
        }.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE period_selection_state ADD COLUMN {name} {definition}")

    @staticmethod
    def _migrate_v10_recovery_state(connection):
        connection.execute(
            """CREATE TABLE IF NOT EXISTS recovery_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                active INTEGER NOT NULL DEFAULT 0,
                category TEXT,
                reason TEXT,
                origin_mode TEXT,
                target_mode TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                successful_snapshots INTEGER NOT NULL DEFAULT 0,
                required_snapshots INTEGER NOT NULL DEFAULT 2,
                started_at_ms INTEGER,
                last_probe_at_ms INTEGER,
                next_probe_at_ms INTEGER,
                last_error TEXT,
                heartbeat_at_ms INTEGER,
                manual_required INTEGER NOT NULL DEFAULT 0,
                resume_pending_cycle INTEGER NOT NULL DEFAULT 0
            )"""
        )

    @staticmethod
    def _migrate_v12_single_paused_mode(connection):
        # SAFE is folded into PAUSED.  The durable reason/manual flag still
        # carries the safety lock, so migration changes presentation without
        # weakening write protection.
        connection.execute("UPDATE runtime_state SET mode='PAUSED' WHERE mode='SAFE'")
        connection.execute("UPDATE runtime_state SET previous_mode='PAUSED' WHERE previous_mode='SAFE'")
        connection.execute("UPDATE recovery_state SET origin_mode='PAUSED' WHERE origin_mode='SAFE'")
        connection.execute("UPDATE recovery_state SET target_mode='PAUSED' WHERE target_mode='SAFE'")

    @staticmethod
    def _migrate_v13_unattended_recovery(connection):
        # Only an explicit Dashboard pause stops recovery. Older versions used
        # these flags for non-manual failures; release them into the read-only
        # recovery loop without allowing any speculative write.
        connection.execute("UPDATE runtime_state SET safe_manual=0")
        connection.execute(
            """UPDATE recovery_state
               SET manual_required=0,
                   next_probe_at_ms=COALESCE(next_probe_at_ms, started_at_ms)
               WHERE active=1"""
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

    @staticmethod
    def _migrate_v5_columns(connection):
        existing = {row["name"] for row in connection.execute("PRAGMA table_info(credits)")}
        if "attribution_state" not in existing:
            connection.execute("ALTER TABLE credits ADD COLUMN attribution_state TEXT NOT NULL DEFAULT 'EXTERNAL'")
        connection.execute(
            """UPDATE credits SET attribution_state = CASE
                   WHEN managed = 1 THEN 'MANAGED'
                   WHEN attribution_state IS NULL OR attribution_state = '' THEN 'EXTERNAL'
                   ELSE attribution_state END"""
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

    @staticmethod
    def _migrate_v6_reprice_columns(connection):
        required = {
            "chain_key": "TEXT",
            "stage": "INTEGER",
            "benchmark_rate": "TEXT",
            "floor_rate": "TEXT",
        }
        existing = {row["name"] for row in connection.execute("PRAGMA table_info(reprice_events)")}
        for name, definition in required.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE reprice_events ADD COLUMN {name} {definition}")

    @staticmethod
    def _migrate_v7_reprice_chain_columns(connection):
        existing = {row["name"] for row in connection.execute("PRAGMA table_info(reprice_chains)")}
        if "market_anchor_rate" not in existing:
            connection.execute("ALTER TABLE reprice_chains ADD COLUMN market_anchor_rate TEXT")

    @staticmethod
    def _migrate_v8_recovery_columns(connection):
        existing = {row["name"] for row in connection.execute("PRAGMA table_info(offer_history)")}
        if "amount_original" not in existing:
            connection.execute("ALTER TABLE offer_history ADD COLUMN amount_original TEXT")

    def runtime(self):
        with self.read_connection() as connection:
            row = connection.execute("SELECT * FROM runtime_state WHERE singleton = 1").fetchone()
        return dict(row)

    @staticmethod
    def _recovery_payload(row):
        value = dict(row)
        return {
            "active": bool(value.get("active")),
            "category": value.get("category"),
            "reason": value.get("reason"),
            "originMode": value.get("origin_mode"),
            "targetMode": value.get("target_mode"),
            "attempts": int(value.get("attempts") or 0),
            "successfulSnapshots": int(value.get("successful_snapshots") or 0),
            "requiredSnapshots": int(value.get("required_snapshots") or RECOVERY_REQUIRED_SNAPSHOTS),
            "startedAt": value.get("started_at_ms"),
            "lastProbeAt": value.get("last_probe_at_ms"),
            "nextProbeAt": value.get("next_probe_at_ms"),
            "lastError": value.get("last_error"),
            "heartbeatAt": value.get("heartbeat_at_ms"),
            "manualRequired": bool(value.get("manual_required")),
        }

    def recovery_status(self):
        with self.read_connection() as connection:
            row = connection.execute("SELECT * FROM recovery_state WHERE singleton=1").fetchone()
        return self._recovery_payload(row)

    def touch_heartbeat(self, now_ms=None):
        now = int(now_ms if now_ms is not None else self._now_ms())
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE recovery_state SET heartbeat_at_ms=? WHERE singleton=1",
                (now,),
            )
        return now

    @staticmethod
    def _migrate_v11_allocation_state(connection):
        # Tables are created in the normal initialization list. Keeping an
        # explicit migration hook documents the durable Schema 11 boundary.
        connection.execute(
            "CREATE INDEX IF NOT EXISTS demand_confirmation_updated_idx ON demand_confirmation_state(updated_at_ms)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS external_takeover_state_idx ON external_takeover_state(state, updated_at_ms)"
        )

    def begin_recovery(
        self,
        category,
        reason,
        *,
        origin_mode=None,
        target_mode=None,
        manual_required=False,
        now_ms=None,
    ):
        now = int(now_ms if now_ms is not None else self._now_ms())
        with self.transaction(immediate=True) as connection:
            runtime = connection.execute("SELECT * FROM runtime_state WHERE singleton=1").fetchone()
            current = connection.execute("SELECT * FROM recovery_state WHERE singleton=1").fetchone()
            origin = str(origin_mode or runtime["previous_mode"] or runtime["mode"] or "PAUSED").upper()
            target = str(target_mode or ("LIVE" if origin == "LIVE" else origin)).upper()
            if target not in {"LIVE", "PAUSED", "REPLAY"}:
                target = "PAUSED"
            if current["active"]:
                # A recovery episode owns one immutable resume destination.
                # Secondary failures (for example stale market data after a
                # network timeout) may update diagnostics and backoff, but must
                # never downgrade an authorized LIVE recovery to PAUSED.
                if current["origin_mode"] in {"LIVE", "PAUSED", "REPLAY"}:
                    origin = current["origin_mode"]
                if current["target_mode"] in {"LIVE", "PAUSED", "REPLAY"}:
                    target = current["target_mode"]
                manual_required = bool(manual_required or current["manual_required"])
                started = current["started_at_ms"]
                attempts = int(current["attempts"] or 0)
            else:
                started = now
                attempts = 0
            connection.execute(
                """UPDATE recovery_state SET active=1, category=?, reason=?, origin_mode=?,
                   target_mode=?, attempts=?, successful_snapshots=0, required_snapshots=?,
                   started_at_ms=?, last_probe_at_ms=?, next_probe_at_ms=?, last_error=?,
                   manual_required=?, resume_pending_cycle=0 WHERE singleton=1""",
                (
                    str(category),
                    str(reason),
                    origin,
                    target,
                    attempts,
                    RECOVERY_REQUIRED_SNAPSHOTS,
                    started,
                    None,
                    None if manual_required else now + recovery_delay_seconds(attempts) * 1000,
                    str(reason),
                    int(bool(manual_required)),
                ),
            )
        return self.recovery_status()

    def record_recovery_failure(self, error_text, category=None, now_ms=None):
        now = int(now_ms if now_ms is not None else self._now_ms())
        with self.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM recovery_state WHERE singleton=1").fetchone()
            if not row["active"] or row["manual_required"]:
                return self._recovery_payload(row)
            attempts = int(row["attempts"] or 0) + 1
            delay = recovery_delay_seconds(attempts - 1)
            connection.execute(
                """UPDATE recovery_state SET category=COALESCE(?, category), attempts=?,
                   successful_snapshots=0, last_probe_at_ms=?, next_probe_at_ms=?,
                   last_error=? WHERE singleton=1""",
                (category, attempts, now, now + delay * 1000, str(error_text)),
            )
        return self.recovery_status()

    def recovery_probe_due(self, now_ms=None):
        now = int(now_ms if now_ms is not None else self._now_ms())
        with self.read_connection() as connection:
            row = connection.execute("SELECT * FROM recovery_state WHERE singleton=1").fetchone()
        return bool(
            row["active"]
            and not row["manual_required"]
            and (row["next_probe_at_ms"] is None or now >= int(row["next_probe_at_ms"]))
        )

    def record_recovery_snapshot(self, now_ms=None):
        now = int(now_ms if now_ms is not None else self._now_ms())
        resumed = False
        with self.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM recovery_state WHERE singleton=1").fetchone()
            if not row["active"] or row["manual_required"]:
                return {"resumed": False, "recovery": self._recovery_payload(row)}
            last = row["last_probe_at_ms"]
            count = int(row["successful_snapshots"] or 0)
            if last is None or now - int(last) >= RECOVERY_MINIMUM_GAP_MS:
                count += 1
                connection.execute(
                    """UPDATE recovery_state SET successful_snapshots=?, last_probe_at_ms=?,
                       next_probe_at_ms=?, last_error=NULL WHERE singleton=1""",
                    (count, now, now + RECOVERY_MINIMUM_GAP_MS),
                )
            unresolved = connection.execute(
                "SELECT COUNT(*) FROM order_intents WHERE state IN ('PLANNED','SUBMITTING','AMBIGUOUS')"
            ).fetchone()[0]
            if count >= int(row["required_snapshots"] or RECOVERY_REQUIRED_SNAPSHOTS) and not unresolved:
                target = row["target_mode"] if row["target_mode"] in {"LIVE", "PAUSED", "REPLAY"} else "PAUSED"
                current_mode = connection.execute("SELECT mode FROM runtime_state WHERE singleton=1").fetchone()[0]
                connection.execute(
                    """UPDATE runtime_state SET mode=?, previous_mode=NULL, safe_reason=NULL,
                       safe_manual=0, consistent_syncs=0, last_consistent_sync_ms=NULL,
                       updated_at_ms=? WHERE singleton=1""",
                    (target, now),
                )
                connection.execute(
                    "INSERT INTO mode_events(from_mode,to_mode,reason,created_at_ms) VALUES(?,?,?,?)",
                    (current_mode, target, "automatic recovery confirmed", now),
                )
                connection.execute(
                    """UPDATE recovery_state SET active=0, successful_snapshots=0,
                       next_probe_at_ms=NULL, manual_required=0, resume_pending_cycle=1,
                       last_error=NULL WHERE singleton=1"""
                )
                resumed = True
            final = connection.execute("SELECT * FROM recovery_state WHERE singleton=1").fetchone()
        return {"resumed": resumed, "recovery": self._recovery_payload(final)}

    def consume_resume_barrier(self):
        with self.transaction(immediate=True) as connection:
            row = connection.execute("SELECT resume_pending_cycle FROM recovery_state WHERE singleton=1").fetchone()
            pending = bool(row[0])
            if pending:
                connection.execute("UPDATE recovery_state SET resume_pending_cycle=0 WHERE singleton=1")
        return pending

    def clear_recovery(self):
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE recovery_state SET active=0, category=NULL, reason=NULL,
                   origin_mode=NULL, target_mode=NULL, attempts=0, successful_snapshots=0,
                   started_at_ms=NULL, last_probe_at_ms=NULL, next_probe_at_ms=NULL,
                   last_error=NULL, manual_required=0, resume_pending_cycle=0 WHERE singleton=1"""
            )
        return self.recovery_status()

    def latest_mode_event(self):
        with self.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM mode_events ORDER BY created_at_ms DESC, id DESC LIMIT 1"
            ).fetchone()
        return None if row is None else dict(row)

    def set_mode(self, mode, reason=""):
        mode = str(mode).upper()
        if mode not in RUNTIME_MODES:
            raise StateStoreError(f"unsupported runtime mode: {mode}")
        now = self._now_ms()
        with self.transaction(immediate=True) as connection:
            current = connection.execute("SELECT * FROM runtime_state WHERE singleton = 1").fetchone()
            from_mode = current["mode"]
            if current["safe_manual"]:
                raise StateStoreError("protected PAUSED must be resolved through the ambiguous intent workflow")
            if from_mode == "LIVE" and mode == "REPLAY":
                raise StateStoreError("LIVE must transition to PAUSED before REPLAY")
            previous = current["previous_mode"]
            connection.execute(
                """UPDATE runtime_state
                   SET mode = ?, previous_mode = ?, safe_reason = ?, safe_manual = 0,
                       consistent_syncs = 0, last_consistent_sync_ms = NULL, updated_at_ms = ?
                   WHERE singleton = 1""",
                (mode, previous, None, now),
            )
            connection.execute(
                "INSERT INTO mode_events(from_mode, to_mode, reason, created_at_ms) VALUES(?, ?, ?, ?)",
                (from_mode, mode, reason, now),
            )
            if str(reason) in {
                "dashboard_stop",
                "dashboard_pause",
                "worker_build_mismatch_stopped",
                "dashboard_started_without_live_process",
            }:
                connection.execute(
                    """UPDATE recovery_state SET active=0, category=NULL, reason=NULL,
                       origin_mode=NULL, target_mode=NULL, attempts=0, successful_snapshots=0,
                       started_at_ms=NULL, last_probe_at_ms=NULL, next_probe_at_ms=NULL,
                       last_error=NULL, manual_required=0, resume_pending_cycle=0 WHERE singleton=1"""
                )
        return self.runtime()

    def enter_protected_pause(self, reason, manual=False):
        now = self._now_ms()
        with self.transaction(immediate=True) as connection:
            current = connection.execute("SELECT * FROM runtime_state WHERE singleton = 1").fetchone()
            previous = current["previous_mode"] if current["safe_reason"] else current["mode"]
            sticky_manual = bool(manual or current["safe_manual"])
            safe_reason = current["safe_reason"] if sticky_manual and current["safe_manual"] else str(reason)
            connection.execute(
                """UPDATE runtime_state
                   SET mode = 'PAUSED', previous_mode = ?, safe_reason = ?, safe_manual = ?,
                       consistent_syncs = 0, last_consistent_sync_ms = NULL, updated_at_ms = ?
                   WHERE singleton = 1""",
                (previous, safe_reason, int(sticky_manual), now),
            )
            if not current["safe_reason"]:
                connection.execute(
                    "INSERT INTO mode_events(from_mode, to_mode, reason, created_at_ms) VALUES(?, 'PAUSED', ?, ?)",
                    (current["mode"], str(reason), now),
                )
        category = recovery_category_for_reason(reason)
        if category is not None:
            self.begin_recovery(
                category,
                reason,
                origin_mode=previous,
                target_mode=previous,
                manual_required=manual,
                now_ms=now,
            )
        return self.runtime()

    def record_consistent_sync(self, now_ms=None):
        now = int(now_ms if now_ms is not None else self._now_ms())
        recovery = self.recovery_status()
        if recovery["active"]:
            result = self.record_recovery_snapshot(now)
            return self.runtime() if result["resumed"] else self.runtime()
        with self.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM runtime_state WHERE singleton = 1").fetchone()
            if (
                row["mode"] != "PAUSED"
                or row["safe_manual"]
                or str(row["safe_reason"] or "") not in AUTO_RECOVERABLE_PAUSE_REASONS
            ):
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
                    "VALUES('PAUSED', ?, 'reconciled', ?)",
                    (target, now),
                )
        return self.runtime()

    @staticmethod
    def _safe_resume_target(row):
        return row["previous_mode"] if row["previous_mode"] in {"LIVE", "PAUSED", "REPLAY"} else "PAUSED"

    @staticmethod
    def _resume_protected_pause(connection, row, now, reason):
        target = LendingStateStore._safe_resume_target(row)
        connection.execute(
            """UPDATE runtime_state SET mode = 'PAUSED', previous_mode = ?, safe_reason = ?,
               safe_manual = 0, consistent_syncs = 0, last_consistent_sync_ms = NULL,
               updated_at_ms = ? WHERE singleton = 1""",
            (target, "POST_AMBIGUOUS_RECONCILIATION", now),
        )
        connection.execute(
            """UPDATE recovery_state SET active=1, category='AMBIGUOUS_WRITE', reason=?,
               origin_mode=?, target_mode=?, attempts=0, successful_snapshots=0,
               required_snapshots=2, started_at_ms=?, last_probe_at_ms=?,
               next_probe_at_ms=?, last_error=NULL, manual_required=0,
               resume_pending_cycle=0 WHERE singleton=1""",
            (str(reason), target, target, now, now, now + RECOVERY_MINIMUM_GAP_MS),
        )
        connection.execute(
            """INSERT INTO mode_events(from_mode, to_mode, reason, created_at_ms)
               VALUES('PAUSED', 'PAUSED', ?, ?)""",
            (f"{reason}; awaiting clean snapshots", now),
        )
        return "PAUSED"

    def observe_ambiguous_cancel(self, active_offer_ids, now_ms=None):
        """Resolve an uncertain cancel from repeated authoritative Offers snapshots.

        Whether the offer is still present or absent is safe: presence means the
        cancel did not take effect and the strategy may retry it; absence means the
        cancel or a fill removed it. Two complete snapshots at least 30 seconds
        apart protect against a transient or eventually-consistent account view.
        """
        now = int(now_ms if now_ms is not None else self._now_ms())
        active_ids = {int(value) for value in active_offer_ids}
        with self.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM runtime_state WHERE singleton = 1").fetchone()
            reason = str(row["safe_reason"] or "")
            if row["mode"] != "PAUSED" or not reason.startswith("AMBIGUOUS_CANCEL:"):
                return dict(row)
            try:
                offer_id = int(reason.split(":", 1)[1])
            except (TypeError, ValueError):
                return dict(row)
            previous_sync = row["last_consistent_sync_ms"]
            count = int(row["consistent_syncs"])
            if previous_sync is None or now - int(previous_sync) >= 30_000:
                count += 1
                connection.execute(
                    """UPDATE runtime_state SET consistent_syncs=?, last_consistent_sync_ms=?,
                       updated_at_ms=? WHERE singleton=1""",
                    (count, now, now),
                )
            if count >= 2:
                present = offer_id in active_ids
                connection.execute(
                    """INSERT INTO ownership_events(
                           offer_id, credit_id, event_type, details_json, created_at_ms
                       ) VALUES(?, NULL, ?, ?, ?)""",
                    (
                        offer_id,
                        "CANCEL_RECONCILED_PRESENT" if present else "CANCEL_RECONCILED_ABSENT",
                        json.dumps(
                            {"authoritativeSnapshots": count, "offerPresent": present},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
                self._resume_protected_pause(connection, row, now, "ambiguous cancel reconciled")
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
            if from_version is not None:
                connection.execute(
                    """INSERT OR IGNORE INTO reprice_chains(
                           chain_key, strategy_version, base_slice_key, pool, layer,
                           origin_rate, started_at_ms, current_stage, current_offer_id,
                           last_reprice_at_ms, pending_action, pending_target_rate,
                           market_anchor_rate, status, updated_at_ms
                       )
                       SELECT
                           ? || CASE
                               WHEN instr(chain_key, '|') > 0
                                   THEN substr(chain_key, instr(chain_key, '|'))
                               ELSE '|' || chain_key
                           END,
                           ?, base_slice_key, pool, layer, origin_rate, started_at_ms,
                           current_stage, current_offer_id, last_reprice_at_ms,
                           pending_action, pending_target_rate, market_anchor_rate,
                           status, ?
                       FROM reprice_chains
                       WHERE strategy_version=? AND status='ACTIVE'""",
                    (version_id, version_id, now, from_version),
                )
            connection.execute(
                """INSERT INTO strategy_events(event_type, from_version, to_version, plan_hash, reason, created_at_ms)
                   VALUES('SCHEMA_NORMALIZATION', ?, ?, NULL, ?, ?)""",
                (from_version, version_id, str(reason), now),
            )
        return version_id

    def repair_normalized_reprice_chains(self, strategy_version, now_ms=None):
        now = int(now_ms if now_ms is not None else self._now_ms())
        with self.transaction(immediate=True) as connection:
            event = connection.execute(
                """SELECT from_version FROM strategy_events
                   WHERE event_type='SCHEMA_NORMALIZATION' AND to_version=?
                   ORDER BY created_at_ms DESC, id DESC LIMIT 1""",
                (str(strategy_version),),
            ).fetchone()
            if event is None or not event["from_version"]:
                return 0
            before = connection.total_changes
            connection.execute(
                """INSERT OR IGNORE INTO reprice_chains(
                       chain_key, strategy_version, base_slice_key, pool, layer,
                       origin_rate, started_at_ms, current_stage, current_offer_id,
                       last_reprice_at_ms, pending_action, pending_target_rate,
                       market_anchor_rate, status, updated_at_ms
                   )
                   SELECT
                       ? || CASE
                           WHEN instr(chain_key, '|') > 0
                               THEN substr(chain_key, instr(chain_key, '|'))
                           ELSE '|' || chain_key
                       END,
                       ?, base_slice_key, pool, layer, origin_rate, started_at_ms,
                       current_stage, current_offer_id, last_reprice_at_ms,
                       pending_action, pending_target_rate, market_anchor_rate,
                       status, ?
                   FROM reprice_chains
                   WHERE strategy_version=? AND status='ACTIVE'""",
                (
                    str(strategy_version),
                    str(strategy_version),
                    now,
                    event["from_version"],
                ),
            )
            return connection.total_changes - before

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

    def submission_attempt_count_since(self, since_ms, currency="USD"):
        """Count conservatively reserved submit attempts in a rolling window."""

        with self.read_connection() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS attempt_count FROM order_intents
                   WHERE currency=? AND created_at_ms>=?""",
                (str(currency).upper(), int(since_ms)),
            ).fetchone()
        return int(row["attempt_count"] or 0)

    def observe_period_selection(
        self,
        strategy_version,
        pool,
        selected_period,
        scores,
        now_ms=None,
        advantage=D("0.20"),
        hold_ms=600_000,
    ):
        """Persist an active term and require a qualified challenger to hold for ten minutes."""

        now = int(now_ms if now_ms is not None else self._now_ms())
        version = str(strategy_version)
        pool = str(pool)
        period = None if selected_period is None else int(selected_period)
        score_rows = scores or []
        serialized = json.dumps(score_rows, ensure_ascii=False, sort_keys=True, default=str)
        score_by_period = {
            int(row["period"]): D(row.get("totalScore") or 0)
            for row in score_rows
            if row.get("period") is not None and row.get("eligible", True)
        }
        with self.transaction(immediate=True) as connection:
            previous = connection.execute(
                """SELECT * FROM period_selection_state
                   WHERE strategy_version=? AND pool=?""",
                (version, pool),
            ).fetchone()
            active = None if previous is None else previous["selected_period"]
            selected_since = (
                now if previous is None or previous["selected_since_ms"] is None else int(previous["selected_since_ms"])
            )
            challenger = None
            challenger_since = None
            promoted = False
            if period is None:
                active = None
                selected_since = now
            elif previous is None or active is None:
                active = period
                selected_since = now
            elif int(active) != period:
                old_score = score_by_period.get(int(active))
                new_score = score_by_period.get(period)
                qualifies = new_score is not None and (
                    old_score is None or (new_score > old_score and new_score >= old_score * (D("1") + D(advantage)))
                )
                if qualifies:
                    previous_challenger = previous["challenger_period"]
                    challenger = period
                    challenger_since = (
                        int(previous["challenger_since_ms"])
                        if previous_challenger == period and previous["challenger_since_ms"] is not None
                        else now
                    )
                    if now - challenger_since >= int(hold_ms):
                        active = period
                        selected_since = now
                        challenger = None
                        challenger_since = None
                        promoted = True
            connection.execute(
                """INSERT INTO period_selection_state(
                       strategy_version, pool, selected_period, selected_since_ms,
                       scores_json, updated_at_ms, challenger_period, challenger_since_ms
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(strategy_version, pool) DO UPDATE SET
                       selected_period=excluded.selected_period,
                       selected_since_ms=excluded.selected_since_ms,
                       scores_json=excluded.scores_json,
                       updated_at_ms=excluded.updated_at_ms,
                       challenger_period=excluded.challenger_period,
                       challenger_since_ms=excluded.challenger_since_ms""",
                (
                    version,
                    pool,
                    active,
                    selected_since,
                    serialized,
                    now,
                    challenger,
                    challenger_since,
                ),
            )
        return {
            "strategyVersion": version,
            "pool": pool,
            "selectedPeriod": None if active is None else int(active),
            "selectedSinceMs": selected_since,
            "leaderPeriod": period,
            "challengerPeriod": challenger,
            "challengerSinceMs": challenger_since,
            "challengerDurationMs": 0 if challenger_since is None else max(0, now - challenger_since),
            "promoted": promoted,
            "updatedAtMs": now,
        }

    def observe_demand_confirmation(
        self,
        strategy_version,
        scope,
        signal_key,
        share,
        below_threshold,
        now_ms=None,
        required_cycles=2,
    ):
        """Confirm a low-demand signal only across distinct five-minute buckets."""

        now = int(now_ms if now_ms is not None else self._now_ms())
        cycle = now // 300_000
        with self.transaction(immediate=True) as connection:
            previous = connection.execute(
                """SELECT * FROM demand_confirmation_state
                   WHERE strategy_version=? AND scope=? AND signal_key=?""",
                (str(strategy_version), str(scope), str(signal_key)),
            ).fetchone()
            count = 0 if previous is None else int(previous["consecutive_cycles"] or 0)
            last_cycle = None if previous is None else previous["last_cycle"]
            if below_threshold is None or not bool(below_threshold):
                count = 0
            elif last_cycle != cycle:
                count = count + 1 if last_cycle == cycle - 1 else 1
            confirmed = bool(count >= int(required_cycles))
            connection.execute(
                """INSERT INTO demand_confirmation_state(
                       strategy_version, scope, signal_key, consecutive_cycles,
                       confirmed, last_cycle, last_share, updated_at_ms
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(strategy_version, scope, signal_key) DO UPDATE SET
                       consecutive_cycles=excluded.consecutive_cycles,
                       confirmed=excluded.confirmed,
                       last_cycle=excluded.last_cycle,
                       last_share=excluded.last_share,
                       updated_at_ms=excluded.updated_at_ms""",
                (
                    str(strategy_version),
                    str(scope),
                    str(signal_key),
                    count,
                    int(confirmed),
                    cycle,
                    None if share is None else _decimal_text(share),
                    now,
                ),
            )
        return {"cycles": count, "confirmed": confirmed, "cycle": cycle}

    def consolidation_status(self):
        with self.read_connection() as connection:
            row = connection.execute("SELECT * FROM consolidation_state WHERE singleton=1").fetchone()
        return {} if row is None else dict(row)

    def begin_consolidation(self, offer_id, wallet, offer_amount, target_period, strategy_version, now_ms=None):
        now = int(now_ms if now_ms is not None else self._now_ms())
        with self.transaction(immediate=True) as connection:
            current = connection.execute("SELECT * FROM consolidation_state WHERE singleton=1").fetchone()
            if current is not None and current["state"] != "IDLE":
                return dict(current)
            connection.execute(
                """INSERT INTO consolidation_state(
                       singleton, state, offer_id, captured_wallet, captured_offer_amount,
                       target_period, strategy_version, started_at_ms, updated_at_ms, last_error
                   ) VALUES(1, 'PLANNED', ?, ?, ?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(singleton) DO UPDATE SET
                       state='PLANNED', offer_id=excluded.offer_id,
                       captured_wallet=excluded.captured_wallet,
                       captured_offer_amount=excluded.captured_offer_amount,
                       target_period=excluded.target_period,
                       strategy_version=excluded.strategy_version,
                       started_at_ms=excluded.started_at_ms,
                       updated_at_ms=excluded.updated_at_ms, last_error=NULL""",
                (
                    int(offer_id),
                    _decimal_text(wallet),
                    _decimal_text(offer_amount),
                    int(target_period),
                    str(strategy_version),
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM consolidation_state WHERE singleton=1").fetchone()
        return dict(row)

    def update_consolidation(self, state, error=None, now_ms=None):
        now = int(now_ms if now_ms is not None else self._now_ms())
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE consolidation_state SET state=?, last_error=?, updated_at_ms=? WHERE singleton=1",
                (str(state), error, now),
            )
            row = connection.execute("SELECT * FROM consolidation_state WHERE singleton=1").fetchone()
        return dict(row)

    def clear_consolidation(self, now_ms=None):
        now = int(now_ms if now_ms is not None else self._now_ms())
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE consolidation_state SET state='IDLE', offer_id=NULL,
                   captured_wallet=NULL, captured_offer_amount=NULL, target_period=NULL,
                   strategy_version=NULL, started_at_ms=NULL, updated_at_ms=?, last_error=NULL
                   WHERE singleton=1""",
                (now,),
            )

    @staticmethod
    def _takeover_digest(offer):
        payload = {
            key: offer.get(key)
            for key in ("id", "amount", "amount_original", "rate", "rate_real", "period", "offer_type", "flags")
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def observe_external_takeover(self, offer, now_ms=None, minimum_gap_ms=30_000):
        now = int(now_ms if now_ms is not None else self._now_ms())
        offer_id = int(offer.get("id") or offer.get("offer_id"))
        digest = self._takeover_digest(offer)
        with self.transaction(immediate=True) as connection:
            previous = connection.execute(
                "SELECT * FROM external_takeover_state WHERE offer_id=?", (offer_id,)
            ).fetchone()
            first_seen = now
            state = "OBSERVED"
            if previous is not None and previous["snapshot_digest"] == digest:
                first_seen = int(previous["first_seen_ms"])
                state = previous["state"]
                if state == "OBSERVED" and now - first_seen >= int(minimum_gap_ms):
                    state = "CONFIRMED"
            connection.execute(
                """INSERT INTO external_takeover_state(
                       offer_id, state, snapshot_digest, first_seen_ms, confirmed_at_ms,
                       updated_at_ms, last_error
                   ) VALUES(?, ?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(offer_id) DO UPDATE SET
                       state=excluded.state, snapshot_digest=excluded.snapshot_digest,
                       first_seen_ms=excluded.first_seen_ms,
                       confirmed_at_ms=excluded.confirmed_at_ms,
                       updated_at_ms=excluded.updated_at_ms, last_error=NULL""",
                (offer_id, state, digest, first_seen, now if state == "CONFIRMED" else None, now),
            )
            row = connection.execute("SELECT * FROM external_takeover_state WHERE offer_id=?", (offer_id,)).fetchone()
        return dict(row)

    def update_external_takeover(self, offer_id, state, error=None, now_ms=None):
        now = int(now_ms if now_ms is not None else self._now_ms())
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE external_takeover_state SET state=?, last_error=?, updated_at_ms=?
                   WHERE offer_id=?""",
                (str(state), error, now, int(offer_id)),
            )

    def reset_unconfirmed_external_takeovers(self):
        """Discard snapshot confirmations after a non-authoritative account read."""

        with self.transaction(immediate=True) as connection:
            connection.execute("DELETE FROM external_takeover_state WHERE state IN ('OBSERVED', 'CONFIRMED')")

    def discard_unconfirmed_external_takeover(self, offer_id):
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM external_takeover_state WHERE offer_id=? AND state IN ('OBSERVED', 'CONFIRMED')",
                (int(offer_id),),
            )

    def external_takeovers(self, states=None):
        query = "SELECT * FROM external_takeover_state"
        params = []
        if states:
            placeholders = ",".join("?" for _ in states)
            query += f" WHERE state IN ({placeholders})"
            params.extend(sorted(states))
        query += " ORDER BY offer_id"
        with self.read_connection() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def period_activity(self, since_ms, currency="USD"):
        """Return recent robot submissions and managed fills grouped by term."""

        with self.read_connection() as connection:
            submitted = connection.execute(
                """SELECT period, COUNT(*) AS order_count, COALESCE(SUM(CAST(amount AS REAL)), 0) AS amount
                   FROM order_intents
                   WHERE currency=? AND created_at_ms>=?
                   GROUP BY period ORDER BY period""",
                (str(currency).upper(), int(since_ms)),
            ).fetchall()
            traded = connection.execute(
                """SELECT period, COUNT(*) AS trade_count, COALESCE(SUM(ABS(CAST(amount AS REAL))), 0) AS amount
                   FROM funding_trades
                   WHERE currency=? AND managed=1 AND mts>=?
                   GROUP BY period ORDER BY period""",
                (str(currency).upper(), int(since_ms)),
            ).fetchall()
        return {
            "submitted": [
                {"period": int(row["period"]), "count": int(row["order_count"]), "amount": D(str(row["amount"]))}
                for row in submitted
            ],
            "traded": [
                {"period": int(row["period"]), "count": int(row["trade_count"]), "amount": D(str(row["amount"]))}
                for row in traded
            ],
        }

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
        self.enter_protected_pause(f"AMBIGUOUS_SUBMIT:{intent_id}")
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
            self.enter_protected_pause(f"AMBIGUOUS_SUBMIT:{row['id']}")
        return {"closedBeforeSend": planned, "ambiguousAfterSend": len(submitting)}

    def resolve_ambiguous_intent(self, intent_id, exchange_offer_id=None, close=False):
        """Resolve an uncertain write only after an operator has reconciled it.

        Binding requires a concrete exchange id.  Closing means the operator has
        confirmed that no offer exists. The protected pause remains read-only
        until the normal two-snapshot recovery barrier completes.
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
            if can_clear_ambiguous_pause(unresolved, runtime["mode"], runtime["safe_manual"]):
                connection.execute(
                    """UPDATE runtime_state SET mode='PAUSED', previous_mode=NULL,
                       safe_reason=NULL, safe_manual=0, consistent_syncs=0,
                       last_consistent_sync_ms=NULL, updated_at_ms=? WHERE singleton=1""",
                    (now,),
                )
                connection.execute(
                    """INSERT INTO mode_events(from_mode, to_mode, reason, created_at_ms)
                       VALUES('PAUSED', 'PAUSED', 'ambiguous intent resolved', ?)""",
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

    def reconcile_ambiguous_candidates(self, confirm_absent=False, now_ms=None):
        """Reconcile uncertain submits and resume the mode that was interrupted.

        A unique offer/trade is bound immediately. Absence is accepted only when
        the caller confirms that both the active Offers and Funding Trades reads
        were authoritative, twice at least 30 seconds apart. Multiple candidates
        remain in protected PAUSED for operator review because choosing one would be guesswork.
        """
        resolved = []
        closed_absent = []
        now = int(now_ms if now_ms is not None else self._now_ms())
        with self.transaction(immediate=True) as connection:
            intents = connection.execute("SELECT * FROM order_intents WHERE state='AMBIGUOUS' ORDER BY id").fetchall()
            candidate_counts = {}
            for intent in intents:
                request_ms = int(intent["request_started_at_ms"] or intent["updated_at_ms"] or 0)
                start = request_ms - 300_000
                end = request_ms + 600_000
                candidate_ids = set()
                offers = connection.execute(
                    """SELECT offer_id FROM offers
                       WHERE status != 'CLOSED' AND currency=?
                         AND (amount=? OR COALESCE(amount_original, amount)=?) AND period=?
                         AND offer_type=? AND flags=?
                         AND (rate=? OR rate_real=?)
                         AND COALESCE(mts_created, 0) BETWEEN ? AND ?""",
                    (
                        intent["currency"],
                        intent["amount"],
                        intent["amount"],
                        intent["period"],
                        intent["offer_type"],
                        intent["flags"],
                        intent["submitted_rate"],
                        intent["effective_rate"],
                        start,
                        end,
                    ),
                ).fetchall()
                candidate_ids.update(int(row["offer_id"]) for row in offers)
                trades = connection.execute(
                    """SELECT offer_id FROM funding_trades
                       WHERE currency=? AND period=? AND mts BETWEEN ? AND ?
                         AND offer_id IS NOT NULL
                         AND ABS(CAST(rate AS REAL) - CAST(? AS REAL)) <= 0.0000001
                       GROUP BY offer_id
                       HAVING ABS(SUM(CAST(amount AS REAL)) - CAST(? AS REAL)) <= 0.00000001""",
                    (
                        intent["currency"],
                        intent["period"],
                        start,
                        end,
                        intent["effective_rate"],
                        intent["amount"],
                    ),
                ).fetchall()
                candidate_ids.update(int(row["offer_id"]) for row in trades if row["offer_id"] is not None)
                historical_offers = connection.execute(
                    """SELECT offer_id FROM offer_history
                       WHERE currency=? AND (amount=? OR COALESCE(amount_original, amount)=?)
                         AND period=? AND offer_type=? AND flags=?
                         AND (rate=? OR rate_real=?)
                         AND COALESCE(mts_created, 0) BETWEEN ? AND ?""",
                    (
                        intent["currency"],
                        intent["amount"],
                        intent["amount"],
                        intent["period"],
                        intent["offer_type"],
                        intent["flags"],
                        intent["submitted_rate"],
                        intent["effective_rate"],
                        start,
                        end,
                    ),
                ).fetchall()
                candidate_ids.update(int(row["offer_id"]) for row in historical_offers)
                bound = {
                    int(row["exchange_offer_id"])
                    for row in connection.execute(
                        "SELECT exchange_offer_id FROM order_intents WHERE exchange_offer_id IS NOT NULL AND id != ?",
                        (intent["id"],),
                    ).fetchall()
                }
                candidate_ids.difference_update(bound)
                candidate_counts[int(intent["id"])] = len(candidate_ids)
                offer_id = unique_unbound_candidate(candidate_ids, bound)
                if offer_id is None:
                    continue
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
            remaining = connection.execute("SELECT * FROM order_intents WHERE state='AMBIGUOUS' ORDER BY id").fetchall()
            runtime = connection.execute("SELECT * FROM runtime_state WHERE singleton=1").fetchone()
            if (
                remaining
                and confirm_absent
                and all(
                    candidate_counts.get(int(item["id"]), 0) == 0 and str(item["offer_type"]).upper() == "LIMIT"
                    for item in remaining
                )
            ):
                previous_sync = runtime["last_consistent_sync_ms"]
                count = int(runtime["consistent_syncs"])
                if previous_sync is None or now - int(previous_sync) >= 30_000:
                    count += 1
                    connection.execute(
                        """UPDATE runtime_state SET consistent_syncs=?, last_consistent_sync_ms=?,
                           updated_at_ms=? WHERE singleton=1""",
                        (count, now, now),
                    )
                if count >= 2:
                    ids = [int(item["id"]) for item in remaining]
                    placeholders = ",".join("?" for _ in ids)
                    connection.execute(
                        f"""UPDATE order_intents SET state='CLOSED',
                            error_text='authoritative snapshots confirmed absent',
                            write_phase='NOT_CREATED', resolution='AUTO_CONFIRMED_ABSENT',
                            resolved_at_ms=?, updated_at_ms=?
                            WHERE id IN ({placeholders})""",
                        (now, now, *ids),
                    )
                    closed_absent.extend(ids)
                    remaining = []
            elif remaining and any(candidate_counts.get(int(item["id"]), 0) != 0 for item in remaining):
                connection.execute(
                    """UPDATE runtime_state SET consistent_syncs=0, last_consistent_sync_ms=NULL,
                       updated_at_ms=? WHERE singleton=1""",
                    (now,),
                )
            if not remaining and (resolved or closed_absent):
                runtime = connection.execute("SELECT * FROM runtime_state WHERE singleton=1").fetchone()
                if runtime["mode"] == "PAUSED" and str(runtime["safe_reason"] or "").startswith("AMBIGUOUS_SUBMIT:"):
                    self._resume_protected_pause(
                        connection,
                        runtime,
                        now,
                        "ambiguous submit reconciled" if resolved else "ambiguous submit confirmed absent",
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

    @staticmethod
    def _reprice_chain_key(strategy_version, slice_key):
        return f"{strategy_version}|{base_slice_key(slice_key)}"

    def ensure_reprice_chain(self, offer, strategy_version, now_ms=None):
        offer_id = int(offer.get("offer_id") or offer.get("id"))
        now = int(now_ms if now_ms is not None else self._now_ms())
        observed_rate = _decimal_text(offer.get("rate_real") or offer["rate"])
        started_at = int(offer.get("mts_created") or now)
        with self.transaction(immediate=True) as connection:
            bound = connection.execute(
                """SELECT * FROM reprice_chains
                   WHERE strategy_version=? AND current_offer_id=? AND status='ACTIVE'
                   ORDER BY updated_at_ms DESC LIMIT 1""",
                (str(strategy_version), offer_id),
            ).fetchone()
            if bound is not None:
                return dict(bound)
            intent = connection.execute(
                "SELECT * FROM order_intents WHERE exchange_offer_id=?",
                (offer_id,),
            ).fetchone()
            if intent is None:
                return None
            base_key = base_slice_key(intent["slice_key"])
            chain_key = f"{self._reprice_chain_key(strategy_version, base_key)}|intent:{int(intent['id'])}"
            current = connection.execute(
                "SELECT * FROM reprice_chains WHERE chain_key=?",
                (chain_key,),
            ).fetchone()
            if current is None:
                predecessor = connection.execute(
                    """SELECT * FROM reprice_chains
                       WHERE current_offer_id=? AND status='ACTIVE'
                         AND strategy_version<>?
                       ORDER BY updated_at_ms DESC, rowid DESC LIMIT 1""",
                    (offer_id, str(strategy_version)),
                ).fetchone()
                if predecessor is None:
                    connection.execute(
                        """INSERT INTO reprice_chains(
                               chain_key, strategy_version, base_slice_key, pool, layer,
                               origin_rate, started_at_ms, current_stage, current_offer_id,
                               status, updated_at_ms
                           ) VALUES(?, ?, ?, ?, ?, ?, ?, 0, ?, 'ACTIVE', ?)""",
                        (
                            chain_key,
                            str(strategy_version),
                            base_key,
                            str(offer.get("pool") or intent["pool"]),
                            str(offer.get("layer") or intent["layer"]),
                            observed_rate,
                            started_at,
                            offer_id,
                            now,
                        ),
                    )
                else:
                    connection.execute(
                        """INSERT INTO reprice_chains(
                               chain_key, strategy_version, base_slice_key, pool, layer,
                               origin_rate, started_at_ms, current_stage, current_offer_id,
                               last_reprice_at_ms, pending_action, pending_target_rate,
                               market_anchor_rate, status, updated_at_ms
                           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)""",
                        (
                            chain_key,
                            str(strategy_version),
                            base_key,
                            predecessor["pool"],
                            predecessor["layer"],
                            predecessor["origin_rate"],
                            predecessor["started_at_ms"],
                            predecessor["current_stage"],
                            offer_id,
                            predecessor["last_reprice_at_ms"],
                            predecessor["pending_action"],
                            predecessor["pending_target_rate"],
                            predecessor["market_anchor_rate"],
                            now,
                        ),
                    )
            elif int(current["current_offer_id"] or 0) != offer_id:
                if current["pending_action"] == "AGE_STAGE":
                    connection.execute(
                        """UPDATE reprice_chains
                           SET current_offer_id=?, pending_action=NULL,
                               pending_target_rate=NULL, status='ACTIVE', updated_at_ms=?
                           WHERE chain_key=?""",
                        (offer_id, now, chain_key),
                    )
                else:
                    connection.execute(
                        """UPDATE reprice_chains
                           SET pool=?, layer=?, origin_rate=?, started_at_ms=?,
                               current_stage=0, current_offer_id=?, last_reprice_at_ms=NULL,
                               pending_action=NULL, pending_target_rate=NULL,
                               market_anchor_rate=NULL,
                               status='ACTIVE', updated_at_ms=?
                           WHERE chain_key=?""",
                        (
                            str(offer.get("pool") or intent["pool"]),
                            str(offer.get("layer") or intent["layer"]),
                            observed_rate,
                            started_at,
                            offer_id,
                            now,
                            chain_key,
                        ),
                    )
            row = connection.execute(
                "SELECT * FROM reprice_chains WHERE chain_key=?",
                (chain_key,),
            ).fetchone()
        return None if row is None else dict(row)

    def reprice_chain(self, chain_key):
        with self.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM reprice_chains WHERE chain_key=?",
                (str(chain_key),),
            ).fetchone()
        return None if row is None else dict(row)

    def reprice_chain_for_offer(self, offer_id):
        with self.read_connection() as connection:
            row = connection.execute(
                """SELECT * FROM reprice_chains
                   WHERE current_offer_id=? AND status='ACTIVE'
                   ORDER BY updated_at_ms DESC LIMIT 1""",
                (int(offer_id),),
            ).fetchone()
        return None if row is None else dict(row)

    def reprice_chains(self, active_only=False):
        query = "SELECT * FROM reprice_chains"
        if active_only:
            query += " WHERE status='ACTIVE'"
        query += " ORDER BY updated_at_ms, chain_key"
        with self.read_connection() as connection:
            return [dict(row) for row in connection.execute(query).fetchall()]

    def complete_reprice_stage(self, chain_key, stage, now_ms=None, market_anchor_rate=None):
        now = int(now_ms if now_ms is not None else self._now_ms())
        with self.transaction(immediate=True) as connection:
            if market_anchor_rate is None:
                connection.execute(
                    """UPDATE reprice_chains
                       SET current_stage=MAX(current_stage, ?), updated_at_ms=?
                       WHERE chain_key=?""",
                    (int(stage), now, str(chain_key)),
                )
            else:
                connection.execute(
                    """UPDATE reprice_chains
                       SET current_stage=MAX(current_stage, ?),
                           market_anchor_rate=COALESCE(market_anchor_rate, ?),
                           updated_at_ms=?
                       WHERE chain_key=?""",
                    (int(stage), _decimal_text(market_anchor_rate), now, str(chain_key)),
                )
            row = connection.execute(
                "SELECT * FROM reprice_chains WHERE chain_key=?",
                (str(chain_key),),
            ).fetchone()
        return None if row is None else dict(row)

    def set_reprice_market_anchor(self, chain_key, rate, now_ms=None):
        now = int(now_ms if now_ms is not None else self._now_ms())
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE reprice_chains
                   SET market_anchor_rate=COALESCE(market_anchor_rate, ?), updated_at_ms=?
                   WHERE chain_key=?""",
                (_decimal_text(rate), now, str(chain_key)),
            )
            row = connection.execute(
                "SELECT * FROM reprice_chains WHERE chain_key=?",
                (str(chain_key),),
            ).fetchone()
        return None if row is None else dict(row)

    def mark_reprice_pending(
        self,
        chain_key,
        action,
        target_rate,
        stage=None,
        now_ms=None,
        market_anchor_rate=None,
    ):
        now = int(now_ms if now_ms is not None else self._now_ms())
        with self.transaction(immediate=True) as connection:
            if str(action) == "AGE_STAGE":
                connection.execute(
                    """UPDATE reprice_chains
                       SET current_stage=MAX(current_stage, ?), current_offer_id=NULL,
                           last_reprice_at_ms=?, pending_action=?,
                           pending_target_rate=?,
                           market_anchor_rate=COALESCE(market_anchor_rate, ?),
                           updated_at_ms=?
                       WHERE chain_key=?""",
                    (
                        int(stage or 0),
                        now,
                        str(action),
                        _decimal_text(target_rate),
                        None if market_anchor_rate is None else _decimal_text(market_anchor_rate),
                        now,
                        str(chain_key),
                    ),
                )
            else:
                connection.execute(
                    """UPDATE reprice_chains
                       SET current_offer_id=NULL, last_reprice_at_ms=?,
                           pending_action=?, pending_target_rate=?, updated_at_ms=?
                       WHERE chain_key=?""",
                    (now, str(action), _decimal_text(target_rate), now, str(chain_key)),
                )
            row = connection.execute(
                "SELECT * FROM reprice_chains WHERE chain_key=?",
                (str(chain_key),),
            ).fetchone()
        return None if row is None else dict(row)

    def pending_reprice_for_base(self, base_key, strategy_version):
        chain_key = self._reprice_chain_key(strategy_version, base_key)
        with self.read_connection() as connection:
            row = connection.execute(
                """SELECT * FROM reprice_chains
                   WHERE chain_key=? AND status='ACTIVE'
                     AND pending_action IS NOT NULL AND pending_target_rate IS NOT NULL""",
                (chain_key,),
            ).fetchone()
            if row is None:
                pool, layer = slice_pool_layer(base_key)
                if pool is not None:
                    row = connection.execute(
                        """SELECT * FROM reprice_chains
                           WHERE strategy_version=? AND pool=? AND layer=?
                             AND status='ACTIVE' AND current_offer_id IS NULL
                             AND pending_action IS NOT NULL AND pending_target_rate IS NOT NULL
                           ORDER BY last_reprice_at_ms, updated_at_ms, chain_key LIMIT 1""",
                        (str(strategy_version), pool, layer),
                    ).fetchone()
        return None if row is None else dict(row)

    def bind_reprice_replacement(
        self,
        base_key,
        strategy_version,
        offer_id,
        effective_rate,
        now_ms=None,
    ):
        now = int(now_ms if now_ms is not None else self._now_ms())
        chain_key = self._reprice_chain_key(strategy_version, base_key)
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM reprice_chains WHERE chain_key=?",
                (chain_key,),
            ).fetchone()
            if row is None:
                pool, layer = slice_pool_layer(base_key)
                if pool is not None:
                    row = connection.execute(
                        """SELECT * FROM reprice_chains
                           WHERE strategy_version=? AND pool=? AND layer=?
                             AND status='ACTIVE' AND current_offer_id IS NULL
                             AND pending_action IS NOT NULL
                           ORDER BY last_reprice_at_ms, updated_at_ms, chain_key LIMIT 1""",
                        (str(strategy_version), pool, layer),
                    ).fetchone()
            if row is None:
                return None
            chain_key = row["chain_key"]
            if row["pending_action"] == "AGE_STAGE":
                connection.execute(
                    """UPDATE reprice_chains
                       SET current_offer_id=?, pending_action=NULL,
                           pending_target_rate=NULL, status='ACTIVE', updated_at_ms=?
                       WHERE chain_key=?""",
                    (int(offer_id), now, chain_key),
                )
            else:
                connection.execute(
                    """UPDATE reprice_chains
                       SET origin_rate=?, started_at_ms=?, current_stage=0,
                           current_offer_id=?, pending_action=NULL,
                           pending_target_rate=NULL, market_anchor_rate=NULL,
                           status='ACTIVE', updated_at_ms=?
                       WHERE chain_key=?""",
                    (_decimal_text(effective_rate), now, int(offer_id), now, chain_key),
                )
            result = connection.execute(
                "SELECT * FROM reprice_chains WHERE chain_key=?",
                (chain_key,),
            ).fetchone()
        return None if result is None else dict(result)

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

    def adopt_external_offers(self, offers, strategy_version):
        """Persist explicit preflight-approved ownership without touching the exchange."""
        adopted = []
        now = self._now_ms()
        with self.transaction(immediate=True) as connection:
            normalized = {int(value.get("id") or value.get("offer_id")): value for value in offers or []}
            for raw_id in sorted(normalized):
                offer = connection.execute(
                    "SELECT * FROM offers WHERE offer_id=? AND status!='CLOSED'", (raw_id,)
                ).fetchone()
                if offer is None:
                    source = normalized[raw_id]
                    connection.execute(
                        """INSERT INTO offers(offer_id, currency, amount, amount_original, rate, rate_real,
                               period, offer_type, display_type, flags, status, managed, pool, layer,
                               plan_hash, strategy_variant, mts_created, mts_updated, last_seen_ms)
                           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, NULL, 'baseline', ?, ?, ?)""",
                        (
                            raw_id,
                            str(source.get("currency", "USD")).upper(),
                            _decimal_text(source["amount"]),
                            _decimal_text(source.get("amount_original") or source["amount"]),
                            _decimal_text(source["rate"]),
                            None if source.get("rate_real") is None else _decimal_text(source["rate_real"]),
                            int(source["period"]),
                            source.get("offer_type", "LIMIT"),
                            source.get("display_type"),
                            int(source.get("flags", 0)),
                            source.get("status", "ACTIVE"),
                            source.get("pool"),
                            source.get("mts_created"),
                            source.get("mts_updated"),
                            now,
                        ),
                    )
                    offer = connection.execute("SELECT * FROM offers WHERE offer_id=?", (raw_id,)).fetchone()
                if offer["managed"]:
                    continue
                pool = offer["pool"] or "short"
                layer = offer["layer"] or "balanced"
                display_type = offer["display_type"] or offer["offer_type"]
                slice_key = f"adopted:{raw_id}"
                fingerprint = order_intent_fingerprint(
                    offer["currency"],
                    offer["amount"],
                    offer["rate_real"] or offer["rate"],
                    offer["period"],
                    offer["offer_type"],
                    offer["flags"],
                    strategy_version,
                    slice_key,
                )
                connection.execute(
                    """INSERT OR IGNORE INTO order_intents(
                           fingerprint, currency, slice_key, pool, layer, amount, submitted_rate,
                           effective_rate, period, offer_type, display_type, flags, strategy_version,
                           plan_hash, state, exchange_offer_id, write_phase, resolution,
                           request_started_at_ms, resolved_at_ms, strategy_variant, created_at_ms, updated_at_ms
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'CONFIRMED', ?,
                                'CONFIRMED', 'PREFLIGHT_ADOPTED', ?, ?, 'baseline', ?, ?)""",
                    (
                        fingerprint,
                        offer["currency"],
                        slice_key,
                        pool,
                        layer,
                        offer["amount"],
                        offer["rate"],
                        offer["rate_real"] or offer["rate"],
                        offer["period"],
                        offer["offer_type"],
                        display_type,
                        offer["flags"],
                        str(strategy_version),
                        raw_id,
                        now,
                        now,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """UPDATE offers SET managed=1, pool=?, layer=?, display_type=?,
                              strategy_variant='baseline' WHERE offer_id=?""",
                    (pool, layer, display_type, raw_id),
                )
                connection.execute(
                    """INSERT INTO ownership_events(offer_id, credit_id, event_type, details_json, created_at_ms)
                       VALUES(?, NULL, 'ADOPT_EXTERNAL', ?, ?)""",
                    (raw_id, json.dumps({"strategyVersion": str(strategy_version)}, sort_keys=True), now),
                )
                connection.execute(
                    """INSERT INTO external_takeover_state(
                           offer_id, state, snapshot_digest, first_seen_ms,
                           confirmed_at_ms, updated_at_ms, last_error
                       ) VALUES(?, 'ADOPTED', ?, ?, ?, ?, NULL)
                       ON CONFLICT(offer_id) DO UPDATE SET
                           state='ADOPTED', snapshot_digest=excluded.snapshot_digest,
                           confirmed_at_ms=excluded.confirmed_at_ms,
                           updated_at_ms=excluded.updated_at_ms, last_error=NULL""",
                    (raw_id, self._takeover_digest(normalized[raw_id]), now, now, now),
                )
                adopted.append(raw_id)
        return adopted

    def record_ownership_event(self, event_type, offer_id=None, credit_id=None, details=None):
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO ownership_events(offer_id, credit_id, event_type, details_json, created_at_ms)
                   VALUES(?, ?, ?, ?, ?)""",
                (
                    None if offer_id is None else int(offer_id),
                    None if credit_id is None else int(credit_id),
                    str(event_type),
                    json.dumps(details or {}, sort_keys=True),
                    self._now_ms(),
                ),
            )

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
        chain_key=None,
        stage=None,
        benchmark_rate=None,
        floor_rate=None,
    ):
        created = int(created_at_ms if created_at_ms is not None else self._now_ms())
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO reprice_events(
                    offer_id, intent_id, reason, old_rate, new_rate,
                    strategy_version, plan_hash, display_type, chain_key,
                    stage, benchmark_rate, floor_rate, created_at_ms
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    None if offer_id is None else int(offer_id),
                    None if intent_id is None else int(intent_id),
                    str(reason),
                    None if old_rate is None else _decimal_text(old_rate),
                    None if new_rate is None else _decimal_text(new_rate),
                    None if strategy_version is None else str(strategy_version),
                    plan_hash,
                    display_type,
                    chain_key,
                    None if stage is None else int(stage),
                    None if benchmark_rate is None else _decimal_text(benchmark_rate),
                    None if floor_rate is None else _decimal_text(floor_rate),
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
                attribution_state = str(credit.get("attribution_state") or "EXTERNAL").upper()
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
                if offer_id is None and credit.get("mts_opening"):
                    direct = connection.execute(
                        """SELECT intents.exchange_offer_id, intents.pool, intents.layer,
                                  intents.display_type, intents.strategy_variant
                           FROM order_intents AS intents
                           LEFT JOIN offers ON offers.offer_id = intents.exchange_offer_id
                           WHERE intents.state IN ('CONFIRMED', 'CLOSED')
                             AND intents.currency = ? AND intents.period = ? AND intents.amount = ?
                             AND ABS(CAST(intents.effective_rate AS REAL) - ?) <= 0.0000000001
                             AND intents.updated_at_ms BETWEEN ? AND ?
                             AND (offers.offer_id IS NULL OR offers.status = 'CLOSED')""",
                        (
                            str(credit.get("currency", "USD")).upper(),
                            int(credit["period"]),
                            _decimal_text(credit["amount"]),
                            float(D(credit.get("rate_real") or credit["rate"])),
                            int(credit["mts_opening"]) - 300_000,
                            int(credit["mts_opening"]) + 300_000,
                        ),
                    ).fetchall()
                    if len(direct) == 1:
                        candidate = direct[0]
                        offer_id = candidate["exchange_offer_id"]
                        inferred_pool = candidate["pool"]
                        inferred_layer = candidate["layer"]
                        inferred_display_type = candidate["display_type"]
                        inferred_variant = candidate["strategy_variant"]
                    elif direct:
                        attribution_state = "ATTRIBUTION_PENDING"
                if offer_id is not None:
                    intent = connection.execute(
                        "SELECT display_type, strategy_variant FROM order_intents WHERE exchange_offer_id = ?",
                        (int(offer_id),),
                    ).fetchone()
                    managed = managed or intent is not None
                    if intent is not None:
                        inferred_display_type = inferred_display_type or intent["display_type"]
                        inferred_variant = intent["strategy_variant"]
                attribution_state = "MANAGED" if managed else attribution_state
                raw_rate_type = str(credit.get("rate_type") or "").upper()
                if not inferred_display_type:
                    if raw_rate_type in {"FIXED", "LIMIT"}:
                        inferred_display_type = "LIMIT"
                    elif raw_rate_type in {"VAR", "VARIABLE", "FRRDELTAVAR"}:
                        inferred_display_type = "VARIABLE_UNKNOWN"
                connection.execute(
                    """INSERT INTO credits(
                        credit_id, currency, amount, rate, rate_real, period, rate_type, display_type, hidden,
                        status, managed, pool, layer, offer_id, strategy_variant, attribution_state,
                        mts_opening, mts_updated, last_seen_ms
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(credit_id) DO UPDATE SET amount=excluded.amount, rate=excluded.rate,
                        rate_real=excluded.rate_real, status=excluded.status,
                        managed=MAX(credits.managed, excluded.managed), last_seen_ms=excluded.last_seen_ms,
                        pool=COALESCE(credits.pool, excluded.pool),
                        layer=COALESCE(credits.layer, excluded.layer),
                        display_type=COALESCE(credits.display_type, excluded.display_type),
                        offer_id=COALESCE(credits.offer_id, excluded.offer_id),
                        strategy_variant=CASE WHEN excluded.managed=1 THEN excluded.strategy_variant
                                              ELSE credits.strategy_variant END,
                        attribution_state=CASE WHEN excluded.managed=1 THEN 'MANAGED'
                                               WHEN credits.managed=1 THEN 'MANAGED'
                                               ELSE excluded.attribution_state END,
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
                        attribution_state,
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
                        offer_id, currency, amount, amount_original, rate, rate_real,
                        period, offer_type, flags, status, mts_created, mts_updated, managed
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        int(offer["id"]),
                        str(offer.get("currency", "USD")).upper(),
                        _decimal_text(offer["amount"]),
                        _decimal_text(offer.get("amount_original", offer["amount"])),
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
        rows = [
            (
                str(trade["id"]),
                int(trade["mts"]),
                _decimal_text(abs(D(trade["amount"]))),
                _decimal_text(trade["rate"]),
                int(trade["period"]),
            )
            for trade in (trades or [])
        ]
        if not rows:
            return
        with self.transaction(immediate=True) as connection:
            connection.executemany(
                """INSERT OR IGNORE INTO market_trades(trade_id, mts, amount, rate, period)
                   VALUES(?, ?, ?, ?, ?)""",
                rows,
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
            book_count = connection.execute("DELETE FROM book_snapshots WHERE mts < ?", (threshold,)).rowcount
            stats_count = connection.execute("DELETE FROM funding_stats WHERE mts < ?", (threshold,)).rowcount
        return {"trades": trade_count, "bars": bar_count, "books": book_count, "stats": stats_count}

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
                "sampleFrom": None,
                "sampleTo": None,
                "sampleDays": "0",
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
        sample_from = int(rows[0]["mts"])
        sample_to = int(rows[-1]["mts"])
        sample_days = D(max(0, sample_to - sample_from)) / D("86400000")
        utilization = D("0") if principal_time <= 0 else utilized_time / principal_time
        apr = D("0") if principal_time <= 0 else apr_interest / principal_time * D("365")
        return {
            "windowDays": window_days,
            "sampleFrom": sample_from,
            "sampleTo": sample_to,
            "sampleDays": _decimal_text(sample_days),
            "utilizationPercent": _decimal_text(utilization * D("100")),
            "netInterest": _decimal_text(ledger_interest),
            "actualNetAprPercent": _decimal_text(apr * D("100")),
            "idlePrincipalTime": _decimal_text(idle_time),
            "repriceCount": reprices,
            **details,
        }
