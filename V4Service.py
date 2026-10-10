"""V4 dashboard actions and CLI composition; all starts require read-only preflight."""

import datetime
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from decimal import Decimal

from AdaptiveEngines import REGISTRY, algorithm_engine, template_for
from bitfinex import Bitfinex
from Configuration import (
    ConfigError,
    build_settings,
    ensure_active_strategy_v3,
    read_config,
    settings_for_currency,
    strategy_v3_api_values,
    strategy_v3_config_values,
    strategy_v3_from_record,
    update_config_file_preserving_comments,
    validate_settings,
)
from Currency import SUPPORTED_CURRENCIES, funding_sizing, require_currency
from RuntimeV4 import FundingWriteGate, V4Coordinator
from StateStore import LendingStateStore
from StrategyV3 import json_decimal
import Lifecycle


def load_profiles(config_path):
    import lendingbot as app

    config, _ = read_config(config_path)
    settings = build_settings(app.parse_args(["--config", config_path]), config)
    validate_settings(settings)
    return settings


def stores_for_profiles(settings, clock=time.time):
    return {
        currency: LendingStateStore(path, clock=clock, currency=currency, config_path=settings.config_path)
        for currency, path in settings.state_databases.items()
    }


def restart_control_digest(config_path):
    """Keep credentials, paths, enablement and transfer permissions bound to authorization."""
    config, _ = read_config(config_path)
    profiles = load_profiles(config_path)
    policy_keys = set(strategy_v3_config_values(profiles.policies["USD"]))
    values = {
        section: {
            key: value
            for key, value in config.items(section)
            if not (section.startswith("STRATEGY_V") and key in policy_keys)
        }
        for section in config.sections()
    }
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def version_performance(store, active):
    """Count only this ACTIVE version's submitted offers, never adopted loans.

    Funding ledger entries do not reliably identify an offer or credit. Account
    interest therefore remains separate until exact attribution is available.
    Closed credits are retained locally and count alongside outstanding loans.
    """
    if active is None:
        return None
    policy = strategy_v3_from_record(active)
    activated = active.get("activated_at_ms")
    if activated is None:
        return None
    parameters = (store.currency, active["version_id"], int(activated))
    own_intents = """SELECT exchange_offer_id FROM order_intents
                     WHERE currency=? AND strategy_version=? AND created_at_ms>=?
                       AND exchange_offer_id IS NOT NULL
                       AND COALESCE(resolution, '')!='PREFLIGHT_ADOPTED'
                       AND slice_key NOT LIKE 'adopted:%'"""
    with store.read_connection() as connection:
        fills = connection.execute(
            f"""SELECT trade_id, amount FROM funding_trades
                WHERE currency=? AND mts>=? AND offer_id IN ({own_intents})""",
            (store.currency, int(activated), *parameters),
        ).fetchall()
        loans = connection.execute(
            f"""SELECT credit_id, amount FROM credits
                WHERE currency=? AND mts_opening>=? AND offer_id IN ({own_intents})""",
            (store.currency, int(activated), *parameters),
        ).fetchall()
    return {
        "engine": policy.strategy_engine,
        "strategyVersion": active["version_id"],
        "activatedAtMs": int(activated),
        "newFillCount": len(fills),
        "newFillPrincipal": sum((abs(Decimal(row["amount"])) for row in fills), Decimal(0)),
        "newLoanCount": len(loans),
        "newLoanPrincipal": sum((abs(Decimal(row["amount"])) for row in loans), Decimal(0)),
        "netInterest": None,
        "interestAttribution": "UNAVAILABLE",
        "coverage": "LOCALLY_LINKED_OFFERS_ONLY",
    }


