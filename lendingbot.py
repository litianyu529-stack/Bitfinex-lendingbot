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
from decimal import Decimal, ROUND_DOWN, getcontext
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from bitfinex import Bitfinex, BitfinexApiError, currency_to_symbol, decimal_from_api, symbol_to_currency
from FileUtils import atomic_write_text
from Logger import Logger
from StrategyEngine import (
    BUCKETS,
    ManagedOfferRegistry,
    PublicMarketCache,
    StrategyPolicy,
    WINDOW_MS,
    build_market_signals,
    build_strategy_plan,
    extract_submitted_offer_id,
    parse_funding_stats,
    parse_funding_trades,
    plan_to_json,
    policy_to_json,
    policy_with_overrides,
    preset_policy,
    replay_strategy,
    signals_to_json,
    validate_policy,
)
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
    build_strategy_plan_v3,
    gross_daily_floor,
    json_decimal,
    pool_for_period,
    policy_v3_to_json,
    policy_v3_with_overrides,
    replay_strategy_v3,
    validate_policy_v3,
)


getcontext().prec = 28

SATOSHI = Decimal("0.00000001")
RATE_UNDERCUT = Decimal("0.000001")
DEFAULT_CONFIG = "default.cfg"
DEFAULT_CONFIG_EXAMPLE = "default.cfg.example"
DEFAULT_DASHBOARD_JSON = os.path.join("www", "botlog.json")
DEFAULT_PROCESS_LOG = os.path.join("www", "bot-process.log")
DEFAULT_MANAGED_OFFER_STATE = os.path.join(".state", "managed-offers.json")
DEFAULT_V3_STATE_DB = os.path.join(".state", "lendingbot-v3.sqlite3")
DEFAULT_LIVE_LOCK = os.path.join(".state", "lendingbot-live.lock")
DEFAULT_DASHBOARD_LOCK = os.path.join(".state", "lendingbot-dashboard.lock")
STATUS_SCHEMA_VERSION = 3
PREFLIGHT_TTL_SECONDS = 300
DASHBOARD_SERVICE_ID = "mika-lending-dashboard-v3"
DASHBOARD_BUILD_PLACEHOLDER = "__MIKA_DASHBOARD_BUILD_ID__"
DASHBOARD_STARTED_AT = timestamp() if "timestamp" in globals() else datetime.datetime.now().astimezone().isoformat(timespec="seconds")
DASHBOARD_BUILD_FILES = (
    "lendingbot.py", "bitfinex.py", "FileUtils.py", "Logger.py", "MarketDataStream.py",
    "RuntimeV3.py", "StateStore.py", "StrategyV3.py",
    os.path.join("www", "lendingbot.html"), os.path.join("www", "lendingbot.js"),
    os.path.join("www", "v3-dashboard.js"), os.path.join("www", "lendingbot.css"),
)
market_data_cache = PublicMarketCache(max_requests_per_minute=12, stale_seconds=1800)
managed_registry_cache = {}
managed_registry_lock = threading.RLock()


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

    def __init__(self, path=DEFAULT_LIVE_LOCK):
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
    def inspect(cls, path=DEFAULT_LIVE_LOCK):
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
DASHBOARD_BITFINEX_FIELDS = {"currencies"}
DASHBOARD_BOT_FIELDS = {
    "sleeptimeactive",
    "sleeptimeinactive",
    "mindailyrate",
    "maxdailyrate",
    "spreadlend",
    "gapbottom",
    "gaptop",
    "xdaythreshold",
    "xdays",
    "minloansize",
    "platformfeerate",
    "maxtolent",
    "maxpercenttolent",
    "maxtolentrate",
    "transferablecurrencies",
    "transferfromwallets",
    "outputcurrency",
    "jsonfile",
    "jsonlogsize",
    "startwebserver",
    "smartstrategy",
    "smartrateoffset",
    "smartfastdepth",
    "smartbalanceddepth",
    "smartopportunitydepth",
    "smartopportunitypremium",
    "smartfastshare",
    "smartlongshare",
    "smartfloordepth",
    "smartlongperiod",
    "smartlongwaitminutes",
    "repricestaleoffers",
    "repriceafterminutes",
    "repriceminratedelta",
    "strategyversion",
    "strategyprofile",
    "strategyautotypes",
    "strategyreplaywindow",
    "strategyfastshare",
    "strategylongshare",
    "strategyfastperiod",
    "strategybalancedperiod",
    "strategylongperiod",
    "strategyfastwaitminutes",
    "strategybalancedwaitminutes",
    "strategylongwaitminutes",
    "strategyrateoffset",
    "strategylongpremium",
    "strategyfastdepth",
    "strategybalanceddepth",
    "strategylongdepth",
    "strategyfloordepth",
    "strategytrendmindelta",
    "strategyutilizationlow",
    "strategyutilizationhigh",
    "strategyrepriceminratedelta",
    "strategyfastordertype",
    "strategybalancedordertype",
    "strategylongordertype",
    "strategyfastfrroffset",
    "strategybalancedfrroffset",
    "strategylongfrroffset",
    "managedofferstatefile",
    "statedbfile",
}


class ConfigError(Exception):
    pass


class ApiRequestError(ConfigError):
    def __init__(self, message, code="REQUEST_REJECTED", status=400, details=None):
        super().__init__(message)
        self.code = code
        self.status = int(status)
        self.details = details


def managed_offer_registry(path):
    absolute_path = os.path.abspath(path or DEFAULT_MANAGED_OFFER_STATE)
    with managed_registry_lock:
        registry = managed_registry_cache.get(absolute_path)
        if registry is None:
            registry = ManagedOfferRegistry(absolute_path)
            managed_registry_cache[absolute_path] = registry
        return registry


@dataclass
class CoinConfig:
    min_rate: Decimal
    enabled: bool
    max_to_lend: Decimal
    max_percent_to_lend: Decimal
    max_to_lend_rate: Decimal
    min_loan_size: Decimal | None = None


@dataclass
class Settings:
    api_key: str
    api_secret: str
    currencies: list[str]
    sleep_active: float
    sleep_inactive: float
    min_daily_rate: Decimal
    max_daily_rate: Decimal
    spread_lend: int
    gap_bottom: Decimal
    gap_top: Decimal
    xday_threshold: Decimal
    xdays: int
    min_loan_size: Decimal
    max_to_lend: Decimal
    max_percent_to_lend: Decimal
    max_to_lend_rate: Decimal
    coin_cfg: dict[str, CoinConfig]
    transferable_currencies: list[str]
    transfer_from_wallets: list[str]
    output_currency: str
    platform_fee_rate: Decimal
    json_file: str
    json_log_size: int
    web_server: bool
    once: bool
    smart_strategy: bool
    smart_rate_offset: Decimal
    smart_fast_depth: Decimal
    smart_balanced_depth: Decimal
    smart_opportunity_depth: Decimal
    smart_opportunity_premium: Decimal
    smart_fast_share: Decimal
    smart_long_share: Decimal
    smart_floor_depth: Decimal
    smart_long_period: bool
    smart_long_wait_minutes: Decimal
    reprice_stale_offers: bool
    reprice_after_minutes: Decimal
    reprice_min_rate_delta: Decimal
    strategy_policy: StrategyPolicy
    strategy_overrides: dict[str, StrategyPolicy]
    strategy_auto_migrated: bool
    managed_offer_state_file: str
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
    parser.add_argument("-key", "--apikey", help="Bitfinex API key")
    parser.add_argument("-secret", "--apisecret", help="Bitfinex API secret")
    parser.add_argument("-curr", "--currencies", help="comma-separated funding currencies, e.g. USD,UST")
    parser.add_argument("-sleepactive", "--sleeptimeactive", help="seconds between active iterations")
    parser.add_argument("-sleepinactive", "--sleeptimeinactive", help="seconds between inactive iterations")
    parser.add_argument("-minrate", "--mindailyrate", help="minimum daily funding rate, percent")
    parser.add_argument("-maxrate", "--maxdailyrate", help="maximum daily funding rate, percent")
    parser.add_argument("-spread", "--spreadlend", help="number of funding offers to split into")
    parser.add_argument("-gapbot", "--gapbottom", help="book depth percent for first offer")
    parser.add_argument("-gaptop", "--gaptop", help="book depth percent for last offer")
    parser.add_argument("-xdaythreshold", "--xdaythreshold", help="rate threshold for xdays, percent")
    parser.add_argument("-xdays", "--xdays", help="funding period when threshold is met")
    parser.add_argument("-trans", "--transferablecurrencies", help="currencies to transfer into funding wallet")
    parser.add_argument("-minloan", "--minloansize", help="minimum funding offer amount")
    parser.add_argument("-json", "--json", "--jsonfile", dest="jsonfile", help="path to json status log")
    parser.add_argument("-jsonsize", "--jsonsize", "--jsonlogsize", dest="jsonlogsize", help="number of json log lines to keep")
    parser.add_argument("-server", "--server", "--startwebserver", dest="startwebserver", action="store_true", help="serve ./www on 127.0.0.1:8000")
    parser.add_argument("--no-server", action="store_true", help="disable config-driven web server startup")
    parser.add_argument("-coincfg", "--coinconfig", help="custom per-coin config")
    parser.add_argument("-outcurr", "--outputcurrency", help="summary output currency")
    parser.add_argument("-maxlent", "--maxtolent", help="max amount to lend below conditional rate")
    parser.add_argument("-maxplent", "--maxpercenttolent", help="max percent to lend below conditional rate")
    parser.add_argument("-maxlentr", "--maxtolentrate", help="conditional rate for max-to-lend limits, percent")
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


def parse_coin_config(raw):
    if not raw:
        return {}
    try:
        entries = json.loads(raw)
    except ValueError:
        entries = [item.strip() for item in raw.split(",") if item.strip()]

    coin_cfg = {}
    for entry in entries:
        parts = [part.strip() for part in str(entry).split(":")]
        if len(parts) < 6:
            raise ConfigError(
                "coinconfig entries must be COIN:mindailyrate:enabled:maxtolent:"
                "maxpercenttolent:maxtolentrate[:minloansize]"
            )
        currency = symbol_to_currency(parts[0]).upper()
        min_loan_size = Decimal(parts[6]) if len(parts) > 6 and parts[6] != "" else None
        coin_cfg[currency] = CoinConfig(
            min_rate=Decimal(parts[1]) / Decimal("100"),
            enabled=Decimal(parts[2]) > 0,
            max_to_lend=Decimal(parts[3]),
            max_percent_to_lend=Decimal(parts[4]) / Decimal("100"),
            max_to_lend_rate=Decimal(parts[5]) / Decimal("100"),
            min_loan_size=min_loan_size,
        )
    return coin_cfg


STRATEGY_CONFIG_FIELDS = {
    "fast_share": ("fastshare", "decimal"),
    "long_share": ("longshare", "decimal"),
    "fast_period": ("fastperiod", "int"),
    "balanced_period": ("balancedperiod", "int"),
    "long_period": ("longperiod", "int"),
    "fast_wait_minutes": ("fastwaitminutes", "decimal"),
    "balanced_wait_minutes": ("balancedwaitminutes", "decimal"),
    "long_wait_minutes": ("longwaitminutes", "decimal"),
    "rate_offset": ("rateoffset", "percent"),
    "long_premium": ("longpremium", "percent"),
    "fast_depth": ("fastdepth", "decimal"),
    "balanced_depth": ("balanceddepth", "decimal"),
    "long_depth": ("longdepth", "decimal"),
    "floor_depth": ("floordepth", "decimal"),
    "trend_min_delta": ("trendmindelta", "percent"),
    "utilization_low": ("utilizationlow", "percent"),
    "utilization_high": ("utilizationhigh", "percent"),
    "reprice_min_delta": ("repriceminratedelta", "percent"),
    "fast_order_type": ("fastordertype", "string"),
    "balanced_order_type": ("balancedordertype", "string"),
    "long_order_type": ("longordertype", "string"),
    "fast_frr_offset": ("fastfrroffset", "percent"),
    "balanced_frr_offset": ("balancedfrroffset", "percent"),
    "long_frr_offset": ("longfrroffset", "percent"),
}


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
V3_CONFIG_FIELDS = tuple(
    name for name in StrategyPolicyV3.__dataclass_fields__
    if name not in {"version", "currency"}
)


