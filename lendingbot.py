import argparse
import configparser
import datetime
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, getcontext
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from bitfinex import Bitfinex, BitfinexApiError, currency_to_symbol, decimal_from_api, symbol_to_currency
from Logger import Logger


getcontext().prec = 28

SATOSHI = Decimal("0.00000001")
RATE_UNDERCUT = Decimal("0.000001")
DEFAULT_CONFIG = "default.cfg"
DEFAULT_CONFIG_EXAMPLE = "default.cfg.example"
SIMULATED_DRYRUN_BALANCE = Decimal("300")
DEFAULT_DASHBOARD_JSON = os.path.join("www", "botlog.json")
DEFAULT_PROCESS_LOG = os.path.join("www", "bot-process.log")
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
    "dryrunbalance",
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
    "repricestaleoffers",
    "repriceafterminutes",
}


class ConfigError(Exception):
    pass


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
    dry_run: bool
    once: bool
    dryrun_balance: Decimal
    smart_strategy: bool
    smart_rate_offset: Decimal
    smart_fast_depth: Decimal
    smart_balanced_depth: Decimal
    smart_opportunity_depth: Decimal
    smart_opportunity_premium: Decimal
    reprice_stale_offers: bool
    reprice_after_minutes: Decimal


def timestamp():
    ts = time.time()
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Bitfinex Lending Bot")
    parser.add_argument("-cfg", "--config", default=DEFAULT_CONFIG, help="configuration file path")
    parser.add_argument("--dashboard", action="store_true", help="start only the local web dashboard")
    parser.add_argument("--live", action="store_true", help="submit and cancel real Bitfinex funding offers")
    parser.add_argument("-dry", "--dryrun", action="store_true", help="force dry-run mode")
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


def build_settings(args, config, config_created=False):
    if args.live and args.dryrun:
        raise ConfigError("Use either --live or --dryrun, not both")

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

    dry_run = not args.live
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
        dry_run=dry_run,
        once=args.once,
        dryrun_balance=get_decimal(config, "BOT", "dryrunbalance", str(SIMULATED_DRYRUN_BALANCE)),
        smart_strategy=get_boolean(config, "BOT", "smartstrategy", True),
        smart_rate_offset=get_decimal_percent(config, "BOT", "smartrateoffset", "0.001"),
        smart_fast_depth=get_decimal(config, "BOT", "smartfastdepth", "5"),
        smart_balanced_depth=get_decimal(config, "BOT", "smartbalanceddepth", "150"),
        smart_opportunity_depth=get_decimal(config, "BOT", "smartopportunitydepth", "300"),
        smart_opportunity_premium=get_decimal_percent(config, "BOT", "smartopportunitypremium", "0.01"),
        reprice_stale_offers=get_boolean(config, "BOT", "repricestaleoffers", True),
        reprice_after_minutes=get_decimal(config, "BOT", "repriceafterminutes", "15"),
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
    if settings.smart_rate_offset < 0 or settings.smart_rate_offset > Decimal("0.05"):
        raise ConfigError("smartrateoffset must be 0-5 percent")
    if settings.smart_opportunity_premium < 0 or settings.smart_opportunity_premium > Decimal("0.05"):
        raise ConfigError("smartopportunitypremium must be 0-5 percent")
    for name, value in (
        ("smartfastdepth", settings.smart_fast_depth),
        ("smartbalanceddepth", settings.smart_balanced_depth),
        ("smartopportunitydepth", settings.smart_opportunity_depth),
    ):
        if value < 0 or value > Decimal("10000"):
            raise ConfigError(f"{name} must be 0-10000")
    if settings.reprice_after_minutes < 1 or settings.reprice_after_minutes > Decimal("1440"):
        raise ConfigError("repriceafterminutes must be 1-1440")


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