class V4DashboardService:
    def __init__(self, config_path, status_path, context):
        self.config_path = config_path
        self.status_path = status_path
        self.context = context
        self.tokens = {}
        from ResearchV4 import ResearchJobs

        def store_factory(currency):
            import lendingbot as app

            return app.v3_store_for_config(self.config_path, currency)[0]

        self.research = ResearchJobs(
            store_factory,
            context.now,
            public_client_factory=(lambda: context.client_factory("", "")) if context.client_factory else None,
        )

    def _client(self, settings):
        factory = self.context.client_factory or Bitfinex
        return factory(settings.api_key, settings.api_secret)

    def config(self, currency):
        import lendingbot as app

        currency = require_currency(currency)
        settings = load_profiles(self.config_path)
        store, selected = app.v3_store_for_config(self.config_path, currency)
        active, policy = ensure_active_strategy_v3(store, selected)
        from ResearchV4 import ModelRepository
        from StrategyV4 import ENGINE

        repo = ModelRepository(store.path, currency)
        models = {engine: repo.candidate(int(self.context.now() * 1000), engine) for engine in REGISTRY}
        candidate = models[ENGINE]
        from OperationalV41 import status as operational_status

        def template_info(engine):
            return {
                **strategy_v3_api_values(template_for(engine, policy)),
                "engine": engine,
                "algorithm": REGISTRY[engine][0],
                "currency": currency,
            }

        def model_info(model):
            if model is None:
                return None
            from AdaptiveRuntime import eligible

            return {
                **{key: model[key] for key in ("id", "confidence", "coverage")},
                "engine": algorithm_engine(model["algorithm"]),
                "algorithm": model["algorithm"],
                "currency": model["currency"],
                "eligibleForLiveCandidate": eligible(store, model),
                **operational_status(repo, model, int(self.context.now() * 1000)),
                "dataBasis": model.get("dataBasis", {}),
                "typeConfidence": {
                    kind: "CALIBRATED" if count >= 20 else "LOW"
                    for kind, count in model.get("typeObservationCounts", {}).items()
                },
            }

        details = {model["id"]: model_info(model) for model in models.values() if model}
        for record in (active, store.strategy("DRAFT"), store.strategy("PENDING")):
            if record:
                model_id = strategy_v3_from_record(record).model_id
                if model_id and model_id not in details:
                    try:
                        details[model_id] = model_info(repo.load(model_id, int(self.context.now() * 1000)))
                    except ValueError:
                        details[model_id] = None
        return {
            "credentialsConfigured": self._client(settings).has_credentials(),
            "currency": currency,
            "enabled": currency in settings.enabled_currencies,
            "autoTransfer": currency in settings.transferable_currencies,
            "strategyV3": strategy_v3_api_values(policy),
            "strategyV3Draft": strategy_v3_api_values(strategy_v3_from_record(store.strategy("DRAFT")))
            if store.strategy("DRAFT")
            else None,
            "strategyV3Pending": strategy_v3_api_values(strategy_v3_from_record(store.strategy("PENDING")))
            if store.strategy("PENDING")
            else None,
            "activeStrategy": {**active, "engine": policy.strategy_engine},
            "supportedCurrencies": list(SUPPORTED_CURRENCIES),
            "adaptiveTemplate": template_info(ENGINE),
            "adaptiveTemplates": {
                engine: template_info(engine) for engine in REGISTRY
            },
            "candidateModels": {engine: model_info(model) for engine, model in models.items()},
            "modelDetails": details,
            "candidateModel": model_info(candidate),
            "research": self.research.status(currency),
        }

    def runtime(self):
        import lendingbot as app

        return {
            currency: app.runtime_v3_payload(self.config_path, self.context, currency=currency)
            for currency in SUPPORTED_CURRENCIES
        }

    def statistics(self):
        import lendingbot as app

        return {
            currency: app.stats_v3_payload(app.v3_store_for_config(self.config_path, currency)[0])
            for currency in SUPPORTED_CURRENCIES
        }

    def status(self, currency):
        import lendingbot as app

        currency = require_currency(currency)
        raw = app.read_status_payload(self.status_path)
        status = (raw.get("currencies") or {}).get(currency)
        if status is None:
            status = (
                raw
                if currency == "USD" and not raw.get("currencies")
                else {
                    "schemaVersion": 3,
                    "snapshotAvailable": False,
                }
            )
        status = dict(status)
        store, _ = app.v3_store_for_config(self.config_path, currency)
        status.update(
            currency=currency,
            runtime=store.runtime(),
            recovery=store.recovery_status(),
            operationMode=store.runtime()["mode"],
            outputCurrency={"currency": currency},
            realizedIncome=store.realized_income_summary(currency),
            incomeHistorySync=store.income_history_sync_payload(currency),
            releaseComparison=store.release_comparison_v4(),
        )
        snapshot_time = status.get("last_update")
        if not raw.get("currencies"):
            snapshot_time = snapshot_time or raw.get("last_update")
        status["last_update"] = snapshot_time or raw.get("last_update")
        status["log"] = raw.get("log", [])
        control = dict(app.controlled_bot_status(self.config_path, self.context))
        # Global Worker health cannot make a stale or absent currency snapshot fresh.
        snapshot_time = snapshot_time if status.get("snapshotAvailable", True) else None
        age = None
        if snapshot_time:
            try:
                age = self.context.now() - datetime.datetime.fromisoformat(str(snapshot_time)).timestamp()
            except (ValueError, TypeError, OverflowError):
                pass
        control["dataAgeSeconds"] = None if age is None else max(0, int(age))
        control["sourceFresh"] = bool(control.get("running") and age is not None and 0 <= age <= 60)
        status["control"] = control
        for key in ("sourceFresh", "dataAgeSeconds", "lastLifecycleEvent", "stopReason"):
            status[key] = control.get(key)
        active = store.strategy("ACTIVE")
        if active:
            status["activeStrategy"] = {**active, "engine": strategy_v3_from_record(active).strategy_engine}
        status["versionPerformance"] = version_performance(store, active)
        return json_decimal(status)

    def preflight(self, currencies, issue_token=True):
        import lendingbot as app

        selected = tuple(require_currency(currency) for currency in currencies)
        if not selected or len(set(selected)) != len(selected):
            raise ConfigError("select one or both distinct currencies")
        settings = load_profiles(self.config_path)
        digest = app.config_sha256(self.config_path)
        profiles = {}
        client = self._client(settings)
        gate = FundingWriteGate(client, {}, self.context.now)
        for currency in selected:
            try:
                if currency not in settings.enabled_currencies:
                    raise ConfigError(f"{currency} 尚未启用，请在该币种总览中勾选启用放贷，等待保存成功后重新预检。")
                with funding_sizing(gate.minimum(currency)):
                    profiles[currency] = app.evaluate_live_preflight(
                        self.config_path,
                        client_factory=lambda *_args: client,
                        context=self.context,
                        currency=currency,
                    )
            except Exception as exc:
                profiles[currency] = {
                    "checks": [{"id": "config", "label": "币种启用与配置", "status": "fail", "detail": str(exc)}],
                    "warnings": [],
                    "summary": {"strategyVersion": 4},
                }
        checks = [
            {**check, "id": f"{currency}:{check['id']}", "label": f"{currency} · {check['label'].replace('V3', 'V4')}"}
            for currency, profile in profiles.items()
            for check in profile["checks"]
        ]
        warnings = [
            {**warning, "message": f"{currency} · {warning['message'].replace('V3', 'V4')}"}
            for currency, profile in profiles.items()
            for warning in profile["warnings"]
        ]
        if app.config_sha256(self.config_path) != digest:
            checks.append(
                {
                    "id": "config_changed",
                    "label": "配置稳定性",
                    "status": "fail",
                    "detail": "配置在预检过程中变化，请重新预检",
                }
            )
        can_start = bool(checks) and all(check["status"] == "pass" for check in checks)
        expires = self.context.now() + app.PREFLIGHT_TTL_SECONDS
        token = secrets.token_urlsafe(24) if can_start and issue_token else None
        response = {
            "canStart": can_start,
            "preflightId": token,
            "expiresAt": datetime.datetime.fromtimestamp(expires, datetime.timezone.utc).isoformat(),
            "checks": checks,
            "warnings": warnings,
            "currencies": list(selected),
            "profiles": profiles,
            "summary": {**profiles[selected[0]]["summary"], "currency": selected[0], "strategyVersion": 4},
        }
        if token:
            with self.context.process_state.lock:
                self.tokens = {
                    key: value for key, value in self.tokens.items() if value["expires"] >= self.context.now()
                }
                if len(self.tokens) >= 128:
                    self.tokens.clear()
                self.tokens[token] = {
                    "expires": expires,
                    "digest": digest,
                    "build": app.worker_build_id(),
                    "currencies": selected,
                    "profiles": profiles,
                }
        return response

    @staticmethod
    def _binding(summary):
        return {
            key: summary.get(key)
            for key in (
                "activeStrategyVersion",
                "policyHash",
                "planHash",
                "modelHash",
                "operationalReportHash",
                "accountDigest",
                "externalAdoptionDigest",
                "pendingCancellations",
            )
        }

    def start(self, token, currencies):
        import lendingbot as app

        selected = tuple(require_currency(currency) for currency in currencies)
        with self.context.process_state.lock:
            saved = self.tokens.pop(str(token), None)
            if (
                saved is None
                or saved["currencies"] != selected
                or saved["expires"] < self.context.now()
                or saved["digest"] != app.config_sha256(self.config_path)
                or saved["build"] != app.worker_build_id()
            ):
                raise ConfigError("预检已过期、已使用或币种/配置发生变化，请重新预检")
            refreshed = self.preflight(selected, issue_token=False)
            if not refreshed["canStart"] or any(
                self._binding(saved["profiles"][currency]["summary"])
                != self._binding(refreshed["profiles"][currency]["summary"])
                for currency in selected
            ):
                raise ConfigError("账户、策略或挂单集合发生变化，请重新预检")
            return self._launch(selected)

    def _launch(self, selected, recovering=False):
        import lendingbot as app

        state = self.context.process_state
        running = app.controlled_bot_running(self.config_path, self.context)
        settings = load_profiles(self.config_path)
        stores = stores_for_profiles(settings, self.context.now)
        if running:
            metadata = app.LiveProcessLock.inspect(self.context.live_lock_path).get("metadata") or {}
            if metadata.get("v4") is not True:
                raise ConfigError("正在运行旧版本 Worker，请先停止再启动 V4")
        # Recovered workers preserve every durable pause. Fresh starts only authorize selected currencies.
        if not recovering:
            for currency in selected:
                store = stores[currency]
                if store.runtime().get("safe_manual"):
                    raise ConfigError("存在需要人工处理的未决写入")
                store.authorize_live_after_preflight(revalidated_adaptive=True)
        if not running:
            os.makedirs(os.path.dirname(self.context.process_log_path), exist_ok=True)
            app.cleanup_controlled_bot_handle(self.context)
            state.log_handle = open(self.context.process_log_path, "ab", buffering=0)
            command = [
                sys.executable,
                os.path.join(self.context.project_root, "lendingbot.py"),
                "--config",
                self.config_path,
                "--live",
                "--v4-worker",
                "--confirmed-preflight",
                "--currencies",
                ",".join(selected),
                "--no-server",
                "--json",
                self.status_path,
                "--jsonsize",
                "200",
            ]
            startupinfo = None
            flags = 0
            if os.name == "nt":
                flags = subprocess.CREATE_NEW_PROCESS_GROUP
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            try:
                state.process = subprocess.Popen(
                    command,
                    cwd=self.context.project_root,
                    stdin=subprocess.DEVNULL,
                    stdout=state.log_handle,
                    stderr=subprocess.STDOUT,
                    startupinfo=startupinfo,
                    creationflags=flags,
                )
            except Exception:
                app.cleanup_controlled_bot_handle(self.context)
                for currency in selected:
                    stores[currency].set_mode("PAUSED", "dashboard_stop")
                raise
            state.started_at = app.timestamp()
            Lifecycle.record(
                self.context,
                "WORKER_STARTED",
                pid=state.process.pid,
                currencies=selected,
                authorized=True,
                reason="supervisor_recovery" if recovering else "confirmed_preflight",
            )
        old = state.auto_restart_authorization or {}
        authorized = sorted(set(old.get("currencies", ())) | set(selected))
        state.auto_restart_authorization = {
            "v4": True,
            "session": state.supervisor_session,
            "configDigest": app.config_sha256(self.config_path),
            "controlDigest": restart_control_digest(self.config_path),
            "buildId": app.worker_build_id(),
            "currencies": authorized,
            "authorizedAt": self.context.now(),
            "strategies": {currency: stores[currency].strategy("ACTIVE")["version_id"] for currency in authorized},
        }
        state.stop_reason = None
        return app.controlled_bot_status(self.config_path, self.context)

    def pause(self, currency):
        import lendingbot as app

        currency = require_currency(currency)
        with self.context.process_state.lock:
            store, _ = app.v3_store_for_config(self.config_path, currency)
            runtime = store.pause_currency("dashboard_pause")
            authorization = self.context.process_state.auto_restart_authorization
            if authorization and authorization.get("v4"):
                authorization["currencies"] = [coin for coin in authorization["currencies"] if coin != currency]
            return runtime

    def stop(self):
        import lendingbot as app

        with self.context.process_state.lock:
            result = app.stop_controlled_bot(self.config_path, context=self.context)
            for currency in SUPPORTED_CURRENCIES:
                store, _ = app.v3_store_for_config(self.config_path, currency)
                store.pause_currency("dashboard_stop")
            return result

    def settings(self, payload):
        import lendingbot as app

        currency = require_currency(payload.get("currency"))
        if not isinstance(payload.get("enabled"), bool) or not isinstance(payload.get("autoTransfer"), bool):
            raise ConfigError("enabled and autoTransfer must be booleans")
        with self.context.process_state.lock:
            store, _ = app.v3_store_for_config(self.config_path, currency)
            if store.runtime()["mode"] == "LIVE" or store.recovery_status()["active"]:
                raise ConfigError("请先暂停该币种，再修改启用和钱包转入设置")
            settings = load_profiles(self.config_path)
            transfers = set(settings.transferable_currencies)
            if payload["autoTransfer"]:
                transfers.add(currency)
            else:
                transfers.discard(currency)
            update_config_file_preserving_comments(
                self.config_path,
                {
                    f"STRATEGY_V4_{currency}": {"enabled": str(payload["enabled"]).lower()},
                    "BOT": {"transferablecurrencies": ",".join(sorted(transfers))},
                },
            )
            return self.config(currency)