def strategy_v3_from_config(config):
    section = "STRATEGY_V3"
    values = {}
    if config.has_section(section):
        for field_name in V3_CONFIG_FIELDS:
            raw = get_option(config, section, field_name, None)
            if raw is None:
                continue
            if field_name in {
                "short_floor_apr", "medium_floor_apr", "long_floor_apr",
                "hidden_max_share", "max_lend_amount",
            } and str(raw).strip() == "":
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
    payload["hidden_max_share"] = None if policy.hidden_max_share is None else decimal_to_config(policy.hidden_max_share)
    payload["gross_daily_floors_percent"] = {
        pool: None if policy.floor_apr(pool) is None else decimal_percent_to_config(
            Decimal(payload["gross_daily_floors"][pool])
        )
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
        if field_name in {"short_floor_apr", "medium_floor_apr", "long_floor_apr", "hidden_max_share", "max_lend_amount"} and value in (None, ""):
            values[field_name] = None
        elif field_name in V3_PERCENT_FIELDS:
            values[field_name] = Decimal(str(value)) / Decimal("100")
        elif field_name in V3_BOOL_FIELDS:
            values[field_name] = value if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "on"}
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
        pre_serialized = json.dumps(json_decimal(pre_policy.__dict__), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
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


def _strategy_option(config, section, option, global_section=False):
    key = f"strategy{option}" if global_section else option
    return get_option(config, section, key, None)


def _parse_strategy_value(raw, kind):
    if kind == "percent":
        return Decimal(str(raw)) / Decimal("100")
    if kind == "decimal":
        return Decimal(str(raw))
    if kind == "int":
        return int(raw)
    return str(raw).strip().upper() if "ordertype" in str(raw).lower() else str(raw).strip()


def strategy_policy_from_config(config, section="BOT", base=None, global_section=False):
    if global_section:
        profile = get_option(config, section, "strategyprofile", None)
        auto_types = get_option(config, section, "strategyautotypes", None)
        replay_window = get_option(config, section, "strategyreplaywindow", None)
    else:
        profile = get_option(config, section, "profile", None)
        auto_types = get_option(config, section, "autotypes", None)
        replay_window = get_option(config, section, "replaywindow", None)
    if base is None:
        base = preset_policy(profile or "balanced_yield")
    elif profile and profile != "custom":
        base = preset_policy(profile)
    values = {}
    if profile:
        values["profile"] = profile
    if auto_types is not None:
        values["auto_order_types"] = str(auto_types).strip().lower() in {"1", "true", "yes", "on"}
    if replay_window:
        values["replay_window"] = replay_window
    for field_name, (option, kind) in STRATEGY_CONFIG_FIELDS.items():
        raw = _strategy_option(config, section, option, global_section)
        if raw is not None and str(raw).strip() != "":
            if kind == "string":
                values[field_name] = str(raw).strip().upper()
            else:
                values[field_name] = _parse_strategy_value(raw, kind)
    policy = policy_with_overrides(base, values)
    return validate_policy(policy)


def build_strategy_policies(config, currencies):
    auto_migrated = not config.has_option("BOT", "strategyversion")
    global_policy = strategy_policy_from_config(config, "BOT", global_section=True)
    overrides = {}
    for section in config.sections():
        if not section.upper().startswith("STRATEGY:"):
            continue
        currency = section.split(":", 1)[1].strip().upper()
        if currency not in currencies:
            continue
        if config.getboolean(section, "inherit", fallback=False):
            continue
        overrides[currency] = strategy_policy_from_config(config, section, base=global_policy)
    return global_policy, overrides, auto_migrated


def strategy_policy_for(settings, currency):
    return settings.strategy_overrides.get(currency.upper(), settings.strategy_policy)


def strategy_policy_config_values(policy, global_section=False):
    prefix = "strategy" if global_section else ""
    values = {
        f"{prefix}version" if global_section else "version": "2",
        f"{prefix}profile" if global_section else "profile": policy.profile,
        f"{prefix}autotypes" if global_section else "autotypes": str(policy.auto_order_types).lower(),
        f"{prefix}replaywindow" if global_section else "replaywindow": policy.replay_window,
    }
    for field_name, (option, kind) in STRATEGY_CONFIG_FIELDS.items():
        value = getattr(policy, field_name)
        if kind == "percent":
            text_value = decimal_percent_to_config(value)
        elif isinstance(value, Decimal):
            text_value = decimal_to_config(value)
        else:
            text_value = str(value)
        values[f"{prefix}{option}"] = text_value
    return values


def build_settings(args, config, config_created=False):
    api_key = (
        args.apikey
        or os.environ.get("BITFINEX_API_KEY")
        or get_option(config, "BITFINEX", "apikey", "")
    )
    api_secret = (
        args.apisecret
        or os.environ.get("BITFINEX_API_SECRET")
        or get_option(config, "BITFINEX", "secret", "")
    )
    currencies = split_csv(
        args.currencies or get_option(config, "BITFINEX", "currencies", "USD,UST")
    )
    if not currencies:
        raise ConfigError("At least one Bitfinex funding currency is required")

    coin_cfg = parse_coin_config(args.coinconfig or get_option(config, "BOT", "coinconfig", ""))
    min_loan_size = Decimal(args.minloansize or get_option(config, "BOT", "minloansize", "150"))
    for currency in currencies:
        if currency not in {"USD", "UST"}:
            if currency not in coin_cfg or coin_cfg[currency].min_loan_size is None:
                raise ConfigError(
                    f"{currency} requires coinconfig with explicit minloansize as the 7th field"
                )

    # V2 strategy fields remain in the file for non-destructive compatibility,
    # but are deliberately not parsed into any dashboard or LIVE execution path.
    strategy_policy = preset_policy("balanced_yield")
    strategy_overrides = {}
    strategy_auto_migrated = False
    strategy_v3 = strategy_v3_from_config(config)

    return Settings(
        api_key=api_key,
        api_secret=api_secret,
        currencies=currencies,
        sleep_active=float(args.sleeptimeactive or get_option(config, "BOT", "sleeptimeactive", "60")),
        sleep_inactive=float(args.sleeptimeinactive or get_option(config, "BOT", "sleeptimeinactive", "300")),
        min_daily_rate=Decimal(args.mindailyrate or get_option(config, "BOT", "mindailyrate", "0.04")) / Decimal("100"),
        max_daily_rate=Decimal(args.maxdailyrate or get_option(config, "BOT", "maxdailyrate", "2")) / Decimal("100"),
        spread_lend=int(args.spreadlend or get_option(config, "BOT", "spreadlend", "3")),
        gap_bottom=Decimal(args.gapbottom or get_option(config, "BOT", "gapbottom", "10")),
        gap_top=Decimal(args.gaptop or get_option(config, "BOT", "gaptop", "200")),
        xday_threshold=Decimal(args.xdaythreshold or get_option(config, "BOT", "xdaythreshold", "0.2")) / Decimal("100"),
        xdays=int(args.xdays or get_option(config, "BOT", "xdays", "60")),
        min_loan_size=min_loan_size,
        max_to_lend=Decimal(args.maxtolent or get_option(config, "BOT", "maxtolent", "0")),
        max_percent_to_lend=Decimal(args.maxpercenttolent or get_option(config, "BOT", "maxpercenttolent", "0")) / Decimal("100"),
        max_to_lend_rate=Decimal(args.maxtolentrate or get_option(config, "BOT", "maxtolentrate", "0")) / Decimal("100"),
        coin_cfg=coin_cfg,
        transferable_currencies=split_csv(
            args.transferablecurrencies or get_option(config, "BOT", "transferablecurrencies", "")
        ),
        transfer_from_wallets=split_csv(get_option(config, "BOT", "transferfromwallets", "exchange,margin")),
        output_currency=(args.outputcurrency or get_option(config, "BOT", "outputcurrency", "USD")).upper(),
        platform_fee_rate=Decimal(get_option(config, "BOT", "platformfeerate", "15")),
        json_file=args.jsonfile or get_option(config, "BOT", "jsonfile", ""),
        json_log_size=int(args.jsonlogsize or get_option(config, "BOT", "jsonlogsize", "-1")),
        web_server=(args.startwebserver or config.getboolean("BOT", "startwebserver", fallback=False)) and not args.no_server,
        once=args.once,
        smart_strategy=get_boolean(config, "BOT", "smartstrategy", True),
        smart_rate_offset=get_decimal_percent(config, "BOT", "smartrateoffset", "0.001"),
        smart_fast_depth=get_decimal(config, "BOT", "smartfastdepth", "5"),
        smart_balanced_depth=get_decimal(config, "BOT", "smartbalanceddepth", "150"),
        smart_opportunity_depth=get_decimal(config, "BOT", "smartopportunitydepth", "300"),
        smart_opportunity_premium=get_decimal_percent(config, "BOT", "smartopportunitypremium", "0.01"),
        smart_fast_share=get_decimal(config, "BOT", "smartfastshare", "50"),
        smart_long_share=get_decimal(config, "BOT", "smartlongshare", "40"),
        smart_floor_depth=get_decimal(config, "BOT", "smartfloordepth", "2"),
        smart_long_period=get_boolean(config, "BOT", "smartlongperiod", True),
        smart_long_wait_minutes=get_decimal(config, "BOT", "smartlongwaitminutes", "120"),
        reprice_stale_offers=get_boolean(config, "BOT", "repricestaleoffers", True),
        reprice_after_minutes=get_decimal(config, "BOT", "repriceafterminutes", "15"),
        reprice_min_rate_delta=get_decimal_percent(config, "BOT", "repriceminratedelta", "0.002"),
        strategy_policy=strategy_policy,
        strategy_overrides=strategy_overrides,
        strategy_auto_migrated=strategy_auto_migrated,
        managed_offer_state_file=get_option(config, "BOT", "managedofferstatefile", DEFAULT_MANAGED_OFFER_STATE),
        strategy_v3=strategy_v3,
        state_db_file=get_option(config, "BOT", "statedbfile", DEFAULT_V3_STATE_DB),
    )


def validate_settings(settings):
    if settings.sleep_active < 1 or settings.sleep_active > 3600:
        raise ConfigError("sleeptimeactive must be 1-3600")
    if settings.sleep_inactive < 1 or settings.sleep_inactive > 3600:
        raise ConfigError("sleeptimeinactive must be 1-3600")
    if settings.min_daily_rate < Decimal("0.00003") or settings.min_daily_rate > Decimal("0.05"):
        raise ConfigError("mindailyrate must be 0.003-5 percent")
    if settings.max_daily_rate < Decimal("0.00003") or settings.max_daily_rate > Decimal("0.05"):
        raise ConfigError("maxdailyrate must be 0.003-5 percent")
    if settings.min_daily_rate > settings.max_daily_rate:
        raise ConfigError("mindailyrate cannot be higher than maxdailyrate")
    if settings.spread_lend < 1 or settings.spread_lend > 20:
        raise ConfigError("spreadlend must be 1-20")
    if settings.gap_bottom < 0 or settings.gap_top < settings.gap_bottom:
        raise ConfigError("gaptop must be greater than or equal to gapbottom")
    if settings.xdays < 2 or settings.xdays > 120:
        raise ConfigError("xdays must be 2-120 for Bitfinex funding")
    if settings.min_loan_size <= 0:
        raise ConfigError("minloansize must be positive")
    if settings.max_to_lend < 0:
        raise ConfigError("maxtolent cannot be negative")
    if settings.max_percent_to_lend < 0 or settings.max_percent_to_lend > 1:
        raise ConfigError("maxpercenttolent must be 0-100 percent")
    if settings.max_to_lend_rate < 0 or settings.max_to_lend_rate > Decimal("0.05"):
        raise ConfigError("maxtolentrate must be 0-5 percent")
    if settings.platform_fee_rate < 0 or settings.platform_fee_rate > 100:
        raise ConfigError("platformfeerate must be 0-100 percent")
    unknown_transfer_currencies = set(settings.transferable_currencies) - set(settings.currencies)
    if unknown_transfer_currencies:
        raise ConfigError("transferablecurrencies must be included in currencies")
    for currency, coin in settings.coin_cfg.items():
        if coin.max_to_lend < 0:
            raise ConfigError(f"{currency} maxtolent cannot be negative")
        if coin.max_percent_to_lend < 0 or coin.max_percent_to_lend > 1:
            raise ConfigError(f"{currency} maxpercenttolent must be 0-100 percent")
        if coin.max_to_lend_rate < 0 or coin.max_to_lend_rate > Decimal("0.05"):
            raise ConfigError(f"{currency} maxtolentrate must be 0-5 percent")
    if settings.smart_rate_offset < 0 or settings.smart_rate_offset > Decimal("0.05"):
        raise ConfigError("smartrateoffset must be 0-5 percent")
    if settings.smart_opportunity_premium < 0 or settings.smart_opportunity_premium > Decimal("0.05"):
        raise ConfigError("smartopportunitypremium must be 0-5 percent")
    if settings.smart_fast_share < 0 or settings.smart_fast_share > 100:
        raise ConfigError("smartfastshare must be 0-100 percent")
    if settings.smart_long_share < 0 or settings.smart_long_share > 100:
        raise ConfigError("smartlongshare must be 0-100 percent")
    if settings.smart_fast_share + settings.smart_long_share > 100:
        raise ConfigError("smartfastshare plus smartlongshare cannot exceed 100 percent")
    for name, value in (
        ("smartfastdepth", settings.smart_fast_depth),
        ("smartbalanceddepth", settings.smart_balanced_depth),
        ("smartopportunitydepth", settings.smart_opportunity_depth),
    ):
        if value < 0 or value > Decimal("10000"):
            raise ConfigError(f"{name} must be 0-10000")
    if settings.smart_floor_depth < 0 or settings.smart_floor_depth > Decimal("100"):
        raise ConfigError("smartfloordepth must be 0-100 percent")
    if settings.smart_long_wait_minutes < 1 or settings.smart_long_wait_minutes > Decimal("1440"):
        raise ConfigError("smartlongwaitminutes must be 1-1440")
    if settings.reprice_after_minutes < 1 or settings.reprice_after_minutes > Decimal("1440"):
        raise ConfigError("repriceafterminutes must be 1-1440")
    if settings.reprice_min_rate_delta < 0 or settings.reprice_min_rate_delta > Decimal("0.05"):
        raise ConfigError("repriceminratedelta must be 0-5 percent")
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


def strategy_policy_api_values(policy):
    payload = policy_to_json(policy)
    for field_name, (_, kind) in STRATEGY_CONFIG_FIELDS.items():
        if kind == "percent":
            payload[field_name] = decimal_percent_to_config(getattr(policy, field_name))
        elif isinstance(getattr(policy, field_name), Decimal):
            payload[field_name] = decimal_to_config(getattr(policy, field_name))
    payload["balanced_share"] = decimal_to_config(policy.balanced_share)
    return payload


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
            "sleeptimeactive": str(int(settings.sleep_active) if settings.sleep_active.is_integer() else settings.sleep_active),
            "sleeptimeinactive": str(int(settings.sleep_inactive) if settings.sleep_inactive.is_integer() else settings.sleep_inactive),
            "platformfeerate": decimal_to_config(settings.platform_fee_rate),
            "transferablecurrencies": ",".join(settings.transferable_currencies),
            "transferfromwallets": ",".join(settings.transfer_from_wallets).lower(),
            "outputcurrency": settings.output_currency,
            "jsonfile": settings.json_file or DEFAULT_DASHBOARD_JSON.replace("\\", "/"),
            "jsonlogsize": str(settings.json_log_size if settings.json_log_size != -1 else 200),
            "startwebserver": str(settings.web_server).lower(),
        },
        "strategyV3": strategy_v3_api_values(active_policy),
        "strategyV3Draft": None if store.strategy("DRAFT") is None else strategy_v3_api_values(strategy_v3_from_record(store.strategy("DRAFT"))),
        "strategyV3Pending": None if store.strategy("PENDING") is None else strategy_v3_api_values(strategy_v3_from_record(store.strategy("PENDING"))),
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


