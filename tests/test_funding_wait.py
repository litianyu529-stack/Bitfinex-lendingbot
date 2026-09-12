"""Complete order-chain wait statistics; all exchanges and stores are isolated."""

import json
from decimal import Decimal as D
from pathlib import Path
import subprocess

import pytest

from StateStore import LendingStateStore


START = 1_900_000_000_000
MINUTE = 60_000


@pytest.fixture
def store(tmp_path):
    return LendingStateStore(tmp_path / "wait.sqlite3", clock=lambda: START / 1000)


def offer(store, offer_id, mts, *, slice_key=None, amount="150", chain=False):
    store.clock = lambda: mts / 1000
    order = {
        "currency": "USD", "amount": D(amount), "submitted_rate": D("0.0003"),
        "effective_rate": D("0.0003"), "period": 30, "offer_type": "LIMIT",
        "slice_key": slice_key or f"v:medium:high:{offer_id}", "strategy_version": "v",
        "pool": "medium", "layer": "high",
    }
    _, intent = store.reserve_intent(order, D("100000"))
    store.confirm_intent(intent["id"], offer_id)
    payload = {
        "id": offer_id, "currency": "USD", "amount": D(amount), "rate": D("0.0003"),
        "period": 30, "offer_type": "LIMIT", "mts_created": mts, "mts_updated": mts,
        "managed": True, "pool": "medium", "layer": "high",
    }
    store.reconcile_offers([payload], mts)
    result = store.ensure_reprice_chain({**payload, "offer_id": offer_id}, "v", mts) if chain else None
    return intent, result


def fill(store, trade_id, offer_id, mts, amount="150", managed=True):
    with store.transaction() as connection:
        connection.execute(
            """INSERT OR REPLACE INTO funding_trades
               (trade_id,currency,offer_id,amount,rate,period,mts,managed) VALUES(?, 'USD', ?, ?, ?, 30, ?, ?)""",
            (trade_id, offer_id, amount, "0.0003", mts, int(managed)),
        )


def replace_offer(store, chain, old_id, new_id, mts, stage=1, amount="150"):
    store.record_reprice(old_id, f"age_stage_{stage}", chain_key=chain["chain_key"], created_at_ms=mts)
    store.mark_reprice_pending(
        chain["chain_key"], "AGE_STAGE", D("0.0003"), stage=stage, now_ms=mts, source_offer_id=old_id
    )
    offer(store, new_id, mts, slice_key=f"v:medium:high:{old_id}:r{stage}", amount=amount)
    store.bind_reprice_replacement_chain(chain["chain_key"], new_id, D("0.0003"), mts)


def period(store, start=0, end=START + 1000 * MINUTE):
    return store.period_activity(start, until_ms=end)["traded"][0]


def test_reprice_wait_is_fifty_minutes_in_every_interface(store):
    _, chain = offer(store, 101, START, chain=True)
    replace_offer(store, chain, 101, 102, START + 30 * MINUTE)
    fill(store, 1, 102, START + 50 * MINUTE)
    # A later intent state timestamp must never affect the result.
    with store.transaction() as connection:
        connection.execute("UPDATE order_intents SET updated_at_ms=?", (START + 900 * MINUTE,))
    row = period(store, START + 40 * MINUTE)
    assert row["weightedWaitMinutes"] == 50
    assert row["waitValidCount"] == 1
    assert row["waitMissingCount"] == 0
    assert D(row["waitCoveragePercent"]) == 100
    stats = store.statistics(None, START + 50 * MINUTE)
    assert D(stats["averageWaitSeconds"]) == 3000
    comparison = store.release_comparison(START + 60 * MINUTE)
    assert comparison["after"]["traded"][0]["weightedWaitMinutes"] == 50


