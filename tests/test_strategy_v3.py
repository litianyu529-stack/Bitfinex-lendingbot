import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import configparser

import pytest

import lendingbot

from Logger import Logger
from MarketDataStream import BitfinexMarketDataHub
from RuntimeV3 import LendingRuntimeV3, parse_ledger_rows_v3
from StateStore import InsufficientReservedBalance, LendingStateStore, StateStoreError
from StrategyV3 import (
    StrategyPolicyV3,
    _candidate_types,
    build_market_signals_v3,
    build_strategy_plan_v3,
    deterministic_amounts,
    dynamic_pool_shares,
    filter_supported_trades,
    gross_daily_floor,
    json_decimal,
    replay_strategy_v3,
    validate_policy_v3,
)
from bitfinex import Bitfinex


D = Decimal


def policy(**overrides):
    values = {
        "short_floor_apr": D("0.05"),
        "medium_floor_apr": D("0.06"),
        "long_floor_apr": D("0.07"),
        **overrides,
    }
    return replace(StrategyPolicyV3(), **values)


def signals(**overrides):
    result = {
        "regime": "neutral",
        "best_bid": D("0.00040"),
        "best_offer": D("0.00042"),
        "anchor_rate": D("0.00040"),
        "frr_daily_rate": D("0.00035"),
        "utilization": D("0.8"),
        "volume_ratio_5m": D("1"),
        "trend_threshold": D("0.00002"),
        "windows": {
            "1h": {"median": D("0.00040")},
            "24h": {"q25": D("0.00030"), "q75": D("0.00050")},
        },
    }
    result.update(overrides)
    return result


def limit_policy(**overrides):
    return policy(
        enable_frr=False,
        enable_frr_delta_fixed=False,
        enable_frr_delta_variable=False,
        **overrides,
    )


def intent_order(index=0, amount="150"):
    return {
        "currency": "USD",
        "slice_key": f"v3:short:quick:{index}",
        "pool": "short",
        "layer": "quick",
        "amount": D(amount),
        "submitted_rate": D("0.0002"),
        "effective_rate": D("0.0002"),
        "period": 2,
        "offer_type": "LIMIT",
        "flags": 0,
        "strategy_version": "v3",
    }


def test_exact_50_slices_are_deterministic_and_conserve_amount():
    first = build_strategy_plan_v3(D("10000"), D("10000"), {}, limit_policy(), signals(), "same")
    second = build_strategy_plan_v3(D("10000"), D("10000"), {}, limit_policy(), signals(), "same")
    assert len(first["plan"]) == 50
    assert [row["amount"] for row in first["plan"]] == [row["amount"] for row in second["plan"]]
    assert sum((row["amount"] for row in first["plan"]), D("0")) == D("10000.00000000")
    assert all(row["amount"] >= D("150") for row in first["plan"])
    assert len({row["amount"] for row in first["plan"]}) > 1


def test_insufficient_balance_reduces_slice_count_and_never_overallocates():
    result = build_strategy_plan_v3(D("1000"), D("1000"), {}, limit_policy(), signals(), "small")
    assert result["target_slice_count"] == 6
    assert len(result["plan"]) == 6
    assert result["planned_amount"] == D("1000.00000000")


def test_decimal_amount_allocator_quantizes_to_eight_places():
    amounts = deterministic_amounts(D("1234.56789012"), 7, D("150"), D("0.03"), "seed")
    assert sum(amounts, D("0")) == D("1234.56789012")
    assert all(value.as_tuple().exponent >= -8 for value in amounts)


def test_pool_and_layer_splits_and_period_ladders_are_diverse():
    result = build_strategy_plan_v3(D("10000"), D("10000"), {}, limit_policy(), signals(), "split")
    pools = {name: [row for row in result["plan"] if row["pool"] == name] for name in ("short", "medium", "long")}
    assert [len(pools[name]) for name in ("short", "medium", "long")] == [25, 15, 10]
    assert {row["period"] for row in pools["short"]} == {2, 3, 5, 7}
    assert {row["period"] for row in pools["medium"]} == {8, 14, 21, 30}
    assert {row["period"] for row in pools["long"]} == {120}
    assert len({row["effective_rate"] for row in result["plan"]}) > 1