def run_worker(args, settings, context, log):
    import lendingbot as app

    selected = (
        tuple(require_currency(item) for item in args.currencies.split(","))
        if args.currencies
        else settings.enabled_currencies
    )
    if not selected or len(set(selected)) != len(selected):
        raise ConfigError("select enabled USD and/or USDT currencies")
    service = V4DashboardService(args.config, context.status_path, context)
    if not args.confirmed_preflight:
        preflight = service.preflight(selected, issue_token=False)
        if not preflight["canStart"]:
            for check in preflight["checks"]:
                if check["status"] != "pass":
                    log.log(f"实盘预检失败：{check['label']} — {check['detail']}")
            return 1
        if input(f"只读预检通过，币种 {','.join(selected)}。输入 LIVE 确认：").strip() != "LIVE":
            return 1
    lock = app.LiveProcessLock(context.live_lock_path)
    if not lock.acquire(
        args.config,
        {
            "role": "live_worker",
            "service": "mika-lending-worker-v3",
            "v4": True,
            "buildId": app.worker_build_id(),
            "currencies": list(selected),
        },
    ):
        raise ConfigError("另一个机器人已持有 LIVE 锁")
    coordinator = None
    stores = {}
    try:
        stores = stores_for_profiles(settings, context.now)
        policies = {}
        for currency, store in stores.items():
            _, policy = ensure_active_strategy_v3(store, settings_for_currency(settings, currency))
            policies[currency] = policy
            if currency in selected:
                if currency not in settings.enabled_currencies:
                    raise ConfigError(f"{currency} is disabled")
                from StrategyV3 import validate_policy_v3

                validate_policy_v3(policy, require_live_floors=True)
                store.authorize_live_after_preflight()
            else:
                store.pause_currency("dashboard_pause")
        coordinator = V4Coordinator(
            service._client(settings),
            stores,
            policies,
            settings,
            on_policy_activated=lambda policy, _version: app.mirror_active_strategy_v3(args.config, policy),
            clock=context.now,
        )
        coordinator.start()
        if settings.web_server:
            threading.Thread(
                target=app.start_web_server, args=(log, args.config, context.status_path, context), daemon=True
            ).start()
        while True:
            current_settings = load_profiles(args.config)
            coordinator.gate.require_wallet_write = bool(current_settings.transferable_currencies)
            for currency, runtime in coordinator.runtimes.items():
                runtime.auto_transfer_wallets = (
                    tuple(current_settings.transfer_from_wallets)
                    if currency in current_settings.transferable_currencies
                    else ()
                )
                if currency not in current_settings.enabled_currencies:
                    if runtime.store.runtime()["mode"] == "LIVE":
                        runtime.store.pause_currency("dashboard_pause")
            statuses = coordinator.cycle()
            for currency, status in statuses.items():
                status["runtime"] = stores[currency].runtime()
                status["recovery"] = stores[currency].recovery_status()
                status["operationMode"] = status["runtime"]["mode"]
            for key, value in json_decimal(statuses.get("USD", {})).items():
                log.updateMetaValue(key, value)
            log.updateMetaValue("v4SchemaVersion", 4)
            log.updateMetaValue("currencies", json_decimal(statuses))
            log.persistStatus()
            if args.once:
                break
            time.sleep(
                min(
                    settings.sleep_active,
                    10
                    if any(
                        runtime.policy.strategy_engine in REGISTRY
                        for runtime in coordinator.runtimes.values()
                    )
                    else 30,
                )
            )
    except KeyboardInterrupt:
        pass
    finally:
        if coordinator is not None:
            coordinator.shutdown()
        if settings.web_server:
            app.stop_web_server(log, context)
        for store in stores.values():
            try:
                if not store.runtime().get("safe_reason"):
                    store.set_mode("PAUSED", "live_process_stopped")
            except sqlite3.Error:
                pass
        lock.release()
    return 0