def config_api_payload(config_path):
    config, _ = read_config(config_path)
    args = parse_args(["--dryrun", "--config", config_path])
    settings = build_settings(args, config)
    validate_settings(settings)
    client = Bitfinex(settings.api_key, settings.api_secret)
    return {
        "configPath": os.path.abspath(config_path),
        "credentialsConfigured": client.has_credentials(),
        "bitfinex": {
            "currencies": ",".join(settings.currencies),
        },
        "bot": {
            "sleeptimeactive": str(int(settings.sleep_active) if settings.sleep_active.is_integer() else settings.sleep_active),
            "sleeptimeinactive": str(int(settings.sleep_inactive) if settings.sleep_inactive.is_integer() else settings.sleep_inactive),
            "mindailyrate": decimal_percent_to_config(settings.min_daily_rate),
            "maxdailyrate": decimal_percent_to_config(settings.max_daily_rate),
            "spreadlend": str(settings.spread_lend),
            "gapbottom": decimal_to_config(settings.gap_bottom),
            "gaptop": decimal_to_config(settings.gap_top),
            "xdaythreshold": decimal_percent_to_config(settings.xday_threshold),
            "xdays": str(settings.xdays),
            "minloansize": decimal_to_config(settings.min_loan_size),
            "dryrunbalance": decimal_to_config(settings.dryrun_balance),
            "platformfeerate": decimal_to_config(settings.platform_fee_rate),
            "maxtolent": decimal_to_config(settings.max_to_lend),
            "maxpercenttolent": decimal_percent_to_config(settings.max_percent_to_lend),
            "maxtolentrate": decimal_percent_to_config(settings.max_to_lend_rate),
            "transferablecurrencies": ",".join(settings.transferable_currencies),
            "transferfromwallets": ",".join(settings.transfer_from_wallets).lower(),
            "outputcurrency": settings.output_currency,
            "jsonfile": settings.json_file or DEFAULT_DASHBOARD_JSON.replace("\\", "/"),
            "jsonlogsize": str(settings.json_log_size if settings.json_log_size != -1 else 200),
            "startwebserver": str(settings.web_server).lower(),
            "smartstrategy": str(settings.smart_strategy).lower(),
            "smartrateoffset": decimal_percent_to_config(settings.smart_rate_offset),
            "smartfastdepth": decimal_to_config(settings.smart_fast_depth),
            "smartbalanceddepth": decimal_to_config(settings.smart_balanced_depth),
            "smartopportunitydepth": decimal_to_config(settings.smart_opportunity_depth),
            "smartopportunitypremium": decimal_percent_to_config(settings.smart_opportunity_premium),
            "repricestaleoffers": str(settings.reprice_stale_offers).lower(),
            "repriceafterminutes": decimal_to_config(settings.reprice_after_minutes),
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
    return updates


def merged_config_for_validation(config_path, updates):
    config, _ = read_config(config_path)
    for section in ("BITFINEX", "BOT"):
        if not config.has_section(section):
            config.add_section(section)
        for key, value in updates.get(section, {}).items():
            config.set(section, key, value)
    return config


def update_config_file_preserving_comments(config_path, updates):
    if not os.path.exists(config_path):
        ensure_config_file(config_path)
    with open(config_path, "r", encoding="utf-8") as file:
        lines = file.readlines()

    section_names = {"BITFINEX", "BOT"}
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
        if current_section in section_names and "=" in line and not stripped.startswith("#"):
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

    with open(config_path, "w", encoding="utf-8", newline="") as file:
        file.writelines(output)


def save_dashboard_config(config_path, payload):
    updates = normalize_dashboard_updates(payload)
    merged = merged_config_for_validation(config_path, updates)
    args = parse_args(["--dryrun", "--config", config_path])
    settings = build_settings(args, merged)
    validate_settings(settings)
    update_config_file_preserving_comments(config_path, updates)
    return config_api_payload(config_path)


def empty_status_payload():
    return {
        "last_status": "还没有 botlog.json",
        "last_update": "",
        "log": [],
        "outputCurrency": {"currency": "USD", "highestBid": "1"},
        "platformFeeRate": "15",
        "raw_data": {},
    }


def read_status_payload(status_path):
    if not os.path.exists(status_path):
        return empty_status_payload()
    with open(status_path, "r", encoding="utf-8") as file:
        return json.load(file)


controlled_bot_process = None
controlled_bot_mode = None
controlled_bot_started_at = None
controlled_bot_command = []
controlled_bot_log_handle = None
controlled_bot_stop_reason = None


def cleanup_controlled_bot_handle():
    global controlled_bot_log_handle
    if controlled_bot_log_handle is not None:
        try:
            controlled_bot_log_handle.close()
        except Exception:
            pass
        controlled_bot_log_handle = None


def controlled_bot_running():
    return controlled_bot_process is not None and controlled_bot_process.poll() is None


def controlled_bot_status():
    global controlled_bot_process
    running = controlled_bot_running()
    return_code = None
    if controlled_bot_process is not None:
        return_code = controlled_bot_process.poll()
    if controlled_bot_process is not None and return_code is not None:
        cleanup_controlled_bot_handle()
    return {
        "running": running,
        "pid": controlled_bot_process.pid if controlled_bot_process is not None and running else None,
        "mode": controlled_bot_mode,
        "startedAt": controlled_bot_started_at,
        "returnCode": return_code,
        "stopReason": controlled_bot_stop_reason,
        "command": " ".join(controlled_bot_command) if controlled_bot_command else "",
    }


def validate_controlled_bot_start(config_path, status_path, mode, confirm_live):
    if mode not in {"dry", "live"}:
        raise ConfigError("运行模式必须是 dry 或 live")
    if mode == "live" and not confirm_live:
        raise ConfigError("实盘模式需要先勾选确认")
    flag = "--live" if mode == "live" else "--dryrun"
    config, _ = read_config(config_path)
    args = parse_args([
        "--config",
        config_path,
        "--no-server",
        "--json",
        status_path,
        "--jsonsize",
        "200",
        flag,
    ])
    settings = build_settings(args, config)
    validate_settings(settings)
    if mode == "live" and not Bitfinex(settings.api_key, settings.api_secret).has_credentials():
        raise ConfigError("实盘模式需要配置 Bitfinex API key/secret")


def start_controlled_bot(config_path, status_path, mode, confirm_live=False):
    global controlled_bot_process, controlled_bot_mode, controlled_bot_started_at
    global controlled_bot_command, controlled_bot_log_handle, controlled_bot_stop_reason
    if controlled_bot_running():
        return controlled_bot_status()

    validate_controlled_bot_start(config_path, status_path, mode, confirm_live)
    os.makedirs(os.path.dirname(status_path) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(DEFAULT_PROCESS_LOG) or ".", exist_ok=True)

    flag = "--live" if mode == "live" else "--dryrun"
    command = [
        sys.executable,
        os.path.abspath(__file__),
        "--config",
        config_path,
        "--no-server",
        flag,
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
    controlled_bot_process = subprocess.Popen(
        command,
        cwd=os.getcwd(),
        stdin=subprocess.DEVNULL,
        stdout=controlled_bot_log_handle,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
        startupinfo=startupinfo,
    )
    controlled_bot_mode = mode
    controlled_bot_started_at = timestamp()
    controlled_bot_command = command
    controlled_bot_stop_reason = None
    return controlled_bot_status()


def stop_controlled_bot():
    global controlled_bot_process, controlled_bot_stop_reason
    if not controlled_bot_running():
        cleanup_controlled_bot_handle()
        return controlled_bot_status()
    controlled_bot_process.terminate()
    try:
        controlled_bot_process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        controlled_bot_process.kill()
        controlled_bot_process.wait(timeout=5)
    cleanup_controlled_bot_handle()
    controlled_bot_stop_reason = "stopped_by_dashboard"
    return controlled_bot_status()


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
        if settings.dry_run:
            log.log("没有配置 Bitfinex API 密钥，正在使用模拟余额运行。")
            return {currency: settings.dryrun_balance for currency in settings.currencies}, {}
        raise ConfigError("实盘模式需要配置 Bitfinex API key/secret")
    return parse_wallets(client.wallets())


def parse_open_offers(rows):
    offers = {}
    for row in rows:
        if len(row) < 16:
            continue
        currency = symbol_to_currency(str(row[1])).upper()
        amount = abs(decimal_from_api(row[4]))
        rate = decimal_from_api(row[14])
        period = int(row[15])
        offer_id = row[0]
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
            }
        )
    return offers


def fetch_open_offers(client, settings, log):
    if not client.has_credentials():
        return {}
    all_offers = {}
    for currency in settings.currencies:
        try:
            rows = client.active_funding_offers(currency_to_symbol(currency))
        except BitfinexApiError as exc:
            if not settings.dry_run:
                raise
            log.log(f"读取 {currency} 当前挂单失败：{exc}")
            rows = []
        for cur, offers in parse_open_offers(rows).items():
            all_offers.setdefault(cur, []).extend(offers)
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
        for endpoint_name, fetcher in (
            ("loans", client.active_funding_loans),
            ("credits", client.active_funding_credits),
        ):
            try:
                rows.extend(fetcher(symbol))
            except BitfinexApiError as exc:
                if not settings.dry_run:
                    raise
                log.log(f"读取 {currency} 已放贷资金失败（{endpoint_name}）：{exc}")
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
    try:
        return parse_funding_book(client.funding_book(symbol, 250))
    except BitfinexApiError as exc:
        if not settings.dry_run:
            raise
        log.log(f"读取 {currency} 公共资金盘口失败：{exc}")
        return []


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


def rate_at_depth(book, depth, min_rate, max_rate):
    cumulative = Decimal("0")
    chosen = None
    for offer in book:
        cumulative += offer["amount"]
        if cumulative >= depth:
            chosen = offer["rate"]
            break
    if chosen is None:
        chosen = max_rate
    else:
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


def smart_min_rate_for(settings, currency, book):
    configured_min = min_rate_for(settings, currency)
    if not settings.smart_strategy or not book:
        return configured_min
    market_floor = book[0]["rate"]
    return clamp_rate(market_floor + settings.smart_rate_offset, Decimal("0.00003"), settings.max_daily_rate)


def smart_depth_for_part(settings, index, parts):
    if parts <= 1:
        return settings.smart_fast_depth
    if parts == 2:
        return settings.smart_fast_depth if index == 0 else settings.smart_balanced_depth
    if index == parts - 1:
        return settings.smart_opportunity_depth
    if index == parts - 2:
        return settings.smart_balanced_depth
    return settings.smart_fast_depth


def choose_offer_rates(book, active_plus_lended, settings, currency, parts):
    min_rate = smart_min_rate_for(settings, currency, book)
    rates = []
    if settings.smart_strategy and book:
        market_floor = book[0]["rate"]
        for index in range(parts):
            depth_percent = smart_depth_for_part(settings, index, parts)
            depth = active_plus_lended * depth_percent / Decimal("100")
            rate = rate_at_depth(book, depth, min_rate, settings.max_daily_rate)
            if parts >= 3 and index == parts - 1:
                rate = max(rate, market_floor + settings.smart_opportunity_premium)
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


def offer_period(settings, rate):
    if settings.xday_threshold == 0:
        return 2
    return settings.xdays if rate > settings.xday_threshold else 2


def submit_or_log_offer(client, settings, log, currency, amount, rate):
    period = offer_period(settings, rate)
    amount_text = format_decimal(amount)
    rate_text = format_rate(rate)
    if settings.dry_run:
        log.offer(amount_text, currency, rate, period, {"message": "dry-run"})
        return
    response = client.submit_funding_offer(currency_to_symbol(currency), amount_text, rate_text, period)
    log.offer(amount_text, currency, rate, period, response)


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
    threshold = settings.reprice_after_minutes
    for currency, offers in open_offers.items():
        for offer in offers:
            target = stale if offer_age_minutes(offer, now_ms) >= threshold else fresh
            target.setdefault(currency, []).append(offer)
    return fresh, stale


def cancel_stale_offers(client, settings, log, stale_offers):
    reprice_amounts = {}
    for currency in settings.currencies:
        offers = stale_offers.get(currency, [])
        if not offers:
            continue
        total = sum((offer["amount"] for offer in offers), Decimal("0"))
        reprice_amounts[currency] = total
        if settings.dry_run:
            log.cancelOrders(
                currency,
                {
                    "message": (
                        f"dry-run, would reprice {len(offers)} stale offers totaling {total:.8f}"
                    )
                },
            )
            continue
        for offer in offers:
            response = client.cancel_funding_offer(offer["id"])
            log.cancelOrders(currency, response)
    return reprice_amounts


def transfer_balances(client, settings, by_wallet, log):
    if not settings.transferable_currencies:
        return
    if settings.dry_run:
        log.log("模拟运行：跳过已配置的钱包自动转入。")
        return
    for currency in settings.transferable_currencies:
        for wallet_type in settings.transfer_from_wallets:
            amount = by_wallet.get((wallet_type.lower(), currency), Decimal("0"))
            if amount <= 0:
                continue
            response = client.transfer_between_wallets(wallet_type.lower(), "funding", currency, amount)
            log.log(log.digestApiMsg(response))


def update_output_currency(settings, log):
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
):
    usable_currencies = 0
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

        book = fetch_book(client, settings, currency, log)
        low_rate = book[0]["rate"] if book else Decimal("0")
        smart_min_rate = smart_min_rate_for(settings, currency, book)
        log.updateStatusValue(currency, "marketDailyRate", low_rate * Decimal("100"))
        log.updateStatusValue(currency, "smartDailyRate", smart_min_rate * Decimal("100"))
        log.updateStatusValue(currency, "strategyMode", "smart" if settings.smart_strategy else "classic")
        active_balance = amount_to_lend(settings, currency, total_balance, available, low_rate, log)
        if active_balance < min_loan_size:
            log.log(
                f"{currency}：可放贷 {active_balance:.8f} 低于最小挂单金额 "
                f"{min_loan_size:.8f}，跳过。"
            )
            continue

        amounts = offer_parts(active_balance, min_loan_size, settings.spread_lend)
        if not amounts:
            continue
        rates = choose_offer_rates(
            book,
            active_balance + currently_lended,
            settings,
            currency,
            len(amounts),
        )
        usable_currencies = 1
        for amount, rate in zip(amounts, rates):
            if amount >= min_loan_size:
                submit_or_log_offer(client, settings, log, currency, amount, rate)

    log.refreshStatus(stringify_total_lended(totals, weighted_rates, log))
    return usable_currencies


