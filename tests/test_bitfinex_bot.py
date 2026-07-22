import configparser
import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from bitfinex import Bitfinex, BitfinexApiError
from FileUtils import atomic_write_text
import lendingbot


class CaptureLog:
    def __init__(self):
        self.offers = []
        self.cancels = []
        self.lines = []
        self.status = {}
        self.meta = {}

    def offer(self, amt, cur, rate, days, msg):
        self.offers.append((amt, cur, rate, days, msg))

    def cancelOrders(self, cur, msg):
        self.cancels.append((cur, msg))

    def log(self, msg):
        self.lines.append(str(msg))

    def updateStatusValue(self, coin, key, value):
        self.status.setdefault(coin, {})[key] = str(value)

    def updateMetaValue(self, key, value):
        self.meta[key] = value

    def refreshStatus(self, message):
        self.meta["lastStatus"] = message


class CancelCaptureClient:
    def __init__(self):
        self.canceled = []

    def cancel_funding_offer(self, offer_id):
        self.canceled.append(offer_id)
        return [0, "fon-cancel", None, None, None, None, "SUCCESS", "canceled"]


class OfferCaptureClient:
    def __init__(self):
        self.submitted = []

    def submit_funding_offer(self, *args):
        self.submitted.append(args)
        return [0, "fon-submit", None, None, None, None, "SUCCESS", "submitted"]


class LedgerClient:
    def __init__(self, rows_by_currency):
        self.rows_by_currency = rows_by_currency
        self.calls = []

    def has_credentials(self):
        return True

    def ledgers(self, currency=None, **kwargs):
        self.calls.append((currency, kwargs))
        return self.rows_by_currency.get(currency, [])


class FakePreflightClient:
    permissions = [
        ["wallets", 1, 0],
        ["funding", 1, 1],
        ["withdraw", 0, 0],
        ["ui_withdraw", 0, 0],
    ]
    wallet_rows = [
        ["funding", "USD", "500", None, "450"],
        ["exchange", "USD", "80", None, "75"],
    ]

    def __init__(self, key, secret):
        self.key = key
        self.secret = secret

    def has_credentials(self):
        return bool(self.key and self.secret)

    def key_permissions(self):
        return self.permissions

    def wallets(self):
        return self.wallet_rows

    def funding_book(self, symbol, length=250):
        return [["0.0004", 2, 1, "1000"]]


class FakeControlledProcess:
    def __init__(self, pid=4321):
        self.pid = pid
        self.return_code = None

    def poll(self):
        return self.return_code

    def terminate(self):
        self.return_code = 0

    def kill(self):
        self.return_code = -9

    def wait(self, timeout=None):
        return self.return_code


def write_test_config(path, extra_bot=None):
    bot = {
        "mindailyrate": "0.04",
        "maxdailyrate": "2",
        "spreadlend": "3",
        "minloansize": "150",
        "maxtolent": "400",
        "statedbfile": os.path.join(os.path.dirname(path), "state.sqlite3"),
    }
    bot.update(extra_bot or {})
    config = configparser.ConfigParser()
    config.read_dict({
        "BITFINEX": {"apikey": "key", "secret": "secret", "currencies": "USD"},
        "BOT": bot,
        "STRATEGY_V3": {
            "short_floor_apr": "6",
            "medium_floor_apr": "8",
            "long_floor_apr": "10",
            "enable_limit": "true",
            "enable_frr": "false",
            "enable_frr_delta_fixed": "false",
            "enable_frr_delta_variable": "false",
        },
    })
    with open(path, "w", encoding="utf-8") as file:
        config.write(file)


def build_strategy_settings(extra_bot=None):
    bot = {
        "mindailyrate": "0.04",
        "maxdailyrate": "2",
        "spreadlend": "3",
        "minloansize": "150",
        "smartstrategy": "true",
    }
    bot.update(extra_bot or {})
    config = configparser.ConfigParser()
    config.read_dict({
        "BITFINEX": {"apikey": "key", "secret": "secret", "currencies": "USD"},
        "BOT": bot,
    })
    settings = lendingbot.build_settings(lendingbot.parse_args([]), config)
    lendingbot.validate_settings(settings)
    return settings