def test_dynamic_pool_shift_respects_direction_and_ten_point_limit():
    base = policy()
    spike = dynamic_pool_shares(base, {"regime": "spike"})
    low = dynamic_pool_shares(base, {"regime": "low"})
    assert spike["short"] == D("40") and spike["long"] == D("30")
    assert low["short"] == D("60") and low["long"] == D("10")
    assert all(abs(spike[name] - base.pool_shares()[name]) <= 10 for name in spike)


def test_net_apr_floors_and_fee_difference_are_enforced():
    normal = gross_daily_floor(D("0.05"), D("0.15"))
    hidden = gross_daily_floor(D("0.05"), D("0.18"))
    assert normal == D("0.05") / D("365") / D("0.85")
    assert hidden > normal
    result = build_strategy_plan_v3(D("10000"), D("10000"), {}, limit_policy(), signals(), "floors")
    for row in result["plan"]:
        assert row["effective_rate"] >= gross_daily_floor(result_floor(row["pool"]), D("0.15"))


def test_live_requires_all_three_positive_floors_but_paused_does_not():
    validate_policy_v3(StrategyPolicyV3())
    with pytest.raises(ValueError):
        validate_policy_v3(StrategyPolicyV3(), require_live_floors=True)
    validate_policy_v3(policy(), require_live_floors=True)


def test_empty_max_lend_amount_config_means_unlimited_amount():
    config = configparser.ConfigParser()
    config.read_dict({"STRATEGY_V3": {"max_lend_amount": "", "max_lend_percent": "100"}})
    parsed = lendingbot.strategy_v3_from_config(config)
    assert parsed.max_lend_amount is None
    assert parsed.max_lend_percent == D("100")


def result_floor(pool_name):
    return {"short": D("0.05"), "medium": D("0.06"), "long": D("0.07")}[pool_name]


def test_unsupported_long_floor_stays_idle_without_breaking_other_pools():
    result = build_strategy_plan_v3(
        D("10000"), D("10000"), {}, limit_policy(long_floor_apr=D("1")), signals(), "idle-long"
    )
    assert all(row["pool"] != "long" for row in result["plan"])
    assert result["idle_amount"] >= D("1900")


def test_all_offer_types_are_generated_and_plain_frr_is_zero_variable_delta():
    generated = _candidate_types(policy(), D("0.00035"), D("0.00042"))
    displays = {row[3] for row in generated}
    assert displays == {"LIMIT", "FRR", "FRR_DELTA_FIXED", "FRR_DELTA_VARIABLE"}
    plain = next(row for row in generated if row[3] == "FRR")
    assert plain[:3] == ("FRRDELTAVAR", D("0"), D("0.00035"))


def test_bitfinex_offer_payload_maps_frr_and_hidden_flag():
    client = Bitfinex("key", "secret")
    writes = []
    client._auth_write = lambda path, payload: writes.append((path, payload)) or [0, "SUCCESS"]
    client.submit_funding_offer("fUSD", "150", "0.0003", 2, "FRR", hidden=True)
    client.submit_funding_offer("fUSD", "150", "0.0001", 14, "FRRDELTAFIX")
    client.submit_funding_offer("fUSD", "150", "0.0001", 30, "FRRDELTAVAR")
    assert writes[0][1]["type"] == "FRRDELTAVAR"
    assert writes[0][1]["rate"] == "0"
    assert writes[0][1]["flags"] == 64
    assert [row[1]["type"] for row in writes[1:]] == ["FRRDELTAFIX", "FRRDELTAVAR"]