def run_cycle(client, settings, log):
    update_output_currency(settings, log)
    totals, weighted_rates = fetch_active_funding(client, settings, log)
    balances, by_wallet = fetch_wallet_state(client, settings, log)
    open_offers = fetch_open_offers(client, settings, log)
    update_earnings_stats(client, settings, balances, open_offers, totals, log)
    transfer_balances(client, settings, by_wallet, log)
    fresh_open_offers, stale_offers = split_stale_open_offers(settings, open_offers)
    reprice_amounts = cancel_stale_offers(client, settings, log, stale_offers)
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
    )
    log.persistStatus()
    sys.stdout.flush()
    return settings.sleep_active if usable else settings.sleep_inactive


server = None


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    config_path = DEFAULT_CONFIG
    status_path = DEFAULT_DASHBOARD_JSON

    def log_message(self, format, *args):
        return

    def _send_json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
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
                self._send_json({"ok": True, "time": timestamp()})
                return
            if path == "/api/status":
                self._send_json(read_status_payload(self.status_path))
                return
            if path == "/api/config":
                self._send_json(config_api_payload(self.config_path))
                return
            if path == "/api/control/status":
                self._send_json(controlled_bot_status())
                return
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=500)
            return
        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/config":
                payload = self._read_json_body()
                saved = save_dashboard_config(self.config_path, payload)
                self._send_json({"ok": True, "config": saved})
                return
            if path == "/api/control/start":
                payload = self._read_json_body()
                status = start_controlled_bot(
                    self.config_path,
                    self.status_path,
                    str(payload.get("mode", "dry")).lower(),
                    bool(payload.get("confirmLive", False)),
                )
                self._send_json({"ok": True, "bot": status})
                return
            if path == "/api/control/stop":
                status = stop_controlled_bot()
                self._send_json({"ok": True, "bot": status})
                return
            self._send_json({"ok": False, "error": "Not found"}, status=404)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)


