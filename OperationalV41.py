"""V4.1 execution acceptance, separate from profitability research.

Probes use an isolated temporary store and an in-memory exchange emulator.
There is no network or authenticated exchange client in this module.
"""

import json
import math
from dataclasses import replace
from decimal import Decimal as D
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from FileUtils import atomic_write_text
from StrategyV4 import DAY, MULTI_VERSION, digest, validate_model


@lru_cache(maxsize=1)
def software_version():
    root = Path(__file__).resolve().parent
    files = (
        "OperationalV41.py",
        "ResearchV4.py",
        "StrategyV4.py",
        "StrategyV41.py",
        "StrategyV3.py",
        "AdaptiveRuntime.py",
        "RuntimeV3.py",
        "RuntimeV4.py",
        "WriteRecovery.py",
        "Recovery.py",
        "StateStore.py",
        "ExchangeModels.py",
        "Configuration.py",
        "V4Service.py",
        "lendingbot.py",
        "DashboardServer.py",
    )
    return "V4.1_OPERATIONAL_1:" + digest({name: (root / name).read_text(encoding="utf-8") for name in files})


def report_path(repo, model):
    return repo.directory / ("operational-" + model["id"] + ".json")


def status(repo, model, now_ms):
    result = {"operationalReady": False, "operationalReportHash": None, "operationalBlockReasons": []}
    try:
        if not model or model.get("algorithm") != MULTI_VERSION:
            raise ValueError("V4.1 模型尚未准备")
        validate_model(model, repo.currency, now_ms)
        report = json.loads(report_path(repo, model).read_text(encoding="utf-8"))
        body = {k: v for k, v in report.items() if k != "reportHash"}
        required = {
            "FRR_HISTORY",
            "LIMIT",
            "FRR",
            "FRR_DELTA_FIXED",
            "FRR_DELTA_VARIABLE",
            "INTENT_CONFIRMED",
            "INTENT_UNKNOWN",
            "INTENT_DEFINITE_REJECT",
        }
        if (
            digest(body) != report.get("reportHash")
            or report.get("modelId") != model["id"]
            or report.get("currency") != repo.currency
            or report.get("softwareVersion") != software_version()
            or report.get("checkedAtMs", now_ms + 1) > now_ms
            or report.get("operationalReady") is not True
            or not report.get("checks")
            or {row.get("id") for row in report["checks"]} != required
            or not all(row.get("passed") is True for row in report["checks"])
        ):
            raise ValueError("运行验收报告已变化或与模型、软件版本不匹配，请重新快速准备")
        result.update(operationalReady=True, operationalReportHash=report["reportHash"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ArithmeticError) as exc:
        result["operationalBlockReasons"] = [
            str(exc) if isinstance(exc, ValueError) else "运行验收报告缺失或损坏，请快速准备模型"
        ]
    return result


class _SimulatedExchange:
    """Only emulated submissions; deliberately has no network credentials."""

    def __init__(self, outcome):
        self.outcome, self.calls = outcome, 0

    def submit_funding_offer_result(self, *_args, **_kwargs):
        from DomainTypes import WriteResult

        self.calls += 1
        return WriteResult(self.outcome, response=[0, 0, 0, 0, [10000 + self.calls]], error="simulated")


def execution_checks(model, now_ms):
    from DomainTypes import WriteOutcome
    from RuntimeV3 import LendingRuntimeV3
    from StateStore import LendingStateStore
    from StrategyV3 import StrategyPolicyV3
    from StrategyV41 import FIELDS, build_plan, template
    from StrategyV4 import gross_floor
    from Currency import funding_sizing

    checks = []
    # Hypothetical quotes exercise code paths. They are never performance evidence.
    frr = D(".001")
    trades = [dict(mts=now_ms - 1000, rate=D(".0012"), amount=D(100000), period=2)]
    book = [dict(rate=D(".0012"), amount=D(-10000), period=2)]
    account = dict(total=D(1000), wallet=D(1000), exposure=dict(short=D(0), medium=D(0), long=D(0)))
    policy = replace(template(StrategyPolicyV3(currency=model["currency"])), model_id=model["id"])
    with funding_sizing(D(150)), TemporaryDirectory(prefix="v41-acceptance-") as directory:
        for kind, field in FIELDS.items():
            selected = replace(policy, **{name: name == field for name in FIELDS.values()}, variable_max_share=D(100))
            plan = build_plan(account, selected, model, book, trades, now_ms, "simulation", frr)
            valid = not plan.get("blockReasons") and len(plan["plan"]) <= 8
            valid = valid and sum((r["amount"] for r in plan["plan"]), D(0)) <= account["wallet"]
            valid = valid and all(
                r["display_type"] == kind and r["amount"] >= 150 and 2 <= r["period"] <= 120 and r["flags"] == 0
                for r in plan["plan"]
            )
            valid = valid and all(math.isfinite(r["conservativeNetApr"]) for r in plan.get("candidates", []))
            valid = valid and all(r["effective_rate"] >= gross_floor(selected, r["period"]) for r in plan["plan"])
            checks.append({"id": kind, "passed": bool(valid), "note": "模拟流程通过；未被选中的报价不代表成交失败"})
        # Exercise actual durable intents, including unknown outcome and restart visibility.
        row = dict(
            amount=D(150),
            rate=D(".0012"),
            effective_rate=D(".0012"),
            submitted_rate=D(".0012"),
            period=2,
            offer_type="LIMIT",
            display_type="LIMIT",
            flags=0,
            pool="short",
            layer="balanced",
            slice_index=0,
        )
        for outcome in (WriteOutcome.CONFIRMED, WriteOutcome.UNKNOWN, WriteOutcome.DEFINITE_REJECT):
            path = Path(directory) / (outcome.name + ".sqlite3")
            store = LendingStateStore(path, currency=model["currency"], clock=lambda: now_ms / 1000)
            client = _SimulatedExchange(outcome)
            runtime = SimpleNamespace(
                store=store,
                currency=model["currency"],
                client=client,
                clock=lambda: now_ms / 1000,
                _log=lambda _message: None,
            )
            result = LendingRuntimeV3._submit_plan(
                runtime, dict(plan=[row], plan_hash="simulation"), D(150), "simulation"
            )
            restarted = LendingStateStore(path, currency=model["currency"], clock=lambda: now_ms / 1000)
            if outcome == WriteOutcome.UNKNOWN:
                runtime.store = restarted
                duplicate = LendingRuntimeV3._submit_plan(
                    runtime, dict(plan=[row], plan_hash="simulation"), D(150), "simulation"
                )
                if duplicate:
                    raise ValueError("未知写入被重复提交")
            states = [r["state"] for r in restarted.intents()]
            expected = {
                WriteOutcome.CONFIRMED: "CONFIRMED",
                WriteOutcome.UNKNOWN: "AMBIGUOUS",
                WriteOutcome.DEFINITE_REJECT: "CLOSED",
            }[outcome]
            checks.append(
                {
                    "id": "INTENT_" + outcome.name,
                    "passed": client.calls == 1
                    and expected in states
                    and bool(result) == (outcome == WriteOutcome.CONFIRMED),
                }
            )
    return checks


def accept(repo, model, now_ms):
    validate_model(model, repo.currency, now_ms)
    if model["algorithm"] != MULTI_VERSION:
        raise ValueError("快速运行验收只适用于 V4.1")
    recent = {
        int(r["mts"]) // DAY
        for r in model.get("frrDays", [])
        if now_ms - 30 * DAY <= int(r["mts"]) <= now_ms and D(str(r["rate"])) > 0
    }
    checks = [{"id": "FRR_HISTORY", "passed": len(recent) >= 20, "validDays": len(recent)}]
    if checks[0]["passed"]:
        checks.extend(execution_checks(model, now_ms))
    report = dict(
        currency=repo.currency,
        modelId=model["id"],
        softwareVersion=software_version(),
        checkedAtMs=now_ms,
        operationalReady=all(r["passed"] for r in checks),
        checks=checks,
        eligibleForLiveCandidate=False,
        note="运行验收使用模拟账户，未证明收益优势或真实成交",
    )
    report["reportHash"] = digest(report)
    repo.directory.mkdir(parents=True, exist_ok=True)
    atomic_write_text(str(report_path(repo, model)), json.dumps(report, ensure_ascii=False, sort_keys=True))
    return report
