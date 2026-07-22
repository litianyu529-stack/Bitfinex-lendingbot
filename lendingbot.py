import argparse
import configparser
import datetime
import hashlib
import json
import os
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import mimetypes
from dataclasses import dataclass
from decimal import Decimal, getcontext
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from bitfinex import Bitfinex, BitfinexApiError
from FileUtils import atomic_write_text
from Logger import Logger
from AppContext import AppContext
from ExchangeModels import parse_funding_stats, parse_funding_trades
from StateStore import LendingStateStore, StateStoreError
from RuntimeV3 import (
    LendingRuntimeV3,
    parse_book_v3,
    parse_credit_rows_v3,
    parse_offer_rows_v3,
    parse_wallet_rows_v3,
)
from MarketDataStream import BitfinexMarketDataHub, websocket_dependency_available
from StrategyV3 import (
    StrategyPolicyV3,
    build_market_signals_v3,
    gross_daily_floor,
    json_decimal,
    pool_for_period,
    policy_v3_to_json,
    policy_v3_with_overrides,
    replay_strategy_v3,
    validate_policy_v3,
)
from StrategyResearch import (
    backfill_public_market_data,
    evaluate_strategies,
    write_research_report,
)


getcontext().prec = 28

DEFAULT_CONFIG = "default.cfg"
DEFAULT_CONFIG_EXAMPLE = "default.cfg.example"
DEFAULT_DASHBOARD_JSON = os.path.join("www", "botlog.json")
DEFAULT_V3_STATE_DB = os.path.join(".state", "lendingbot-v3.sqlite3")
STATUS_SCHEMA_VERSION = 3
PREFLIGHT_TTL_SECONDS = 300
DASHBOARD_SERVICE_ID = "mika-lending-dashboard-v3"
DASHBOARD_BUILD_PLACEHOLDER = "__MIKA_DASHBOARD_BUILD_ID__"
DASHBOARD_CSRF_PLACEHOLDER = "__MIKA_DASHBOARD_CSRF_TOKEN__"
DASHBOARD_MAX_BODY_BYTES = 64 * 1024
DASHBOARD_STARTED_AT = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
DASHBOARD_BUILD_FILES = (
    "lendingbot.py",
    "bitfinex.py",
    "FileUtils.py",
    "Logger.py",
    "MarketDataStream.py",
    "AppContext.py",
    "DomainTypes.py",
    "ExchangeModels.py",
    "RuntimeV3.py",
    "StateStore.py",
    "StrategyV3.py",
    "StrategyResearch.py",
    "WriteRecovery.py",
    os.path.join("www", "lendingbot.html"),
    os.path.join("www", "lendingbot.js"),
    os.path.join("www", "v3-dashboard.js"),
    os.path.join("www", "lendingbot.css"),
)


def dashboard_build_id(root=None):
    root = os.path.abspath(root or os.path.dirname(__file__))
    digest = hashlib.sha256()
    for relative in sorted(DASHBOARD_BUILD_FILES):
        path = os.path.join(root, relative)
        digest.update(relative.replace("\\", "/").encode("utf-8"))
        try:
            with open(path, "rb") as file:
                digest.update(file.read())
        except OSError:
            digest.update(b"<missing>")
    return digest.hexdigest()[:20]