def normalize_dashboard_updates(payload):
    bitfinex = payload.get("bitfinex", {})
    bot = payload.get("bot", {})
    updates = {"BITFINEX": {}, "BOT": {}}
    for key, value in bitfinex.items():
        lowered = key.lower()
        if lowered in DASHBOARD_BITFINEX_FIELDS:
            updates["BITFINEX"][lowered] = str(value).strip().upper()
    for key, value in bot.items():
        lowered = key.lower()
        if lowered in DASHBOARD_BOT_FIELDS:
            updates["BOT"][lowered] = str(value).strip()
    strategy = payload.get("strategy")
    if isinstance(strategy, dict):
        global_policy = strategy.get("global", {})
        if isinstance(global_policy, dict):
            updates["BOT"]["strategyversion"] = "2"
            if "profile" in global_policy:
                updates["BOT"]["strategyprofile"] = str(global_policy["profile"]).strip().lower()
            if "auto_order_types" in global_policy:
                updates["BOT"]["strategyautotypes"] = str(global_policy["auto_order_types"]).lower()
            if "replay_window" in global_policy:
                updates["BOT"]["strategyreplaywindow"] = str(global_policy["replay_window"]).strip().lower()
            for field_name, (option, _) in STRATEGY_CONFIG_FIELDS.items():
                if field_name in global_policy:
                    updates["BOT"][f"strategy{option}"] = str(global_policy[field_name]).strip()
        overrides = strategy.get("overrides", {})
        if isinstance(overrides, dict):
            for raw_currency, values in overrides.items():
                currency = str(raw_currency).strip().upper()
                if not currency or len(currency) > 12 or not currency.replace("_", "").isalnum():
                    continue
                if not isinstance(values, dict):
                    continue
                section = f"STRATEGY:{currency}"
                updates[section] = {"inherit": str(values.get("inherit", False)).lower()}
                if "profile" in values:
                    updates[section]["profile"] = str(values["profile"]).strip().lower()
                if "auto_order_types" in values:
                    updates[section]["autotypes"] = str(values["auto_order_types"]).lower()
                if "replay_window" in values:
                    updates[section]["replaywindow"] = str(values["replay_window"]).strip().lower()
                for field_name, (option, _) in STRATEGY_CONFIG_FIELDS.items():
                    if field_name in values:
                        updates[section][option] = str(values[field_name]).strip()
    strategy_v3 = payload.get("strategyV3")
    if isinstance(strategy_v3, dict):
        updates["STRATEGY_V3"] = {}
        for field_name in V3_CONFIG_FIELDS:
            if field_name not in strategy_v3:
                continue
            value = strategy_v3[field_name]
            if isinstance(value, (list, tuple)):
                value = ",".join(str(item) for item in value)
            updates["STRATEGY_V3"][field_name] = "" if value is None else str(value).strip()
    return updates


def merged_config_for_validation(config_path, updates):
    config, _ = read_config(config_path)
    for section, values in updates.items():
        if not config.has_section(section):
            config.add_section(section)
        for key, value in values.items():
            config.set(section, key, value)
    return config


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


def save_dashboard_config(config_path, payload):
    # V3 strategy writes must go through /api/strategy/v3/draft so a normal
    # settings save can never alter the effective live policy.
    sanitized = dict(payload or {})
    sanitized.pop("strategyV3", None)
    updates = normalize_dashboard_updates(sanitized)
    merged = merged_config_for_validation(config_path, updates)
    args = parse_args(["--config", config_path])
    settings = build_settings(args, merged)
    validate_settings(settings)
    update_config_file_preserving_comments(config_path, updates)
    return config_api_payload(config_path)


def strategy_preview(config_path, payload, client_factory=Bitfinex, now_ms=None):
    """Preview an unsaved strategy using public data only; never submits an offer."""
    strategy_payload = payload.get("strategy", {})
    updates = normalize_dashboard_updates({"strategy": strategy_payload})
    proposed_config = merged_config_for_validation(config_path, updates)
    saved_config, _ = read_config(config_path)
    args = parse_args(["--config", config_path])
    proposed_settings = build_settings(args, proposed_config)
    saved_settings = build_settings(args, saved_config)
    validate_settings(proposed_settings)
    validate_settings(saved_settings)

    requested_currency = str(payload.get("currency") or proposed_settings.currencies[0]).strip().upper()
    if requested_currency not in proposed_settings.currencies:
        raise ConfigError("预览币种不在当前 currencies 配置中")
    policy = strategy_policy_for(proposed_settings, requested_currency)
    window = str(payload.get("window") or policy.replay_window).strip().lower()
    policy = policy_with_overrides(policy, {"replay_window": window})
    try:
        validate_policy(policy)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if requested_currency in proposed_settings.strategy_overrides:
        proposed_settings.strategy_overrides[requested_currency] = policy
    else:
        proposed_settings.strategy_policy = policy

    principal_source = "example"
    principal_value = payload.get("principal")
    if principal_value in (None, ""):
        principal = Decimal("1000")
    else:
        principal = Decimal(str(principal_value))
        principal_source = "user"
    if principal <= 0:
        raise ConfigError("示例本金必须大于 0")

    # Empty credentials intentionally guarantee that this endpoint can only use public APIs.
    client = client_factory("", "")
    public_warnings = []
    try:
        book = parse_funding_book(client.funding_book(currency_to_symbol(requested_currency), 250))
    except Exception as exc:
        book = []
        public_warnings.append(f"资金盘口暂不可用，使用最低利率作为固定盘口回退：{exc}")
    signals, trades, stats = load_public_market_signals(
        client,
        proposed_settings,
        requested_currency,
        book,
        principal,
        window,
        now_ms,
    )
    public_warnings.extend(signals.get("warnings", []))
    minimum = min_loan_size_for(proposed_settings, requested_currency)
    plan = build_strategy_plan(
        principal,
        minimum,
        min_rate_for(proposed_settings, requested_currency),
        proposed_settings.max_daily_rate,
        policy,
        signals,
        max_parts=3,
    )
    replay = replay_strategy(
        policy,
        trades,
        stats,
        principal,
        minimum,
        min_rate_for(proposed_settings, requested_currency),
        proposed_settings.max_daily_rate,
        window,
        now_ms,
    )

    saved_policy = strategy_policy_for(saved_settings, requested_currency)
    saved_replay = replay_strategy(
        saved_policy,
        trades,
        stats,
        principal,
        min_loan_size_for(saved_settings, requested_currency),
        min_rate_for(saved_settings, requested_currency),
        saved_settings.max_daily_rate,
        window,
        now_ms,
    )
    return {
        "currency": requested_currency,
        "principal": status_decimal(principal),
        "principalSource": principal_source,
        "signals": signals_to_json(signals),
        "regime": signals["regime"],
        "plan": plan_to_json(plan),
        "replay": replay,
        "comparison": {
            "savedProfile": saved_policy.profile,
            "draftProfile": policy.profile,
            "savedReplay": saved_replay,
        },
        "warnings": list(dict.fromkeys(str(item) for item in public_warnings if item)),
    }


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
            wallets = [{
                "wallet_type": "funding", "currency": "USD",
                "balance": Decimal(sample["wallet_available"]),
                "available": Decimal(sample["wallet_available"]),
                "unsettled_interest": Decimal("0"),
            }]
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
        trades = parse_funding_trades(client.funding_trades(
            "fUSD", start=now_ms - 7 * 86_400_000, end=now_ms, limit=10000, sort=1
        ))
    except Exception as exc:
        trades = []
        warnings.append(f"Funding Trades 不可用：{exc}")
    try:
        stats = parse_funding_stats(client.funding_stats(
            "fUSD", start=now_ms - 7 * 86_400_000, end=now_ms, limit=250
        ))
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
            for row in sorted(rows, key=lambda item: int(item.get("id") or item.get("offer_id") or item.get("credit_id") or 0))
        ]
    return _canonical_sha256({
        "account": account,
        "offers": compact(snapshot.get("offers", []), ("id", "amount", "rate", "rate_real", "period", "display_type", "flags", "managed")),
        "credits": compact(snapshot.get("credits", []), ("id", "amount", "rate", "rate_real", "period", "display_type", "hidden", "managed")),
    })