class BitfinexBotTests(unittest.TestCase):
    def setUp(self):
        lendingbot.controlled_bot_preflight = None

    def test_auth_headers_signature(self):
        client = Bitfinex("key", "secret")
        path = "v2/auth/r/wallets"
        nonce = "12345"
        headers = client.auth_headers_for_test(path, nonce, {})
        payload = "/api/" + path + nonce + "{}"
        expected = hmac.new(b"secret", payload.encode("utf-8"), hashlib.sha384).hexdigest()
        self.assertEqual(headers["bfx-apikey"], "key")
        self.assertEqual(headers["bfx-nonce"], nonce)
        self.assertEqual(headers["bfx-signature"], expected)

    def test_nonce_is_monotonic(self):
        client = Bitfinex("key", "secret")
        first = int(client._nonce())
        client._last_nonce = max(first + 10, int(time.time() * 1000000) + 1000000)
        previous = client._last_nonce
        self.assertEqual(int(client._nonce()), previous + 1)

    def test_authenticated_requests_serialize_monotonic_nonces(self):
        client = Bitfinex("key", "secret")
        observed = []
        active = 0
        maximum_active = 0
        request_lock = threading.Lock()

        def fake_request(*args, **kwargs):
            nonlocal active, maximum_active
            with request_lock:
                active += 1
                maximum_active = max(maximum_active, active)
                observed.append(int(kwargs["headers"]["bfx-nonce"]))
            time.sleep(0.005)
            with request_lock:
                active -= 1
            return []

        with mock.patch.object(client, "_request_json", side_effect=fake_request):
            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(lambda _: client.wallets(), range(24)))
        self.assertEqual(maximum_active, 1)
        self.assertEqual(observed, sorted(observed))
        self.assertEqual(len(observed), len(set(observed)))

    def test_key_permissions_uses_official_endpoint(self):
        client = Bitfinex("key", "secret")
        with mock.patch.object(client, "_auth_post", return_value=[]) as auth_post:
            client.key_permissions()
        auth_post.assert_called_once_with("v2/auth/r/permissions")

    def test_write_notification_rejects_http_200_business_error(self):
        client = Bitfinex("key", "secret")
        response = [123, "fon-req", None, None, None, 10020, "ERROR", "not enough balance"]
        with mock.patch.object(client, "_auth_post", return_value=response):
            with self.assertRaisesRegex(BitfinexApiError, "not enough balance"):
                client.submit_funding_offer("fUSD", "150", "0.0004", 2)

    def test_write_notification_accepts_explicit_success(self):
        client = Bitfinex("key", "secret")
        response = [123, "fon-req", None, None, [], None, "SUCCESS", "submitted"]
        with mock.patch.object(client, "_auth_post", return_value=response):
            self.assertIs(client.cancel_funding_offer(123), response)

    def test_dryrun_argument_is_rejected(self):
        with self.assertRaises(SystemExit):
            lendingbot.parse_args(["--dryrun"])

    def test_bot_requires_explicit_live_flag(self):
        with mock.patch.object(lendingbot, "read_config") as read_config:
            self.assertEqual(lendingbot.main([]), 1)
        read_config.assert_not_called()

    def test_dashboard_is_available_without_live_flag(self):
        args = lendingbot.parse_args(["--dashboard"])
        self.assertTrue(args.dashboard)
        self.assertFalse(args.live)

    def test_dashboard_main_never_constructs_trading_client(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "test.cfg")
            write_test_config(path, {"statedbfile": os.path.join(directory, "state.sqlite3")})
            fake_thread = mock.Mock()
            with mock.patch.object(lendingbot.threading, "Thread", return_value=fake_thread), mock.patch.object(
                lendingbot.time, "sleep", side_effect=KeyboardInterrupt
            ), mock.patch.object(lendingbot, "stop_controlled_bot"), mock.patch.object(
                lendingbot, "stop_web_server"
            ), mock.patch.object(
                lendingbot, "reconcile_orphaned_live_runtime"
            ), mock.patch.object(
                lendingbot.LiveProcessLock, "acquire", return_value=True
            ), mock.patch.object(
                lendingbot.LiveProcessLock, "release"
            ), mock.patch.object(lendingbot, "Bitfinex", side_effect=AssertionError("dashboard must not create API client")):
                self.assertEqual(lendingbot.main(["--dashboard", "--config", path]), 0)
        fake_thread.start.assert_called_once_with()

    def test_funding_book_keeps_positive_offers(self):
        rows = [[0.0002, 2, 1, -1000], [0.00015, 2, 1, 300], [0.0001, 30, 1, 200]]
        offers = lendingbot.parse_funding_book(rows)
        self.assertEqual([offer["amount"] for offer in offers], [Decimal("200"), Decimal("300")])

    def test_open_offers_are_sanitized_for_status_schema(self):
        rows = [[123, "fUSD", 1000, 2000, "150", "150", "LIMIT", None, None, 0, "ACTIVE", None, None, None, "0.0004", 2]]
        serialized = lendingbot.serialize_open_offers(lendingbot.parse_open_offers(rows))
        self.assertEqual(serialized[0]["id"], "123")
        self.assertEqual(serialized[0]["dailyRatePercent"], "0.04")
        self.assertEqual(serialized[0]["status"], "ACTIVE")
        self.assertNotIn("symbol", serialized[0])

    def test_rate_at_depth_clamps_to_minimum(self):
        book = [{"rate": Decimal("0.00018"), "period": 2, "amount": Decimal("1000")}]
        self.assertEqual(lendingbot.rate_at_depth(book, Decimal("300"), Decimal("0.0004"), Decimal("0.02")), Decimal("0.0004"))

    def test_smart_strategy_follows_market_floor(self):
        settings = build_strategy_settings({"smartrateoffset": "0.001"})
        book = [{"rate": Decimal("0.00018"), "period": 2, "amount": Decimal("1000")}]
        self.assertEqual(lendingbot.smart_min_rate_for(settings, "USD", book), Decimal("0.0004"))

    def test_smart_market_reference_ignores_dust_offer(self):
        settings = build_strategy_settings({"smartrateoffset": "0.001", "smartfloordepth": "2"})
        book = [
            {"rate": Decimal("0.0001"), "period": 2, "amount": Decimal("1")},
            {"rate": Decimal("0.0005"), "period": 2, "amount": Decimal("1000")},
        ]
        reference = lendingbot.smart_market_reference_rate(settings, "USD", book, Decimal("1000"))
        self.assertEqual(reference, Decimal("0.0005"))
        self.assertEqual(
            lendingbot.smart_min_rate_for(settings, "USD", book, Decimal("1000")),
            Decimal("0.00051"),
        )

    def test_smart_offer_plan_allocates_long_high_rate_bucket(self):
        settings = build_strategy_settings({
            "smartfastshare": "50",
            "smartlongshare": "40",
            "smartopportunitypremium": "0.01",
            "xdays": "60",
        })
        book = [
            {"rate": Decimal("0.0002"), "period": 2, "amount": Decimal("1")},
            {"rate": Decimal("0.0005"), "period": 2, "amount": Decimal("1000")},
            {"rate": Decimal("0.0007"), "period": 30, "amount": Decimal("5000")},
        ]
        plan = lendingbot.build_offer_plan(book, Decimal("1000"), Decimal("0"), settings, "USD")
        self.assertEqual([item["bucket"] for item in plan], ["fast", "balanced", "long"])
        self.assertEqual([item["amount"] for item in plan], [
            Decimal("425.00000000"),
            Decimal("205.00000000"),
            Decimal("370.00000000"),
        ])
        self.assertEqual(sum((item["amount"] for item in plan), Decimal("0")), Decimal("1000"))
        self.assertEqual([item["period"] for item in plan], [2, 7, 60])
        self.assertGreater(plan[-1]["rate"], plan[0]["rate"])

    def test_two_part_plan_still_has_long_bucket(self):
        settings = build_strategy_settings({"spreadlend": "2", "xdays": "30"})
        book = [{"rate": Decimal("0.0005"), "period": 2, "amount": Decimal("5000")}]
        plan = lendingbot.build_offer_plan(book, Decimal("400"), Decimal("0"), settings, "USD")
        self.assertEqual([item["bucket"] for item in plan], ["fast", "long"])
        self.assertEqual(plan[-1]["period"], 60)
        self.assertEqual(sum((item["amount"] for item in plan), Decimal("0")), Decimal("400"))

    def test_shallow_book_uses_last_market_rate_not_maximum_cap(self):
        settings = build_strategy_settings()
        book = [{"rate": Decimal("0.0005"), "period": 2, "amount": Decimal("50")}]
        rates = lendingbot.choose_offer_rates(book, Decimal("1000"), settings, "USD", 3)
        self.assertTrue(all(rate < Decimal("0.001") for rate in rates))

    def test_long_offer_uses_longer_reprice_wait(self):
        settings = build_strategy_settings({
            "xdays": "60",
            "repriceafterminutes": "15",
            "smartlongwaitminutes": "120",
        })
        now_ms = 10_000_000
        created = now_ms - 60 * 60 * 1000
        offers = {"USD": [
            {"id": 1, "amount": Decimal("150"), "rate": Decimal("0.0005"), "period": 2, "created": created},
            {"id": 2, "amount": Decimal("150"), "rate": Decimal("0.0007"), "period": 60, "created": created},
        ]}
        fresh, aged = lendingbot.split_stale_open_offers(settings, offers, now_ms)
        self.assertEqual([item["id"] for item in aged["USD"]], [1])
        self.assertEqual([item["id"] for item in fresh["USD"]], [2])

    def test_market_reprice_guard_avoids_small_rate_churn(self):
        settings = build_strategy_settings({"repriceminratedelta": "0.002"})
        book = [{"rate": Decimal("0.0005"), "period": 2, "amount": Decimal("5000")}]
        aged = {"USD": [
            {"id": 1, "amount": Decimal("150"), "rate": Decimal("0.00052"), "period": 2},
            {"id": 2, "amount": Decimal("150"), "rate": Decimal("0.00060"), "period": 2},
        ]}
        fresh, reprice = lendingbot.filter_market_reprice_candidates(
            settings, {}, aged, {"USD": book}, {"USD": Decimal("1000")}
        )
        self.assertEqual([item["id"] for item in fresh["USD"]], [1])
        self.assertEqual([item["id"] for item in reprice["USD"]], [2])

    def test_lending_cycle_uses_cached_book_and_fake_client_only(self):
        settings = build_strategy_settings({"xdays": "60"})
        book = [
            {"rate": Decimal("0.0005"), "period": 2, "amount": Decimal("1000")},
            {"rate": Decimal("0.0007"), "period": 30, "amount": Decimal("5000")},
        ]
        client = OfferCaptureClient()
        log = CaptureLog()
        usable = lendingbot.lend_available_balances(
            client,
            settings,
            log,
            {"USD": Decimal("1000")},
            {},
            {},
            {"USD": Decimal("0")},
            {},
            {"USD": book},
        )
        self.assertEqual(usable, 1)
        self.assertEqual(len(client.submitted), 3)
        self.assertEqual(sum((Decimal(call[1]) for call in client.submitted), Decimal("0")), Decimal("1000"))
        self.assertEqual([call[3] for call in client.submitted], [2, 7, 60])
        self.assertEqual(log.status["USD"]["plannedOfferCount"], "3")

    def test_submit_offer_always_calls_real_client(self):
        settings = SimpleNamespace(xday_threshold=Decimal("0.002"), xdays=60)
        client = OfferCaptureClient()
        log = CaptureLog()
        lendingbot.submit_offer(client, settings, log, "USD", Decimal("150"), Decimal("0.0004"))
        self.assertEqual(client.submitted, [("fUSD", "150.00000000", "0.00040000", 2, "LIMIT")])
        self.assertEqual(len(log.offers), 1)

    def test_stale_cancel_always_uses_offer_ids(self):
        settings = SimpleNamespace(currencies=["USD"])
        client = CancelCaptureClient()
        amounts = lendingbot.cancel_stale_offers(client, settings, CaptureLog(), {"USD": [{"id": 11, "amount": Decimal("150")}, {"id": 12, "amount": Decimal("200")}]})
        self.assertEqual(client.canceled, [11, 12])
        self.assertEqual(amounts["USD"], Decimal("350"))

    def test_preflight_permission_parser(self):
        permissions = lendingbot.parse_key_permissions([["wallets", "1", "0"], ["funding", 1, 1]])
        self.assertTrue(permissions["wallets"]["read"])
        self.assertFalse(permissions["wallets"]["write"])
        self.assertTrue(permissions["funding"]["write"])

    def test_v3_preview_and_preflight_share_version_plan_hash_and_order_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "test.cfg")
            write_test_config(path)
            config_payload = lendingbot.config_api_payload(path)
            preview = lendingbot.strategy_v3_preview(
                path, {"strategyV3": config_payload["strategyV3"]}, FakePreflightClient,
                now_ms=2_000_000_000_000,
            )
            preflight = lendingbot.evaluate_live_preflight(path, FakePreflightClient)
        self.assertEqual(preview["proposedVersion"], preflight["summary"]["activeStrategyVersion"])
        self.assertEqual(preview["plan"]["plan_hash"], preflight["summary"]["planHash"])
        preview_orders = [
            (row["display_type"], row["amount"], row["period"], row["submitted_rate"], row["flags"])
            for row in preview["plan"]["plan"]
        ]
        preflight_orders = [
            (row["display_type"], row["amount"], row["period"], row["submitted_rate"], row["flags"])
            for row in preflight["summary"]["strategyPlan"]
        ]
        self.assertEqual(preview_orders, preflight_orders)

    def test_v3_draft_does_not_write_config_and_blocks_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "test.cfg")
            write_test_config(path)
            config_before = open(path, "rb").read()
            payload = lendingbot.config_api_payload(path)["strategyV3"]
            payload["target_slices"] = 12
            preview = lendingbot.strategy_v3_preview(path, {"strategyV3": payload}, FakePreflightClient)
            saved = lendingbot.save_strategy_v3_draft(path, {
                "strategyV3": payload,
                "previewToken": preview["previewToken"],
            })
            result = lendingbot.evaluate_live_preflight(path, FakePreflightClient)
            self.assertEqual(open(path, "rb").read(), config_before)
        self.assertEqual(saved["status"], "DRAFT")
        state_check = next(row for row in result["checks"] if row["id"] == "strategy_state")
        self.assertEqual(state_check["status"], "fail")

    def test_v3_saving_loaded_active_policy_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "test.cfg")
            write_test_config(path)
            loaded = lendingbot.config_api_payload(path)["strategyV3"]
            preview = lendingbot.strategy_v3_preview(path, {"strategyV3": loaded}, FakePreflightClient)
            saved = lendingbot.save_strategy_v3_draft(path, {
                "strategyV3": loaded,
                "previewToken": preview["previewToken"],
            })
            store, _ = lendingbot.v3_store_for_config(path)
            draft = store.strategy("DRAFT")
        self.assertEqual(saved["status"], "UNCHANGED")
        self.assertIsNone(draft)

    def test_config_payload_exposes_only_v3_strategy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "test.cfg")
            write_test_config(path)
            payload = lendingbot.config_api_payload(path)
        self.assertNotIn("strategy", payload)
        self.assertEqual(payload["strategyV3"]["version"], 3)

    def test_v3_apply_is_bound_to_exact_saved_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "test.cfg")
            write_test_config(path)
            policy = lendingbot.config_api_payload(path)["strategyV3"]
            policy["target_slices"] = 12
            preview = lendingbot.strategy_v3_preview(path, {"strategyV3": policy}, FakePreflightClient)
            saved = lendingbot.save_strategy_v3_draft(path, {
                "strategyV3": policy,
                "previewToken": preview["previewToken"],
            })
            with self.assertRaisesRegex(lendingbot.ApiRequestError, "草稿"):
                lendingbot.apply_strategy_v3_draft(path, {
                    "draftVersionId": "another-tab-draft",
                    "applyToken": saved["applyToken"],
                }, FakePreflightClient)

    def test_fixed_credit_is_limit_and_unknown_variable_is_not_false_disabled(self):
        policy = lendingbot.StrategyPolicyV3(
            short_floor_apr=Decimal("6"), medium_floor_apr=Decimal("8"), long_floor_apr=Decimal("10"),
            enable_limit=True, enable_frr=False, enable_frr_delta_fixed=False, enable_frr_delta_variable=False,
        )
        fixed = {"rate_type": "FIXED", "period": 30, "rate": "0.001", "rate_real": "0.001"}
        variable = {"rate_type": "VARIABLE", "period": 30, "rate": "0.001", "rate_real": "0.001"}
        self.assertEqual(lendingbot.v3_credit_display_type(fixed), "LIMIT")
        self.assertNotIn("disabled_type", lendingbot.v3_credit_violations(fixed, policy))
        self.assertEqual(lendingbot.v3_credit_display_type(variable), "VARIABLE_UNKNOWN")
        self.assertNotIn("disabled_type", lendingbot.v3_credit_violations(variable, policy))

    def test_dashboard_static_snapshot_embeds_build_and_is_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            html = os.path.join(directory, "lendingbot.html")
            with open(html, "wb") as file:
                file.write(b"<meta content='__MIKA_DASHBOARD_BUILD_ID__'>")
            snapshot = lendingbot.load_dashboard_static_snapshot(directory, "build-one")
            with open(html, "wb") as file:
                file.write(b"changed-on-disk")
            self.assertIn(b"build-one", snapshot["lendingbot.html"])
            self.assertNotIn(b"changed-on-disk", snapshot["lendingbot.html"])

    def test_active_schema_normalization_backs_up_and_audits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "test.cfg")
            database = os.path.join(directory, "state.sqlite3")
            write_test_config(path, {"statedbfile": database})
            config, _ = lendingbot.read_config(path)
            settings = lendingbot.build_settings(lendingbot.parse_args(["--config", path]), config)
            raw = lendingbot.json_decimal(settings.strategy_v3.__dict__)
            raw.pop("max_lend_percent", None)
            raw.pop("max_lend_amount", None)
            raw["enable_frr"] = True
            store = lendingbot.LendingStateStore(database)
            old_version = store.save_strategy(raw, status="ACTIVE")
            result = lendingbot.normalize_current_active_strategy(path)
            active = store.strategy("ACTIVE")
            with store.read_connection() as connection:
                events = [dict(row) for row in connection.execute("SELECT * FROM strategy_events")]
            backups_exist = os.path.exists(result["backup"]["config"]) and os.path.exists(result["backup"]["database"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["fromVersion"], old_version)
        self.assertTrue(active["policy"]["enable_frr"])
        self.assertEqual(active["policy"]["max_lend_percent"], "100")
        self.assertTrue(backups_exist)
        self.assertEqual([event["event_type"] for event in events], ["SCHEMA_NORMALIZATION"])

    def test_preflight_summary_and_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "test.cfg")
            write_test_config(path, {"maxtolent": "0", "maxpercenttolent": "0"})
            result = lendingbot.evaluate_live_preflight(path, FakePreflightClient)
        self.assertTrue(all(check["status"] == "pass" for check in result["checks"]))
        self.assertEqual(result["summary"]["strategyVersion"], 3)
        self.assertEqual(result["summary"]["account"]["wallet"], "450")
        self.assertEqual(result["summary"]["fundingLimit"]["effectiveCap"], "450.00000000")
        self.assertNotIn("strategyProfile", result["summary"])
        self.assertNotIn("UNLIMITED_EXPOSURE", [warning["code"] for warning in result["warnings"]])

    def test_withdraw_permission_blocks_preflight(self):
        class UnsafeClient(FakePreflightClient):
            permissions = [["wallets", 1, 0], ["funding", 1, 1], ["withdraw", 0, 1], ["ui_withdraw", 0, 0]]

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "test.cfg")
            write_test_config(path)
            result = lendingbot.evaluate_live_preflight(path, UnsafeClient)
        check = next(item for item in result["checks"] if item["id"] == "withdraw_disabled")
        self.assertEqual(check["status"], "fail")

    def test_wallet_write_required_only_for_auto_transfer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "test.cfg")
            write_test_config(path, {"transferablecurrencies": "USD"})
            result = lendingbot.evaluate_live_preflight(path, FakePreflightClient)
        check = next(item for item in result["checks"] if item["id"] == "wallets_write")
        self.assertEqual(check["status"], "fail")

    def test_preflight_token_is_single_use(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "test.cfg")
            write_test_config(path)
            response = lendingbot.create_controlled_bot_preflight(path, FakePreflightClient, now=1000)
            lendingbot.consume_controlled_bot_preflight(path, response["preflightId"], now=1001)
            with self.assertRaisesRegex(lendingbot.ConfigError, "已使用"):
                lendingbot.consume_controlled_bot_preflight(path, response["preflightId"], now=1001)

    def test_preflight_token_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "test.cfg")
            write_test_config(path)
            response = lendingbot.create_controlled_bot_preflight(path, FakePreflightClient, now=1000)
            with self.assertRaisesRegex(lendingbot.ConfigError, "过期"):
                lendingbot.consume_controlled_bot_preflight(path, response["preflightId"], now=1301)

    def test_preflight_token_rejects_config_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "test.cfg")
            write_test_config(path)
            response = lendingbot.create_controlled_bot_preflight(path, FakePreflightClient, now=1000)
            with open(path, "a", encoding="utf-8") as file:
                file.write("\n# changed\n")
            with self.assertRaisesRegex(lendingbot.ConfigError, "发生变化"):
                lendingbot.consume_controlled_bot_preflight(path, response["preflightId"], now=1001)

    def test_preflight_token_rejects_account_change_within_five_minutes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "test.cfg")
            write_test_config(path)
            original = FakePreflightClient.wallet_rows
            try:
                response = lendingbot.create_controlled_bot_preflight(path, FakePreflightClient, now=1000)
                FakePreflightClient.wallet_rows = [["funding", "USD", "500", None, "449"]]
                with self.assertRaisesRegex(lendingbot.ConfigError, "账户快照"):
                    lendingbot.consume_controlled_bot_preflight(path, response["preflightId"], now=1001)
            finally:
                FakePreflightClient.wallet_rows = original

    def test_legacy_status_never_exposes_balance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "botlog.json")
            with open(path, "w", encoding="utf-8") as file:
                json.dump({"raw_data": {"USD": {"totalCoins": "999999"}}, "operationMode": "dry"}, file)
            status = lendingbot.read_status_payload(path)
        self.assertTrue(status["legacyIgnored"])
        self.assertEqual(status["raw_data"], {})
        self.assertIn("旧版状态已忽略", status["last_status"])

    def test_ledger_earnings_stats_summarize_periods(self):
        now_ms = int(time.time() * 1000)
        client = LedgerClient({"USD": [[1, "USD", "funding", now_ms, None, "1.5", "101.5", None, "Margin funding payment"]]})
        settings = SimpleNamespace(currencies=["USD"], output_currency="USD")
        stats = lendingbot.fetch_earnings_stats(client, settings, {"USD": Decimal("100")}, {"USD": []}, {"USD": Decimal("100")}, CaptureLog())
        self.assertTrue(stats["available"])
        self.assertEqual(stats["today"], "1.50000000")
        self.assertEqual(client.calls[0][1]["category"], 28)

    def test_atomic_write_keeps_original_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "status.json")
            with open(path, "w", encoding="utf-8") as file:
                file.write("original")
            with mock.patch("FileUtils.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    atomic_write_text(path, "replacement")
            with open(path, "r", encoding="utf-8") as file:
                self.assertEqual(file.read(), "original")

    def test_dashboard_concurrent_start_creates_one_process(self):
        original_log_path = lendingbot.DEFAULT_PROCESS_LOG
        original_state = (
            lendingbot.controlled_bot_process,
            lendingbot.controlled_bot_started_at,
            lendingbot.controlled_bot_log_handle,
            lendingbot.controlled_bot_stop_reason,
            lendingbot.controlled_bot_preflight,
        )
        popen_calls = []
        call_lock = threading.Lock()

        def fake_popen(*args, **kwargs):
            time.sleep(0.05)
            with call_lock:
                popen_calls.append((args, kwargs))
            return FakeControlledProcess()

        try:
            with tempfile.TemporaryDirectory() as directory:
                config_path = os.path.join(directory, "test.cfg")
                status_path = os.path.join(directory, "botlog.json")
                write_test_config(config_path)
                lendingbot.DEFAULT_PROCESS_LOG = os.path.join(directory, "bot-process.log")
                lendingbot.controlled_bot_process = None
                response = lendingbot.create_controlled_bot_preflight(config_path, FakePreflightClient)

                def attempt_start(_):
                    try:
                        return lendingbot.start_controlled_bot(config_path, status_path, response["preflightId"])
                    except lendingbot.ConfigError:
                        return None

                with mock.patch.object(lendingbot.subprocess, "Popen", side_effect=fake_popen):
                    with ThreadPoolExecutor(max_workers=8) as executor:
                        results = list(executor.map(attempt_start, range(8)))
                self.assertEqual(len(popen_calls), 1)
                self.assertEqual(sum(result is not None for result in results), 1)
                self.assertIn("--live", popen_calls[0][0][0])
                lendingbot.stop_controlled_bot()
        finally:
            lendingbot.cleanup_controlled_bot_handle()
            lendingbot.DEFAULT_PROCESS_LOG = original_log_path
            (
                lendingbot.controlled_bot_process,
                lendingbot.controlled_bot_started_at,
                lendingbot.controlled_bot_log_handle,
                lendingbot.controlled_bot_stop_reason,
                lendingbot.controlled_bot_preflight,
            ) = original_state

    def test_live_process_lock_is_discoverable_across_dashboard_state(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = os.path.join(directory, "lendingbot-live.lock")
            config_path = os.path.join(directory, "test.cfg")
            write_test_config(config_path)
            first = lendingbot.LiveProcessLock(lock_path)
            second = lendingbot.LiveProcessLock(lock_path)
            self.assertTrue(first.acquire(config_path))
            try:
                inspection = lendingbot.LiveProcessLock.inspect(lock_path)
                self.assertTrue(inspection["locked"])
                self.assertEqual(inspection["metadata"]["pid"], os.getpid())
                self.assertFalse(second.acquire(config_path))
                with mock.patch.object(lendingbot, "DEFAULT_LIVE_LOCK", lock_path):
                    status = lendingbot.controlled_bot_status(config_path)
                self.assertTrue(status["running"])
                self.assertTrue(status["managedExternally"])
                self.assertEqual(status["pid"], os.getpid())
            finally:
                first.release()
            self.assertFalse(lendingbot.LiveProcessLock.inspect(lock_path)["locked"])

    def test_safe_status_is_persisted_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            status_path = os.path.join(directory, "botlog.json")
            store = lendingbot.LendingStateStore(os.path.join(directory, "state.sqlite3"))
            store.set_mode("LIVE", "test")
            logger = lendingbot.Logger(status_path, 20)
            lendingbot.publish_safe_status(logger, store, RuntimeError("network down"))
            with open(status_path, "r", encoding="utf-8") as file:
                status = json.load(file)
            self.assertEqual(status["schemaVersion"], 3)
            self.assertEqual(status["operationMode"], "SAFE")
            self.assertEqual(status["runtime"]["mode"], "SAFE")
            self.assertIn("RuntimeError", status["last_status"])
            self.assertTrue(status["last_update"])

    def test_dashboard_reconciles_orphaned_live_runtime_and_overlays_status(self):
        with tempfile.TemporaryDirectory() as directory:
            database = os.path.join(directory, "state.sqlite3")
            config_path = os.path.join(directory, "test.cfg")
            status_path = os.path.join(directory, "botlog.json")
            write_test_config(config_path, {"statedbfile": database})
            store = lendingbot.LendingStateStore(database)
            store.set_mode("LIVE", "test")
            with open(status_path, "w", encoding="utf-8") as file:
                json.dump({
                    "schemaVersion": 3,
                    "operationMode": "LIVE",
                    "last_update": "2026-07-21 15:00:00",
                    "account": {"total": "123"},
                }, file)
            with mock.patch.object(lendingbot, "controlled_bot_running", return_value=False):
                runtime = lendingbot.reconcile_orphaned_live_runtime(config_path)
            self.assertEqual(runtime["mode"], "PAUSED")
            payload = lendingbot.dashboard_status_payload(status_path, config_path)
            self.assertEqual(payload["operationMode"], "PAUSED")
            self.assertNotIn("account", payload)
            self.assertEqual(payload["last_update"], "")


if __name__ == "__main__":
    unittest.main()