class LiveProcessLock:
    """Cross-process metadata lock held for a dashboard or LIVE worker."""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.handle = None

    @staticmethod
    def _lock(handle, blocking=False):
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            msvcrt.locking(handle.fileno(), mode, 1)
        else:
            import fcntl

            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), flags)

    @staticmethod
    def _unlock(handle):
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _read_metadata(handle):
        try:
            handle.seek(1)
            raw = handle.read().decode("utf-8").strip("\x00\r\n ")
            return json.loads(raw) if raw else {}
        except (OSError, UnicodeDecodeError, ValueError):
            return {}

    def acquire(self, config_path, metadata=None):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        handle = open(self.path, "a+b", buffering=0)
        if os.path.getsize(self.path) == 0:
            handle.write(b"\x00")
            handle.flush()
        try:
            self._lock(handle)
        except (OSError, BlockingIOError):
            handle.close()
            return False
        lock_metadata = {
            "pid": os.getpid(),
            "startedAt": timestamp(),
            "configPath": os.path.abspath(config_path),
            "projectRoot": os.path.abspath(os.path.dirname(__file__)),
            "executablePath": os.path.abspath(sys.executable),
            "buildId": dashboard_build_id(),
        }
        lock_metadata.update(metadata or {})
        handle.seek(1)
        handle.truncate(1)
        handle.write(json.dumps(lock_metadata, ensure_ascii=False).encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
        self.handle = handle
        return True

    def release(self):
        if self.handle is None:
            return
        try:
            self._unlock(self.handle)
        finally:
            self.handle.close()
            self.handle = None

    @classmethod
    def inspect(cls, path):
        absolute = os.path.abspath(path)
        if not os.path.exists(absolute):
            return {"locked": False, "metadata": {}}
        handle = open(absolute, "a+b", buffering=0)
        metadata = cls._read_metadata(handle)
        try:
            cls._lock(handle)
        except (OSError, BlockingIOError):
            handle.close()
            return {"locked": True, "metadata": metadata}
        try:
            return {"locked": False, "metadata": metadata}
        finally:
            cls._unlock(handle)
            handle.close()


class ConfigError(Exception):
    pass


class ApiRequestError(ConfigError):
    def __init__(self, message, code="REQUEST_REJECTED", status=400, details=None):
        super().__init__(message)
        self.code = code
        self.status = int(status)
        self.details = details


@dataclass
class Settings:
    api_key: str
    api_secret: str
    currencies: list[str]
    sleep_active: float
    sleep_inactive: float
    transferable_currencies: list[str]
    transfer_from_wallets: list[str]
    output_currency: str
    json_file: str
    json_log_size: int
    web_server: bool
    once: bool
    strategy_v3: StrategyPolicyV3
    state_db_file: str


def timestamp():
    ts = time.time()
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Bitfinex Lending Bot")
    parser.add_argument("-cfg", "--config", default=DEFAULT_CONFIG, help="configuration file path")
    parser.add_argument("--dashboard", action="store_true", help="start only the local web dashboard")
    parser.add_argument("--live", action="store_true", help="submit and cancel real Bitfinex funding offers")
    parser.add_argument("--confirmed-preflight", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument(
        "--migrate-legacy", action="store_true", help="offline one-time migration of legacy ownership state"
    )
    parser.add_argument(
        "--backfill-market-data", action="store_true", help="backfill at least 90 days of public USD funding data"
    )
    parser.add_argument(
        "--evaluate-strategy", action="store_true", help="run the offline chronological strategy evaluation"
    )
    parser.add_argument("--research-days", type=int, default=90, help="number of days used for backfill/evaluation")
    parser.add_argument("--principal", default="10000", help="offline evaluation principal in USD")
    parser.add_argument("-key", "--apikey", help="Bitfinex API key")
    parser.add_argument("-secret", "--apisecret", help="Bitfinex API secret")
    parser.add_argument("-sleepactive", "--sleeptimeactive", help="seconds between active iterations")
    parser.add_argument("-sleepinactive", "--sleeptimeinactive", help="seconds between inactive iterations")
    parser.add_argument("-json", "--json", "--jsonfile", dest="jsonfile", help="path to json status log")
    parser.add_argument(
        "-jsonsize", "--jsonsize", "--jsonlogsize", dest="jsonlogsize", help="number of json log lines to keep"
    )
    parser.add_argument(
        "-server",
        "--server",
        "--startwebserver",
        dest="startwebserver",
        action="store_true",
        help="serve ./www on 127.0.0.1:8000",
    )
    parser.add_argument("--no-server", action="store_true", help="disable config-driven web server startup")
    return parser.parse_args(argv)


def ensure_config_file(config_location):
    if os.path.exists(config_location):
        return False
    if config_location == DEFAULT_CONFIG and os.path.exists(DEFAULT_CONFIG_EXAMPLE):
        shutil.copy(DEFAULT_CONFIG_EXAMPLE, config_location)
        return True
    return False


def read_config(config_location):
    created = ensure_config_file(config_location)
    config = configparser.ConfigParser()
    config.read(config_location)
    return config, created


def get_option(config, section, option, fallback=None):
    if config.has_option(section, option):
        return config.get(section, option)
    return fallback


def get_decimal(config, section, option, fallback):
    return Decimal(str(get_option(config, section, option, fallback)))


def get_decimal_percent(config, section, option, fallback):
    return Decimal(str(get_option(config, section, option, fallback))) / Decimal("100")


def get_boolean(config, section, option, fallback):
    if config.has_option(section, option):
        return config.getboolean(section, option)
    return fallback


def split_csv(raw):
    if not raw:
        return []
    return [part.strip().upper() for part in raw.split(",") if part.strip()]


V3_PERCENT_FIELDS = {
    "short_floor_apr",
    "medium_floor_apr",
    "long_floor_apr",
    "amount_jitter",
    "normal_fee_rate",
    "hidden_fee_rate",
    "minimum_rate_change",
    "outlier_min_volume_share",
}
V3_BOOL_FIELDS = {
    "enable_limit",
    "enable_frr",
    "enable_frr_delta_fixed",
    "enable_frr_delta_variable",
    "enable_hidden",
}
V3_INT_FIELDS = {
    "target_slices",
    "minimum_offer_minutes",
    "reprice_cooldown_minutes",
    "max_reprices_per_hour",
    "ws_fallback_seconds",
    "rest_stale_seconds",
    "market_retention_days",
}
V3_LIST_FIELDS = {"short_periods", "medium_periods", "long_periods"}
V3_CONFIG_FIELDS = tuple(name for name in StrategyPolicyV3.__dataclass_fields__ if name not in {"version", "currency"})


def strategy_v3_from_config(config):
    section = "STRATEGY_V3"
    values = {}
    if config.has_section(section):
        for field_name in V3_CONFIG_FIELDS:
            raw = get_option(config, section, field_name, None)
            if raw is None:
                continue
            if (
                field_name
                in {
                    "short_floor_apr",
                    "medium_floor_apr",
                    "long_floor_apr",
                    "hidden_max_share",
                    "max_lend_amount",
                }
                and str(raw).strip() == ""
            ):
                values[field_name] = None
            elif field_name in V3_PERCENT_FIELDS:
                values[field_name] = Decimal(str(raw)) / Decimal("100")
            elif field_name in V3_BOOL_FIELDS:
                values[field_name] = str(raw).strip().lower() in {"1", "true", "yes", "on"}
            elif field_name in V3_INT_FIELDS:
                values[field_name] = int(raw)
            elif field_name in V3_LIST_FIELDS:
                values[field_name] = tuple(int(item.strip()) for item in str(raw).split(",") if item.strip())
            else:
                values[field_name] = Decimal(str(raw))
    try:
        return validate_policy_v3(policy_v3_with_overrides(StrategyPolicyV3(), values))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def strategy_v3_config_values(policy):
    values = {}
    for field_name in V3_CONFIG_FIELDS:
        value = getattr(policy, field_name)
        if value is None:
            values[field_name] = ""
        elif field_name in V3_PERCENT_FIELDS:
            values[field_name] = decimal_percent_to_config(value)
        elif isinstance(value, bool):
            values[field_name] = str(value).lower()
        elif isinstance(value, tuple):
            values[field_name] = ",".join(str(item) for item in value)
        elif isinstance(value, Decimal):
            values[field_name] = decimal_to_config(value)
        else:
            values[field_name] = str(value)
    return values


def strategy_v3_api_values(policy):
    payload = policy_v3_to_json(policy)
    for field_name in V3_PERCENT_FIELDS:
        value = getattr(policy, field_name)
        payload[field_name] = None if value is None else decimal_percent_to_config(value)
    payload["hidden_max_share"] = (
        None if policy.hidden_max_share is None else decimal_to_config(policy.hidden_max_share)
    )
    payload["gross_daily_floors_percent"] = {
        pool: None
        if policy.floor_apr(pool) is None
        else decimal_percent_to_config(Decimal(payload["gross_daily_floors"][pool]))
        for pool in ("short", "medium", "long")
    }
    return payload


def strategy_v3_from_api_payload(payload, base=None):
    values = {}
    payload = payload or {}
    for field_name in V3_CONFIG_FIELDS:
        if field_name not in payload:
            continue
        value = payload[field_name]
        if field_name in {
            "short_floor_apr",
            "medium_floor_apr",
            "long_floor_apr",
            "hidden_max_share",
            "max_lend_amount",
        } and value in (None, ""):
            values[field_name] = None
        elif field_name in V3_PERCENT_FIELDS:
            values[field_name] = Decimal(str(value)) / Decimal("100")
        elif field_name in V3_BOOL_FIELDS:
            values[field_name] = (
                value if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "on"}
            )
        elif field_name in V3_INT_FIELDS:
            values[field_name] = int(value)
        elif field_name in V3_LIST_FIELDS:
            raw = value if isinstance(value, (list, tuple)) else str(value).split(",")
            values[field_name] = tuple(int(item) for item in raw if str(item).strip())
        else:
            values[field_name] = Decimal(str(value))
    try:
        return validate_policy_v3(policy_v3_with_overrides(base or StrategyPolicyV3(), values))
    except (ValueError, ArithmeticError) as exc:
        raise ConfigError(str(exc)) from exc


def strategy_v3_from_record(record, require_live=False):
    if record is None:
        return None
    try:
        return validate_policy_v3(
            policy_v3_with_overrides(StrategyPolicyV3(), record["policy"]),
            require_live_floors=require_live,
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def strategy_v3_semantically_equal(left, right):
    return json_decimal(left.__dict__) == json_decimal(right.__dict__)


def ensure_active_strategy_v3(store, settings):
    active = store.strategy("ACTIVE")
    if active is None:
        store.save_strategy(json_decimal(settings.strategy_v3.__dict__), status="ACTIVE")
        active = store.strategy("ACTIVE")
    return active, strategy_v3_from_record(active)


def mirror_active_strategy_v3(config_path, policy):
    update_config_file_preserving_comments(
        config_path,
        {"STRATEGY_V3": strategy_v3_config_values(policy)},
    )


def backup_strategy_state(config_path, state_db_file):
    backup_dir = os.path.join(os.path.dirname(os.path.abspath(state_db_file)), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    suffix = datetime.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    config_backup = os.path.join(backup_dir, f"default-{suffix}.cfg")
    database_backup = os.path.join(backup_dir, f"lendingbot-v3-{suffix}.sqlite3")
    shutil.copy2(config_path, config_backup)
    source = sqlite3.connect(state_db_file)
    target = sqlite3.connect(database_backup)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return {"config": config_backup, "database": database_backup}


def normalize_current_active_strategy(config_path):
    config, _ = read_config(config_path)
    settings = build_settings(parse_args(["--config", config_path]), config)
    pre_migration_active = None
    if os.path.exists(settings.state_db_file):
        try:
            connection = sqlite3.connect(settings.state_db_file)
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM strategy_versions WHERE status='ACTIVE' ORDER BY activated_at_ms DESC LIMIT 1"
            ).fetchone()
            if row is not None:
                pre_migration_active = dict(row)
                pre_migration_active["policy"] = json.loads(pre_migration_active.pop("policy_json"))
        except (sqlite3.Error, ValueError):
            pre_migration_active = None
        finally:
            try:
                connection.close()
            except (NameError, sqlite3.Error):
                pass
    backup = None
    if pre_migration_active is not None:
        pre_policy = strategy_v3_from_record(pre_migration_active)
        pre_serialized = json.dumps(
            json_decimal(pre_policy.__dict__), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        pre_version = hashlib.sha256(pre_serialized.encode("utf-8")).hexdigest()[:16]
        if pre_migration_active["version_id"] != pre_version:
            backup = backup_strategy_state(config_path, settings.state_db_file)
    store = LendingStateStore(settings.state_db_file)
    active, policy = ensure_active_strategy_v3(store, settings)
    canonical = json_decimal(policy.__dict__)
    serialized = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    canonical_version = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
    if active["version_id"] == canonical_version:
        return {"changed": False, "versionId": canonical_version, "backup": None}
    if backup is None:
        backup = backup_strategy_state(config_path, settings.state_db_file)
    version_id = store.normalize_active_strategy(
        canonical,
        reason=f"normalized incomplete ACTIVE {active['version_id']} to full V3 schema",
    )
    mirror_active_strategy_v3(config_path, policy)
    return {"changed": True, "fromVersion": active["version_id"], "versionId": version_id, "backup": backup}


def build_settings(args, config, config_created=False):
    api_key = args.apikey or os.environ.get("BITFINEX_API_KEY") or get_option(config, "BITFINEX", "apikey", "")
    api_secret = args.apisecret or os.environ.get("BITFINEX_API_SECRET") or get_option(config, "BITFINEX", "secret", "")
    currencies = split_csv(get_option(config, "BITFINEX", "currencies", "USD"))

    return Settings(
        api_key=api_key,
        api_secret=api_secret,
        currencies=currencies,
        sleep_active=float(args.sleeptimeactive or get_option(config, "BOT", "sleeptimeactive", "60")),
        sleep_inactive=float(args.sleeptimeinactive or get_option(config, "BOT", "sleeptimeinactive", "300")),
        transferable_currencies=split_csv(get_option(config, "BOT", "transferablecurrencies", "")),
        transfer_from_wallets=split_csv(get_option(config, "BOT", "transferfromwallets", "exchange,margin")),
        output_currency="USD",
        json_file=args.jsonfile or get_option(config, "BOT", "jsonfile", ""),
        json_log_size=int(args.jsonlogsize or get_option(config, "BOT", "jsonlogsize", "-1")),
        web_server=(args.startwebserver or config.getboolean("BOT", "startwebserver", fallback=False))
        and not args.no_server,
        once=args.once,
        strategy_v3=strategy_v3_from_config(config),
        state_db_file=get_option(config, "BOT", "statedbfile", DEFAULT_V3_STATE_DB),
    )


def validate_settings(settings):
    if settings.sleep_active < 1 or settings.sleep_active > 3600:
        raise ConfigError("sleeptimeactive must be 1-3600")
    if settings.sleep_inactive < 1 or settings.sleep_inactive > 3600:
        raise ConfigError("sleeptimeinactive must be 1-3600")
    if settings.currencies != ["USD"]:
        raise ConfigError("strategy v3 supports exactly currencies = USD")
    if set(settings.transferable_currencies) - {"USD"}:
        raise ConfigError("transferablecurrencies can only contain USD")
    try:
        validate_policy_v3(settings.strategy_v3)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def decimal_percent_to_config(value):
    text = format(Decimal(value) * Decimal("100"), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def decimal_to_config(value):
    text = format(Decimal(value), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def status_decimal(value):
    return format(Decimal(value).quantize(Decimal("0.00000001")), "f")


def config_api_payload(config_path):
    config, _ = read_config(config_path)
    args = parse_args(["--config", config_path])
    settings = build_settings(args, config)
    validate_settings(settings)
    client = Bitfinex(settings.api_key, settings.api_secret)
    store = LendingStateStore(settings.state_db_file)
    active, active_policy = ensure_active_strategy_v3(store, settings)
    return {
        "configPath": os.path.abspath(config_path),
        "credentialsConfigured": client.has_credentials(),
        "bitfinex": {
            "currencies": ",".join(settings.currencies),
        },
        "bot": {
            "sleeptimeactive": str(
                int(settings.sleep_active) if settings.sleep_active.is_integer() else settings.sleep_active
            ),
            "sleeptimeinactive": str(
                int(settings.sleep_inactive) if settings.sleep_inactive.is_integer() else settings.sleep_inactive
            ),
            "transferablecurrencies": ",".join(settings.transferable_currencies),
            "transferfromwallets": ",".join(settings.transfer_from_wallets).lower(),
            "outputcurrency": settings.output_currency,
            "jsonfile": settings.json_file or DEFAULT_DASHBOARD_JSON.replace("\\", "/"),
            "jsonlogsize": str(settings.json_log_size if settings.json_log_size != -1 else 200),
            "startwebserver": str(settings.web_server).lower(),
        },
        "strategyV3": strategy_v3_api_values(active_policy),
        "strategyV3Draft": None
        if store.strategy("DRAFT") is None
        else strategy_v3_api_values(strategy_v3_from_record(store.strategy("DRAFT"))),
        "strategyV3Pending": None
        if store.strategy("PENDING") is None
        else strategy_v3_api_values(strategy_v3_from_record(store.strategy("PENDING"))),
        "strategyV3State": {
            "active": active,
            "draft": store.strategy("DRAFT"),
            "pending": store.strategy("PENDING"),
        },
        "runtimeV3": {
            "defaultMode": "PAUSED",
            "stateDatabase": os.path.abspath(settings.state_db_file),
            "supportedCurrencies": ["USD"],
        },
    }


def update_config_file_preserving_comments(config_path, updates):
    if not os.path.exists(config_path):
        ensure_config_file(config_path)
    with open(config_path, "r", encoding="utf-8") as file:
        lines = file.readlines()

    pending = {section: dict(values) for section, values in updates.items() if values}
    output = []
    current_section = None

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if current_section in pending and pending[current_section]:
                for key, value in pending[current_section].items():
                    output.append(f"{key} = {value}\n")
                pending[current_section].clear()
            current_section = stripped[1:-1].upper()
            output.append(line)
            continue

        handled = False
        if current_section in pending and "=" in line and not stripped.startswith("#"):
            key_part = line.split("=", 1)[0].strip().lower()
            section_updates = pending.get(current_section, {})
            if key_part in section_updates:
                output.append(f"{key_part} = {section_updates.pop(key_part)}\n")
                handled = True
        if not handled:
            output.append(line)

    if current_section in pending and pending[current_section]:
        for key, value in pending[current_section].items():
            output.append(f"{key} = {value}\n")
        pending[current_section].clear()

    for section, values in pending.items():
        if values:
            output.append(f"\n[{section}]\n")
            for key, value in values.items():
                output.append(f"{key} = {value}\n")

    atomic_write_text(config_path, "".join(output))


def strategy_v3_version_id(policy):
    serialized = json.dumps(json_decimal(policy.__dict__), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def load_v3_account_context(client, store, now_ms):
    """Read the live account shape without changing exchange or local state."""
    basis = {"source": "REAL_ACCOUNT", "timestamp": int(now_ms), "stale": False, "warnings": []}
    try:
        wallets = parse_wallet_rows_v3(client.wallets())
        try:
            offers = parse_offer_rows_v3(client.active_funding_offers("fUSD"))
        except AttributeError:
            offers = []
        try:
            credits = parse_credit_rows_v3(client.active_funding_credits("fUSD"))
        except AttributeError:
            credits = []
    except Exception as exc:
        basis.update({"source": "HISTORICAL_SNAPSHOT", "stale": True})
        basis["warnings"].append(f"实时账户快照不可用，使用最近已保存快照：{exc}")
        with store.read_connection() as connection:
            sample = connection.execute("SELECT * FROM account_samples ORDER BY mts DESC LIMIT 1").fetchone()
        offers = [dict(row, id=row["offer_id"]) for row in store.offers(active_only=True)]
        credits = [dict(row, id=row["credit_id"]) for row in store.credits(active_only=True)]
        if sample is None:
            wallets = []
            basis["timestamp"] = None
        else:
            wallets = [
                {
                    "wallet_type": "funding",
                    "currency": "USD",
                    "balance": Decimal(sample["wallet_available"]),
                    "available": Decimal(sample["wallet_available"]),
                    "unsettled_interest": Decimal("0"),
                }
            ]
            basis["timestamp"] = int(sample["mts"])

    managed_offers = {int(row["offer_id"]): row for row in store.offers() if row["managed"]}
    for offer in offers:
        managed = managed_offers.get(int(offer["id"]))
        offer["managed"] = managed is not None
        offer["pool"] = (managed or {}).get("pool") or pool_for_period(offer["period"])
        offer["layer"] = (managed or {}).get("layer")
        offer["display_type"] = (managed or {}).get("display_type") or v3_offer_display_type(offer)
    stored_credits = {int(row["credit_id"]): row for row in store.credits()}
    for credit in credits:
        stored = stored_credits.get(int(credit["id"]))
        credit["managed"] = bool(stored and stored["managed"])
        credit["pool"] = (stored or {}).get("pool") or pool_for_period(credit["period"])
        credit["layer"] = (stored or {}).get("layer")
        credit["display_type"] = v3_credit_display_type({**credit, **(stored or {})})
    snapshot = {"wallets": wallets, "offers": offers, "credits": credits}
    return LendingRuntimeV3._account(snapshot), snapshot, basis


def load_v3_market_context(client, policy, now_ms):
    warnings = []
    try:
        book = parse_book_v3(client.funding_book("fUSD", 250))
    except Exception as exc:
        book = []
        warnings.append(f"Funding Book 不可用：{exc}")
    try:
        trades = parse_funding_trades(
            client.funding_trades("fUSD", start=now_ms - 7 * 86_400_000, end=now_ms, limit=10000, sort=1)
        )
    except Exception as exc:
        trades = []
        warnings.append(f"Funding Trades 不可用：{exc}")
    try:
        stats = parse_funding_stats(client.funding_stats("fUSD", start=now_ms - 7 * 86_400_000, end=now_ms, limit=250))
    except Exception as exc:
        stats = []
        warnings.append(f"Funding Stats 不可用：{exc}")
    return book, trades, stats, build_market_signals_v3(book, trades, stats, policy, now_ms), warnings


strategy_preview_tokens = {}
strategy_apply_tokens = {}
strategy_token_lock = threading.RLock()


def _canonical_sha256(payload):
    serialized = json.dumps(json_decimal(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _account_context_digest(account, snapshot):
    def compact(rows, fields):
        return [
            {field: json_decimal(row.get(field)) for field in fields}
            for row in sorted(
                rows, key=lambda item: int(item.get("id") or item.get("offer_id") or item.get("credit_id") or 0)
            )
        ]

    return _canonical_sha256(
        {
            "account": account,
            "offers": compact(
                snapshot.get("offers", []),
                ("id", "amount", "rate", "rate_real", "period", "display_type", "flags", "managed"),
            ),
            "credits": compact(
                snapshot.get("credits", []),
                ("id", "amount", "rate", "rate_real", "period", "display_type", "hidden", "managed"),
            ),
        }
    )


def _token_expiry_iso(expires_at):
    return datetime.datetime.fromtimestamp(expires_at, datetime.timezone.utc).isoformat(timespec="seconds")


def strategy_v3_preview(
    config_path,
    payload,
    client_factory=None,
    now_ms=None,
    issue_token=True,
    app_context=None,
):
    app_context = process_context(config_path, app_context, client_factory=client_factory)
    client_factory = client_factory or app_context.client_factory or Bitfinex
    store, settings = v3_store_for_config(config_path)
    active, active_policy = ensure_active_strategy_v3(store, settings)
    policy = strategy_v3_from_api_payload(payload.get("strategyV3", {}), base=active_policy)
    proposed_version = (
        active["version_id"]
        if strategy_v3_semantically_equal(policy, active_policy)
        else strategy_v3_version_id(policy)
    )
    now = int(now_ms if now_ms is not None else app_context.now() * 1000)
    client = client_factory(settings.api_key, settings.api_secret)
    account, account_snapshot, basis = load_v3_account_context(client, store, now)
    book, trades, stats, signals, warnings = load_v3_market_context(client, policy, now)
    warnings = [*basis["warnings"], *warnings]
    result = LendingRuntimeV3._build_plan(account, policy, signals, proposed_version)
    if not policy_v3_to_json(policy)["floorsConfigured"]:
        warnings.append("三个最低净年化尚未全部填写；允许预览，但 LIVE 将被阻止。")
    proposed_record = {"version_id": proposed_version, "policy": json_decimal(policy.__dict__)}
    incompatible = []
    for offer in account_snapshot["offers"]:
        violations = v3_offer_violations(offer, policy)
        if violations:
            incompatible.append({**json_decimal(offer), "violations": violations})
    non_changeable_credits = []
    for credit in account_snapshot["credits"]:
        violations = v3_credit_violations(credit, policy)
        if violations:
            non_changeable_credits.append({**json_decimal(credit), "violations": violations})
    account_digest = _account_context_digest(account, account_snapshot)
    response = {
        "currency": "USD",
        "principal": status_decimal(account["total"]),
        "available": status_decimal(account["wallet"]),
        "accountSnapshot": basis,
        "activeVersion": active["version_id"],
        "proposedVersion": proposed_version,
        "strategyDiff": v3_strategy_diff(active, proposed_record),
        "policy": strategy_v3_api_values(policy),
        "signals": json_decimal(signals),
        "plan": json_decimal(result),
        "fundingLimit": {
            "amount": None if policy.max_lend_amount is None else status_decimal(policy.max_lend_amount),
            "percent": decimal_to_config(policy.max_lend_percent),
            "effectiveCap": status_decimal(result["funding_cap"]),
            "existingExposure": status_decimal(result["existing_exposure"]),
        },
        "incompatibleOffers": incompatible,
        "nonChangeableCredits": non_changeable_credits,
        "replay": replay_strategy_v3(policy, trades, stats, account["total"], book, now),
        "warnings": list(dict.fromkeys(warnings)),
        "accountDigest": account_digest,
        "buildId": dashboard_build_id(),
    }
    if issue_token:
        issued_at = app_context.now()
        token = secrets.token_urlsafe(24)
        context = {
            "token": token,
            "expiresAt": issued_at + PREFLIGHT_TTL_SECONDS,
            "buildId": dashboard_build_id(),
            "configPath": os.path.abspath(config_path),
            "activeVersion": active["version_id"],
            "proposedVersion": proposed_version,
            "policyHash": _canonical_sha256(policy.__dict__),
            "accountDigest": account_digest,
            "planHash": result["plan_hash"],
        }
        with strategy_token_lock:
            strategy_preview_tokens[token] = context
        response["previewToken"] = token
        response["expiresAt"] = _token_expiry_iso(context["expiresAt"])
    return response


def v3_store_for_config(config_path):
    config, _ = read_config(config_path)
    settings = build_settings(parse_args(["--config", config_path]), config)
    return LendingStateStore(settings.state_db_file), settings


def v3_strategy_diff(left, right):
    if left is None or right is None:
        return []
    left_policy = left.get("policy", {})
    right_policy = right.get("policy", {})
    return [
        {"field": key, "from": left_policy.get(key), "to": right_policy.get(key)}
        for key in sorted(set(left_policy) | set(right_policy))
        if left_policy.get(key) != right_policy.get(key)
    ]


def v3_offer_display_type(offer):
    return LendingRuntimeV3._offer_display_type(offer)


def v3_credit_display_type(credit):
    display = str(credit.get("display_type") or "").upper()
    if display:
        return display
    rate_type = str(credit.get("rate_type") or "").upper()
    if rate_type in {"FIXED", "LIMIT"}:
        return "LIMIT"
    if rate_type in {"VAR", "VARIABLE", "FRRDELTAVAR"}:
        return "VARIABLE_UNKNOWN"
    return rate_type or "UNKNOWN"


def v3_credit_violations(credit, policy):
    display_type = v3_credit_display_type(credit)
    pseudo_offer = {
        **credit,
        "offer_type": display_type,
        "display_type": display_type,
        "flags": 64 if credit.get("hidden") else 0,
    }
    violations = v3_offer_violations(pseudo_offer, policy)
    if display_type == "VARIABLE_UNKNOWN":
        violations = [item for item in violations if item != "disabled_type"]
    return violations


def v3_offer_violations(offer, policy):
    pool = offer.get("pool") or pool_for_period(int(offer.get("period") or 0))
    display_type = v3_offer_display_type(offer)
    hidden = bool(int(offer.get("flags") or 0) & 64) or bool(offer.get("hidden"))
    violations = []
    if not LendingRuntimeV3._display_type_enabled(policy, display_type):
        violations.append("disabled_type")
    if hidden and not policy.enable_hidden:
        violations.append("hidden_disabled")
    if pool not in {"short", "medium", "long"} or int(offer.get("period") or 0) not in policy.periods(pool):
        violations.append("period_not_allowed")
    floor_apr = policy.floor_apr(pool) if pool in {"short", "medium", "long"} else None
    if floor_apr is not None:
        fee = policy.hidden_fee_rate if hidden else policy.normal_fee_rate
        effective = Decimal(str(offer.get("rate_real") or offer.get("rate") or 0))
        if effective < gross_daily_floor(floor_apr, fee):
            violations.append("below_new_floor")
    return violations


def runtime_v3_payload(config_path, context=None):
    context = process_context(config_path, context)
    store, settings = v3_store_for_config(config_path)
    market_snapshot = None
    if context.process_state.market_hub is not None:
        market_snapshot = json_decimal(context.process_state.market_hub.snapshot())
    runtime = store.runtime()
    active, active_policy = ensure_active_strategy_v3(store, settings)
    draft = store.strategy("DRAFT")
    pending = store.strategy("PENDING")
    proposed = pending or draft or active
    proposed_policy = strategy_v3_from_record(proposed) if proposed else active_policy
    incompatible = []
    for offer in store.offers(active_only=True):
        violations = v3_offer_violations(offer, proposed_policy)
        if violations:
            incompatible.append({**offer, "display_type": v3_offer_display_type(offer), "violations": violations})
    return {
        "runtime": runtime,
        "displayMode": "APPLYING" if runtime["mode"] == "LIVE" and pending is not None else runtime["mode"],
        "process": controlled_bot_status(config_path, context),
        "activeStrategy": active,
        "draftStrategy": draft,
        "pendingStrategy": pending,
        "effectiveStrategy": active,
        "policy": strategy_v3_api_values(active_policy),
        "strategyDiff": v3_strategy_diff(active, proposed),
        "incompatibleOffers": incompatible,
        "marketSnapshot": market_snapshot,
        "realizedIncome": store.realized_income_summary("USD"),
        "incomeHistorySync": store.income_history_sync_payload("USD"),
    }


def stats_v3_payload(store):
    return {
        "statistics": {
            "1d": store.statistics(1),
            "7d": store.statistics(7),
            "30d": store.statistics(30),
            "90d": store.statistics(90),
            "all": store.statistics(None),
        },
        "realizedIncome": store.realized_income_summary("USD"),
        "incomeHistorySync": store.income_history_sync_payload("USD"),
    }


def save_strategy_v3_draft(config_path, payload, app_context=None):
    now = app_context.now if app_context is not None else time.time
    store, settings = v3_store_for_config(config_path)
    active, active_policy = ensure_active_strategy_v3(store, settings)
    policy = strategy_v3_from_api_payload(payload.get("strategyV3", {}), base=active_policy)
    token = str(payload.get("previewToken") or "")
    with strategy_token_lock:
        context = strategy_preview_tokens.pop(token, None)
    if not context or now() > context["expiresAt"]:
        raise ApiRequestError("策略预览已过期，请重新计算", "PREVIEW_STALE", 409)
    if context["buildId"] != dashboard_build_id() or context["configPath"] != os.path.abspath(config_path):
        raise ApiRequestError("Dashboard build 或配置路径已变化，请重新计算", "PREVIEW_STALE", 409)
    if active["version_id"] != context["activeVersion"] or _canonical_sha256(policy.__dict__) != context["policyHash"]:
        raise ApiRequestError("ACTIVE 或拟议策略已变化，请重新计算", "PREVIEW_STALE", 409)
    if strategy_v3_semantically_equal(policy, active_policy):
        return {
            "versionId": active["version_id"],
            "draftVersionId": active["version_id"],
            "status": "UNCHANGED",
            "strategy": active,
            "diff": [],
        }
    version_id = store.save_strategy(json_decimal(policy.__dict__), status="DRAFT")
    if version_id == active["version_id"]:
        return {
            "versionId": version_id,
            "draftVersionId": version_id,
            "status": "UNCHANGED",
            "strategy": active,
            "diff": [],
        }
    draft = store.strategy("DRAFT")
    apply_token = secrets.token_urlsafe(24)
    apply_context = {
        **context,
        "token": apply_token,
        "draftVersionId": version_id,
        "expiresAt": now() + PREFLIGHT_TTL_SECONDS,
    }
    with strategy_token_lock:
        strategy_apply_tokens[apply_token] = apply_context
    return {
        "versionId": version_id,
        "draftVersionId": version_id,
        "applyToken": apply_token,
        "expiresAt": _token_expiry_iso(apply_context["expiresAt"]),
        "status": "DRAFT",
        "strategy": draft,
        "diff": v3_strategy_diff(active, draft),
    }


def apply_strategy_v3_draft(config_path, payload, client_factory=None, app_context=None):
    app_context = process_context(config_path, app_context, client_factory=client_factory)
    client_factory = client_factory or app_context.client_factory or Bitfinex
    store, settings = v3_store_for_config(config_path)
    active, _ = ensure_active_strategy_v3(store, settings)
    draft = store.strategy("DRAFT")
    if draft is None:
        return {"status": "UNCHANGED", "strategy": active, "versionId": active["version_id"]}
    draft_version = str(payload.get("draftVersionId") or "")
    token = str(payload.get("applyToken") or "")
    with strategy_token_lock:
        context = strategy_apply_tokens.pop(token, None)
    if not context or app_context.now() > context["expiresAt"]:
        raise ApiRequestError("应用确认已过期，请重新预览并保存草稿", "PREVIEW_STALE", 409)
    if context["buildId"] != dashboard_build_id() or context["activeVersion"] != active["version_id"]:
        raise ApiRequestError("Dashboard build 或 ACTIVE 已变化，请重新预览", "PREVIEW_STALE", 409)
    if draft_version != draft["version_id"] or context["draftVersionId"] != draft["version_id"]:
        raise ApiRequestError("待应用草稿与已确认版本不一致", "PREVIEW_STALE", 409)
    draft_policy = strategy_v3_from_record(draft)
    refreshed = strategy_v3_preview(
        config_path,
        {"strategyV3": strategy_v3_api_values(draft_policy)},
        client_factory=client_factory,
        issue_token=False,
        app_context=app_context,
    )
    if refreshed["accountDigest"] != context["accountDigest"] or refreshed["plan"]["plan_hash"] != context["planHash"]:
        raise ApiRequestError(
            "账户或计划已变化，请检查新预览后再次确认",
            "PREVIEW_STALE",
            409,
            details={"preview": refreshed},
        )
    if controlled_bot_running(config_path, app_context) or store.runtime()["mode"] == "LIVE":
        strategy = store.promote_draft_to_pending()
        return {"status": "PENDING", "strategy": strategy}
    store.promote_draft_to_pending()
    strategy = store.activate_pending_strategy(reason="activated while live process stopped")
    mirror_active_strategy_v3(config_path, strategy_v3_from_record(strategy))
    return {"status": "ACTIVE", "strategy": strategy}


def discard_strategy_v3_draft(config_path):
    store, _ = v3_store_for_config(config_path)
    store.discard_strategy("DRAFT")
    return {"status": "DISCARDED", "activeStrategy": store.strategy("ACTIVE")}


def replay_v3_from_store(config_path, now_ms=None, context=None):
    context = process_context(config_path, context)
    store, settings = v3_store_for_config(config_path)
    if store.runtime()["mode"] == "LIVE" or controlled_bot_running(config_path, context):
        raise ConfigError("必须先暂停 LIVE 才能进入 REPLAY")
    store.set_mode("REPLAY", "dashboard_replay")
    now = int(now_ms if now_ms is not None else context.now() * 1000)
    trades = store.market_trades(now - 7 * 86_400_000, now)
    with store.read_connection() as connection:
        latest = connection.execute("SELECT total_principal FROM account_samples ORDER BY mts DESC LIMIT 1").fetchone()
    principal = Decimal(latest["total_principal"]) if latest else Decimal("10000")
    _, active_policy = ensure_active_strategy_v3(store, settings)
    return replay_strategy_v3(active_policy, trades, [], principal, [], now)


def empty_status_payload():
    return {
        "schemaVersion": STATUS_SCHEMA_VERSION,
        "operationMode": "PAUSED",
        "last_status": "当前已暂停；进入 LIVE 前必须完成预检并人工确认。",
        "last_update": "",
        "log": [],
        "outputCurrency": {"currency": "USD", "highestBid": "1"},
        "platformFeeRate": "15",
        "raw_data": {},
        "openOffers": [],
        "strategyDecision": {},
        "legacyIgnored": False,
    }


def read_status_payload(status_path):
    if not os.path.exists(status_path):
        return empty_status_payload()
    try:
        with open(status_path, "r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, ValueError):
        payload = None
    valid_modes = {"live", "LIVE", "PAUSED", "REPLAY", "SAFE", "APPLYING"}
    if (
        not isinstance(payload, dict)
        or payload.get("schemaVersion") != STATUS_SCHEMA_VERSION
        or payload.get("operationMode") not in valid_modes
    ):
        ignored = empty_status_payload()
        ignored["last_status"] = "旧版状态已忽略，等待实盘机器人首次同步。"
        ignored["legacyIgnored"] = True
        return ignored
    payload.setdefault("openOffers", [])
    payload.setdefault("raw_data", {})
    payload.setdefault("log", [])
    payload.setdefault("strategyDecision", {})
    payload["legacyIgnored"] = False
    return payload


def dashboard_status_payload(status_path, config_path, context=None):
    payload = read_status_payload(status_path)
    try:
        store, _ = v3_store_for_config(config_path)
        runtime = store.runtime()
        process = controlled_bot_status(config_path, context)
        if not process["running"]:
            empty = empty_status_payload()
            empty["runtime"] = runtime
            empty["operationMode"] = "PAUSED" if runtime["mode"] != "SAFE" else "SAFE"
            empty["last_status"] = "机器人进程已停止；账户与收益实时数据已清空。"
            empty["snapshotAvailable"] = False
            empty["process"] = process
            return empty
        payload["runtime"] = runtime
        payload["process"] = process
        payload["operationMode"] = runtime["mode"]
        payload["snapshotAvailable"] = True
        if runtime["mode"] == "PAUSED":
            payload["last_status"] = "策略已暂停；实盘进程仍在运行。"
        elif runtime["mode"] == "SAFE":
            payload["last_status"] = f"SAFE：{runtime.get('safe_reason') or '策略已安全暂停'}"
        payload.update(stats_v3_payload(store))
    except Exception:
        pass
    return payload


def process_context(config_path, context=None, client_factory=None, now=None):
    if context is not None:
        return context
    return AppContext.for_project(
        os.path.abspath(os.path.dirname(__file__)),
        config_path=config_path,
        client_factory=client_factory or Bitfinex,
        now=now or time.time,
    )


def reconcile_orphaned_live_runtime(config_path, context=None):
    context = process_context(config_path, context)
    store, _ = v3_store_for_config(config_path)
    external = external_live_process(config_path, context)
    if external and external.get("buildMismatch"):
        if external.get("stateError"):
            store.enter_safe("WORKER_BUILD_MISMATCH_UNVERIFIED", manual=True)
            return store.runtime()
        identity_error = _live_process_identity_error(external)
        if identity_error:
            store.enter_safe("WORKER_BUILD_MISMATCH_UNVERIFIED", manual=True)
            return store.runtime()
        stop_controlled_bot(config_path, reason="worker_build_mismatch", context=context)
        store.set_mode("PAUSED", "worker_build_mismatch_stopped")
        return store.runtime()
    runtime = store.runtime()
    if runtime["mode"] == "LIVE" and not controlled_bot_running(config_path, context):
        return store.set_mode("PAUSED", "dashboard_started_without_live_process")
    return runtime


def cleanup_controlled_bot_handle(context):
    state = context.process_state
    if state.log_handle is not None:
        try:
            state.log_handle.close()
        except Exception:
            pass
        state.log_handle = None


def _pid_is_alive(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not process:
                return False
            try:
                exit_code = wintypes.DWORD()
                if not ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == 259
            finally:
                ctypes.windll.kernel32.CloseHandle(process)
        os.kill(pid, 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _process_identity(pid):
    try:
        pid = int(pid)
        if os.name == "nt":
            command = (
                '$p=Get-CimInstance Win32_Process -Filter "ProcessId=%d"; '
                "if($p){[pscustomobject]@{ExecutablePath=$p.ExecutablePath;"
                "CommandLine=$p.CommandLine}|ConvertTo-Json -Compress}"
            ) % pid
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return json.loads(result.stdout.strip()) if result.stdout.strip() else {}
        command_line = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\x00", b" ").decode("utf-8", "replace")
        executable = os.path.realpath(f"/proc/{pid}/exe")
        return {"ExecutablePath": executable, "CommandLine": command_line}
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}


def _live_process_identity_error(metadata):
    pid = metadata.get("pid")
    identity = _process_identity(pid) if pid else {}
    command_line = str(identity.get("CommandLine") or "")
    executable = str(identity.get("ExecutablePath") or "")
    if not command_line or "lendingbot.py" not in command_line.lower() or "--live" not in command_line.lower():
        return "实盘锁 PID 的命令行无法验证为 lendingbot.py --live"
    recorded_executable = str(metadata.get("executablePath") or "")
    if (
        recorded_executable
        and executable
        and os.path.normcase(os.path.abspath(recorded_executable)) != os.path.normcase(os.path.abspath(executable))
    ):
        return "实盘锁记录的 Python 可执行文件与实际进程不一致"
    return None


def external_live_process(config_path=DEFAULT_CONFIG, context=None):
    context = process_context(config_path, context)
    inspection = LiveProcessLock.inspect(context.live_lock_path)
    if not inspection["locked"]:
        return None
    metadata = inspection["metadata"]
    state_error = None
    if not metadata.get("pid"):
        state_error = "实盘锁已占用，但缺少 PID 元数据"
    elif not _pid_is_alive(metadata["pid"]):
        state_error = "实盘锁已占用，但记录的 PID 无法验证"
    expected_config = os.path.abspath(config_path)
    if metadata.get("configPath") and os.path.abspath(metadata["configPath"]) != expected_config:
        state_error = f"实盘进程使用其他配置：{metadata['configPath']}"
    current_build = dashboard_build_id()
    worker_build = metadata.get("buildId")
    return {
        **metadata,
        "stateError": state_error,
        "workerBuildId": worker_build,
        "currentBuildId": current_build,
        "buildMismatch": worker_build != current_build,
    }


def controlled_bot_running(config_path=DEFAULT_CONFIG, context=None):
    context = process_context(config_path, context)
    process = context.process_state.process
    internal = process is not None and process.poll() is None
    return internal or external_live_process(config_path, context) is not None


def controlled_bot_status(config_path=DEFAULT_CONFIG, context=None):
    context = process_context(config_path, context)
    state = context.process_state
    with state.lock:
        process = state.process
        internal_running = process is not None and process.poll() is None
        external = None if internal_running else external_live_process(config_path, context)
        running = internal_running or external is not None
        return_code = None
        if process is not None:
            return_code = process.poll()
        if process is not None and return_code is not None:
            cleanup_controlled_bot_handle(context)
        return {
            "running": running,
            "pid": process.pid if internal_running else (external or {}).get("pid"),
            "startedAt": state.started_at if internal_running else (external or {}).get("startedAt"),
            "returnCode": return_code,
            "stopReason": state.stop_reason,
            "managedExternally": bool(external),
            "dashboardBuildId": dashboard_build_id(),
            "workerBuildId": dashboard_build_id() if internal_running else (external or {}).get("workerBuildId"),
            "buildMismatch": bool(external and external.get("buildMismatch")),
            **({"stateError": external["stateError"]} if external and external.get("stateError") else {}),
        }


def config_sha256(config_path):
    digest = hashlib.sha256()
    with open(config_path, "rb") as file:
        for chunk in iter(lambda: file.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_key_permissions(rows):
    permissions = {}
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        permissions[str(row[0]).lower()] = {
            "read": str(row[1]).strip().lower() in {"1", "true"},
            "write": str(row[2]).strip().lower() in {"1", "true"},
        }
    return permissions


def permission_enabled(permissions, scope, access):
    return bool(permissions.get(scope, {}).get(access, False))


def evaluate_live_preflight(config_path, client_factory=None, context=None):
    """Perform the single V3 preflight used by both dashboard and live child."""
    context = process_context(config_path, context, client_factory=client_factory)
    client_factory = client_factory or context.client_factory or Bitfinex
    checks = []
    warnings = []

    def add_check(check_id, label, passed, detail):
        checks.append({"id": check_id, "label": label, "status": "pass" if passed else "fail", "detail": detail})

    try:
        config, _ = read_config(config_path)
        settings = build_settings(parse_args(["--config", config_path, "--live", "--no-server"]), config)
        store = LendingStateStore(settings.state_db_file)
        active, policy = ensure_active_strategy_v3(store, settings)
        validate_policy_v3(policy, require_live_floors=True)
        add_check("config", "V3 策略配置", True, f"SQLite ACTIVE {active['version_id']} 有效")
    except Exception as exc:
        add_check("config", "V3 策略配置", False, str(exc))
        return {"checks": checks, "warnings": warnings, "summary": {"strategyVersion": 3}}

    draft = store.strategy("DRAFT")
    pending = store.strategy("PENDING")
    clean_state = draft is None and pending is None
    add_check(
        "strategy_state",
        "策略版本状态",
        clean_state,
        "没有未应用草稿或待切换策略" if clean_state else "存在 DRAFT 或 PENDING；请先应用或放弃后再启动",
    )
    if not strategy_v3_semantically_equal(settings.strategy_v3, policy):
        warnings.append(
            {
                "code": "CONFIG_MIRROR_DIFFERS",
                "message": "配置文件中的 V3 镜像与 SQLite ACTIVE 不一致；本次预检和实盘只使用 ACTIVE。",
            }
        )
    add_check("v3_usd_only", "V3 币种范围", settings.currencies == ["USD"], "V3 仅允许 currencies = USD")
    add_check(
        "v3_websocket_dependency",
        "WebSocket 运行库",
        websocket_dependency_available(),
        "websockets 已安装" if websocket_dependency_available() else "请先安装 websockets 依赖",
    )

    client = client_factory(settings.api_key, settings.api_secret)
    if not client.has_credentials():
        add_check("credentials", "API 凭据", False, "未配置有效的 Bitfinex API key/secret")
        return {
            "checks": checks,
            "warnings": warnings,
            "summary": {"strategyVersion": 3, "activeStrategyVersion": active["version_id"]},
        }
    try:
        permissions = parse_key_permissions(client.key_permissions())
        add_check("credentials", "API 凭据", True, "凭据有效，权限接口可访问")
    except Exception as exc:
        add_check("credentials", "API 凭据", False, f"权限读取失败：{exc}")
        return {
            "checks": checks,
            "warnings": warnings,
            "summary": {"strategyVersion": 3, "activeStrategyVersion": active["version_id"]},
        }

    wallets_read = permission_enabled(permissions, "wallets", "read")
    funding_read = permission_enabled(permissions, "funding", "read")
    funding_write = permission_enabled(permissions, "funding", "write")
    add_check("wallets_read", "钱包读取权限", wallets_read, "wallets 需要读取权限")
    add_check("funding_read", "放贷读取权限", funding_read, "funding 需要读取权限")
    add_check("funding_write", "放贷写入权限", funding_write, "funding 需要写入权限以挂单和撤单")
    if settings.transferable_currencies:
        add_check(
            "wallets_write",
            "钱包转账权限",
            permission_enabled(permissions, "wallets", "write"),
            "启用自动转入时 wallets 需要写权限",
        )
    add_check(
        "withdraw_disabled",
        "API 提现权限",
        not permission_enabled(permissions, "withdraw", "write"),
        "withdraw 写权限必须关闭",
    )
    add_check(
        "ui_withdraw_disabled",
        "界面提现权限",
        not permission_enabled(permissions, "ui_withdraw", "write"),
        "ui_withdraw 写权限必须关闭",
    )

    now = int(context.now() * 1000)
    account, snapshot, basis = load_v3_account_context(client, store, now)
    add_check(
        "account_snapshot",
        "真实账户快照",
        basis["source"] == "REAL_ACCOUNT",
        "已读取实时 Funding 钱包、挂单和贷款"
        if basis["source"] == "REAL_ACCOUNT"
        else "实时账户读取失败，不能使用历史快照启动实盘",
    )
    for message in basis["warnings"]:
        warnings.append({"code": "ACCOUNT_SNAPSHOT_WARNING", "message": message})
    book, trades, stats, signals, market_warnings = load_v3_market_context(client, policy, now)
    add_check("book_usd", "USD Funding 市场", bool(book), "Funding Book 可用" if book else "Funding Book 没有可用报价")
    for message in market_warnings:
        warnings.append({"code": "MARKET_DATA_WARNING", "message": message})

    result = LendingRuntimeV3._build_plan(account, policy, signals, active["version_id"])
    incompatible_managed = []
    incompatible_external = []
    for offer in snapshot["offers"]:
        violations = v3_offer_violations(offer, policy)
        if not violations:
            continue
        item = {**json_decimal(offer), "display_type": v3_offer_display_type(offer), "violations": violations}
        (incompatible_managed if offer.get("managed") else incompatible_external).append(item)
    incompatible_credits = []
    for credit in snapshot["credits"]:
        violations = v3_credit_violations(credit, policy)
        if violations:
            incompatible_credits.append({**json_decimal(credit), "violations": violations})

    if account["wallet"] <= 0:
        warnings.append(
            {"code": "NO_AVAILABLE_BALANCE", "message": "USD 当前没有可用资金；仍可先撤销不兼容的机器人挂单。"}
        )
    elif account["wallet"] < policy.min_order_amount:
        warnings.append({"code": "BELOW_MINIMUM", "message": "USD 可用余额低于 V3 最低单笔金额。"})
    if incompatible_managed:
        warnings.append(
            {
                "code": "INCOMPATIBLE_MANAGED_OFFERS",
                "message": f"启动后将先撤销 {len(incompatible_managed)} 笔不兼容机器人挂单，确认消失后才创建新单。",
            }
        )
    if incompatible_external:
        warnings.append(
            {
                "code": "INCOMPATIBLE_EXTERNAL_OFFERS",
                "message": (
                    f"发现 {len(incompatible_external)} 笔外部挂单不符合策略；机器人不会撤销，但会计入资金上限。"
                ),
            }
        )
    if incompatible_credits:
        warnings.append(
            {
                "code": "INCOMPATIBLE_ACTIVE_CREDITS",
                "message": (
                    f"发现 {len(incompatible_credits)} 笔已成交贷款不符合新策略；无法撤销，只会阻止继续创建同类订单。"
                ),
            }
        )
    if result["over_cap"]:
        warnings.append(
            {
                "code": "FUNDING_CAP_EXCEEDED",
                "message": "当前账户敞口超过 V3 资金上限；不会创建新单，只能撤销机器人挂单。",
            }
        )

    enabled_types = [
        label
        for label, enabled in (
            ("LIMIT", policy.enable_limit),
            ("FRR", policy.enable_frr),
            ("FRR_DELTA_FIXED", policy.enable_frr_delta_fixed),
            ("FRR_DELTA_VARIABLE", policy.enable_frr_delta_variable),
        )
        if enabled
    ]
    summary = {
        "strategyVersion": 3,
        "buildId": dashboard_build_id(),
        "strategySource": "SQLITE_ACTIVE",
        "activeStrategyVersion": active["version_id"],
        "policyHash": strategy_v3_version_id(policy),
        "enabledOrderTypes": enabled_types,
        "onlyLimit": enabled_types == ["LIMIT"],
        "policy": strategy_v3_api_values(policy),
        "accountSnapshot": basis,
        "accountDigest": _account_context_digest(account, snapshot),
        "account": json_decimal(account),
        "fundingPools": {
            pool: {
                "share": decimal_to_config(policy.pool_shares()[pool]),
                "netFloorAprPercent": decimal_percent_to_config(policy.floor_apr(pool)),
                "periods": list(policy.periods(pool)),
            }
            for pool in ("short", "medium", "long")
        },
        "executionLayers": {layer: decimal_to_config(value) for layer, value in policy.layer_shares().items()},
        "fundingLimit": {
            "maxAmount": None if policy.max_lend_amount is None else status_decimal(policy.max_lend_amount),
            "maxPercent": decimal_to_config(policy.max_lend_percent),
            "effectiveCap": status_decimal(result["funding_cap"]),
            "existingExposure": status_decimal(result["existing_exposure"]),
            "capRemaining": status_decimal(result["cap_remaining"]),
        },
        "targetSlices": policy.target_slices,
        "actualSlices": len(result["plan"]),
        "planHash": result["plan_hash"],
        "strategyPlan": json_decimal(result["plan"]),
        "pendingCancellations": incompatible_managed,
        "externalIncompatibilities": incompatible_external,
        "nonChangeableCredits": incompatible_credits,
        "marketSignals": json_decimal(signals),
    }
    return {"checks": checks, "warnings": warnings, "summary": summary}


def create_controlled_bot_preflight(config_path, client_factory=None, now=None, context=None):
    context = process_context(config_path, context, client_factory=client_factory)
    state = context.process_state
    client_factory = client_factory or context.client_factory or Bitfinex
    with state.lock:
        if controlled_bot_running(config_path, context):
            raise ConfigError("机器人已在运行")
    read_config(config_path)
    digest_before = config_sha256(config_path)
    result = evaluate_live_preflight(config_path, client_factory=client_factory, context=context)
    digest_after = config_sha256(config_path)
    if digest_before != digest_after:
        result["checks"].append(
            {
                "id": "config_changed",
                "label": "策略配置稳定性",
                "status": "fail",
                "detail": "策略配置在预检过程中发生变化，请重新运行预检",
            }
        )
    can_start = bool(result["checks"]) and all(check["status"] == "pass" for check in result["checks"])
    issued_at = context.now() if now is None else float(now)
    expires_at = issued_at + PREFLIGHT_TTL_SECONDS
    preflight_id = secrets.token_urlsafe(24) if can_start else None
    response = {
        "preflightId": preflight_id,
        "expiresAt": datetime.datetime.fromtimestamp(expires_at, datetime.timezone.utc).isoformat(timespec="seconds"),
        "canStart": can_start,
        **result,
    }
    with state.lock:
        state.preflight = None
        if controlled_bot_running(config_path, context):
            response["canStart"] = False
            response["preflightId"] = None
            response["checks"].append(
                {"id": "process", "label": "机器人进程", "status": "fail", "detail": "机器人已在运行"}
            )
        elif can_start:
            summary = result.get("summary", {})
            state.preflight = {
                "preflightId": preflight_id,
                "expiresAt": expires_at,
                "configDigest": digest_after,
                "activeStrategyVersion": summary.get("activeStrategyVersion"),
                "policyHash": summary.get("policyHash"),
                "planHash": summary.get("planHash"),
                "accountDigest": summary.get("accountDigest"),
                "buildId": summary.get("buildId"),
                "cancellationDigest": _canonical_sha256(summary.get("pendingCancellations") or []),
                "clientFactory": client_factory,
            }
    return response


def consume_controlled_bot_preflight(config_path, preflight_id, now=None, context=None):
    context = process_context(config_path, context)
    state = context.process_state
    current = state.preflight
    state.preflight = None
    if not current or not preflight_id or current["preflightId"] != preflight_id:
        raise ConfigError("预检令牌无效或已使用，请重新运行预检")
    checked_at = context.now() if now is None else float(now)
    if checked_at > current["expiresAt"]:
        raise ConfigError("预检已过期，请重新运行预检")
    if config_sha256(config_path) != current["configDigest"]:
        raise ConfigError("策略配置在预检后发生变化，请重新运行预检")
    store, settings = v3_store_for_config(config_path)
    active, policy = ensure_active_strategy_v3(store, settings)
    if store.strategy("DRAFT") is not None or store.strategy("PENDING") is not None:
        raise ConfigError("预检后出现 DRAFT 或 PENDING 策略，请先应用或放弃并重新预检")
    if active["version_id"] != current.get("activeStrategyVersion"):
        raise ConfigError("ACTIVE 策略在预检后发生变化，请重新运行预检")
    if strategy_v3_version_id(policy) != current.get("policyHash"):
        raise ConfigError("ACTIVE 策略哈希在预检后发生变化，请重新运行预检")
    if dashboard_build_id() != current.get("buildId"):
        raise ConfigError("Dashboard build 在预检后发生变化，请重新运行预检")
    refreshed = evaluate_live_preflight(
        config_path,
        client_factory=current.get("clientFactory") or context.client_factory or Bitfinex,
        context=context,
    )
    failed = [check for check in refreshed.get("checks", []) if check.get("status") != "pass"]
    if failed:
        raise ConfigError("启动前重新核验失败，请重新运行预检")
    summary = refreshed.get("summary", {})
    if summary.get("accountDigest") != current.get("accountDigest"):
        raise ConfigError("账户快照在预检后发生变化，请重新运行预检")
    if summary.get("planHash") != current.get("planHash"):
        raise ConfigError("实际 V3 计划在预检后发生变化，请重新运行预检")
    if _canonical_sha256(summary.get("pendingCancellations") or []) != current.get("cancellationDigest"):
        raise ConfigError("待撤销挂单集合在预检后发生变化，请重新运行预检")


def start_controlled_bot(config_path, status_path, preflight_id, context=None):
    context = process_context(config_path, context)
    state = context.process_state
    with state.lock:
        if controlled_bot_running(config_path, context):
            raise ConfigError("机器人已在运行")
        consume_controlled_bot_preflight(config_path, preflight_id, context=context)
        os.makedirs(os.path.dirname(status_path) or ".", exist_ok=True)
        os.makedirs(os.path.dirname(context.process_log_path) or ".", exist_ok=True)

        command = [
            sys.executable,
            os.path.abspath(__file__),
            "--config",
            config_path,
            "--no-server",
            "--live",
            "--confirmed-preflight",
            "--json",
            status_path,
            "--jsonsize",
            "200",
        ]
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        cleanup_controlled_bot_handle(context)
        state.log_handle = open(context.process_log_path, "ab", buffering=0)
        try:
            state.process = subprocess.Popen(
                command,
                cwd=context.project_root,
                stdin=subprocess.DEVNULL,
                stdout=state.log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                startupinfo=startupinfo,
            )
        except Exception:
            cleanup_controlled_bot_handle(context)
            raise
        state.started_at = timestamp()
        state.stop_reason = None
        return controlled_bot_status(config_path, context)


def stop_controlled_bot(config_path=DEFAULT_CONFIG, reason="stopped_by_dashboard", context=None):
    context = process_context(config_path, context)
    state = context.process_state
    with state.lock:
        state.preflight = None
        process = state.process
        internal_running = process is not None and process.poll() is None
        external = None if internal_running else external_live_process(config_path, context)
        if not internal_running and external is None:
            cleanup_controlled_bot_handle(context)
            return controlled_bot_status(config_path, context)
        if external is not None:
            if external.get("stateError"):
                raise ConfigError(external["stateError"])
            identity_error = _live_process_identity_error(external)
            if identity_error:
                raise ConfigError(identity_error)
            pid = int(external["pid"])
            os.kill(pid, signal.SIGTERM)
            deadline = context.now() + 5
            while context.now() < deadline and external_live_process(config_path, context) is not None:
                time.sleep(0.05)
            if external_live_process(config_path, context) is not None:
                raise ConfigError("实盘进程未在超时时间内释放单实例锁")
            state.stop_reason = reason
            return controlled_bot_status(config_path, context)
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        cleanup_controlled_bot_handle(context)
        state.stop_reason = reason
        return controlled_bot_status(config_path, context)


def load_dashboard_static_snapshot(directory, build_id, csrf_token=""):
    assets = {}
    root = os.path.abspath(directory)
    for current, _, files in os.walk(root):
        for name in files:
            path = os.path.join(current, name)
            relative = os.path.relpath(path, root).replace("\\", "/")
            if relative in {"botlog.json", "bot-process.log"}:
                continue
            try:
                with open(path, "rb") as file:
                    data = file.read()
            except OSError:
                continue
            if relative == "lendingbot.html":
                data = data.replace(DASHBOARD_BUILD_PLACEHOLDER.encode("utf-8"), build_id.encode("ascii"))
                data = data.replace(DASHBOARD_CSRF_PLACEHOLDER.encode("utf-8"), csrf_token.encode("ascii"))
            assets[relative] = data
    return assets


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    config_path = DEFAULT_CONFIG
    status_path = DEFAULT_DASHBOARD_JSON
    build_id = ""
    static_assets = {}
    dashboard_started_at = DASHBOARD_STARTED_AT
    csrf_token = ""
    app_context = None

    def log_message(self, format, *args):
        return

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self'; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def _send_json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Mika-Dashboard-Build", self.build_id)
        self.end_headers()
        self.wfile.write(data)

    def _send_api_error(self, code, error, status=400, details=None):
        payload = {"ok": False, "code": str(code), "error": str(error), "details": details}
        self._send_json(payload, status=status)

    def _send_static(self, path):
        relative = path.lstrip("/") or "lendingbot.html"
        if relative == "lendingbot.html" and relative in self.static_assets:
            data = self.static_assets[relative]
        else:
            data = self.static_assets.get(relative)
        if data is None:
            self.send_error(404, "Not found")
            return
        content_type = mimetypes.guess_type(relative)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header(
            "Content-Type",
            content_type
            + (
                "; charset=utf-8"
                if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}
                else ""
            ),
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Mika-Dashboard-Build", self.build_id)
        self.end_headers()
        self.wfile.write(data)

    def _validate_json_envelope(self):
        content_type = str(self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ApiRequestError("写接口只接受 application/json", "CONTENT_TYPE_REQUIRED", 415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiRequestError("Content-Length 无效", "INVALID_CONTENT_LENGTH", 400) from exc
        if length < 0:
            raise ApiRequestError("Content-Length 无效", "INVALID_CONTENT_LENGTH", 400)
        if length > DASHBOARD_MAX_BODY_BYTES:
            raise ApiRequestError("请求体不能超过 64 KiB", "REQUEST_TOO_LARGE", 413)
        return length

    def _read_json_body(self):
        length = self._validate_json_envelope()
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def _validate_write_request(self):
        host = str(self.headers.get("Host") or "").lower()
        if host not in {"127.0.0.1:8000", "localhost:8000"}:
            raise ApiRequestError("写请求 Host 不受信任", "INVALID_HOST", 403)
        origin = str(self.headers.get("Origin") or "").lower()
        if origin not in {"http://127.0.0.1:8000", "http://localhost:8000"}:
            raise ApiRequestError("写请求必须来自本地控制台", "INVALID_ORIGIN", 403)
        supplied = str(self.headers.get("X-Mika-CSRF") or "")
        if not supplied or not secrets.compare_digest(supplied, self.csrf_token):
            raise ApiRequestError("控制台安全令牌无效，请刷新页面", "INVALID_CSRF", 403)
        self._validate_json_envelope()

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/health":
                self._send_json(
                    {
                        "ok": True,
                        "service": DASHBOARD_SERVICE_ID,
                        "buildId": self.build_id,
                        "pid": os.getpid(),
                        "startedAt": self.dashboard_started_at,
                        "projectRoot": os.path.abspath(os.path.dirname(__file__)),
                        "configPath": os.path.abspath(self.config_path),
                        "time": timestamp(),
                    }
                )
                return
            if path == "/api/status":
                self._send_json(dashboard_status_payload(self.status_path, self.config_path, self.app_context))
                return
            if path == "/api/config":
                self._send_json(config_api_payload(self.config_path))
                return
            if path == "/api/control/status":
                self._send_json(controlled_bot_status(self.config_path, self.app_context))
                return
            if path == "/api/runtime/v3":
                self._send_json({"ok": True, **runtime_v3_payload(self.config_path, self.app_context)})
                return
            if path == "/api/stats/v3":
                store, _ = v3_store_for_config(self.config_path)
                self._send_json({"ok": True, **stats_v3_payload(store)})
                return
        except Exception as exc:
            self._send_api_error("INTERNAL_ERROR", exc, status=500)
            return
        self._send_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            self._validate_write_request()
            if path == "/api/config":
                self._read_json_body()
                self._send_json(
                    {
                        "ok": False,
                        "code": "V2_STRATEGY_DISABLED",
                        "details": None,
                        "error": "V2 配置写入已永久禁用；请使用 /api/strategy/v3/*",
                    },
                    status=410,
                )
                return
            if path == "/api/strategy/v3/preview":
                payload = self._read_json_body()
                preview = strategy_v3_preview(self.config_path, payload, app_context=self.app_context)
                self._send_json({"ok": True, **preview})
                return
            if path == "/api/strategy/v3/draft":
                payload = self._read_json_body()
                result = save_strategy_v3_draft(self.config_path, payload, app_context=self.app_context)
                self._send_json({"ok": True, **result})
                return
            if path == "/api/strategy/v3/apply":
                payload = self._read_json_body()
                result = apply_strategy_v3_draft(self.config_path, payload, app_context=self.app_context)
                self._send_json({"ok": True, **result})
                return
            if path == "/api/strategy/v3/discard":
                self._read_json_body()
                result = discard_strategy_v3_draft(self.config_path)
                self._send_json({"ok": True, **result})
                return
            if path == "/api/runtime/v3/mode":
                payload = self._read_json_body()
                target = str(payload.get("mode", "")).strip().upper()
                store, _ = v3_store_for_config(self.config_path)
                if target == "PAUSED":
                    if controlled_bot_running(self.config_path, self.app_context):
                        stop_controlled_bot(self.config_path, context=self.app_context)
                    runtime = store.set_mode("PAUSED", "dashboard_pause")
                    self._send_json({"ok": True, "runtime": runtime})
                    return
                if target == "REPLAY":
                    replay = replay_v3_from_store(self.config_path, context=self.app_context)
                    self._send_json({"ok": True, "runtime": store.runtime(), "replay": replay})
                    return
                if target == "LIVE":
                    raise ConfigError("LIVE 必须通过 /api/control/preflight 和 /api/control/start 启动")
                raise ConfigError("仅允许切换到 PAUSED、REPLAY；SAFE 由安全状态机管理")
            if path == "/api/runtime/v3/resolve-ambiguous":
                payload = self._read_json_body()
                store, _ = v3_store_for_config(self.config_path)
                result = store.resolve_ambiguous_intent(
                    payload.get("intentId"),
                    exchange_offer_id=payload.get("exchangeOfferId"),
                    close=bool(payload.get("confirmAbsent", False)),
                )
                self._send_json({"ok": True, **result})
                return
            if path == "/api/control/preflight":
                self._read_json_body()
                preflight = create_controlled_bot_preflight(self.config_path, context=self.app_context)
                self._send_json({"ok": True, **preflight})
                return
            if path == "/api/control/start":
                payload = self._read_json_body()
                status = start_controlled_bot(
                    self.config_path,
                    self.status_path,
                    str(payload.get("preflightId", "")),
                    context=self.app_context,
                )
                self._send_json({"ok": True, "bot": status})
                return
            if path == "/api/control/stop":
                status = stop_controlled_bot(self.config_path, context=self.app_context)
                store, _ = v3_store_for_config(self.config_path)
                runtime = store.runtime()
                if runtime["mode"] != "SAFE":
                    runtime = store.set_mode("PAUSED", "dashboard_stop")
                self._send_json({"ok": True, "bot": status, "runtime": runtime})
                return
            self._send_api_error("NOT_FOUND", "Not found", status=404)
        except ApiRequestError as exc:
            self._send_api_error(exc.code, exc, status=exc.status, details=exc.details)
        except Exception as exc:
            self._send_api_error("REQUEST_REJECTED", exc, status=400)


def make_dashboard_handler(directory, config_path, status_path, build_id=None, context=None):
    context = process_context(config_path, context)
    build_id = build_id or dashboard_build_id()
    csrf_token = secrets.token_urlsafe(32)
    static_assets = load_dashboard_static_snapshot(directory, build_id, csrf_token)

    class Handler(DashboardRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

    Handler.config_path = config_path
    Handler.status_path = status_path
    Handler.build_id = build_id
    Handler.static_assets = static_assets
    Handler.csrf_token = csrf_token
    Handler.app_context = context
    Handler.dashboard_started_at = timestamp()
    return Handler


def start_web_server(log, config_path, status_path, context=None):
    context = process_context(config_path, context)
    state = context.process_state
    port = 8000
    host = "127.0.0.1"
    directory = os.path.join(os.getcwd(), "www")
    handler = make_dashboard_handler(directory, config_path, status_path, context=context)
    try:
        if state.market_hub is None and websocket_dependency_available():
            store, settings = v3_store_for_config(config_path)
            _, active_policy = ensure_active_strategy_v3(store, settings)
            state.market_hub = BitfinexMarketDataHub(
                "",
                "",
                symbol="fUSD",
                store=store,
                fallback_seconds=active_policy.ws_fallback_seconds,
                rest_stale_seconds=active_policy.rest_stale_seconds,
            )
            state.market_hub.start()
        state.dashboard_server = ThreadingHTTPServer((host, port), handler)
        log.log(f"网页控制台已启动：http://{host}:{port}/lendingbot.html")
        state.dashboard_server.serve_forever()
    except Exception as exc:
        log.log(f"网页控制台启动失败：{exc}")


def stop_web_server(log, context=None):
    context = process_context(DEFAULT_CONFIG, context)
    state = context.process_state
    if state.market_hub is not None:
        state.market_hub.stop()
        state.market_hub = None
    if state.dashboard_server is None:
        return
    try:
        log.log("正在停止网页控制台")
        state.dashboard_server.shutdown()
        state.dashboard_server = None
    except Exception as exc:
        log.log(f"停止网页控制台失败：{exc}")


def publish_safe_status(log, store, exc):
    runtime = store.enter_safe(f"UNEXPECTED_RUNTIME_ERROR:{type(exc).__name__}")
    log.updateMetaValue("schemaVersion", STATUS_SCHEMA_VERSION)
    log.updateMetaValue("operationMode", "SAFE")
    log.updateMetaValue("runtime", runtime)
    log.refreshStatus(f"SAFE：{type(exc).__name__}：{exc}")
    log.persistStatus()
    return runtime


def build_app_context(args, settings):
    root = os.path.abspath(os.path.dirname(__file__))
    context = AppContext.for_project(
        root,
        config_path=args.config,
        status_path=settings.json_file or DEFAULT_DASHBOARD_JSON,
        state_db_path=settings.state_db_file,
        client_factory=Bitfinex,
    )
    args.config = context.config_path
    settings.state_db_file = context.state_db_path
    if settings.json_file:
        settings.json_file = context.status_path
    return context


def ensure_offline_database_access(context):
    inspection = LiveProcessLock.inspect(context.live_lock_path)
    if inspection["locked"]:
        pid = inspection.get("metadata", {}).get("pid")
        raise ConfigError(f"offline command rejected while LIVE worker lock is held (PID {pid})")


def migrate_legacy_state(context, store):
    migration_id = "managed-offers-v2-to-schema-v4"
    existing = store.legacy_migration(migration_id)
    if existing is not None:
        return {**existing["result"], "migrationId": migration_id, "idempotent": True}
    source_path = os.path.join(context.project_root, ".state", "managed-offers.json")
    imported = store.import_legacy_managed_offers(source_path)
    result = {
        "migrationId": migration_id,
        "sourcePath": source_path,
        "sourcePresent": os.path.exists(source_path),
        "importedOffers": imported,
        "idempotent": False,
    }
    store.record_legacy_migration(migration_id, source_path, result)
    return result


def run_offline_commands(args, settings, context):
    ensure_offline_database_access(context)
    store = LendingStateStore(context.state_db_path, clock=context.now)
    results = {}
    if args.migrate_legacy:
        results["legacyMigration"] = migrate_legacy_state(context, store)
    if args.backfill_market_data:
        results["marketBackfill"] = backfill_public_market_data(Bitfinex("", ""), store, days=args.research_days)
    if args.evaluate_strategy:
        _, active_policy = ensure_active_strategy_v3(store, settings)
        report = evaluate_strategies(
            store,
            active_policy,
            Decimal(str(args.principal)),
            days=args.research_days,
        )
        report_path = os.path.join(context.project_root, "docs", "strategy-validation-report.json")
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        write_research_report(report_path, report)
        results["strategyEvaluation"] = {
            "reportPath": report_path,
            "selection": report["selection"],
            "data": report["data"],
        }
    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    return 0


def main(argv=None):
    args = parse_args(argv)
    offline_requested = any((args.migrate_legacy, args.backfill_market_data, args.evaluate_strategy))
    project_root = os.path.abspath(os.path.dirname(__file__))
    args.config = AppContext.for_project(project_root, config_path=args.config).config_path
    if args.dashboard and args.live:
        print("Configuration error: --dashboard 不能与 --live 同时使用", file=sys.stderr)
        return 1
    if offline_requested and (args.dashboard or args.live):
        print("Configuration error: offline commands cannot be combined with --dashboard or --live", file=sys.stderr)
        return 1
    if not offline_requested and not args.dashboard and not args.live:
        print("Configuration error: 运行机器人必须显式传入 --live；如只需控制台请使用 --dashboard", file=sys.stderr)
        return 1
    config, config_created = read_config(args.config)
    try:
        settings = build_settings(args, config, config_created)
        validate_settings(settings)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    if args.dashboard:
        settings.web_server = True
    if settings.web_server and not settings.json_file:
        settings.json_file = DEFAULT_DASHBOARD_JSON.replace("\\", "/")
        settings.json_log_size = 200

    context = build_app_context(args, settings)
    if offline_requested:
        try:
            return run_offline_commands(args, settings, context)
        except (ConfigError, StateStoreError, ValueError, RuntimeError, BitfinexApiError) as exc:
            print(f"Offline command failed: {exc}", file=sys.stderr)
            return 1

    log = Logger(
        settings.json_file,
        settings.json_log_size,
        sensitive_values=(settings.api_key, settings.api_secret),
    )
    if config_created:
        log.log("已从 default.cfg.example 复制出 default.cfg，请在里面填写 Bitfinex API 密钥。")

    if args.dashboard:
        dashboard_lock = LiveProcessLock(context.dashboard_lock_path)
        if not dashboard_lock.acquire(args.config, {"role": "dashboard", "service": DASHBOARD_SERVICE_ID}):
            print("Dashboard startup rejected: another dashboard instance holds the lock", file=sys.stderr)
            return 1
        reconcile_orphaned_live_runtime(args.config, context)
        normalization = normalize_current_active_strategy(args.config)
        if normalization["changed"]:
            log.log(
                f"V3 ACTIVE 已规范化：{normalization['fromVersion']} -> {normalization['versionId']}；"
                f"配置和数据库备份位于 {os.path.dirname(normalization['backup']['database'])}"
            )
        log.log("控制台已就绪；只有完成只读预检并确认后才会启动实盘机器人。")
        thread = threading.Thread(
            target=start_web_server,
            args=(log, args.config, settings.json_file, context),
            daemon=True,
        )
        thread.start()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            stop_web_server(log, context)
            dashboard_lock.release()
            log.log("已退出")
        return 0

    normalization = normalize_current_active_strategy(args.config)
    if normalization["changed"]:
        log.log(f"V3 ACTIVE 已规范化：{normalization['fromVersion']} -> {normalization['versionId']}")
    log.log("欢迎使用 Bitfinex 自动放贷机器人（实盘运行）")
    preflight = evaluate_live_preflight(args.config)
    failed_checks = [check for check in preflight["checks"] if check["status"] == "fail"]
    if failed_checks:
        for check in failed_checks:
            log.log(f"实盘预检失败：{check['label']} — {check['detail']}")
        return 1
    for warning in preflight["warnings"]:
        log.log("实盘预检警告：" + warning["message"])
    if not args.confirmed_preflight:
        try:
            confirmation = input("只读预检已通过。输入 LIVE 确认本次实盘启动：").strip()
        except (EOFError, KeyboardInterrupt):
            confirmation = ""
        if confirmation != "LIVE":
            log.log("未完成人工确认，保持 PAUSED。")
            return 1
    live_lock = LiveProcessLock(context.live_lock_path)
    if not live_lock.acquire(args.config, {"role": "live_worker", "service": "mika-lending-worker-v3"}):
        log.log("实盘启动被拒绝：另一个机器人进程已持有单实例锁。")
        return 1
    log.log("实盘预检通过，开始同步账户并执行策略。")
    client = Bitfinex(settings.api_key, settings.api_secret)
    state_store_v3 = LendingStateStore(settings.state_db_file, clock=context.now)
    _, active_policy = ensure_active_strategy_v3(state_store_v3, settings)
    state_store_v3.set_mode("LIVE", "live_preflight_confirmed")
    runtime_v3 = LendingRuntimeV3(
        client,
        active_policy,
        state_store_v3,
        log=log,
        auto_transfer_wallets=(settings.transfer_from_wallets if "USD" in settings.transferable_currencies else ()),
        on_policy_activated=lambda policy, _version: mirror_active_strategy_v3(args.config, policy),
        clock=context.now,
    )

    if settings.web_server:
        thread = threading.Thread(
            target=start_web_server,
            args=(log, args.config, settings.json_file, context),
            daemon=True,
        )
        thread.start()

    sleep_time = settings.sleep_active
    try:
        while True:
            try:
                runtime_v3.cycle()
                sleep_time = settings.sleep_active
                if settings.once:
                    break
                time.sleep(sleep_time)
            except Exception as exc:
                log.log("错误：" + str(exc))
                publish_safe_status(log, state_store_v3, exc)
                print(timestamp())
                print(traceback.format_exc())
                if settings.once:
                    return 1
                time.sleep(sleep_time)
    except KeyboardInterrupt:
        pass
    finally:
        runtime_v3.shutdown()
        if state_store_v3.runtime()["mode"] != "SAFE":
            state_store_v3.set_mode("PAUSED", "live_process_stopped")
        if settings.web_server:
            stop_web_server(log, context)
        live_lock.release()
        log.log("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
