import configparser
import json
import os
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

from bitfinex import Bitfinex, BitfinexApiError
import lendingbot
from StrategyEngine import (
    ManagedOfferRegistry,
    PublicMarketCache,
    build_market_signals,
    build_strategy_plan,
    parse_funding_stats,
    parse_funding_trades,
    policy_with_overrides,
    preset_policy,
    replay_strategy,
    validate_policy,
    weighted_offer_amounts,
    weighted_quantile,
    winsorized_trades,
)


NOW_MS = 2_000_000_000_000


def write_config(path, bot=None, strategy_sections=None):
    values = {
        "mindailyrate": "0.04",
        "maxdailyrate": "2",
        "spreadlend": "3",
        "minloansize": "150",
        "maxtolent": "1000",
        "statedbfile": os.path.join(os.path.dirname(path), "state.sqlite3"),
    }
    values.update(bot or {})
    config = configparser.ConfigParser()
    config.read_dict({
        "BITFINEX": {"apikey": "private-key", "secret": "private-secret", "currencies": "USD"},
        "BOT": values,
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
    for section, section_values in (strategy_sections or {}).items():
        config[section] = section_values
    with open(path, "w", encoding="utf-8") as file:
        config.write(file)


def signal_fixture(regime="neutral", frr=Decimal("0.0005")):
    return {
        "as_of": NOW_MS,
        "regime": regime,
        "book_reference": Decimal("0.0005"),
        "trade_median_1h": Decimal("0.0005"),
        "trade_median_24h": Decimal("0.0005"),
        "trade_q25_24h": Decimal("0.00048"),
        "trade_q75_24h": Decimal("0.00062"),
        "trade_iqr_24h": Decimal("0.00014"),
        "trend_change": Decimal("0"),
        "trend_threshold": Decimal("0.000035"),
        "frr_daily_rate": frr,
        "frr_p25_7d": frr,
        "average_period": Decimal("14"),
        "utilization": Decimal("0.75"),
        "anchor_rate": Decimal("0.0005"),
        "trade_count_24h": 10,
        "warnings": [],
    }


class PublicPreviewClient:
    created_with = []

    def __init__(self, key, secret):
        self.created_with.append((key, secret))

    def funding_book(self, symbol, length=250):
        return [["0.00050", 2, 1, "5000"]]

    def funding_trades(self, symbol, **kwargs):
        return [
            [1, NOW_MS - 2 * 60 * 60 * 1000, "800", "0.00050", 7],
            [2, NOW_MS - 30 * 60 * 1000, "500", "0.00055", 60],
        ]

    def funding_stats(self, symbol, **kwargs):
        return [[NOW_MS - 5 * 60 * 1000, 0, 0, str(Decimal("0.0005") / Decimal("365")), 14, 0, 0, 10000, 8600]]


class StrategyEngineTests(unittest.TestCase):
    def setUp(self):
        lendingbot.market_data_cache.clear()
        lendingbot.managed_registry_cache.clear()
        PublicPreviewClient.created_with.clear()

    def test_public_market_rows_are_parsed_and_sorted(self):
        trades = parse_funding_trades([
            [2, 200, "-20", "0.0005", 30],
            [1, 100, "10", "0.0004", 2],
            [3, 300, "0", "0.0008", 2],
        ])
        self.assertEqual([item["id"] for item in trades], ["1", "2"])
        self.assertEqual(trades[1]["amount"], Decimal("20"))
        stats = parse_funding_stats([[100, 0, 0, str(Decimal("0.0005") / 365), 12, 0, 0, 1000, 850]])
        self.assertEqual(stats[0]["frr_daily_rate"].quantize(Decimal("0.00000001")), Decimal("0.00050000"))
        self.assertEqual(stats[0]["utilization"], Decimal("0.85"))

    def test_weighted_statistics_limit_outlier_influence(self):
        trades = [
            {"rate": Decimal("0.0004"), "amount": Decimal("100")},
            {"rate": Decimal("0.0005"), "amount": Decimal("100")},
            {"rate": Decimal("0.5"), "amount": Decimal("1")},
        ]
        clipped = winsorized_trades(trades)
        self.assertEqual(weighted_quantile(clipped, Decimal("0.5")), Decimal("0.0005"))
        self.assertLess(max(item["rate"] for item in clipped), Decimal("0.01"))

    def test_market_regimes_use_trades_iqr_and_utilization(self):
        policy = preset_policy()
        older = [
            {"id": str(index), "mts": NOW_MS - 2 * 60 * 60 * 1000, "amount": Decimal("100"), "rate": Decimal("0.0005"), "period": 7}
            for index in range(10)
        ]
        rising = older + [{"id": "r", "mts": NOW_MS - 1000, "amount": Decimal("100"), "rate": Decimal("0.00055"), "period": 60}]
        high_stat = [{"mts": NOW_MS, "frr_daily_rate": Decimal("0.0005"), "average_period": Decimal("14"), "provided": Decimal("1000"), "used": Decimal("860"), "utilization": Decimal("0.86")}]
        self.assertEqual(build_market_signals(Decimal("0.0005"), rising, high_stat, policy, Decimal("0.0004"), Decimal("0.02"), NOW_MS)["regime"], "rising")

        falling_trades = [dict(item, rate=Decimal("0.0006")) for item in older]
        falling_trades.append({"id": "f", "mts": NOW_MS - 1000, "amount": Decimal("200"), "rate": Decimal("0.00054"), "period": 7})
        low_stat = [dict(high_stat[0], used=Decimal("500"), utilization=Decimal("0.5"))]
        self.assertEqual(build_market_signals(Decimal("0.0005"), falling_trades, low_stat, policy, Decimal("0.0004"), Decimal("0.02"), NOW_MS)["regime"], "falling")

    def test_automatic_offer_types_follow_regime(self):
        policy = preset_policy()
        expected = {
            "rising": ["LIMIT", "FRRDELTAFIX", "LIMIT"],
            "neutral": ["LIMIT", "FRRDELTAFIX", "FRRDELTAFIX"],
            "falling": ["LIMIT", "FRRDELTAVAR", "FRRDELTAVAR"],
        }
        for regime, offer_types in expected.items():
            plan = build_strategy_plan(Decimal("1000"), Decimal("150"), Decimal("0.0004"), Decimal("0.02"), policy, signal_fixture(regime))
            self.assertEqual([item["offer_type"] for item in plan], offer_types)

    def test_missing_or_low_frr_falls_back_to_limit(self):
        policy = preset_policy()
        plan = build_strategy_plan(Decimal("1000"), Decimal("150"), Decimal("0.0004"), Decimal("0.02"), policy, signal_fixture("falling", Decimal("0")))
        self.assertEqual([item["offer_type"] for item in plan], ["LIMIT", "LIMIT", "LIMIT"])
        low = signal_fixture("falling", Decimal("0.0003"))
        plan = build_strategy_plan(Decimal("1000"), Decimal("150"), Decimal("0.0004"), Decimal("0.02"), policy, low)
        self.assertEqual(plan[-1]["offer_type"], "LIMIT")

    def test_manual_fixed_frr_accepts_zero_and_negative_offsets(self):
        zero = policy_with_overrides(preset_policy(), {
            "profile": "custom", "auto_order_types": False,
            "balanced_order_type": "FRRDELTAFIX", "balanced_frr_offset": "0",
            "long_order_type": "FRRDELTAFIX", "long_frr_offset": "-0.00002",
        })
        plan = build_strategy_plan(Decimal("1000"), Decimal("150"), Decimal("0.0004"), Decimal("0.02"), zero, signal_fixture())
        self.assertEqual(plan[1]["submitted_rate"], Decimal("0"))
        self.assertEqual(plan[2]["submitted_rate"], Decimal("-0.00002"))

    def test_manual_variable_frr_offset_must_be_nonnegative(self):
        invalid = policy_with_overrides(preset_policy(), {
            "profile": "custom", "auto_order_types": False,
            "balanced_order_type": "FRRDELTAVAR", "balanced_frr_offset": "-0.0001",
        })
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            validate_policy(invalid)

    def test_presets_and_amounts_match_approved_ladders(self):
        expected = {
            "utilization": (Decimal("65"), Decimal("25"), Decimal("10"), (2, 7, 30)),
            "balanced_yield": (Decimal("50"), Decimal("10"), Decimal("40"), (2, 7, 60)),
            "yield": (Decimal("30"), Decimal("10"), Decimal("60"), (2, 14, 90)),
        }
        for name, values in expected.items():
            policy = preset_policy(name)
            self.assertEqual((policy.fast_share, policy.balanced_share, policy.long_share), values[:3])
            self.assertEqual((policy.fast_period, policy.balanced_period, policy.long_period), values[3])
        amounts = weighted_offer_amounts(Decimal("1000"), Decimal("150"), [50, 10, 40])
        self.assertEqual(sum(amounts, Decimal("0")), Decimal("1000.00000000"))
        self.assertTrue(all(item >= Decimal("150") for item in amounts))

    def test_bucket_depth_controls_use_different_book_levels(self):
        config = configparser.ConfigParser()
        config.read_dict({
            "BITFINEX": {"currencies": "USD"},
            "BOT": {"mindailyrate": "0.04", "maxdailyrate": "2", "minloansize": "150", "strategyversion": "2"},
        })
        settings = lendingbot.build_settings(lendingbot.parse_args([]), config)
        book = [
            {"rate": Decimal("0.0004"), "period": 2, "amount": Decimal("200")},
            {"rate": Decimal("0.0006"), "period": 7, "amount": Decimal("1300")},
            {"rate": Decimal("0.0008"), "period": 60, "amount": Decimal("1500")},
        ]
        rates = lendingbot.strategy_bucket_book_rates(settings, "USD", book, Decimal("1000"), settings.strategy_policy)
        self.assertEqual(rates, {
            "fast_book_rate": Decimal("0.0004"),
            "balanced_book_rate": Decimal("0.0006"),
            "long_book_rate": Decimal("0.0008"),
        })

    def test_all_three_official_offer_types_are_submitted(self):
        client = Bitfinex("key", "secret")
        success = [1, "fon-submit", None, None, [], None, "SUCCESS", "submitted"]
        for offer_type in ("LIMIT", "FRRDELTAFIX", "FRRDELTAVAR"):
            with mock.patch.object(client, "_auth_post", return_value=success) as auth_post:
                client.submit_funding_offer("fUSD", "150", "0.0001", 7, offer_type)
            self.assertEqual(auth_post.call_args.args[1]["type"], offer_type)
        with self.assertRaises(BitfinexApiError):
            client.submit_funding_offer("fUSD", "150", "-0.0001", 7, "FRRDELTAVAR")
        with self.assertRaises(BitfinexApiError):
            client.submit_funding_offer("fUSD", "150", "0.0001", 121, "LIMIT")

    def test_public_cache_honors_ttl_limit_and_stale_fallback(self):
        cache = PublicMarketCache(max_requests_per_minute=1, stale_seconds=1800)
        calls = []
        first = cache.get("a", 300, lambda: calls.append("fetch") or [1], now=100)
        cached = cache.get("a", 300, lambda: calls.append("unexpected") or [2], now=101)
        limited = cache.get("b", 300, lambda: [2], now=102)
        stale = cache.get("a", 300, lambda: (_ for _ in ()).throw(RuntimeError("offline")), now=500)
        self.assertEqual(first, ([1], False, ""))
        self.assertEqual(cached, ([1], False, ""))
        self.assertEqual(limited[0], [])
        self.assertEqual(stale[0], [1])
        self.assertTrue(stale[1])
        self.assertEqual(calls, ["fetch"])

    def test_managed_offer_registry_recovers_and_treats_corruption_as_external(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "managed.json")
            registry = ManagedOfferRegistry(path)
            registry.record(10, "usd", "long", "LIMIT", "hash", created_at=123)
            restarted = ManagedOfferRegistry(path)
            self.assertTrue(restarted.is_managed(10))
            self.assertEqual(restarted.metadata(10)["bucket"], "long")
            restarted.reconcile([10])
            restarted.reconcile([])
            self.assertFalse(restarted.is_managed(10))
            with open(path, "w", encoding="utf-8") as file:
                file.write("not-json")
            corrupt = ManagedOfferRegistry(path)
            self.assertTrue(corrupt.load_error)
            self.assertFalse(corrupt.is_managed(10))

    def test_open_offer_status_includes_registry_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ManagedOfferRegistry(os.path.join(directory, "managed.json"))
            registry.record(123, "USD", "long", "FRRDELTAFIX", "hash", created_at=1)
            row = [123, "fUSD", 1, 1, "150", "150", "FRRDELTAFIX", None, None, 0, "ACTIVE", None, None, None, "0.0001", 60]
            parsed = lendingbot.parse_open_offers([row], registry)["USD"][0]
        self.assertTrue(parsed["managed_by_bot"])
        self.assertEqual(parsed["bucket"], "long")
        self.assertEqual(parsed["offer_type"], "FRRDELTAFIX")

    def test_external_offers_are_never_selected_for_cancel(self):
        settings_config = configparser.ConfigParser()
        settings_config.read_dict({"BITFINEX": {"currencies": "USD"}, "BOT": {"mindailyrate": "0.04", "maxdailyrate": "2", "minloansize": "150"}})
        settings = lendingbot.build_settings(lendingbot.parse_args([]), settings_config)
        row = [123, "fUSD", 1, 1, "150", "150", "LIMIT", None, None, 0, "ACTIVE", None, None, None, "0.0005", 2]
        offers = {"USD": lendingbot.parse_open_offers([row]) ["USD"]}
        fresh, stale = lendingbot.split_stale_open_offers(settings, offers, now_ms=10_000_000)
        self.assertEqual([item["id"] for item in fresh["USD"]], [123])
        self.assertEqual(stale, {})

    def test_legacy_v2_strategy_sections_are_ignored_by_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "test.cfg")
            write_config(path, strategy_sections={"STRATEGY:USD": {"profile": "yield", "longshare": "60"}})
            config, _ = lendingbot.read_config(path)
            settings = lendingbot.build_settings(lendingbot.parse_args(["--config", path]), config)
            self.assertFalse(settings.strategy_auto_migrated)
            self.assertEqual(settings.strategy_policy.profile, "balanced_yield")
            self.assertEqual(lendingbot.strategy_policy_for(settings, "USD").profile, "balanced_yield")
            self.assertEqual(lendingbot.strategy_policy_for(settings, "UST").profile, "balanced_yield")
            with open(path, "r", encoding="utf-8") as file:
                self.assertNotIn("strategyversion", file.read().lower())

    def test_strategy_save_preserves_comments_and_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "test.cfg")
            with open(path, "w", encoding="utf-8") as file:
                file.write("[BITFINEX]\n# keep this comment\napikey = private-key\nsecret = private-secret\ncurrencies = USD\n\n[BOT]\nmindailyrate = 0.04\nmaxdailyrate = 2\nminloansize = 150\n")
            lendingbot.save_dashboard_config(path, {"strategy": {"global": {"profile": "yield", "fast_share": "30", "long_share": "60"}, "overrides": {}}})
            with open(path, "r", encoding="utf-8") as file:
                saved = file.read()
            self.assertIn("# keep this comment", saved)
            self.assertIn("apikey = private-key", saved)
            self.assertIn("secret = private-secret", saved)
            self.assertIn("strategyversion = 2", saved)

    def test_strategy_preview_uses_only_public_client_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "test.cfg")
            write_config(path)
            payload = {"strategy": {"global": {"profile": "balanced_yield", "fast_share": "50", "long_share": "40"}, "overrides": {}}, "currency": "USD", "window": "7d", "principal": "1000"}
            first = lendingbot.strategy_preview(path, payload, PublicPreviewClient, NOW_MS)
            lendingbot.market_data_cache.clear()
            second = lendingbot.strategy_preview(path, payload, PublicPreviewClient, NOW_MS)
        self.assertEqual(PublicPreviewClient.created_with, [("", ""), ("", "")])
        self.assertEqual(first["plan"], second["plan"])
        self.assertEqual(first["replay"], second["replay"])
        self.assertEqual(first["regime"], "rising")
        self.assertEqual(len(first["plan"]), 3)

    def test_manual_frr_strategy_blocks_preflight_when_stats_are_missing(self):
        class ReadOnlyClient:
            def __init__(self, key, secret):
                self.key = key
                self.secret = secret

            def has_credentials(self):
                return True

            def key_permissions(self):
                return [["wallets", 1, 0], ["funding", 1, 1], ["withdraw", 0, 0], ["ui_withdraw", 0, 0]]

            def wallets(self):
                return [["funding", "USD", "1000", None, "1000"]]

            def funding_book(self, symbol, length=250):
                return [["0.0005", 2, 1, "5000"]]

            def funding_trades(self, symbol, **kwargs):
                return []

            def funding_stats(self, symbol, **kwargs):
                return []

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "test.cfg")
            write_config(path, {
                "strategyversion": "2",
                "strategyautotypes": "false",
                "strategybalancedordertype": "FRRDELTAFIX",
            })
            result = lendingbot.evaluate_live_preflight(path, ReadOnlyClient)
        self.assertFalse(any(item["id"] == "strategy_frr" for item in result["checks"]))
        self.assertEqual(result["summary"]["strategyVersion"], 3)
        self.assertNotIn("strategyProfile", result["summary"])

    def test_replay_no_data_is_explicit_and_deterministic_for_each_window(self):
        policy = preset_policy()
        for window in ("24h", "7d", "30d"):
            result = replay_strategy(policy, [], [], Decimal("1000"), Decimal("150"), Decimal("0.0004"), Decimal("0.02"), window, NOW_MS)
            self.assertEqual(result["window"], window)
            self.assertEqual(result["sampleCount"], 0)
            self.assertIn("并非完整盘口回测", result["disclaimer"])


if __name__ == "__main__":
    unittest.main()