def test_variable_share_cap_includes_plain_frr():
    only_variable = policy(enable_limit=False, enable_frr_delta_fixed=False, variable_max_share=D("10"))
    result = build_strategy_plan_v3(D("10000"), D("10000"), {}, only_variable, signals(), "variable")
    assert result["variable_amount"] <= D("1000")
    assert all(row["offer_type"] == "FRRDELTAVAR" for row in result["plan"])


def test_hidden_requires_cap_and_never_beats_visible_on_equal_economics():
    with pytest.raises(ValueError):
        validate_policy_v3(policy(enable_hidden=True, hidden_max_share=None))
    result = build_strategy_plan_v3(
        D("10000"), D("10000"), {}, limit_policy(enable_hidden=True, hidden_max_share=D("10")), signals(), "hidden"
    )
    assert result["hidden_amount"] == 0
    assert all(row["flags"] == 0 for row in result["plan"])


def test_small_high_rate_outlier_is_removed_without_removing_supported_levels():
    now = int(time.time() * 1000)
    trades = [
        {"mts": now, "rate": D("0.0003"), "amount": D("1000"), "period": 2},
        {"mts": now, "rate": D("0.00031"), "amount": D("1000"), "period": 2},
        {"mts": now, "rate": D("0.02"), "amount": D("1"), "period": 2},
    ]
    filtered = filter_supported_trades(trades, policy())
    assert len(filtered) == 2
    assert max(row["rate"] for row in filtered) == D("0.00031")


def test_market_windows_and_spike_detection():
    now = 1_900_000_000_000
    historical = [
        {"id": i, "mts": now - 3_600_000 + i * 60_000, "rate": D("0.00020"), "amount": D("100"), "period": 2}
        for i in range(50)
    ]
    recent = [
        {"id": 100 + i, "mts": now - i * 20_000, "rate": D("0.00060"), "amount": D("500"), "period": 14}
        for i in range(10)
    ]
    result = build_market_signals_v3([], historical + recent, [], policy(), now)
    assert set(result["windows"]) == {"1m", "5m", "15m", "1h", "6h", "24h", "7d"}
    assert result["spike"] is True
    assert result["regime"] == "spike"


def test_replay_advances_fills_returns_interest_and_idle_time_without_exchange():
    now = 1_900_000_000_000
    replay_policy = limit_policy(short_share=D("100"), medium_share=D("0"), long_share=D("0"))
    trades = [
        {"id": index, "mts": now - 3 * 86_400_000 + index * 900_000, "rate": D("0.001"), "amount": D("1000"), "period": 2}
        for index in range(3 * 96)
    ]
    result = replay_strategy_v3(replay_policy, trades, [], D("900"), [], now)
    assert result["mode"] == "REPLAY"
    assert len(result["fills"]) > 0
    assert len(result["returns"]) > 0
    assert D(result["netInterest"]) > 0
    assert D(result["idlePrincipalTime"]) >= 0


def test_state_store_intent_idempotency_and_overallocation_guard():
    with tempfile.TemporaryDirectory() as directory:
        store = LendingStateStore(f"{directory}/state.sqlite3")
        created, first = store.reserve_intent(intent_order(), D("200"))
        duplicate, second = store.reserve_intent(intent_order(), D("200"))
        assert created is True and duplicate is False and first["id"] == second["id"]
        with pytest.raises(InsufficientReservedBalance):
            store.reserve_intent(intent_order(index=1, amount="100"), D("200"))


def test_concurrent_duplicate_intent_creates_one_row():
    with tempfile.TemporaryDirectory() as directory:
        store = LendingStateStore(f"{directory}/state.sqlite3")
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: store.reserve_intent(intent_order(), D("1000"))[0], range(8)))
        assert results.count(True) == 1
        assert len(store.intents()) == 1


def test_saving_identical_active_policy_as_draft_is_a_noop():
    with tempfile.TemporaryDirectory() as directory:
        store = LendingStateStore(f"{directory}/state.sqlite3")
        payload = {"version": 3, "enable_limit": True}
        active_id = store.save_strategy(payload, "ACTIVE")
        draft_id = store.save_strategy(payload, "DRAFT")
        assert draft_id == active_id
        assert store.strategy("ACTIVE")["version_id"] == active_id
        assert store.strategy("DRAFT") is None