def _token_expiry_iso(expires_at):
    return datetime.datetime.fromtimestamp(expires_at, datetime.timezone.utc).isoformat(timespec="seconds")


def strategy_v3_preview(config_path, payload, client_factory=Bitfinex, now_ms=None, issue_token=True):
    store, settings = v3_store_for_config(config_path)
    active, active_policy = ensure_active_strategy_v3(store, settings)
    policy = strategy_v3_from_api_payload(payload.get("strategyV3", {}), base=active_policy)
    proposed_version = active["version_id"] if strategy_v3_semantically_equal(policy, active_policy) else strategy_v3_version_id(policy)
    now = int(now_ms if now_ms is not None else time.time() * 1000)
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
        issued_at = time.time()
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


def runtime_v3_payload(config_path):
    store, settings = v3_store_for_config(config_path)
    market_snapshot = None
    if dashboard_v3_hub is not None:
        market_snapshot = json_decimal(dashboard_v3_hub.snapshot())
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
        "process": controlled_bot_status(config_path),
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


def save_strategy_v3_draft(config_path, payload):
    store, settings = v3_store_for_config(config_path)
    active, active_policy = ensure_active_strategy_v3(store, settings)
    policy = strategy_v3_from_api_payload(payload.get("strategyV3", {}), base=active_policy)
    token = str(payload.get("previewToken") or "")
    with strategy_token_lock:
        context = strategy_preview_tokens.pop(token, None)
    if not context or time.time() > context["expiresAt"]:
        raise ApiRequestError("策略预览已过期，请重新计算", "PREVIEW_STALE", 409)
    if context["buildId"] != dashboard_build_id() or context["configPath"] != os.path.abspath(config_path):
        raise ApiRequestError("Dashboard build 或配置路径已变化，请重新计算", "PREVIEW_STALE", 409)
    if active["version_id"] != context["activeVersion"] or _canonical_sha256(policy.__dict__) != context["policyHash"]:
        raise ApiRequestError("ACTIVE 或拟议策略已变化，请重新计算", "PREVIEW_STALE", 409)
    if strategy_v3_semantically_equal(policy, active_policy):
        return {"versionId": active["version_id"], "draftVersionId": active["version_id"], "status": "UNCHANGED", "strategy": active, "diff": []}
    version_id = store.save_strategy(json_decimal(policy.__dict__), status="DRAFT")
    if version_id == active["version_id"]:
        return {"versionId": version_id, "draftVersionId": version_id, "status": "UNCHANGED", "strategy": active, "diff": []}
    draft = store.strategy("DRAFT")
    apply_token = secrets.token_urlsafe(24)
    apply_context = {**context, "token": apply_token, "draftVersionId": version_id, "expiresAt": time.time() + PREFLIGHT_TTL_SECONDS}
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


def apply_strategy_v3_draft(config_path, payload, client_factory=Bitfinex):
    store, settings = v3_store_for_config(config_path)
    active, _ = ensure_active_strategy_v3(store, settings)
    draft = store.strategy("DRAFT")
    if draft is None:
        return {"status": "UNCHANGED", "strategy": active, "versionId": active["version_id"]}
    draft_version = str(payload.get("draftVersionId") or "")
    token = str(payload.get("applyToken") or "")
    with strategy_token_lock:
        context = strategy_apply_tokens.pop(token, None)
    if not context or time.time() > context["expiresAt"]:
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
    )
    if refreshed["accountDigest"] != context["accountDigest"] or refreshed["plan"]["plan_hash"] != context["planHash"]:
        raise ApiRequestError(
            "账户或计划已变化，请检查新预览后再次确认",
            "PREVIEW_STALE",
            409,
            details={"preview": refreshed},
        )
    if controlled_bot_running(config_path) or store.runtime()["mode"] == "LIVE":
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


def replay_v3_from_store(config_path, now_ms=None):
    store, settings = v3_store_for_config(config_path)
    if store.runtime()["mode"] == "LIVE" or controlled_bot_running(config_path):
        raise ConfigError("必须先暂停 LIVE 才能进入 REPLAY")
    store.set_mode("REPLAY", "dashboard_replay")
    now = int(now_ms if now_ms is not None else time.time() * 1000)
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
    if not isinstance(payload, dict) or payload.get("schemaVersion") != STATUS_SCHEMA_VERSION or payload.get("operationMode") not in valid_modes:
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


def dashboard_status_payload(status_path, config_path):
    payload = read_status_payload(status_path)
    try:
        store, _ = v3_store_for_config(config_path)
        runtime = store.runtime()
        process = controlled_bot_status(config_path)
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


def reconcile_orphaned_live_runtime(config_path):
    store, _ = v3_store_for_config(config_path)
    external = external_live_process(config_path)
    if external and external.get("buildMismatch"):
        if external.get("stateError"):
            store.enter_safe("WORKER_BUILD_MISMATCH_UNVERIFIED", manual=True)
            return store.runtime()
        identity_error = _live_process_identity_error(external)
        if identity_error:
            store.enter_safe("WORKER_BUILD_MISMATCH_UNVERIFIED", manual=True)
            return store.runtime()
        stop_controlled_bot(config_path, reason="worker_build_mismatch")
        store.set_mode("PAUSED", "worker_build_mismatch_stopped")
        return store.runtime()
    runtime = store.runtime()
    if runtime["mode"] == "LIVE" and not controlled_bot_running(config_path):
        return store.set_mode("PAUSED", "dashboard_started_without_live_process")
    return runtime


controlled_bot_process = None
controlled_bot_started_at = None
controlled_bot_log_handle = None
controlled_bot_stop_reason = None
controlled_bot_preflight = None
controlled_bot_lock = threading.RLock()


def cleanup_controlled_bot_handle():
    global controlled_bot_log_handle
    if controlled_bot_log_handle is not None:
        try:
            controlled_bot_log_handle.close()
        except Exception:
            pass
        controlled_bot_log_handle = None


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
                "$p=Get-CimInstance Win32_Process -Filter \"ProcessId=%d\"; "
                "if($p){[pscustomobject]@{ExecutablePath=$p.ExecutablePath;CommandLine=$p.CommandLine}|ConvertTo-Json -Compress}"
            ) % pid
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True, text=True, timeout=5, check=False,
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
    if recorded_executable and executable and os.path.normcase(os.path.abspath(recorded_executable)) != os.path.normcase(os.path.abspath(executable)):
        return "实盘锁记录的 Python 可执行文件与实际进程不一致"
    return None


def external_live_process(config_path=DEFAULT_CONFIG):
    inspection = LiveProcessLock.inspect(DEFAULT_LIVE_LOCK)
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


def controlled_bot_running(config_path=DEFAULT_CONFIG):
    internal = controlled_bot_process is not None and controlled_bot_process.poll() is None
    return internal or external_live_process(config_path) is not None


