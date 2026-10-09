import json
import threading
import time
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

import AdaptiveRuntime
import OperationalV41 as readiness
import ResearchV4
import StrategyV4 as base
import StrategyV41 as multi
import lendingbot
from AppContext import AppContext
from Configuration import strategy_v3_api_values, strategy_v3_from_record
from ExchangeModels import current_frr_observation
from ResearchV4 import ModelRepository, ResearchJobs
from StateStore import LendingStateStore
from StrategyV3 import json_decimal
from RuntimeV3 import LendingRuntimeV3
from V4Service import V4DashboardService
from test_v4 import FundingClient, configuration

NOW = 1_900_000_000_000


def model(currency="USD", days=30, trades=()):
    return multi.fit_model(
        currency,
        trades,
        now_ms=NOW,
        frr=[dict(mts=NOW - d * base.DAY, frr_daily_rate=".0008") for d in range(days)],
        allow_frr_reference=True,
    )


class PublicAccount(FundingClient):
    def funding_stats(self, symbol, **kwargs):
        assert symbol in ("fUSD", "fUST")
        return [
            [NOW - d * base.DAY, None, None, str(D(".0008") / 365), 2, 0, 0, 1000, 900]
            for d in range(30)
            if kwargs.get("start", 0) <= NOW - d * base.DAY <= kwargs.get("end", NOW)
        ]

    def ticker(self, symbol):
        return [D(".0008")] + [0] * 12 if symbol.startswith("f") else super().ticker(symbol)


@pytest.mark.parametrize("currency", ["USD", "USDT"])
def test_cold_start_ready_is_not_profitability_qualification(tmp_path, currency):
    store = LendingStateStore(tmp_path / (currency + ".sqlite3"), currency=currency)
    repo = ModelRepository(store.path, currency)
    m = model(currency)
    assert m["dataBasis"]["rollingRateSource"] == "FRR_HISTORY"
    assert set(m["typeObservationCounts"].values()) == {0}
    repo.save(m)
    report = readiness.accept(repo, m, NOW)
    assert report["operationalReady"] and not report["eligibleForLiveCandidate"]
    assert not AdaptiveRuntime.eligible(store, m)
    assert readiness.status(repo, m, NOW)["operationalReady"]
    assert all(row["passed"] for row in report["checks"])
    assert repo.load(m["id"], NOW) == m


def test_report_is_bound_to_model_currency_software_and_checksum(tmp_path, monkeypatch):
    repo = ModelRepository(tmp_path / "usd.sqlite3", "USD")
    m = model()
    repo.save(m)
    report = readiness.accept(repo, m, NOW)
    path = readiness.report_path(repo, m)
    for key, value in [
        ("currency", "USDT"),
        ("modelId", "other"),
        ("checkedAtMs", NOW + 1),
        ("softwareVersion", "other"),
        ("checks", []),
    ]:
        changed = {**report, key: value}
        changed["reportHash"] = base.digest({k: v for k, v in changed.items() if k != "reportHash"})
        path.write_text(json.dumps(changed), encoding="utf-8")
        assert not readiness.status(repo, m, NOW)["operationalReady"]
    path.write_text(json.dumps({**report, "reportHash": "corrupt"}), encoding="utf-8")
    assert not readiness.status(repo, m, NOW)["operationalReady"]
    path.write_text("invalid", encoding="utf-8")
    assert not readiness.status(repo, m, NOW)["operationalReady"]
    for data in ([], {**report, "checks": [1]}):
        path.write_text(json.dumps(data), encoding="utf-8")
        assert not readiness.status(repo, m, NOW)["operationalReady"]
    path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(readiness, "software_version", lambda: "new-software")
    assert not readiness.status(repo, m, NOW)["operationalReady"]


def test_missing_expired_future_and_insufficient_models_block(tmp_path):
    repo = ModelRepository(tmp_path / "usd.sqlite3", "USD")
    assert not readiness.status(repo, None, NOW)["operationalReady"]
    m = model(days=19)
    repo.save(m)
    assert not readiness.accept(repo, m, NOW)["operationalReady"]
    assert not readiness.status(repo, m, NOW)["operationalReady"]
    for changed in (
        {**m, "currency": "USDT"},
        {**m, "trainedUntilMs": NOW + 1},
        {**m, "validUntilMs": NOW - 1},
        {**m, "days": []},
    ):
        changed["id"] = base.digest({k: v for k, v in changed.items() if k != "id"})
        assert not readiness.status(repo, changed, NOW)["operationalReady"]
    old = base.fit_model("USD", [dict(mts=NOW, rate=".001", period=2, amount=1)], now_ms=NOW)
    assert not readiness.status(repo, old, NOW)["operationalReady"]
    with pytest.raises(ValueError):
        readiness.accept(repo, old, NOW)


def wait_job(jobs, coin):
    deadline = time.monotonic() + 10
    while jobs.status(coin)["state"] == "RUNNING" and time.monotonic() < deadline:
        time.sleep(0.01)
    return jobs.status(coin)