def test_amount_percent_and_existing_account_exposure_caps_new_plan():
    capped = limit_policy(max_lend_amount=D("3000"), max_lend_percent=D("50"))
    result = build_strategy_plan_v3(
        D("10000"), D("4000"), {}, capped, signals(), "cap",
        existing_exposure={"total": D("2500"), "variable": D("0"), "hidden": D("0")},
    )
    assert result["funding_cap"] == D("3000")
    assert result["cap_remaining"] == D("500")
    assert result["planned_amount"] <= D("500")
    over = build_strategy_plan_v3(
        D("10000"), D("1000"), {}, capped, signals(), "over-cap",
        existing_exposure={"total": D("3500"), "variable": D("0"), "hidden": D("0")},
    )
    assert over["over_cap"] is True
    assert over["plan"] == []


def test_existing_variable_and_hidden_exposure_count_against_account_caps():
    variable_only = policy(
        enable_limit=False,
        enable_frr=False,
        enable_frr_delta_fixed=False,
        enable_frr_delta_variable=True,
        variable_max_share=D("10"),
    )
    result = build_strategy_plan_v3(
        D("10000"), D("1000"), {}, variable_only, signals(), "existing-variable",
        existing_exposure={"total": D("1000"), "variable": D("1000"), "hidden": D("0")},
    )
    assert result["plan"] == []


def test_pending_limit_switch_never_submits_old_frr_plan_and_waits_for_confirmation():
    now = 1_900_000_000_000

    class Client:
        api_key = ""
        api_secret = ""

        def __init__(self):
            self.canceled = []

        def cancel_funding_offer(self, offer_id):
            self.canceled.append(int(offer_id))
            return [0, "SUCCESS"]

    class Hub:
        fallback_ms = 300_000
        rest_stale_ms = 60_000

        def __init__(self):
            self.offers = [{
                "id": 99, "currency": "USD", "amount": D("200"),
                "amount_original": D("200"), "rate": D("0"),
                "rate_real": D("0.00035"), "period": 2,
                "offer_type": "FRRDELTAVAR", "display_type": "FRR",
                "flags": 0, "status": "ACTIVE", "managed": True,
                "pool": "short", "layer": "quick", "mts_created": now - 60_000,
            }]

        def snapshot(self, at):
            return {
                "as_of": at, "source": "WEBSOCKET", "safeRequired": False,
                "book": [
                    {"rate": D("0.0004"), "period": 2, "count": 1, "amount": D("5000")},
                    {"rate": D("0.0003"), "period": 2, "count": 1, "amount": D("-5000")},
                ],
                "trades": [{"id": 1, "mts": at, "rate": D("0.0004"), "amount": D("1000"), "period": 2}],
                "wallets": [{"wallet_type": "funding", "currency": "USD", "available": D("800")}],
                "offers": list(self.offers), "credits": [], "fundingTrades": [],
            }

        def stop(self):
            pass

    with tempfile.TemporaryDirectory() as directory:
        store = LendingStateStore(f"{directory}/state.sqlite3")
        old = policy()
        new = limit_policy()
        old_id = store.save_strategy(json_decimal(old.__dict__), "ACTIVE")
        new_id = store.save_strategy(json_decimal(new.__dict__), "PENDING")
        order = {
            **intent_order(amount="200"), "offer_type": "FRRDELTAVAR",
            "display_type": "FRR", "submitted_rate": D("0"),
            "effective_rate": D("0.00035"), "strategy_version": old_id,
        }
        _, intent = store.reserve_intent(order, D("1000"))
        store.confirm_intent(intent["id"], 99)
        store.reconcile_offers([{
            "id": 99, "currency": "USD", "amount": D("200"),
            "amount_original": D("200"), "rate": D("0"),
            "rate_real": D("0.00035"), "period": 2,
            "offer_type": "FRRDELTAVAR", "display_type": "FRR",
            "flags": 0, "status": "ACTIVE", "managed": True,
            "pool": "short", "layer": "quick", "mts_created": now - 60_000,
        }], now)
        store.set_mode("LIVE", "test")
        client = Client()
        hub = Hub()
        runtime = LendingRuntimeV3(client, old, store, hub=hub)
        runtime._bootstrapped = True
        runtime._last_rest_sync_ms = now
        submitted_types = []
        runtime._submit_plan = lambda result, wallet, version: submitted_types.extend(
            row["display_type"] for row in result["plan"]
        ) or []

        first = runtime.cycle(now)
        assert client.canceled == [99]
        assert submitted_types == []
        assert store.strategy("PENDING")["version_id"] == new_id
        assert first["strategyV3"]["submitted"] == []

        runtime.cycle(now + 1)
        assert client.canceled == [99]
        assert submitted_types == []

        store.reconcile_offers([], now + 2)
        hub.offers = []
        runtime.cycle(now + 2)
        assert store.strategy("ACTIVE")["version_id"] == new_id
        assert submitted_types and set(submitted_types) == {"LIMIT"}


