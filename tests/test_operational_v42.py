from decimal import Decimal as D

import pytest

import AdaptiveRuntime
import OperationalV41 as readiness
import StrategyV4 as base
import StrategyV42 as core
from ResearchV4 import ModelRepository
from StateStore import LendingStateStore
from bitfinex import Bitfinex

NOW = 1_900_000_000_000


def cold_model(currency="USD", days=30):
    return core.fit_model(
        currency,
        [],
        now_ms=NOW,
        frr=[dict(mts=NOW - index * base.DAY, frr_daily_rate=".0008") for index in range(days)],
        allow_frr_reference=True,
    )


@pytest.mark.parametrize("currency", ["USD", "USDT"])
def test_v42_zero_own_samples_run_real_serializer_for_every_type(tmp_path, monkeypatch, currency):
    monkeypatch.setattr(Bitfinex, "_request_json", lambda *_a, **_kw: pytest.fail("network transport used"))
    store = LendingStateStore(tmp_path / (currency + ".sqlite3"), currency=currency)
    repo = ModelRepository(store.path, currency)
    model = cold_model(currency)
    assert set(model["typeObservationCounts"].values()) == {0}
    assert model["dataBasis"]["rollingRateSource"] == "FRR_HISTORY"
    repo.save(model)
    report = readiness.accept(repo, model, NOW)
    assert report["operationalReady"] and not report["eligibleForLiveCandidate"]
    assert not AdaptiveRuntime.eligible(store, model)
    for row in report["checks"]:
        assert row["passed"]
        if row["id"] in core.TYPES:
            assert row["submittedCount"] > 0
    assert readiness.status(repo, model, NOW)["operationalReady"]


def test_v42_insufficient_frr_history_never_runs_execution_or_passes(tmp_path, monkeypatch):
    repo = ModelRepository(tmp_path / "usd.sqlite3", "USD")
    monkeypatch.setattr(readiness, "execution_checks", lambda *_a: pytest.fail("insufficient data reached execution"))
    report = readiness.accept(repo, cold_model(days=19), NOW)
    assert not report["operationalReady"] and report["checks"] == [dict(id="FRR_HISTORY", passed=False, validDays=19)]
    assert not readiness.status(repo, cold_model(days=19), NOW)["operationalReady"]


def test_operational_probe_never_accepts_empty_plans_as_covered_types(monkeypatch):
    monkeypatch.setattr(core, "build_plan", lambda *_a, **_kw: dict(plan=[], candidates=[]))
    checks = readiness.execution_checks(cold_model(), NOW)
    assert all(not row["passed"] and row["submittedCount"] == 0 for row in checks if row["id"] in core.TYPES)


def test_operational_probe_fixed_negative_offset_reaches_real_payload(monkeypatch):
    payloads = []
    original = readiness._SimulatedExchange._request_json

    def capture(client, url, **kwargs):
        import json

        payloads.append(json.loads(kwargs["data"]))
        return original(client, url, **kwargs)

    monkeypatch.setattr(readiness._SimulatedExchange, "_request_json", capture)

    def plans(_account, policy, *_a, **_kw):
        kind = next(kind for kind, field in core.FIELDS.items() if getattr(policy, field))
        rate = D("-.0002") if kind == "FRR_DELTA_FIXED" else D(0) if kind == "FRR" else D(".0002")
        effective = D(".001") + rate if kind != "LIMIT" else rate
        raw_type = "FRRDELTAFIX" if kind == "FRR_DELTA_FIXED" else "LIMIT" if kind == "LIMIT" else "FRRDELTAVAR"
        row = dict(
            amount=D(150),
            submitted_rate=rate,
            effective_rate=effective,
            period=2,
            display_type=kind,
            offer_type=raw_type,
            flags=0,
            pool="short",
            layer="balanced",
            slice_index=0,
        )
        return dict(plan=[row], candidates=[], plan_hash="serializer")

    monkeypatch.setattr(core, "build_plan", plans)
    checks = readiness.execution_checks(cold_model(), NOW)
    assert all(row["passed"] for row in checks)
    assert any(row["type"] == "FRRDELTAFIX" and D(row["rate"]) < 0 for row in payloads)