def test_prepare_async_public_only_resume_and_two_currency_isolation(tmp_path, monkeypatch):
    client = PublicAccount()
    stores = {coin: LendingStateStore(tmp_path / (coin + ".sqlite3"), currency=coin) for coin in ("USD", "USDT")}
    jobs = ResearchJobs(stores.__getitem__, lambda: NOW / 1000, lambda: client, public_interval=0)
    monkeypatch.setattr(jobs, "_start_backfill", lambda _store: None)

    def forbidden(*_args, **_kwargs):
        pytest.fail("preparation called an authenticated or exchange write interface")

    for name in ("wallets", "active_funding_offers", "submit_funding_offer_result", "key_permissions"):
        monkeypatch.setattr(client, name, forbidden)
    for coin in stores:
        jobs.start(coin, "prepare", engine=multi.ENGINE)
    for coin in stores:
        result = wait_job(jobs, coin)
        assert result["state"] == "COMPLETED", result
        assert result["report"]["operationalReady"]
        assert stores[coin].runtime()["mode"] == "PAUSED"
        assert ModelRepository(stores[coin].path, coin).candidate(NOW, multi.ENGINE)["currency"] == coin
    with pytest.raises(ValueError):
        jobs._public("submit_funding_offer", threading.Event())
    with pytest.raises(ValueError):
        jobs.start("USD", "prepare", engine=base.ENGINE)
    jobs.start("USD", "prepare", resume=True, engine=multi.ENGINE)
    assert wait_job(jobs, "USD")["report"]["operationalReady"]


def test_ticker_frr_is_current_observation_not_relabelled_statistics():
    row = current_frr_observation(PublicAccount(), "fUSD", NOW)
    assert row["frr_daily_rate"] == D(".0008") and row["source"] == "FUNDING_TICKER"
    assert row["observedAtMs"] == NOW and not row["exchangeTimestampAvailable"]
    for data in ([], [1], ["NaN"] + [0] * 12, [0] + [0] * 12):
        with pytest.raises(ValueError):
            current_frr_observation(SimpleNamespace(ticker=lambda _: data), "fUSD", NOW)


def test_streamed_quote_features_do_not_use_future_trades(tmp_path):
    store = LendingStateStore(tmp_path / "usd.sqlite3", currency="USD")
    store.upsert_market_trades(
        [
            dict(id=1, mts=NOW - 1000, amount=100, rate=".0002", period=2),
            dict(id=2, mts=NOW, amount=100000, rate=".02", period=2),
        ]
    )
    with store.read_connection() as connection:
        window = ResearchV4._MarketWindow(connection, NOW - base.DAY, NOW, lambda: False)
        window.advance(NOW - 1)
        assert window.rates(0, 2) == [dict(rate="0.0002", amount=100)]
        window.advance(NOW)
        assert len(window.rates(0, 2)) == 2
        window.advance(NOW + base.DAY + 1)
        assert window.rates(0, 2) == []


def test_preview_apply_and_preflight_bind_operational_report(tmp_path):
    path, _ = configuration(tmp_path)
    client = PublicAccount()
    context = AppContext.for_project(
        tmp_path, config_path=str(path), client_factory=lambda *_: client, now=lambda: NOW / 1000
    )
    service = V4DashboardService(str(path), context.status_path, context)
    store, _ = lendingbot.v3_store_for_config(str(path), "USD")
    service.config("USD")
    repo = ModelRepository(store.path, "USD")
    m = model()
    repo.save(m)
    readiness.accept(repo, m, NOW)
    p = replace(multi.template(strategy_v3_from_record(store.strategy("ACTIVE"))), model_id=m["id"])
    payload = {"strategyV3": strategy_v3_api_values(p)}
    preview = lendingbot.strategy_v3_preview(str(path), payload, app_context=context, v4=True)
    assert preview["plan"]["operationalReady"] and not preview["plan"]["eligibleForLiveCandidate"]
    saved = lendingbot.save_strategy_v3_draft(str(path), {**payload, **preview}, app_context=context, v4=True)
    readiness.report_path(repo, m).unlink()
    with pytest.raises(lendingbot.ApiRequestError):
        lendingbot.apply_strategy_v3_draft(str(path), saved, app_context=context)
    assert store.strategy("ACTIVE")["policy"].get("strategy_engine", "legacy_v3") == "legacy_v3"
    readiness.accept(repo, m, NOW)
    preview = lendingbot.strategy_v3_preview(str(path), payload, app_context=context, v4=True)
    saved = lendingbot.save_strategy_v3_draft(str(path), {**payload, **preview}, app_context=context, v4=True)
    assert lendingbot.apply_strategy_v3_draft(str(path), saved, app_context=context)["status"] == "ACTIVE"
    result = service.preflight(["USD"])
    assert result["canStart"], result["checks"]
    assert result["summary"]["operationalReportHash"] and not result["summary"]["eligibleForLiveCandidate"]
    readiness.report_path(repo, m).unlink()
    with pytest.raises(Exception, match="重新预检"):
        service.start(result["preflightId"], ["USD"])
    assert client.submissions == [] and store.runtime()["mode"] == "PAUSED"
    assert service.config("USD")["candidateModels"][multi.ENGINE]["operationalReady"] is False