def test_closed_slice_gets_new_generation_while_open_slice_remains_idempotent():
    with tempfile.TemporaryDirectory() as directory:
        store = LendingStateStore(f"{directory}/state.sqlite3")
        base = "v3:short:quick:0"
        assert store.replenishment_slice_key(base) == base
        _, intent = store.reserve_intent(intent_order(), D("1000"))
        assert store.replenishment_slice_key(base) == base
        store.confirm_intent(intent["id"], 77)
        store.reconcile_offers([], 2000)
        assert store.replenishment_slice_key(base) == base + ":r1"
        replacement = {**intent_order(), "slice_key": base + ":r1"}
        created, _ = store.reserve_intent(replacement, D("1000"))
        assert created is True


def test_ambiguous_resolution_requires_operator_and_returns_paused():
    with tempfile.TemporaryDirectory() as directory:
        store = LendingStateStore(f"{directory}/state.sqlite3")
        store.set_mode("LIVE", "test")
        _, intent = store.reserve_intent(intent_order(), D("1000"))
        store.mark_submitting(intent["id"])
        store.mark_ambiguous(intent["id"], "timeout")
        assert store.runtime()["mode"] == "SAFE" and store.runtime()["safe_manual"] == 1
        store.enter_safe("MARKET_DATA_STALE")
        assert store.runtime()["safe_manual"] == 1
        assert store.runtime()["safe_reason"].startswith("AMBIGUOUS_SUBMIT")
        with pytest.raises(StateStoreError):
            store.set_mode("PAUSED", "bypass")
        result = store.resolve_ambiguous_intent(intent["id"], close=True)
        assert result["intent"]["state"] == "CLOSED"
        assert result["runtime"]["mode"] == "PAUSED"


def test_safe_recovers_only_after_two_consistent_samples_thirty_seconds_apart():
    with tempfile.TemporaryDirectory() as directory:
        store = LendingStateStore(f"{directory}/state.sqlite3")
        store.set_mode("LIVE", "test")
        store.enter_safe("stale")
        store.record_consistent_sync(1_000_000)
        store.record_consistent_sync(1_020_000)
        assert store.runtime()["mode"] == "SAFE"
        store.record_consistent_sync(1_030_000)
        assert store.runtime()["mode"] == "LIVE"


def test_websocket_snapshot_increment_and_five_minute_safe_boundary():
    hub = BitfinexMarketDataHub(fallback_seconds=300, rest_stale_seconds=60)
    hub.handle_public_message({"event": "subscribed", "chanId": 7, "channel": "book"})
    hub.handle_public_message([7, [["0.0003", 2, 1, "500"], ["0.0004", 7, 1, "-200"]]])
    assert len(hub.snapshot()["book"]) == 2
    hub.handle_public_message([7, ["0.0003", 2, 0, "1"]])
    assert len(hub.snapshot()["book"]) == 1
    now = int(time.time() * 1000)
    hub._set_connected("public", False)
    hub.apply_rest_snapshot(book=[], synced_at_ms=now)
    hub._public_disconnected_since_ms = now - 299_000
    assert hub.snapshot(now)["safeRequired"] is False
    hub._public_disconnected_since_ms = now - 300_000
    assert hub.snapshot(now)["safeRequired"] is True