def controlled_bot_status(config_path=DEFAULT_CONFIG):
    global controlled_bot_process
    with controlled_bot_lock:
        internal_running = controlled_bot_process is not None and controlled_bot_process.poll() is None
        external = None if internal_running else external_live_process(config_path)
        running = internal_running or external is not None
        return_code = None
        if controlled_bot_process is not None:
            return_code = controlled_bot_process.poll()
        if controlled_bot_process is not None and return_code is not None:
            cleanup_controlled_bot_handle()
        return {
            "running": running,
            "pid": controlled_bot_process.pid if internal_running else (external or {}).get("pid"),
            "startedAt": controlled_bot_started_at if internal_running else (external or {}).get("startedAt"),
            "returnCode": return_code,
            "stopReason": controlled_bot_stop_reason,
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


def evaluate_live_preflight(config_path, client_factory=Bitfinex):
    """Perform the single V3 preflight used by both dashboard and live child."""
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
        warnings.append({
            "code": "CONFIG_MIRROR_DIFFERS",
            "message": "配置文件中的 V3 镜像与 SQLite ACTIVE 不一致；本次预检和实盘只使用 ACTIVE。",
        })
    add_check("v3_usd_only", "V3 币种范围", settings.currencies == ["USD"], "V3 仅允许 currencies = USD")
    add_check(
        "v3_websocket_dependency", "WebSocket 运行库", websocket_dependency_available(),
        "websockets 已安装" if websocket_dependency_available() else "请先安装 websockets 依赖",
    )

    client = client_factory(settings.api_key, settings.api_secret)
    if not client.has_credentials():
        add_check("credentials", "API 凭据", False, "未配置有效的 Bitfinex API key/secret")
        return {"checks": checks, "warnings": warnings, "summary": {"strategyVersion": 3, "activeStrategyVersion": active["version_id"]}}
    try:
        permissions = parse_key_permissions(client.key_permissions())
        add_check("credentials", "API 凭据", True, "凭据有效，权限接口可访问")
    except Exception as exc:
        add_check("credentials", "API 凭据", False, f"权限读取失败：{exc}")
        return {"checks": checks, "warnings": warnings, "summary": {"strategyVersion": 3, "activeStrategyVersion": active["version_id"]}}

    wallets_read = permission_enabled(permissions, "wallets", "read")
    funding_read = permission_enabled(permissions, "funding", "read")
    funding_write = permission_enabled(permissions, "funding", "write")
    add_check("wallets_read", "钱包读取权限", wallets_read, "wallets 需要读取权限")
    add_check("funding_read", "放贷读取权限", funding_read, "funding 需要读取权限")
    add_check("funding_write", "放贷写入权限", funding_write, "funding 需要写入权限以挂单和撤单")
    if settings.transferable_currencies:
        add_check("wallets_write", "钱包转账权限", permission_enabled(permissions, "wallets", "write"), "启用自动转入时 wallets 需要写权限")
    add_check("withdraw_disabled", "API 提现权限", not permission_enabled(permissions, "withdraw", "write"), "withdraw 写权限必须关闭")
    add_check("ui_withdraw_disabled", "界面提现权限", not permission_enabled(permissions, "ui_withdraw", "write"), "ui_withdraw 写权限必须关闭")

    now = int(time.time() * 1000)
    account, snapshot, basis = load_v3_account_context(client, store, now)
    add_check(
        "account_snapshot", "真实账户快照", basis["source"] == "REAL_ACCOUNT",
        "已读取实时 Funding 钱包、挂单和贷款" if basis["source"] == "REAL_ACCOUNT" else "实时账户读取失败，不能使用历史快照启动实盘",
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
        warnings.append({"code": "NO_AVAILABLE_BALANCE", "message": "USD 当前没有可用资金；仍可先撤销不兼容的机器人挂单。"})
    elif account["wallet"] < policy.min_order_amount:
        warnings.append({"code": "BELOW_MINIMUM", "message": "USD 可用余额低于 V3 最低单笔金额。"})
    if incompatible_managed:
        warnings.append({"code": "INCOMPATIBLE_MANAGED_OFFERS", "message": f"启动后将先撤销 {len(incompatible_managed)} 笔不兼容机器人挂单，确认消失后才创建新单。"})
    if incompatible_external:
        warnings.append({"code": "INCOMPATIBLE_EXTERNAL_OFFERS", "message": f"发现 {len(incompatible_external)} 笔外部挂单不符合策略；机器人不会撤销，但会计入资金上限。"})
    if incompatible_credits:
        warnings.append({"code": "INCOMPATIBLE_ACTIVE_CREDITS", "message": f"发现 {len(incompatible_credits)} 笔已成交贷款不符合新策略；无法撤销，只会阻止继续创建同类订单。"})
    if result["over_cap"]:
        warnings.append({"code": "FUNDING_CAP_EXCEEDED", "message": "当前账户敞口超过 V3 资金上限；不会创建新单，只能撤销机器人挂单。"})

    enabled_types = [
        label for label, enabled in (
            ("LIMIT", policy.enable_limit),
            ("FRR", policy.enable_frr),
            ("FRR_DELTA_FIXED", policy.enable_frr_delta_fixed),
            ("FRR_DELTA_VARIABLE", policy.enable_frr_delta_variable),
        ) if enabled
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
            } for pool in ("short", "medium", "long")
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


def create_controlled_bot_preflight(config_path, client_factory=Bitfinex, now=None):
    global controlled_bot_preflight
    with controlled_bot_lock:
        if controlled_bot_running(config_path):
            raise ConfigError("机器人已在运行")
    read_config(config_path)
    digest_before = config_sha256(config_path)
    result = evaluate_live_preflight(config_path, client_factory=client_factory)
    digest_after = config_sha256(config_path)
    if digest_before != digest_after:
        result["checks"].append({
            "id": "config_changed",
            "label": "策略配置稳定性",
            "status": "fail",
            "detail": "策略配置在预检过程中发生变化，请重新运行预检",
        })
    can_start = bool(result["checks"]) and all(check["status"] == "pass" for check in result["checks"])
    issued_at = time.time() if now is None else float(now)
    expires_at = issued_at + PREFLIGHT_TTL_SECONDS
    preflight_id = secrets.token_urlsafe(24) if can_start else None
    response = {
        "preflightId": preflight_id,
        "expiresAt": datetime.datetime.fromtimestamp(expires_at, datetime.timezone.utc).isoformat(timespec="seconds"),
        "canStart": can_start,
        **result,
    }
    with controlled_bot_lock:
        controlled_bot_preflight = None
        if controlled_bot_running(config_path):
            response["canStart"] = False
            response["preflightId"] = None
            response["checks"].append({"id": "process", "label": "机器人进程", "status": "fail", "detail": "机器人已在运行"})
        elif can_start:
            summary = result.get("summary", {})
            controlled_bot_preflight = {
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


def consume_controlled_bot_preflight(config_path, preflight_id, now=None):
    global controlled_bot_preflight
    current = controlled_bot_preflight
    controlled_bot_preflight = None
    if not current or not preflight_id or current["preflightId"] != preflight_id:
        raise ConfigError("预检令牌无效或已使用，请重新运行预检")
    checked_at = time.time() if now is None else float(now)
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
    refreshed = evaluate_live_preflight(config_path, client_factory=current.get("clientFactory") or Bitfinex)
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


def start_controlled_bot(config_path, status_path, preflight_id):
    global controlled_bot_process, controlled_bot_started_at
    global controlled_bot_log_handle, controlled_bot_stop_reason
    with controlled_bot_lock:
        if controlled_bot_running(config_path):
            raise ConfigError("机器人已在运行")
        consume_controlled_bot_preflight(config_path, preflight_id)
        os.makedirs(os.path.dirname(status_path) or ".", exist_ok=True)
        os.makedirs(os.path.dirname(DEFAULT_PROCESS_LOG) or ".", exist_ok=True)

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
        cleanup_controlled_bot_handle()
        controlled_bot_log_handle = open(DEFAULT_PROCESS_LOG, "ab", buffering=0)
        try:
            controlled_bot_process = subprocess.Popen(
                command,
                cwd=os.getcwd(),
                stdin=subprocess.DEVNULL,
                stdout=controlled_bot_log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                startupinfo=startupinfo,
            )
        except Exception:
            cleanup_controlled_bot_handle()
            raise
        controlled_bot_started_at = timestamp()
        controlled_bot_stop_reason = None
        return controlled_bot_status(config_path)


def stop_controlled_bot(config_path=DEFAULT_CONFIG, reason="stopped_by_dashboard"):
    global controlled_bot_process, controlled_bot_stop_reason, controlled_bot_preflight
    with controlled_bot_lock:
        controlled_bot_preflight = None
        internal_running = controlled_bot_process is not None and controlled_bot_process.poll() is None
        external = None if internal_running else external_live_process(config_path)
        if not internal_running and external is None:
            cleanup_controlled_bot_handle()
            return controlled_bot_status(config_path)
        if external is not None:
            if external.get("stateError"):
                raise ConfigError(external["stateError"])
            identity_error = _live_process_identity_error(external)
            if identity_error:
                raise ConfigError(identity_error)
            pid = int(external["pid"])
            os.kill(pid, signal.SIGTERM)
            deadline = time.time() + 5
            while time.time() < deadline and external_live_process(config_path) is not None:
                time.sleep(0.05)
            if external_live_process(config_path) is not None:
                raise ConfigError("实盘进程未在超时时间内释放单实例锁")
            controlled_bot_stop_reason = reason
            return controlled_bot_status(config_path)
        controlled_bot_process.terminate()
        try:
            controlled_bot_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            controlled_bot_process.kill()
            controlled_bot_process.wait(timeout=5)
        cleanup_controlled_bot_handle()
        controlled_bot_stop_reason = reason
        return controlled_bot_status(config_path)


def format_decimal(value):
    value = Decimal(value).quantize(SATOSHI, rounding=ROUND_DOWN)
    return format(value, "f")


def format_rate(value):
    return format(Decimal(value).quantize(SATOSHI, rounding=ROUND_DOWN), "f")


def min_loan_size_for(settings, currency):
    coin = settings.coin_cfg.get(currency)
    if coin and coin.min_loan_size is not None:
        return coin.min_loan_size
    return settings.min_loan_size


def min_rate_for(settings, currency):
    coin = settings.coin_cfg.get(currency)
    if coin:
        return coin.min_rate
    return settings.min_daily_rate


def enabled_for(settings, currency):
    coin = settings.coin_cfg.get(currency)
    return True if coin is None else coin.enabled


def max_lending_limits(settings, currency):
    coin = settings.coin_cfg.get(currency)
    if coin:
        return coin.max_to_lend, coin.max_percent_to_lend, coin.max_to_lend_rate
    return settings.max_to_lend, settings.max_percent_to_lend, settings.max_to_lend_rate


def parse_wallets(wallets):
    balances = {}
    by_wallet = {}
    for wallet in wallets:
        if len(wallet) < 5:
            continue
        wallet_type = str(wallet[0]).lower()
        currency = str(wallet[1]).upper()
        balance = decimal_from_api(wallet[2])
        available = wallet[4]
        available_balance = balance if available is None else decimal_from_api(available)
        by_wallet[(wallet_type, currency)] = available_balance
        if wallet_type == "funding":
            balances[currency] = available_balance
    return balances, by_wallet


def fetch_wallet_state(client, settings, log):
    if not client.has_credentials():
        raise ConfigError("实盘运行需要配置 Bitfinex API key/secret")
    return parse_wallets(client.wallets())


def parse_open_offers(rows, registry=None):
    offers = {}
    for row in rows:
        if len(row) < 16:
            continue
        currency = symbol_to_currency(str(row[1])).upper()
        amount = abs(decimal_from_api(row[4]))
        rate = decimal_from_api(row[14])
        period = int(row[15])
        offer_id = row[0]
        offer_type = str(row[6] or "LIMIT").upper()
        metadata = registry.metadata(offer_id) if registry is not None else {}
        mts_created = int(row[2]) if row[2] is not None else 0
        mts_updated = int(row[3]) if row[3] is not None else 0
        offers.setdefault(currency, []).append(
            {
                "id": offer_id,
                "created": mts_created,
                "updated": mts_updated,
                "amount": amount,
                "rate": rate,
                "period": period,
                "offer_type": offer_type,
                "bucket": metadata.get("bucket", ""),
                "managed_by_bot": registry.is_managed(offer_id) if registry is not None else False,
                "status": str(row[10] or "") if len(row) > 10 else "",
            }
        )
    return offers


def serialize_open_offers(open_offers):
    serialized = []
    for currency, offers in open_offers.items():
        for offer in offers:
            serialized.append({
                "id": str(offer["id"]),
                "currency": currency,
                "created": int(offer.get("created") or 0),
                "updated": int(offer.get("updated") or 0),
                "amount": status_decimal(offer["amount"]),
                "rate": status_decimal(offer["rate"]),
                "dailyRatePercent": decimal_percent_to_config(offer["rate"]),
                "period": int(offer["period"]),
                "offerType": offer.get("offer_type", "LIMIT"),
                "bucket": offer.get("bucket", ""),
                "managedByBot": bool(offer.get("managed_by_bot", False)),
                "status": offer.get("status", ""),
            })
    return sorted(serialized, key=lambda item: (item["currency"], item["created"], item["id"]))


def fetch_open_offers(client, settings, log, registry=None):
    if not client.has_credentials():
        return {}
    all_offers = {}
    for currency in settings.currencies:
        rows = client.active_funding_offers(currency_to_symbol(currency))
        for cur, offers in parse_open_offers(rows, registry).items():
            all_offers.setdefault(cur, []).extend(offers)
    if registry is not None:
        registry.reconcile(offer["id"] for offers in all_offers.values() for offer in offers)
    return all_offers


def parse_active_funding(rows):
    totals = {}
    weighted_rates = {}
    for row in rows:
        if len(row) < 13:
            continue
        side = int(row[2])
        if side < 0:
            continue
        currency = symbol_to_currency(str(row[1])).upper()
        amount = abs(decimal_from_api(row[5]))
        rate = decimal_from_api(row[11])
        totals[currency] = totals.get(currency, Decimal("0")) + amount
        weighted_rates[currency] = weighted_rates.get(currency, Decimal("0")) + (amount * rate)
    return totals, weighted_rates


def fetch_active_funding(client, settings, log):
    totals = {}
    weighted_rates = {}
    if not client.has_credentials():
        return totals, weighted_rates
    for currency in settings.currencies:
        symbol = currency_to_symbol(currency)
        rows = []
        for fetcher in (client.active_funding_loans, client.active_funding_credits):
            rows.extend(fetcher(symbol))
        cur_totals, cur_rates = parse_active_funding(rows)
        for cur, amount in cur_totals.items():
            totals[cur] = totals.get(cur, Decimal("0")) + amount
        for cur, rate_amount in cur_rates.items():
            weighted_rates[cur] = weighted_rates.get(cur, Decimal("0")) + rate_amount
    return totals, weighted_rates


def stringify_total_lended(totals, weighted_rates, log):
    result = "已放贷："
    for currency in sorted(totals):
        if totals[currency] <= 0:
            continue
        average_rate = weighted_rates[currency] * Decimal("100") / totals[currency]
        result += f"[{totals[currency]:.4f} {currency} @ {average_rate:.4f}%] "
        log.updateStatusValue(currency, "lentSum", totals[currency])
        log.updateStatusValue(currency, "averageLendingRate", average_rate)
    return result


def parse_funding_book(rows):
    offers = []
    for row in rows:
        if len(row) < 4:
            continue
        amount = decimal_from_api(row[3])
        if amount <= 0:
            continue
        offers.append(
            {
                "rate": decimal_from_api(row[0]),
                "period": int(row[1]),
                "amount": amount,
            }
        )
    return sorted(offers, key=lambda item: (item["rate"], item["period"]))


def fetch_book(client, settings, currency, log):
    symbol = currency_to_symbol(currency)
    return parse_funding_book(client.funding_book(symbol, 250))


def adaptive_book_reference(settings, currency, book, principal, policy=None):
    if not book:
        return min_rate_for(settings, currency)
    resolved = policy or strategy_policy_for(settings, currency)
    depth = max(
        min_loan_size_for(settings, currency),
        Decimal(principal) * resolved.floor_depth / Decimal("100"),
    )
    return book_rate_at_depth(book, depth)


def strategy_bucket_book_rates(settings, currency, book, principal, policy):
    minimum = min_loan_size_for(settings, currency)
    principal = max(Decimal(principal), minimum)
    return {
        f"{bucket}_book_rate": book_rate_at_depth(
            book,
            max(minimum, principal * getattr(policy, f"{bucket}_depth") / Decimal("100")),
        )
        for bucket in BUCKETS
    }


def load_public_market_signals(client, settings, currency, book, principal, window=None, now_ms=None):
    policy = strategy_policy_for(settings, currency)
    selected_window = window or policy.replay_window
    window_ms = WINDOW_MS.get(selected_window, WINDOW_MS["7d"])
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    start = now - max(window_ms, WINDOW_MS["7d"])
    symbol = currency_to_symbol(currency)
    raw_trades, trades_stale, trades_warning = market_data_cache.get(
        ("trades", symbol, selected_window),
        300,
        lambda: client.funding_trades(symbol, start=start, end=now, limit=10000, sort=1),
    )
    raw_stats, stats_stale, stats_warning = market_data_cache.get(
        ("stats", symbol, selected_window),
        900,
        lambda: client.funding_stats(symbol, start=start, end=now, limit=250),
    )
    trades = parse_funding_trades(raw_trades)
    stats = parse_funding_stats(raw_stats)
    reference = adaptive_book_reference(settings, currency, book, principal, policy)
    signals = build_market_signals(
        reference,
        trades,
        stats,
        policy,
        min_rate_for(settings, currency),
        settings.max_daily_rate,
        now,
    )
    signals.update(strategy_bucket_book_rates(settings, currency, book, principal, policy))
    warnings = list(signals.get("warnings", []))
    warnings.extend(item for item in (trades_warning, stats_warning) if item)
    signals["warnings"] = warnings
    signals["trades_stale"] = trades_stale
    signals["stats_stale"] = stats_stale
    return signals, trades, stats


def strategy_config_hash(settings, currency):
    payload = json.dumps(policy_to_json(strategy_policy_for(settings, currency)), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def amount_to_lend(settings, currency, total_balance, available_balance, low_rate, log):
    max_to_lend, max_percent_to_lend, max_to_lend_rate = max_lending_limits(settings, currency)
    min_loan_size = min_loan_size_for(settings, currency)
    restrict = False
    if low_rate > 0 and (max_to_lend_rate == 0 or low_rate <= max_to_lend_rate):
        restrict = True

    active_balance = available_balance
    if restrict:
        if max_to_lend != 0:
            target = max_to_lend
        elif max_percent_to_lend != 0:
            target = total_balance * max_percent_to_lend
        else:
            target = total_balance
        active_balance = max(Decimal("0"), available_balance - max(Decimal("0"), total_balance - target))
        if available_balance - active_balance < min_loan_size:
            active_balance = available_balance
        log.log(
            f"{currency} 当前低利率为 {low_rate * 100:.4f}%，条件利率为 "
            f"{max_to_lend_rate * 100:.4f}%。将在可用 {available_balance:.8f} 中放贷 "
            f"{active_balance:.8f}。"
        )
        log.updateStatusValue(currency, "maxToLend", target)
    else:
        log.updateStatusValue(currency, "maxToLend", total_balance)

    return active_balance


def offer_parts(active_balance, min_loan_size, spread_lend):
    max_parts = int(active_balance // min_loan_size)
    parts = min(spread_lend, max_parts)
    if parts < 1:
        return []

    base = (active_balance / Decimal(parts)).quantize(SATOSHI, rounding=ROUND_DOWN)
    amounts = []
    allocated = Decimal("0")
    for index in range(parts):
        if index == parts - 1:
            amount = active_balance - allocated
        else:
            amount = base
        amount = amount.quantize(SATOSHI, rounding=ROUND_DOWN)
        if amount > 0:
            amounts.append(amount)
            allocated += amount
    return amounts


def weighted_offer_parts(active_balance, min_loan_size, spread_lend, weights):
    max_parts = int(active_balance // min_loan_size)
    parts = min(spread_lend, max_parts, len(weights))
    if parts < 1:
        return []

    normalized = [max(Decimal("0"), Decimal(value)) for value in weights[:parts]]
    weight_total = sum(normalized, Decimal("0"))
    if weight_total <= 0:
        normalized = [Decimal("1") / Decimal(parts)] * parts
    else:
        normalized = [value / weight_total for value in normalized]

    minimum_total = min_loan_size * Decimal(parts)
    remainder = max(Decimal("0"), active_balance - minimum_total)
    amounts = []
    allocated = Decimal("0")
    for index, weight in enumerate(normalized):
        if index == parts - 1:
            amount = active_balance - allocated
        else:
            amount = min_loan_size + (remainder * weight)
        amount = amount.quantize(SATOSHI, rounding=ROUND_DOWN)
        amounts.append(amount)
        allocated += amount
    return amounts


def smart_offer_weights(settings, parts):
    if parts <= 1:
        return [Decimal("1")]
    fast = settings.smart_fast_share / Decimal("100")
    long = settings.smart_long_share / Decimal("100")
    if parts == 2:
        return [fast, long]
    balanced = max(Decimal("0"), Decimal("1") - fast - long)
    middle = balanced / Decimal(parts - 2)
    return [fast] + [middle] * (parts - 2) + [long]


def smart_bucket_for_part(index, parts):
    if parts <= 1 or index == 0:
        return "fast"
    if index == parts - 1:
        return "long"
    return "balanced"


def book_rate_at_depth(book, depth):
    if not book:
        return Decimal("0")
    cumulative = Decimal("0")
    for offer in book:
        cumulative += offer["amount"]
        if cumulative >= depth:
            return offer["rate"]
    return book[-1]["rate"]


def rate_at_depth(book, depth, min_rate, max_rate):
    cumulative = Decimal("0")
    chosen = None
    for offer in book:
        cumulative += offer["amount"]
        if cumulative >= depth:
            chosen = offer["rate"]
            break
    if chosen is None:
        chosen = book[-1]["rate"] if book else max_rate
    if book:
        chosen = chosen - RATE_UNDERCUT
    if chosen < min_rate:
        chosen = min_rate
    if chosen > max_rate:
        chosen = max_rate
    return chosen


def clamp_rate(rate, min_rate, max_rate):
    if rate < min_rate:
        return min_rate
    if rate > max_rate:
        return max_rate
    return rate


def smart_market_reference_rate(settings, currency, book, principal=Decimal("0")):
    if not book:
        return min_rate_for(settings, currency)
    meaningful_depth = max(
        min_loan_size_for(settings, currency),
        Decimal(principal) * settings.smart_floor_depth / Decimal("100"),
    )
    return book_rate_at_depth(book, meaningful_depth)


def smart_min_rate_for(settings, currency, book, principal=Decimal("0")):
    configured_min = min_rate_for(settings, currency)
    if not settings.smart_strategy or not book:
        return configured_min
    market_reference = smart_market_reference_rate(settings, currency, book, principal)
    target = max(configured_min, market_reference + settings.smart_rate_offset)
    return clamp_rate(target, Decimal("0.00003"), settings.max_daily_rate)


def smart_depth_for_part(settings, index, parts):
    bucket = smart_bucket_for_part(index, parts)
    if bucket == "fast":
        return settings.smart_fast_depth
    if bucket == "long":
        return settings.smart_opportunity_depth
    return settings.smart_balanced_depth


def choose_offer_rates(book, active_plus_lended, settings, currency, parts):
    min_rate = smart_min_rate_for(settings, currency, book, active_plus_lended)
    rates = []
    if settings.smart_strategy and book:
        market_reference = smart_market_reference_rate(settings, currency, book, active_plus_lended)
        for index in range(parts):
            depth_percent = smart_depth_for_part(settings, index, parts)
            depth = active_plus_lended * depth_percent / Decimal("100")
            rate = rate_at_depth(book, depth, min_rate, settings.max_daily_rate)
            if smart_bucket_for_part(index, parts) == "long":
                rate = max(rate, market_reference + settings.smart_opportunity_premium)
            rates.append(clamp_rate(rate, min_rate, settings.max_daily_rate))
        return rates

    if parts == 1:
        targets = [settings.gap_bottom]
    else:
        width = settings.gap_top - settings.gap_bottom
        targets = [
            settings.gap_bottom + (width * Decimal(index) / Decimal(parts - 1))
            for index in range(parts)
        ]
    for target_percent in targets:
        depth = active_plus_lended * target_percent / Decimal("100")
        rates.append(rate_at_depth(book, depth, min_rate, settings.max_daily_rate))
    return rates


def offer_period(settings, rate, bucket=None):
    if bucket == "long" and getattr(settings, "smart_long_period", False):
        return settings.xdays
    if settings.xday_threshold == 0:
        return 2
    return settings.xdays if rate > settings.xday_threshold else 2


def build_offer_plan(book, active_balance, currently_lended, settings, currency, market_signals=None):
    min_loan_size = min_loan_size_for(settings, currency)
    max_parts = min(3, settings.spread_lend, int(active_balance // min_loan_size))
    if max_parts < 1:
        return []
    policy = strategy_policy_for(settings, currency)
    signals = market_signals
    if signals is None:
        reference = adaptive_book_reference(
            settings,
            currency,
            book,
            active_balance + currently_lended,
            policy,
        )
        signals = build_market_signals(
            reference,
            [],
            [],
            policy,
            min_rate_for(settings, currency),
            settings.max_daily_rate,
        )
    engine_plan = build_strategy_plan(
        active_balance,
        min_loan_size,
        min_rate_for(settings, currency),
        settings.max_daily_rate,
        policy,
        signals,
        max_parts=max_parts,
    )
    plan = []
    for item in engine_plan:
        plan.append({
            "bucket": item["bucket"],
            "amount": item["amount"],
            "rate": item["submitted_rate"],
            "effective_rate": item["effective_rate"],
            "target_rate": item["target_rate"],
            "offer_type": item["offer_type"],
            "period": item["period"],
            "wait_minutes": item["wait_minutes"],
            "reason": item["reason"],
            "warning": item["warning"],
        })
    return plan


def submit_offer(
    client,
    settings,
    log,
    currency,
    amount,
    rate,
    period=None,
    offer_type="LIMIT",
    effective_rate=None,
    bucket="",
    registry=None,
):
    period = int(period if period is not None else offer_period(settings, rate))
    amount_text = format_decimal(amount)
    rate_text = format_rate(rate)
    response = client.submit_funding_offer(
        currency_to_symbol(currency),
        amount_text,
        rate_text,
        period,
        offer_type,
    )
    displayed_rate = Decimal(effective_rate) if effective_rate is not None else Decimal(rate)
    log.offer(amount_text, currency, displayed_rate, period, response)
    log.log(f"{currency} {bucket or 'strategy'} order type: {offer_type}")
    if registry is not None:
        offer_id = extract_submitted_offer_id(response)
        if offer_id is None:
            log.log(f"{currency}: submitted offer ID missing; the offer will be treated as external for safety")
        else:
            registry.record(
                offer_id,
                currency,
                bucket,
                offer_type,
                strategy_config_hash(settings, currency),
            )
    return response


def offer_age_minutes(offer, now_ms=None):
    created = int(offer.get("created") or 0)
    if created <= 0:
        return Decimal("0")
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    age_ms = max(0, now - created)
    return Decimal(age_ms) / Decimal("60000")


def split_stale_open_offers(settings, open_offers, now_ms=None):
    fresh = {}
    stale = {}
    if not settings.reprice_stale_offers:
        return open_offers, stale
    for currency, offers in open_offers.items():
        try:
            policy = strategy_policy_for(settings, currency)
        except (AttributeError, KeyError):
            policy = None
        for offer in offers:
            if offer.get("managed_by_bot", True) is False:
                fresh.setdefault(currency, []).append(offer)
                continue
            if policy is not None:
                bucket = offer.get("bucket") or (
                    "long" if int(offer.get("period") or 0) == policy.long_period else "balanced"
                )
                offer_threshold = getattr(policy, f"{bucket}_wait_minutes", policy.balanced_wait_minutes)
            else:
                offer_threshold = settings.reprice_after_minutes
            target = stale if offer_age_minutes(offer, now_ms) >= offer_threshold else fresh
            target.setdefault(currency, []).append(offer)
    return fresh, stale


def merge_offer_maps(first, second):
    merged = {currency: list(offers) for currency, offers in first.items()}
    for currency, offers in second.items():
        merged.setdefault(currency, []).extend(offers)
    return merged


def filter_market_reprice_candidates(
    settings,
    fresh_offers,
    aged_offers,
    books,
    principals,
    signals_by_currency=None,
):
    keep = {}
    reprice = {}
    for currency, offers in aged_offers.items():
        book = books.get(currency, [])
        if not book:
            keep.setdefault(currency, []).extend(offers)
            continue
        principal = principals.get(currency, Decimal("0"))
        policy = strategy_policy_for(settings, currency)
        signals = (signals_by_currency or {}).get(currency)
        if signals is None:
            reference = adaptive_book_reference(settings, currency, book, principal, policy)
            signals = build_market_signals(
                reference,
                [],
                [],
                policy,
                min_rate_for(settings, currency),
                settings.max_daily_rate,
            )
        candidate_plan = build_strategy_plan(
            max(principal, min_loan_size_for(settings, currency) * Decimal("3")),
            min_loan_size_for(settings, currency),
            min_rate_for(settings, currency),
            settings.max_daily_rate,
            policy,
            signals,
        )
        targets = {item["bucket"]: item["target_rate"] for item in candidate_plan}
        targets.setdefault("balanced", clamp_rate(
            signals["anchor_rate"] + policy.rate_offset,
            min_rate_for(settings, currency),
            settings.max_daily_rate,
        ))
        for offer in offers:
            bucket = offer.get("bucket") or (
                "long" if int(offer.get("period") or 0) == policy.long_period else "balanced"
            )
            target = targets.get(bucket, targets["balanced"])
            destination = (
                reprice
                if offer["rate"] > target + policy.reprice_min_delta
                else keep
            )
            destination.setdefault(currency, []).append(offer)
    return merge_offer_maps(fresh_offers, keep), reprice


def cancel_stale_offers(client, settings, log, stale_offers, registry=None):
    reprice_amounts = {}
    for currency in settings.currencies:
        offers = stale_offers.get(currency, [])
        if not offers:
            continue
        total = sum((offer["amount"] for offer in offers), Decimal("0"))
        reprice_amounts[currency] = total
        for offer in offers:
            response = client.cancel_funding_offer(offer["id"])
            log.cancelOrders(currency, response)
            if registry is not None:
                registry.remove(offer["id"])
    return reprice_amounts


def transfer_balances(client, settings, by_wallet, log):
    if not settings.transferable_currencies:
        return
    for currency in settings.transferable_currencies:
        for wallet_type in settings.transfer_from_wallets:
            amount = by_wallet.get((wallet_type.lower(), currency), Decimal("0"))
            if amount <= 0:
                continue
            response = client.transfer_between_wallets(wallet_type.lower(), "funding", currency, amount)
            log.log(log.digestApiMsg(response))


def update_output_currency(settings, log):
    log.updateMetaValue("schemaVersion", STATUS_SCHEMA_VERSION)
    log.updateMetaValue("operationMode", "live")
    log.updateOutputCurrency("currency", settings.output_currency)
    log.updateOutputCurrency("highestBid", "1")
    log.updateMetaValue("platformFeeRate", str(settings.platform_fee_rate))


def start_of_today_ms():
    now = datetime.datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(today.timestamp() * 1000)


def ago_ms(days):
    return int((time.time() - (days * 86400)) * 1000)


def parse_ledger_earnings(rows, start_ms):
    earnings = []
    for row in rows:
        if len(row) < 9:
            continue
        wallet = str(row[2] or "").lower()
        if wallet and wallet != "funding":
            continue
        mts = int(row[3] or 0)
        if mts < start_ms:
            continue
        amount = decimal_from_api(row[5])
        if amount <= 0:
            continue
        description = str(row[8] or "").lower()
        if any(word in description for word in ("deposit", "withdrawal", "transfer")):
            continue
        currency = str(row[1]).upper()
        earnings.append({"currency": currency, "mts": mts, "amount": amount})
    return earnings


def sum_earnings(entries, start_ms):
    total = Decimal("0")
    for entry in entries:
        if entry["mts"] >= start_ms:
            total += entry["amount"]
    return total


def status_decimal(value):
    return format(Decimal(value).quantize(SATOSHI, rounding=ROUND_DOWN), "f")


def empty_earnings(error=""):
    return {
        "available": False,
        "summaryCurrency": "USD/UST",
        "today": "0",
        "sevenDays": "0",
        "thirtyDays": "0",
        "thirtyDayApy": "0",
        "idleRatio": "0",
        "byCurrency": {},
        "error": error,
    }


def total_open_offer_amounts(open_offers):
    return {
        currency: sum((offer["amount"] for offer in offers), Decimal("0"))
        for currency, offers in open_offers.items()
    }


def fetch_earnings_stats(client, settings, balances, open_offers, totals, log):
    if not client.has_credentials():
        return empty_earnings("未配置 API 密钥，暂无真实收益统计。")

    now_ms = int(time.time() * 1000)
    today_ms = start_of_today_ms()
    seven_days_ms = ago_ms(7)
    thirty_days_ms = ago_ms(30)
    all_entries = []
    by_currency = {}
    errors = []

    for currency in settings.currencies:
        try:
            rows = client.ledgers(
                currency,
                start=thirty_days_ms,
                end=now_ms,
                limit=2500,
                wallet="funding",
                category=28,
            )
        except BitfinexApiError as exc:
            errors.append(f"{currency}: {exc}")
            log.log(f"读取 {currency} 收益 ledger 失败：{exc}")
            continue

        entries = parse_ledger_earnings(rows, thirty_days_ms)
        all_entries.extend(entries)
        by_currency[currency] = {
            "today": status_decimal(sum_earnings(entries, today_ms)),
            "sevenDays": status_decimal(sum_earnings(entries, seven_days_ms)),
            "thirtyDays": status_decimal(sum_earnings(entries, thirty_days_ms)),
        }

    if not all_entries and errors:
        return empty_earnings("；".join(errors))

    usd_like = [currency for currency in settings.currencies if currency in {"USD", "UST"}]
    summary_currencies = usd_like or settings.currencies[:1]
    summary_label = "USD/UST" if usd_like else (summary_currencies[0] if summary_currencies else "USD")
    summary_entries = [entry for entry in all_entries if entry["currency"] in summary_currencies]
    today = sum_earnings(summary_entries, today_ms)
    seven_days = sum_earnings(summary_entries, seven_days_ms)
    thirty_days = sum_earnings(summary_entries, thirty_days_ms)

    open_offer_totals = total_open_offer_amounts(open_offers)
    principal = Decimal("0")
    idle = Decimal("0")
    for currency in summary_currencies:
        currency_idle = balances.get(currency, Decimal("0"))
        idle += currency_idle
        principal += currency_idle + open_offer_totals.get(currency, Decimal("0")) + totals.get(currency, Decimal("0"))

    apy = Decimal("0")
    idle_ratio = Decimal("0")
    if principal > 0:
        apy = thirty_days / principal * Decimal("365") / Decimal("30") * Decimal("100")
        idle_ratio = idle / principal * Decimal("100")

    return {
        "available": bool(all_entries) or not errors,
        "summaryCurrency": summary_label,
        "today": status_decimal(today),
        "sevenDays": status_decimal(seven_days),
        "thirtyDays": status_decimal(thirty_days),
        "thirtyDayApy": decimal_to_config(apy),
        "idleRatio": decimal_to_config(idle_ratio),
        "byCurrency": by_currency,
        "error": "；".join(errors),
    }


def update_earnings_stats(client, settings, balances, open_offers, totals, log):
    stats = fetch_earnings_stats(client, settings, balances, open_offers, totals, log)
    log.updateMetaValue("earnings", stats)


def lend_available_balances(
    client,
    settings,
    log,
    balances,
    fresh_open_offers,
    reprice_amounts,
    totals,
    weighted_rates,
    books=None,
    signals_by_currency=None,
    registry=None,
):
    usable_currencies = 0
    strategy_decisions = {}
    open_offer_totals = {
        currency: sum((offer["amount"] for offer in offers), Decimal("0"))
        for currency, offers in fresh_open_offers.items()
    }

    for currency in settings.currencies:
        if not enabled_for(settings, currency):
            log.log(f"{currency} 已在 coinconfig 中禁用，跳过。")
            continue

        min_loan_size = min_loan_size_for(settings, currency)
        wallet_available = balances.get(currency, Decimal("0"))
        reprice_available = reprice_amounts.get(currency, Decimal("0"))
        open_offer_total = open_offer_totals.get(currency, Decimal("0"))
        available = wallet_available + reprice_available
        currently_lended = totals.get(currency, Decimal("0"))
        total_balance = available + open_offer_total + currently_lended
        log.updateStatusValue(currency, "totalCoins", total_balance)
        log.updateStatusValue(currency, "lentSum", currently_lended)
        if currently_lended == 0:
            log.updateStatusValue(currency, "averageLendingRate", "0")
        log.updateStatusValue(currency, "highestBid", "1")
        log.updateStatusValue(currency, "couple", f"{currency}_{settings.output_currency}")
        log.updateStatusValue(currency, "walletAvailable", wallet_available)
        log.updateStatusValue(currency, "openOfferSum", open_offer_total)
        log.updateStatusValue(currency, "repriceOfferSum", reprice_available)
        log.updateStatusValue(currency, "openOfferCount", len(fresh_open_offers.get(currency, [])))

        if available <= 0:
            continue

        book = books.get(currency, []) if books is not None else fetch_book(client, settings, currency, log)
        if not book:
            log.log(f"{currency}: funding book is empty; no offers submitted")
            continue
        policy = strategy_policy_for(settings, currency)
        signals = (signals_by_currency or {}).get(currency)
        if signals is None:
            signals, _, _ = load_public_market_signals(
                client, settings, currency, book, total_balance, policy.replay_window
            )
        market_reference = signals["anchor_rate"]
        low_rate = market_reference
        smart_min_rate = max(min_rate_for(settings, currency), market_reference + policy.rate_offset)
        log.updateStatusValue(currency, "marketDailyRate", market_reference * Decimal("100"))
        log.updateStatusValue(currency, "rawBookFloorRate", book[0]["rate"] * Decimal("100"))
        log.updateStatusValue(currency, "smartDailyRate", smart_min_rate * Decimal("100"))
        log.updateStatusValue(currency, "strategyMode", policy.profile)
        log.updateStatusValue(currency, "marketRegime", signals["regime"])
        log.updateStatusValue(currency, "fundingUtilization", (
            "" if signals["utilization"] is None else signals["utilization"] * Decimal("100")
        ))
        active_balance = amount_to_lend(settings, currency, total_balance, available, low_rate, log)
        if active_balance < min_loan_size:
            log.log(
                f"{currency}：可放贷 {active_balance:.8f} 低于最小挂单金额 "
                f"{min_loan_size:.8f}，跳过。"
            )
            continue

        plan = build_offer_plan(book, active_balance, currently_lended, settings, currency, signals)
        if not plan:
            continue
        log.updateStatusValue(currency, "plannedOfferCount", len(plan))
        log.updateStatusValue(
            currency,
            "strategyAllocation",
            f"fast {policy.fast_share}% / balanced {policy.balanced_share}% / long {policy.long_share}%",
        )
        strategy_decisions[currency] = {
            "profile": policy.profile,
            "regime": signals["regime"],
            "signalsAsOf": int(signals["as_of"]),
            "signals": signals_to_json(signals),
            "plan": plan_to_json([
                {
                    "bucket": item["bucket"],
                    "amount": item["amount"],
                    "offer_type": item["offer_type"],
                    "submitted_rate": item["rate"],
                    "effective_rate": item["effective_rate"],
                    "target_rate": item["target_rate"],
                    "period": item["period"],
                    "wait_minutes": item["wait_minutes"],
                    "reason": item["reason"],
                    "warning": item["warning"],
                }
                for item in plan
            ]),
        }
        usable_currencies = 1
        for item in plan:
            if item["amount"] >= min_loan_size:
                submit_offer(
                    client,
                    settings,
                    log,
                    currency,
                    item["amount"],
                    item["rate"],
                    item["period"],
                    item["offer_type"],
                    item["effective_rate"],
                    item["bucket"],
                    registry,
                )

    log.updateMetaValue("strategyDecision", strategy_decisions)
    log.refreshStatus(stringify_total_lended(totals, weighted_rates, log))
    return usable_currencies


def run_cycle(client, settings, log):
    update_output_currency(settings, log)
    totals, weighted_rates = fetch_active_funding(client, settings, log)
    balances, by_wallet = fetch_wallet_state(client, settings, log)
    registry = managed_offer_registry(settings.managed_offer_state_file)
    open_offers = fetch_open_offers(client, settings, log, registry)
    log.updateMetaValue("openOffers", serialize_open_offers(open_offers))
    update_earnings_stats(client, settings, balances, open_offers, totals, log)
    transfer_balances(client, settings, by_wallet, log)
    fresh_open_offers, aged_offers = split_stale_open_offers(settings, open_offers)
    needed_books = {
        currency
        for currency in settings.currencies
        if enabled_for(settings, currency)
        and (balances.get(currency, Decimal("0")) > 0 or aged_offers.get(currency))
    }
    books = {
        currency: fetch_book(client, settings, currency, log)
        for currency in needed_books
    }
    principals = {
        currency: balances.get(currency, Decimal("0"))
        + totals.get(currency, Decimal("0"))
        + sum((offer["amount"] for offer in open_offers.get(currency, [])), Decimal("0"))
        for currency in settings.currencies
    }
    signals_by_currency = {}
    for currency, book in books.items():
        policy = strategy_policy_for(settings, currency)
        signals, _, _ = load_public_market_signals(
            client,
            settings,
            currency,
            book,
            principals.get(currency, Decimal("0")),
            policy.replay_window,
        )
        signals_by_currency[currency] = signals
    fresh_open_offers, stale_offers = filter_market_reprice_candidates(
        settings,
        fresh_open_offers,
        aged_offers,
        books,
        principals,
        signals_by_currency,
    )
    reprice_amounts = cancel_stale_offers(client, settings, log, stale_offers, registry)
    for currency in settings.currencies:
        log.updateStatusValue(currency, "staleOfferCount", len(stale_offers.get(currency, [])))
    usable = lend_available_balances(
        client,
        settings,
        log,
        balances,
        fresh_open_offers,
        reprice_amounts,
        totals,
        weighted_rates,
        books,
        signals_by_currency,
        registry,
    )
    log.persistStatus()
    sys.stdout.flush()
    return settings.sleep_active if usable else settings.sleep_inactive


server = None
dashboard_v3_hub = None


def load_dashboard_static_snapshot(directory, build_id):
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
            assets[relative] = data
    return assets


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    config_path = DEFAULT_CONFIG
    status_path = DEFAULT_DASHBOARD_JSON
    build_id = ""
    static_assets = {}
    dashboard_started_at = DASHBOARD_STARTED_AT

    def log_message(self, format, *args):
        return

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
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
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"} else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Mika-Dashboard-Build", self.build_id)
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/health":
                self._send_json({
                    "ok": True,
                    "service": DASHBOARD_SERVICE_ID,
                    "buildId": self.build_id,
                    "pid": os.getpid(),
                    "startedAt": self.dashboard_started_at,
                    "projectRoot": os.path.abspath(os.path.dirname(__file__)),
                    "configPath": os.path.abspath(self.config_path),
                    "time": timestamp(),
                })
                return
            if path == "/api/status":
                self._send_json(dashboard_status_payload(self.status_path, self.config_path))
                return
            if path == "/api/config":
                self._send_json(config_api_payload(self.config_path))
                return
            if path == "/api/control/status":
                self._send_json(controlled_bot_status(self.config_path))
                return
            if path == "/api/runtime/v3":
                self._send_json({"ok": True, **runtime_v3_payload(self.config_path)})
                return
            if path == "/api/stats/v3":
                store, _ = v3_store_for_config(self.config_path)
                self._send_json({"ok": True, **stats_v3_payload(store)})
                return
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=500)
            return
        self._send_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/config":
                self._read_json_body()
                self._send_json({
                    "ok": False,
                    "code": "V2_STRATEGY_DISABLED",
                    "error": "V2 配置写入已永久禁用；请使用 /api/strategy/v3/*",
                }, status=410)
                return
            if path == "/api/strategy/v3/preview":
                payload = self._read_json_body()
                preview = strategy_v3_preview(self.config_path, payload)
                self._send_json({"ok": True, **preview})
                return
            if path == "/api/strategy/v3/draft":
                payload = self._read_json_body()
                result = save_strategy_v3_draft(self.config_path, payload)
                self._send_json({"ok": True, **result})
                return
            if path == "/api/strategy/v3/apply":
                payload = self._read_json_body()
                result = apply_strategy_v3_draft(self.config_path, payload)
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
                    if controlled_bot_running(self.config_path):
                        stop_controlled_bot(self.config_path)
                    runtime = store.set_mode("PAUSED", "dashboard_pause")
                    self._send_json({"ok": True, "runtime": runtime})
                    return
                if target == "REPLAY":
                    replay = replay_v3_from_store(self.config_path)
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
                preflight = create_controlled_bot_preflight(self.config_path)
                self._send_json({"ok": True, **preflight})
                return
            if path == "/api/control/start":
                payload = self._read_json_body()
                status = start_controlled_bot(
                    self.config_path,
                    self.status_path,
                    str(payload.get("preflightId", "")),
                )
                self._send_json({"ok": True, "bot": status})
                return
            if path == "/api/control/stop":
                status = stop_controlled_bot(self.config_path)
                store, _ = v3_store_for_config(self.config_path)
                runtime = store.runtime()
                if runtime["mode"] != "SAFE":
                    runtime = store.set_mode("PAUSED", "dashboard_stop")
                self._send_json({"ok": True, "bot": status, "runtime": runtime})
                return
            self._send_json({"ok": False, "error": "Not found"}, status=404)
        except ApiRequestError as exc:
            response = {"ok": False, "code": exc.code, "error": str(exc)}
            if exc.details is not None:
                response["details"] = exc.details
            self._send_json(response, status=exc.status)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)


def make_dashboard_handler(directory, config_path, status_path, build_id=None):
    build_id = build_id or dashboard_build_id()
    static_assets = load_dashboard_static_snapshot(directory, build_id)

    class Handler(DashboardRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

    Handler.config_path = config_path
    Handler.status_path = status_path
    Handler.build_id = build_id
    Handler.static_assets = static_assets
    Handler.dashboard_started_at = timestamp()
    return Handler


def start_web_server(log, config_path, status_path):
    global server, dashboard_v3_hub
    port = 8000
    host = "127.0.0.1"
    directory = os.path.join(os.getcwd(), "www")
    handler = make_dashboard_handler(directory, config_path, status_path)
    try:
        if dashboard_v3_hub is None and websocket_dependency_available():
            store, settings = v3_store_for_config(config_path)
            _, active_policy = ensure_active_strategy_v3(store, settings)
            dashboard_v3_hub = BitfinexMarketDataHub(
                "",
                "",
                symbol="fUSD",
                store=store,
                fallback_seconds=active_policy.ws_fallback_seconds,
                rest_stale_seconds=active_policy.rest_stale_seconds,
            )
            dashboard_v3_hub.start()
        server = ThreadingHTTPServer((host, port), handler)
        log.log(f"网页控制台已启动：http://{host}:{port}/lendingbot.html")
        server.serve_forever()
    except Exception as exc:
        log.log(f"网页控制台启动失败：{exc}")


def stop_web_server(log):
    global server, dashboard_v3_hub
    if dashboard_v3_hub is not None:
        dashboard_v3_hub.stop()
        dashboard_v3_hub = None
    if server is None:
        return
    try:
        log.log("正在停止网页控制台")
        server.shutdown()
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


def main(argv=None):
    args = parse_args(argv)
    if args.dashboard and args.live:
        print("Configuration error: --dashboard 不能与 --live 同时使用", file=sys.stderr)
        return 1
    if not args.dashboard and not args.live:
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

    log = Logger(
        settings.json_file,
        settings.json_log_size,
        sensitive_values=(settings.api_key, settings.api_secret),
    )
    if config_created:
        log.log("已从 default.cfg.example 复制出 default.cfg，请在里面填写 Bitfinex API 密钥。")

    if args.dashboard:
        dashboard_lock = LiveProcessLock(DEFAULT_DASHBOARD_LOCK)
        if not dashboard_lock.acquire(args.config, {"role": "dashboard", "service": DASHBOARD_SERVICE_ID}):
            print("Dashboard startup rejected: another dashboard instance holds the lock", file=sys.stderr)
            return 1
        reconcile_orphaned_live_runtime(args.config)
        normalization = normalize_current_active_strategy(args.config)
        if normalization["changed"]:
            log.log(
                f"V3 ACTIVE 已规范化：{normalization['fromVersion']} -> {normalization['versionId']}；"
                f"配置和数据库备份位于 {os.path.dirname(normalization['backup']['database'])}"
            )
        log.log("控制台已就绪；只有完成只读预检并确认后才会启动实盘机器人。")
        thread = threading.Thread(
            target=start_web_server,
            args=(log, args.config, settings.json_file),
            daemon=True,
        )
        thread.start()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            stop_web_server(log)
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
    live_lock = LiveProcessLock(DEFAULT_LIVE_LOCK)
    if not live_lock.acquire(args.config, {"role": "live_worker", "service": "mika-lending-worker-v3"}):
        log.log("实盘启动被拒绝：另一个机器人进程已持有单实例锁。")
        return 1
    log.log("实盘预检通过，开始同步账户并执行策略。")
    client = Bitfinex(settings.api_key, settings.api_secret)
    state_store_v3 = LendingStateStore(settings.state_db_file)
    _, active_policy = ensure_active_strategy_v3(state_store_v3, settings)
    state_store_v3.set_mode("LIVE", "live_preflight_confirmed")
    runtime_v3 = LendingRuntimeV3(
        client,
        active_policy,
        state_store_v3,
        log=log,
        legacy_state_path=settings.managed_offer_state_file,
        auto_transfer_wallets=(
            settings.transfer_from_wallets if "USD" in settings.transferable_currencies else ()
        ),
        on_policy_activated=lambda policy, _version: mirror_active_strategy_v3(args.config, policy),
    )

    if settings.web_server:
        thread = threading.Thread(
            target=start_web_server,
            args=(log, args.config, settings.json_file),
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
            stop_web_server(log)
        live_lock.release()
        log.log("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