def test_cold_start_runtime_uses_ready_model_and_stale_or_missing_report_prevents_writes(tmp_path):
    client = PublicAccount()
    store = LendingStateStore(tmp_path / "usd.sqlite3", currency="USD", clock=lambda: NOW / 1000)
    repo = ModelRepository(store.path, "USD")
    m = model()
    repo.save(m)
    readiness.accept(repo, m, NOW)
    p = replace(multi.template(lendingbot.StrategyPolicyV3()), model_id=m["id"])
    store.save_strategy(json_decimal(p.__dict__), "ACTIVE")
    store.set_mode("LIVE")
    runtime = LendingRuntimeV3(client, p, store, hub=object(), clock=lambda: NOW / 1000)
    runtime._stats = [current_frr_observation(client, "fUSD", NOW)]
    book, trades, _, _, _ = lendingbot.load_v3_market_context(client, p, NOW)
    account = dict(total=D(1000), wallet=D(1000), exposure=dict(short=D(0), medium=D(0), long=D(0)))
    snapshot = dict(book=book, trades=trades, offers=[], bookMts=NOW)
    result = AdaptiveRuntime.cycle(runtime, snapshot, account, {}, NOW, False)
    assert result["submitted"] and client.submissions
    count = len(client.submissions)
    readiness.report_path(repo, m).unlink()
    result = AdaptiveRuntime.cycle(runtime, snapshot, account, {}, NOW + 60000, False)
    assert not result["submitted"] and result["blockReasons"]
    assert len(client.submissions) == count and store.runtime()["mode"] == "PAUSED"


@pytest.mark.parametrize("currency", ["USD", "USDT"])
@pytest.mark.parametrize("explicit_cutoff", [False, True])
def test_first_live_cycle_uses_post_bootstrap_time_without_changing_explicit_cutoff(
    tmp_path, currency, explicit_cutoff
):
    from MarketDataStream import BitfinexMarketDataHub

    client = PublicAccount()
    client.now = NOW
    historical_stats = client.funding_stats
    client.funding_stats = lambda symbol, **kwargs: [r for r in historical_stats(symbol, **kwargs) if r[0] < NOW]

    def clock():
        return client.now / 1000

    store = LendingStateStore(tmp_path / (currency + ".sqlite3"), currency=currency, clock=clock)
    repo = ModelRepository(store.path, currency)
    m = model(currency)
    repo.save(m)
    readiness.accept(repo, m, NOW)
    policy = replace(
        multi.template(lendingbot.StrategyPolicyV3(currency=currency)), model_id=m["id"], max_lend_amount=D(1000)
    )
    store.save_strategy(json_decimal(policy.__dict__), "ACTIVE")
    store.set_mode("LIVE")
    hub = BitfinexMarketDataHub(symbol="fUSD" if currency == "USD" else "fUST", store=store, enable_auth=False)
    runtime = LendingRuntimeV3(client, policy, store, hub=hub, clock=clock)
    runtime.sync_history = lambda *_: None

    def bootstrap(**_kwargs):
        client.now += 1000
        runtime.sync_rest()
        runtime._bootstrapped = True

    runtime.bootstrap = bootstrap
    status = runtime.cycle(now_ms=NOW if explicit_cutoff else None)
    assert store.runtime()["mode"] == ("PAUSED" if explicit_cutoff else "LIVE")
    if explicit_cutoff:
        assert status["strategyV3"]["blockReasons"]
    else:
        assert not status["strategyV3"].get("blockReasons")


def test_prepare_cancel_resume_and_background_public_backfill(tmp_path, monkeypatch):
    client = PublicAccount()
    store = LendingStateStore(tmp_path / "usd.sqlite3", currency="USD")
    jobs = ResearchJobs(lambda _: store, lambda: NOW / 1000, lambda: client, public_interval=0)
    entered, release = threading.Event(), threading.Event()
    original = client.funding_stats

    def blocked(*args, **kwargs):
        entered.set()
        release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(client, "funding_stats", blocked)
    jobs.start("USD", "prepare", engine=multi.ENGINE)
    assert entered.wait(2)
    jobs.stop("USD")
    release.set()
    assert wait_job(jobs, "USD")["state"] == "CANCELLED"
    monkeypatch.setattr(client, "funding_stats", original)
    jobs.start("USD", "prepare", resume=True, engine=base.ENGINE)
    result = wait_job(jobs, "USD")
    assert result["engine"] == multi.ENGINE and result["report"]["operationalReady"]
    deadline = time.monotonic() + 5
    while (
        jobs.backfills.get("USD", {}).get("state") not in ("COMPLETED", "FAILED", "CANCELLED")
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    assert jobs.backfills["USD"]["state"] == "COMPLETED"
    assert client.submissions == []