def supervisor_tick(config_path, status_path, context):
    """Restart only this dashboard session's explicitly authorized currencies."""
    import lendingbot as app

    state = context.process_state
    with state.lock:
        authorization = state.auto_restart_authorization
        if not authorization or not authorization.get("v4") or authorization.get("session") != state.supervisor_session:
            return
        selected = authorization["currencies"]
        if not selected:
            return
        settings = load_profiles(config_path)
        stores = stores_for_profiles(settings, context.now)
        # A confirmed ACTIVE policy mirror may change the file hash. Only renew
        # it when every policy matches durable ACTIVE and control settings match.
        if (
            authorization["configDigest"] != app.config_sha256(config_path)
            and authorization.get("controlDigest") == restart_control_digest(config_path)
            and all(
                stores[currency].strategy("ACTIVE")
                and strategy_v3_config_values(settings.policies[currency])
                == strategy_v3_config_values(strategy_v3_from_record(stores[currency].strategy("ACTIVE")))
                for currency in SUPPORTED_CURRENCIES
            )
        ):
            authorization["configDigest"] = app.config_sha256(config_path)
        now_ms = int(context.now() * 1000)
        status = app.controlled_bot_status(config_path, context)
        if status["running"]:
            baseline = int(authorization["authorizedAt"] * 1000)
            stale = any(
                now_ms - max(baseline, int(stores[currency].recovery_status().get("heartbeatAt") or 0))
                >= app.WORKER_HEARTBEAT_TIMEOUT_MS
                for currency in selected
            )
            if not stale:
                # The worker may legitimately activate a confirmed PENDING policy.
                authorization["strategies"] = {
                    currency: stores[currency].strategy("ACTIVE")["version_id"] for currency in selected
                }
                return
            Lifecycle.record(
                context,
                "HEARTBEAT_TIMEOUT",
                pid=status.get("pid"),
                currencies=selected,
                authorized=True,
                reason="worker_heartbeat_timeout",
                heartbeatAtMs=min(
                    int(stores[currency].recovery_status().get("heartbeatAt") or 0) for currency in selected
                ),
            )
            app.stop_controlled_bot(
                config_path, reason="worker_heartbeat_timeout", context=context, preserve_authorization=True
            )
        valid = (
            authorization["configDigest"] == app.config_sha256(config_path)
            and authorization["buildId"] == app.worker_build_id()
            and all(
                currency in settings.enabled_currencies
                and authorization["strategies"][currency] == stores[currency].strategy("ACTIVE")["version_id"]
                for currency in selected
            )
        )
        if not valid:
            state.auto_restart_authorization = None
            for currency in selected:
                stores[currency].set_mode("PAUSED", "dashboard_pause")
            return
        service = V4DashboardService(config_path, status_path, context)
        for currency in selected:
            store = stores[currency]
            if store.runtime().get("safe_manual"):
                return
            if not store.recovery_status()["active"]:
                store.set_mode("PAUSED", "watchdog_worker_exit")
                store.begin_recovery("WORKER_EXIT", "V4 Worker interrupted", origin_mode="LIVE", target_mode="LIVE")
            if not store.recovery_probe_due(now_ms):
                return
        preflight = service.preflight(selected, issue_token=False)
        if not preflight["canStart"]:
            for currency in selected:
                stores[currency].record_recovery_failure("watchdog preflight failed", "WATCHDOG_PREFLIGHT", now_ms)
            return
        restarted = service._launch(selected, recovering=True)
        Lifecycle.record(
            context,
            "SUPERVISOR_RESTART",
            pid=(restarted or {}).get("pid"),
            currencies=selected,
            authorized=True,
            reason="revalidated_recovery",
        )