def test_partial_fills_history_and_migration_copies_do_not_double_count(store):
    _, chain = offer(store, 101, START, chain=True)
    fill(store, 1, 101, START + 10 * MINUTE, "50")
    replace_offer(store, chain, 101, 102, START + 20 * MINUTE)
    fill(store, 2, 102, START + 30 * MINUTE, "-100")
    replace_offer(store, chain, 102, 103, START + 40 * MINUTE, stage=2)
    fill(store, 3, 103, START + 50 * MINUTE, "150")
    fill(store, 3, 103, START + 50 * MINUTE, "150")
    # The migration itself copies explicit offer/chain evidence.
    store.normalize_active_strategy({"version": 3})
    # Add a same-start copy sharing the original intent and current offer.
    with store.transaction() as connection:
        cols = [row[1] for row in connection.execute("PRAGMA table_info(reprice_chains)")]
        original = dict(connection.execute(
            "SELECT * FROM reprice_chains WHERE chain_key=?", (chain["chain_key"],)
        ).fetchone())
        original["chain_key"] = "new|" + chain["chain_key"].split("|", 1)[1]
        original["strategy_version"] = "new"
        connection.execute(
            f"INSERT INTO reprice_chains ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
            [original[col] for col in cols],
        )
    row = period(store)
    assert row["count"] == row["waitValidCount"] == 3
    assert row["amount"] == 300
    assert row["weightedWaitMinutes"] == D(50 * 10 + 100 * 30 + 150 * 50) / 300


def test_original_offer_fallback_and_no_fill_cancellations(store):
    offer(store, 101, START)
    offer(store, 102, START + MINUTE)
    fill(store, 1, 101, START + 20 * MINUTE)
    row = period(store)
    assert row["count"] == 1
    assert row["weightedWaitMinutes"] == 20


@pytest.mark.parametrize(
    "problem", ["replacement", "missing_exchange", "conflicting_time", "future_time", "wrong_term"]
)
def test_unproven_originals_are_missing_without_changing_trade_totals(store, problem):
    offer(store, 101, START, slice_key="v:medium:high:0:r1" if problem == "replacement" else None)
    with store.transaction() as connection:
        if problem == "missing_exchange":
            connection.execute("DELETE FROM offers")
        elif problem == "future_time":
            connection.execute("UPDATE offers SET mts_created=?", (START + 100 * MINUTE,))
        elif problem == "wrong_term":
            connection.execute("UPDATE offers SET period=120")
    if problem == "conflicting_time":
        store.upsert_offer_history([{
            "id": 101, "currency": "USD", "amount": "0", "rate": "0.0003", "period": 30,
            "offer_type": "LIMIT", "flags": 0, "status": "EXECUTED", "mts_created": START + 1,
            "mts_updated": START + 10 * MINUTE,
        }])
    fill(store, 1, 101, START + 20 * MINUTE)
    row = period(store)
    assert row["weightedWaitMinutes"] is None
    assert row["waitMissingCount"] == row["count"] == 1
    assert row["amount"] == 150
    assert row["weightedDailyRate"] == D("0.0003")
    assert D(row["waitCoveragePercent"]) == 0


@pytest.mark.parametrize("bad_start", [START + 1, START + 100 * MINUTE, 0])
def test_conflicting_chain_starts_are_not_resolved_by_taking_earliest(store, bad_start):
    _, chain = offer(store, 101, START, chain=True)
    with store.transaction() as connection:
        connection.execute(
            """INSERT INTO reprice_chains
               (chain_key,strategy_version,base_slice_key,pool,layer,origin_rate,started_at_ms,
                current_offer_id,updated_at_ms) VALUES('other','other','different','medium','high','0.0003',?,101,?)""",
            (bad_start, START),
        )
    fill(store, 1, 101, START + 20 * MINUTE)
    assert period(store)["weightedWaitMinutes"] is None


def test_consolidation_ancestry_missing_but_earlier_partial_fill_valid(store):
    _, chain = offer(store, 101, START, chain=True)
    fill(store, 1, 101, START + 10 * MINUTE, "50")
    offer(store, 102, START + 20 * MINUTE, slice_key="v:dust-20-101:medium:high:0", amount="200")
    store.bind_consolidation_replacement(101, 102, START + 20 * MINUTE)
    store.clock = lambda: (START + 20 * MINUTE) / 1000
    store.record_ownership_event(
        "DUST_CHAIN_BOUND", offer_id=102, details={"sourceOfferId": 101, "chainKey": chain["chain_key"]}
    )
    replace_offer(store, chain, 102, 103, START + 30 * MINUTE, amount="200")
    fill(store, 2, 103, START + 40 * MINUTE, "200")
    row = period(store)
    assert row["count"] == 2
    assert row["waitValidCount"] == row["waitMissingCount"] == 1
    assert row["weightedWaitMinutes"] == 10
    assert row["amount"] == 250
    assert D(row["waitCoveragePercent"]) == 20