def test_market_trade_snapshots_merge_by_id_and_keep_newest_records():
    hub = BitfinexMarketDataHub(max_trades=3)
    hub.apply_rest_snapshot(trades=[
        {"id": "1", "mts": 1000, "amount": "10", "rate": "0.0001", "period": 2},
        {"id": "2", "mts": 2000, "amount": "20", "rate": "0.0002", "period": 2},
    ])
    hub.apply_rest_snapshot(trades=[
        {"id": "2", "mts": 2000, "amount": "25", "rate": "0.00025", "period": 2},
        {"id": "3", "mts": 3000, "amount": "30", "rate": "0.0003", "period": 2},
        {"id": "4", "mts": 4000, "amount": "40", "rate": "0.0004", "period": 2},
    ])
    trades = hub.snapshot(4000)["trades"]
    assert [row["id"] for row in trades] == ["2", "3", "4"]
    assert trades[0]["amount"] == D("25")


def test_websocket_and_rest_trade_overlap_is_not_double_counted():
    hub = BitfinexMarketDataHub(max_trades=10)
    hub.apply_rest_snapshot(trades=[
        {"id": "9", "mts": 9000, "amount": "9", "rate": "0.0009", "period": 2},
    ])
    hub.handle_public_message({"event": "subscribed", "chanId": 8, "channel": "trades"})
    hub.handle_public_message([8, "fte", [9, 9000, 10, "0.0010", 2]])
    trades = hub.snapshot(9000)["trades"]
    assert len(trades) == 1
    assert trades[0]["amount"] == D("10")
    assert trades[0]["rate"] == D("0.0010")


def test_authenticated_funding_trade_updates_are_deduplicated():
    hub = BitfinexMarketDataHub(api_key="key", api_secret="secret")
    base = [77, "fUSD", 1000, 55, "10", "0.0002", 2, 1]
    updated = [77, "fUSD", 2000, 55, "12", "0.0003", 2, 1]
    hub.handle_auth_message([0, "fte", base])
    hub.handle_auth_message([0, "ftu", updated])
    trades = hub.snapshot(2000)["fundingTrades"]
    assert len(trades) == 1
    assert trades[0]["amount"] == D("12")
    assert trades[0]["rate"] == D("0.0003")


def test_statistics_include_idle_principal_and_external_attribution():
    with tempfile.TemporaryDirectory() as directory:
        store = LendingStateStore(f"{directory}/state.sqlite3")
        start = 1_000_000_000
        store.record_account_sample("1000", "500", "0", "500", "0", start)
        store.record_account_sample("1000", "500", "0", "500", "1", start + 86_400_000)
        store.upsert_income_ledgers([{
            "id": 1, "currency": "USD", "wallet": "funding",
            "amount": D("1"), "balance": D("1001"),
            "description": "Margin Funding Payment", "mts": start + 86_400_000,
        }])
        stats = store.statistics(None, start + 86_400_000)
        assert stats["utilizationPercent"] == "50.0"
        assert stats["actualNetAprPercent"] == "36.500"
        assert D(stats["idlePrincipalTime"]) == D("500")