def make_dashboard_handler(directory, config_path, status_path):
    class Handler(DashboardRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

    Handler.config_path = config_path
    Handler.status_path = status_path
    return Handler


def start_web_server(log, config_path, status_path):
    global server
    port = 8000
    host = "127.0.0.1"
    directory = os.path.join(os.getcwd(), "www")
    handler = make_dashboard_handler(directory, config_path, status_path)
    try:
        server = ThreadingHTTPServer((host, port), handler)
        log.log(f"网页控制台已启动：http://{host}:{port}/lendingbot.html")
        server.serve_forever()
    except Exception as exc:
        log.log(f"网页控制台启动失败：{exc}")


def stop_web_server(log):
    global server
    if server is None:
        return
    try:
        log.log("正在停止网页控制台")
        server.shutdown()
    except Exception as exc:
        log.log(f"停止网页控制台失败：{exc}")


def main(argv=None):
    args = parse_args(argv)
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

    log = Logger(settings.json_file, settings.json_log_size)
    if config_created:
        log.log("已从 default.cfg.example 复制出 default.cfg，请在里面填写 Bitfinex API 密钥。")
    mode = "模拟运行" if settings.dry_run else "实盘运行"
    log.log(f"欢迎使用 Bitfinex 自动放贷机器人（{mode}）")

    client = Bitfinex(settings.api_key, settings.api_secret)
    if not settings.dry_run and not client.has_credentials():
        log.log("实盘模式需要配置 Bitfinex API key/secret")
        return 1
    if settings.dry_run:
        log.log("模拟运行模式：不会调用 Bitfinex 写入接口")

    if args.dashboard:
        log.log("控制台模式：点击网页启动按钮后才会启动机器人")
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
            stop_controlled_bot()
            stop_web_server(log)
            log.log("已退出")
        return 0

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
                sleep_time = run_cycle(client, settings, log)
                if settings.once:
                    break
                time.sleep(sleep_time)
            except Exception as exc:
                log.log("错误：" + str(exc))
                log.persistStatus()
                print(timestamp())
                print(traceback.format_exc())
                if settings.once:
                    return 1
                time.sleep(sleep_time)
    except KeyboardInterrupt:
        pass
    finally:
        if settings.web_server:
            stop_web_server(log)
        log.log("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