def test_equal_slice_names_do_not_link_unrelated_loans(store):
    offer(store, 101, START, slice_key="v:medium:high:0", chain=True)
    # No explicit chain linkage: a replacement-looking intent is not proof.
    offer(store, 102, START + 30 * MINUTE, slice_key="v:medium:high:0:r1")
    fill(store, 1, 102, START + 50 * MINUTE)
    assert period(store)["weightedWaitMinutes"] is None


def test_thirty_day_real_shape_regression(store):
    _, chain = offer(store, 101, START, chain=True)
    replace_offer(store, chain, 101, 102, START + 60 * MINUTE, stage=4)
    replace_offer(store, chain, 102, 103, START + 240 * MINUTE, stage=8)
    replace_offer(store, chain, 103, 104, START + 21_756_112, stage=10)
    fill(store, 1, 104, START + 63015 * 600)
    assert period(store)["weightedWaitMinutes"] == D("630.15")
    assert D(63015 * 600 - 21_756_112) / MINUTE == D("267.5481333333333333333333333")


def test_window_edges_zero_wait_and_no_records(store):
    empty = store.statistics(None, START)
    assert empty["averageWaitSeconds"] is None
    assert empty["waitCoveragePercent"] is None
    assert empty["waitValidCount"] == empty["waitMissingCount"] == 0
    offer(store, 101, START)
    fill(store, 1, 101, START)
    assert period(store, START, START + 1)["weightedWaitMinutes"] == 0
    assert store.period_activity(START - MINUTE, until_ms=START)["traded"] == []
    assert D(store.statistics(None, START)["averageWaitSeconds"]) == 0


def test_external_trades_and_invalid_durations_are_not_zero_waits(store):
    offer(store, 101, START, chain=True)
    fill(store, 1, 101, START - 1)
    fill(store, 2, 101, START + MINUTE, managed=False)
    row = period(store)
    assert row["count"] == 1
    assert row["weightedWaitMinutes"] is None


def test_query_count_is_bounded_and_statistics_are_read_only(store):
    offer(store, 101, START, chain=True)
    for i in range(1, 51):
        fill(store, i, 101, START + i * MINUTE, "1")
    with store.read_connection() as connection:
        connection.execute("BEGIN")
        statements = []
        connection.set_trace_callback(statements.append)
        before = connection.total_changes
        records = store._funding_wait_records(connection, "USD", 0, START + 100 * MINUTE)
        assert len(records) == 50
        assert len(statements) <= 8
        assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
        assert connection.total_changes == before


def test_browser_wait_formatter_handles_null_and_coverage():
    script = Path("www/lendingbot.js").read_text(encoding="utf-8")
    helper = script[script.index("function formatFundingWait("):script.index("function safeNumber(")]
    checks = r'''
const assert = require('node:assert/strict');
assert.match(formatFundingWait(null), /平均等待：—/);
assert.match(formatFundingWait({averageWaitSeconds: null, waitCoveragePercent: null}), /金额覆盖率 —/);
assert.match(formatFundingWait({averageWaitSeconds: '0', waitCoveragePercent: '0'}), /平均等待：0.0 分钟/);
assert.match(formatFundingWait({averageWaitSeconds: '3000', waitCoveragePercent: '75',
    waitValidCount: 3, waitMissingCount: 1}), /50.0 分钟.*75.0%.*有效 3 笔 \/ 缺失 1 笔/);
assert.match(formatFundingWait({weightedWaitMinutes: '630.15'}, 'weightedWaitMinutes', 1), /630.1 分钟/);
assert.match(formatFundingWait({averageWaitSeconds: 'bad'}), /平均等待：—/);
'''
    result = subprocess.run(["node", "-e", helper + checks], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    html = Path("www/lendingbot.html").read_text(encoding="utf-8")
    assert 'id="fundingWaitSummary"' in html
    assert 'formatFundingWait(row, "weightedWaitMinutes", 1)' in Path("www/v3-dashboard.js").read_text(encoding="utf-8")


def test_wire_serialization_preserves_null(store):
    from StrategyV3 import json_decimal

    offer(store, 101, START, slice_key="v:medium:high:0:r1")
    fill(store, 1, 101, START + MINUTE)
    payload = json.loads(json.dumps(json_decimal(store.period_activity(0))))
    assert payload["traded"][0]["weightedWaitMinutes"] is None