def test_realized_income_counts_all_positive_category_28_and_excludes_transfers():
    with tempfile.TemporaryDirectory() as directory:
        store = LendingStateStore(f"{directory}/state.sqlite3")
        now = int(time.time() * 1000)
        store.upsert_income_ledgers([
            {"id": 1, "currency": "USD", "wallet": "funding", "amount": D("1.25"),
             "balance": None, "description": "robot loan interest", "mts": now - 1000},
            {"id": 2, "currency": "USD", "wallet": "funding", "amount": D("2.75"),
             "balance": None, "description": "external loan interest", "mts": now - 500},
            {"id": 3, "currency": "USD", "wallet": "funding", "amount": D("-0.10"),
             "balance": None, "description": "negative adjustment", "mts": now},
        ])
        with store.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO ledger_entries(
                    ledger_id, currency, wallet, amount, description, category, mts
                ) VALUES(4, 'USD', 'funding', '1000', 'Wallet transfer', 51, ?)""",
                (now,),
            )
        summary = store.realized_income_summary("USD", now)
        assert summary["today"] == "4.00"
        assert summary["thirtyDays"] == "4.00"
        assert summary["lifetime"] == "4.00"


def test_income_history_backfill_pages_resume_and_upsert_duplicates():
    class LedgerHistoryClient:
        api_key = ""
        api_secret = ""

        def __init__(self):
            self.calls = []

        def ledgers(self, currency, **kwargs):
            self.calls.append((currency, kwargs))
            if len(self.calls) == 1:
                return [
                    [3, "USD", "funding", 3000, None, "3", "6", None, "interest"],
                    [2, "USD", "funding", 2000, None, "2", "3", None, "interest"],
                ]
            return [
                [2, "USD", "funding", 2000, None, "2", "3", None, "interest"],
                [1, "USD", "funding", 1000, None, "1", "1", None, "interest"],
            ]

    with tempfile.TemporaryDirectory() as directory:
        store = LendingStateStore(f"{directory}/state.sqlite3")
        client = LedgerHistoryClient()
        runtime = LendingRuntimeV3(client, limit_policy(), store, hub=object())
        first = runtime.sync_income_history_once(now_ms=4000, page_limit=2)
        assert first["status"] == "BACKFILLING"
        assert first["next_end_ms"] == 1999
        # A new runtime simulates process restart and resumes the persisted cursor.
        resumed = LendingRuntimeV3(client, limit_policy(), store, hub=object())
        second = resumed.sync_income_history_once(now_ms=5000, page_limit=3)
        assert second["status"] == "COMPLETE"
        assert second["earliest_mts"] == 1000
        assert client.calls[1][1]["end"] == 1999
        assert store.realized_income("USD") == D("6")


def test_income_sync_failure_is_reporting_only_and_keeps_existing_income():
    class FailingClient:
        api_key = ""
        api_secret = ""

        def ledgers(self, *args, **kwargs):
            raise RuntimeError("rate limited")

    with tempfile.TemporaryDirectory() as directory:
        store = LendingStateStore(f"{directory}/state.sqlite3")
        store.upsert_income_ledgers([{
            "id": 1, "currency": "USD", "wallet": "funding", "amount": D("2"),
            "balance": None, "description": "interest", "mts": 1000,
        }])
        runtime = LendingRuntimeV3(FailingClient(), limit_policy(), store, hub=object())
        with pytest.raises(RuntimeError):
            runtime.sync_income_history_once(now_ms=2000)
        # Worker error handling updates only the reporting state; runtime mode is unchanged.
        store.update_income_sync_state("USD", status="ERROR", error="rate limited")
        assert store.runtime()["mode"] == "PAUSED"
        assert store.realized_income("USD") == D("2")


def test_paused_and_replay_cycles_never_call_exchange_writes():
    now = int(time.time() * 1000)

    class FakeClient:
        api_key = ""
        api_secret = ""

        def __init__(self):
            self.writes = 0

        def submit_funding_offer(self, *args, **kwargs):
            self.writes += 1
            raise AssertionError("mode must not submit")

        def cancel_funding_offer(self, *args, **kwargs):
            self.writes += 1
            raise AssertionError("mode must not cancel")

    class FakeHub:
        def snapshot(self, at):
            return {
                "as_of": at,
                "source": "WEBSOCKET",
                "safeRequired": False,
                "book": [
                    {"rate": D("0.0004"), "period": 2, "count": 1, "amount": D("5000")},
                    {"rate": D("0.0003"), "period": 2, "count": 1, "amount": D("-5000")},
                ],
                "trades": [],
                "wallets": [{"wallet_type": "funding", "currency": "USD", "available": D("1000")}],
                "offers": [],
                "credits": [],
                "fundingTrades": [],
            }

        def stop(self):
            pass

    with tempfile.TemporaryDirectory() as directory:
        store = LendingStateStore(f"{directory}/state.sqlite3")
        store.save_strategy({}, "ACTIVE")
        client = FakeClient()
        runtime = LendingRuntimeV3(client, limit_policy(), store, hub=FakeHub())
        runtime._bootstrapped = True
        runtime._last_rest_sync_ms = now
        status = runtime.cycle(now)
        assert "1d" in status["statistics"]
        store.set_mode("REPLAY", "test")
        runtime.cycle(now + 1)
        assert client.writes == 0


def test_logger_redacts_credentials_from_logs_and_nested_status():
    with tempfile.TemporaryDirectory() as directory:
        path = f"{directory}/status.json"
        logger = Logger(path, 20, sensitive_values=("actual-key", "actual-secret"))
        logger.log("api_key=actual-key secret=actual-secret")
        logger.updateMetaValue("nested", {"error": "bfx-apikey: actual-key"})
        logger.persistStatus()
        text = open(path, "r", encoding="utf-8").read()
        assert "actual-key" not in text
        assert "actual-secret" not in text
        assert text.count("[REDACTED]") >= 3


def test_ledger_parser_uses_official_amount_balance_and_description_columns():
    rows = [[42, "USD", "funding", 1_900_000_000_000, None, "1.25", "101.25", None, "Margin Funding Payment"]]
    parsed = parse_ledger_rows_v3(rows)
    assert parsed == [{
        "id": 42,
        "currency": "USD",
        "wallet": "funding",
        "mts": 1_900_000_000_000,
        "amount": D("1.25"),
        "balance": D("101.25"),
        "description": "Margin Funding Payment",
    }]


def test_credit_is_attributed_to_managed_offer_through_funding_trade():
    with tempfile.TemporaryDirectory() as directory:
        store = LendingStateStore(f"{directory}/state.sqlite3")
        _, intent = store.reserve_intent(intent_order(), D("1000"))
        store.confirm_intent(intent["id"], 9001)
        opening = 1_900_000_000_000
        with store.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO funding_trades(trade_id, currency, offer_id, amount, rate, period, mts, managed)
                   VALUES(1, 'USD', 9001, '150', '0.0002', 2, ?, 1)""",
                (opening,),
            )
        store.reconcile_credits([{
            "id": 8001,
            "currency": "USD",
            "amount": D("150"),
            "rate": D("0.0002"),
            "period": 2,
            "status": "ACTIVE",
            "mts_opening": opening,
        }], opening)
        with store.read_connection() as connection:
            credit = dict(connection.execute("SELECT * FROM credits WHERE credit_id=8001").fetchone())
        assert credit["managed"] == 1
        assert credit["offer_id"] == 9001
        assert credit["pool"] == "short"


def test_variable_floor_violation_tracks_start_update_and_end():
    with tempfile.TemporaryDirectory() as directory:
        store = LendingStateStore(f"{directory}/state.sqlite3")
        violation = {"credit_id": 10, "pool": "short", "floor_rate": D("0.0002"), "observed_rate": D("0.0001")}
        store.record_rate_floor_violations([violation], 1000)
        store.record_rate_floor_violations([{**violation, "observed_rate": D("0.00009")}], 2000)
        store.record_rate_floor_violations([], 3000)
        with store.read_connection() as connection:
            rows = [dict(row) for row in connection.execute("SELECT * FROM rate_floor_violations").fetchall()]
        assert len(rows) == 1
        assert rows[0]["started_at_ms"] == 1000
        assert rows[0]["ended_at_ms"] == 3000
        assert rows[0]["observed_rate"] == "0.00009"
