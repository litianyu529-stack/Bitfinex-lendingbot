"""Use the real Worker publishing path with local account activity only."""

import json
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

import V4Service
import lendingbot
from AppContext import AppContext
from Logger import Logger
from StrategyV3 import json_decimal
from test_v4 import FundingClient, configuration, order


@pytest.mark.parametrize("selected", ["USD", "USDT", "USD,USDT"])
def test_worker_serializes_real_release_comparison_without_flattening_structure(tmp_path, monkeypatch, selected):
    path, settings = configuration(tmp_path)
    client = FundingClient()
    context = AppContext.for_project(
        tmp_path, config_path=str(path), client_factory=lambda *_args: client, now=lambda: client.now / 1000
    )
    stores = V4Service.stores_for_profiles(settings, context.now)
    statuses = {}
    for currency, store in stores.items():
        version = store.save_strategy(json_decimal(settings.policies[currency].__dict__), "ACTIVE")
        spec = {**order(currency), "strategy_version": version}
        _, intent = store.reserve_intent(spec, D(1000))
        store.mark_submitting(intent["id"])
        store.confirm_intent(intent["id"], 901 if currency == "USD" else 902)
        comparison = store.release_comparison_v4()
        assert isinstance(comparison["after"]["submitted"][0]["amount"], D)
        statuses[currency] = {
            "currency": currency,
            "releaseComparison": comparison,
            "account": {"wallet": D("313.51"), "reconciled": True, "unread": None},
        }
    coordinator = SimpleNamespace(
        gate=SimpleNamespace(),
        runtimes={
            currency: SimpleNamespace(policy=settings.policies[currency], store=store)
            for currency, store in stores.items()
        },
        start=lambda: None,
        shutdown=lambda: None,
        cycle=lambda: statuses,
    )
    monkeypatch.setattr(V4Service, "V4Coordinator", lambda *_args, **_kwargs: coordinator)
    args = lendingbot.parse_args([
        "--config", str(path), "--live", "--confirmed-preflight", "--currencies", selected,
        "--once", "--no-server",
    ])
    assert V4Service.run_worker(args, settings, context, Logger(context.status_path, 20)) == 0
    published = json.loads(Path(context.status_path).read_text(encoding="utf-8"))
    assert isinstance(published["releaseComparison"], dict)
    assert isinstance(published["releaseComparison"]["after"]["submitted"], list)
    assert published["releaseComparison"] == published["currencies"]["USD"]["releaseComparison"]
    for currency in ("USD", "USDT"):
        status = published["currencies"][currency]
        row = status["releaseComparison"]["after"]["submitted"][0]
        assert isinstance(row["amount"], str) and D(row["amount"]) == spec["amount"]
        assert isinstance(row["weightedDailyRate"], str) and isinstance(row["count"], int)
        assert status["account"] == {"wallet": "313.51", "reconciled": True, "unread": None}
    assert not client.submissions
