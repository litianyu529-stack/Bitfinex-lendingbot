import configparser
import hashlib
import hmac
import time
import unittest
from decimal import Decimal
from types import SimpleNamespace

from bitfinex import Bitfinex
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


class ExplodingClient:
    def submit_funding_offer(self, *args, **kwargs):
        raise AssertionError("dry-run must not submit offers")

    def cancel_funding_offer(self, *args, **kwargs):
        raise AssertionError("dry-run must not cancel offers")


class CancelCaptureClient:
    def __init__(self):
        self.canceled = []

    def cancel_funding_offer(self, offer_id):
        self.canceled.append(offer_id)
        return {"message": "success"}


class LedgerClient:
    def __init__(self, rows_by_currency):
        self.rows_by_currency = rows_by_currency
        self.calls = []

    def has_credentials(self):
        return True

    def ledgers(self, currency=None, **kwargs):
        self.calls.append((currency, kwargs))
        return self.rows_by_currency.get(currency, [])


class BitfinexBotTests(unittest.TestCase):
    def test_auth_headers_signature(self):
        client = Bitfinex("key", "secret")
        path = "v2/auth/r/wallets"
        nonce = "12345"
        headers = client.auth_headers_for_test(path, nonce, {})
        payload = "/api/" + path + nonce + "{}"
        expected = hmac.new(
            b"secret",
            payload.encode("utf-8"),
            hashlib.sha384,
        ).hexdigest()
        self.assertEqual(headers["bfx-apikey"], "key")
        self.assertEqual(headers["bfx-nonce"], nonce)
        self.assertEqual(headers["bfx-signature"], expected)

    def test_nonce_is_monotonic(self):
        client = Bitfinex("key", "secret")
        first = int(client._nonce())
        client._last_nonce = max(first + 10, int(time.time() * 1000000) + 1000000)
        previous = client._last_nonce
        second = int(client._nonce())
        self.assertEqual(second, previous + 1)

    def test_funding_book_keeps_positive_offers(self):
        rows = [
            [0.0002, 2, 1, -1000],
            [0.00015, 2, 1, 300],
            [0.0001, 30, 1, 200],
        ]
        offers = lendingbot.parse_funding_book(rows)
        self.assertEqual([offer["amount"] for offer in offers], [Decimal("200"), Decimal("300")])
        self.assertEqual([offer["rate"] for offer in offers], [Decimal("0.0001"), Decimal("0.00015")])

    def test_parse_open_offers_keeps_created_time(self):
        rows = [
            [
                123,
                "fUSD",
                1000,
                2000,
                "150",
                "150",
                "LIMIT",
                None,
                None,
                0,
                "ACTIVE",
                None,
                None,
                None,
                "0.0004",
                2,
                None,
                None,
                None,
                None,
            ]
        ]
        offers = lendingbot.parse_open_offers(rows)
        self.assertEqual(offers["USD"][0]["id"], 123)
        self.assertEqual(offers["USD"][0]["created"], 1000)
        self.assertEqual(offers["USD"][0]["amount"], Decimal("150"))

    def test_rate_at_depth_clamps_to_minimum_instead_of_maximum(self):
        book = [{"rate": Decimal("0.00018"), "period": 2, "amount": Decimal("1000")}]
        rate = lendingbot.rate_at_depth(
            book,
            Decimal("300"),
            Decimal("0.0004"),
            Decimal("0.02"),
        )
        self.assertEqual(rate, Decimal("0.0004"))

    def test_config_percent_rate_conversion(self):
        config = configparser.ConfigParser()
        config.read_dict(
            {
                "BITFINEX": {"apikey": "key", "secret": "secret", "currencies": "USD"},
                "BOT": {"mindailyrate": "0.04", "maxdailyrate": "2"},
            }
        )
        args = lendingbot.parse_args(["--once", "--dryrun"])
        settings = lendingbot.build_settings(args, config)
        self.assertEqual(settings.min_daily_rate, Decimal("0.0004"))
        self.assertEqual(settings.max_daily_rate, Decimal("0.02"))

    def test_smart_strategy_follows_market_floor(self):
        config = configparser.ConfigParser()
        config.read_dict(
            {
                "BITFINEX": {"apikey": "key", "secret": "secret", "currencies": "USD"},
                "BOT": {
                    "mindailyrate": "0.04",
                    "maxdailyrate": "2",
                    "smartstrategy": "true",
                    "smartrateoffset": "0.001",
                },
            }
        )
        settings = lendingbot.build_settings(lendingbot.parse_args(["--dryrun"]), config)
        book = [{"rate": Decimal("0.00018"), "period": 2, "amount": Decimal("1000")}]
        self.assertEqual(lendingbot.smart_min_rate_for(settings, "USD", book), Decimal("0.00019"))
        rates = lendingbot.choose_offer_rates(book, Decimal("300"), settings, "USD", 2)
        self.assertEqual(rates[0], Decimal("0.00019"))

    def test_classic_strategy_keeps_configured_minimum(self):
        config = configparser.ConfigParser()
        config.read_dict(
            {
                "BITFINEX": {"apikey": "key", "secret": "secret", "currencies": "USD"},
                "BOT": {
                    "mindailyrate": "0.04",
                    "maxdailyrate": "2",
                    "smartstrategy": "false",
                },
            }
        )
        settings = lendingbot.build_settings(lendingbot.parse_args(["--dryrun"]), config)
        book = [{"rate": Decimal("0.00018"), "period": 2, "amount": Decimal("1000")}]
        rates = lendingbot.choose_offer_rates(book, Decimal("300"), settings, "USD", 1)
        self.assertEqual(rates[0], Decimal("0.0004"))

    def test_dry_run_does_not_submit_offer(self):
        settings = SimpleNamespace(dry_run=True, xday_threshold=Decimal("0.002"), xdays=60)
        log = CaptureLog()
        lendingbot.submit_or_log_offer(
            ExplodingClient(),
            settings,
            log,
            "USD",
            Decimal("150"),
            Decimal("0.0004"),
        )
        self.assertEqual(len(log.offers), 1)
        self.assertEqual(log.offers[0][4]["message"], "dry-run")

    def test_stale_offer_split_uses_configured_minutes(self):
        settings = SimpleNamespace(reprice_stale_offers=True, reprice_after_minutes=Decimal("15"))
        now_ms = 10_000_000
        open_offers = {
            "USD": [
                {"id": 1, "created": now_ms - (16 * 60 * 1000), "amount": Decimal("150")},
                {"id": 2, "created": now_ms - (5 * 60 * 1000), "amount": Decimal("150")},
            ]
        }
        fresh, stale = lendingbot.split_stale_open_offers(settings, open_offers, now_ms)
        self.assertEqual([offer["id"] for offer in stale["USD"]], [1])
        self.assertEqual([offer["id"] for offer in fresh["USD"]], [2])

    def test_dry_run_stale_cancel_only_logs(self):
        settings = SimpleNamespace(dry_run=True, currencies=["USD"])
        log = CaptureLog()
        stale = {"USD": [{"id": 1, "amount": Decimal("150")}]}
        amounts = lendingbot.cancel_stale_offers(ExplodingClient(), settings, log, stale)
        self.assertEqual(amounts["USD"], Decimal("150"))
        self.assertEqual(len(log.cancels), 1)
        self.assertIn("would reprice", log.cancels[0][1]["message"])

    def test_live_stale_cancel_uses_single_offer_ids(self):
        settings = SimpleNamespace(dry_run=False, currencies=["USD"])
        log = CaptureLog()
        client = CancelCaptureClient()
        stale = {"USD": [{"id": 11, "amount": Decimal("150")}, {"id": 12, "amount": Decimal("200")}]}
        amounts = lendingbot.cancel_stale_offers(client, settings, log, stale)
        self.assertEqual(client.canceled, [11, 12])
        self.assertEqual(amounts["USD"], Decimal("350"))

    def test_ledger_earnings_stats_summarize_periods(self):
        now_ms = int(time.time() * 1000)
        old_ms = now_ms - (8 * 86400 * 1000)
        client = LedgerClient(
            {
                "USD": [
                    [1, "USD", "funding", now_ms, None, "1.5", "101.5", None, "Margin funding payment"],
                    [2, "USD", "funding", now_ms, None, "-0.5", "101.0", None, "Position funding cost"],
                    [3, "USD", "exchange", now_ms, None, "9.0", "110.0", None, "Margin funding payment"],
                ],
                "UST": [
                    [4, "UST", "funding", old_ms, None, "2.0", "52.0", None, "Margin funding payment"],
                ],
            }
        )
        settings = SimpleNamespace(currencies=["USD", "UST"], output_currency="USD")
        stats = lendingbot.fetch_earnings_stats(
            client,
            settings,
            {"USD": Decimal("100"), "UST": Decimal("50")},
            {"USD": [{"amount": Decimal("100")}], "UST": []},
            {"USD": Decimal("100"), "UST": Decimal("50")},
            CaptureLog(),
        )
        self.assertTrue(stats["available"])
        self.assertEqual(stats["today"], "1.50000000")
        self.assertEqual(stats["sevenDays"], "1.50000000")
        self.assertEqual(stats["thirtyDays"], "3.50000000")
        self.assertEqual(client.calls[0][1]["category"], 28)
        self.assertEqual(client.calls[0][1]["wallet"], "funding")

    def test_validation_rejects_invalid_xdays(self):
        config = configparser.ConfigParser()
        config.read_dict(
            {
                "BITFINEX": {"apikey": "key", "secret": "secret", "currencies": "USD"},
                "BOT": {"xdays": "121"},
            }
        )
        settings = lendingbot.build_settings(lendingbot.parse_args(["--dryrun"]), config)
        with self.assertRaises(lendingbot.ConfigError):
            lendingbot.validate_settings(settings)

    def test_validation_rejects_invalid_reprice_minutes(self):
        config = configparser.ConfigParser()
        config.read_dict(
            {
                "BITFINEX": {"apikey": "key", "secret": "secret", "currencies": "USD"},
                "BOT": {"repriceafterminutes": "0"},
            }
        )
        settings = lendingbot.build_settings(lendingbot.parse_args(["--dryrun"]), config)
        with self.assertRaises(lendingbot.ConfigError):
            lendingbot.validate_settings(settings)

    def test_decimal_config_format_keeps_integer_zeroes(self):
        self.assertEqual(lendingbot.decimal_to_config(Decimal("150")), "150")
        self.assertEqual(lendingbot.decimal_to_config(Decimal("10.5000")), "10.5")
        self.assertEqual(lendingbot.decimal_percent_to_config(Decimal("0.0004")), "0.04")

    def test_no_server_overrides_config_web_server(self):
        config = configparser.ConfigParser()
        config.read_dict(
            {
                "BITFINEX": {"apikey": "key", "secret": "secret", "currencies": "USD"},
                "BOT": {"startwebserver": "true"},
            }
        )
        settings = lendingbot.build_settings(
            lendingbot.parse_args(["--dryrun", "--no-server"]),
            config,
        )
        self.assertFalse(settings.web_server)

    def test_dashboard_arg_is_available(self):
        args = lendingbot.parse_args(["--dashboard"])
        self.assertTrue(args.dashboard)


if __name__ == "__main__":
    unittest.main()
