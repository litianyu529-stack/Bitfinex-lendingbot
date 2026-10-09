"""Adaptive integration: read-only planning and journal-before-write execution."""

from decimal import Decimal as D

from ResearchV4 import ModelRepository
from StrategyV3 import json_decimal
from StrategyV4 import adjustment, build_plan, digest


def context(store, policy, book, trades, now_ms, stats=()):
    repo = ModelRepository(store.path, policy.currency)
    try:
        model = repo.load(policy.model_id, now_ms)
        expected = (
            "ADAPTIVE_NET_YIELD_V2" if policy.strategy_engine == "adaptive_net_yield_v2" else "ADAPTIVE_NET_YIELD_V1"
        )
        if model["algorithm"] != expected:
            model = None
    except ValueError:
        model = None
    valid = [r for r in stats if 0 <= now_ms - int(r["mts"]) <= policy.rest_stale_seconds * 1000]
    latest = max(valid, key=lambda r: int(r["mts"]), default=None)
    return {
        "model": model,
        "book": book,
        "trades": trades,
        "now_ms": now_ms,
        "frr": D(str(latest["frr_daily_rate"])) if latest else None,
    }


def eligible(store, model):
    import json

    if not model or not model.get("eligibleForLiveCandidate"):
        return False
    try:
        repo = ModelRepository(store.path, store.currency)
        report = json.loads((repo.directory / repo.report_name(model["algorithm"])).read_text(encoding="utf-8"))
        trained = {k: v for k, v in model.items() if k not in {"id", "validationReportHash"}}
        trained["eligibleForLiveCandidate"] = False
        return (
            report.get("eligibleForLiveCandidate") is True
            and digest(report) == model.get("validationReportHash")
            and digest(trained) == report.get("testedModelTrainingHash")
        )
    except (OSError, ValueError):
        return False


def plan(account, policy, signals, version):
    ctx = signals.get("adaptiveContext", {})
    if policy.strategy_engine == "adaptive_net_yield_v2":
        from StrategyV41 import build_plan as multi_plan

        return multi_plan(
            account,
            policy,
            ctx.get("model"),
            ctx.get("book", []),
            ctx.get("trades", []),
            ctx.get("now_ms", 0),
            version,
            ctx.get("frr"),
        )
    return build_plan(
        account, policy, ctx.get("model"), ctx.get("book", []), ctx.get("trades", []), ctx.get("now_ms", 0), version
    )


def cycle(runtime, snapshot, account, signals, now, resume_barrier):
    """Cancel confirmation is an authoritative REST barrier, including after restart."""
    from RuntimeV3 import _write_result
    from DomainTypes import WriteOutcome

    policy, store = runtime.policy, runtime.store
    active = store.strategy("ACTIVE")
    version = active["version_id"] if active else "4"
    ctx = context(store, policy, snapshot["book"], snapshot["trades"], now, getattr(runtime, "_stats", ()))
    result = plan(account, policy, {"adaptiveContext": ctx}, version)
    result.update(submitted=[], canceledForReprice=[], decisions=[])
    if store.runtime()["mode"] != "LIVE" or resume_barrier:
        result["recoveryResumeBarrier"] = bool(resume_barrier)
        return result
    if not eligible(store, ctx["model"]):
        result["blockReasons"] = ["模型尚未通过收益研究验收；保持研究状态"]
        store.enter_protected_pause("ADAPTIVE_MODEL_NOT_QUALIFIED")
        return result
    if result.get("blockReasons"):
        store.enter_protected_pause("ADAPTIVE_DATA_UNAVAILABLE")
        return result
    pending = store.pending_reprices(version)
    active_ids = {int(r.get("offer_id") or r.get("id")) for r in snapshot["offers"]}
    if any(int(r["pending_source_offer_id"]) in active_ids for r in pending):
        result["blockReasons"] = ["等待账户确认撤单，禁止提交替代单"]
        return result
    signature = digest({"account": account, "book": snapshot["book"]})
    if signature == getattr(runtime, "_adaptive_signature", None) and now - getattr(runtime, "_adaptive_at", 0) < 60000:
        result["decisions"] = [{"action": "WAIT", "reason": "EVALUATION_INTERVAL"}]
        return result
    if now - getattr(runtime, "_adaptive_at", 0) < 10000:
        return result
    runtime._adaptive_signature, runtime._adaptive_at = signature, now
    confirmations = getattr(runtime, "_adaptive_confirmations", {})
    runtime._adaptive_confirmations = confirmations
    repo = ModelRepository(store.path, policy.currency)

    def journal(action):
        repo.journal(
            {
                "atMs": now,
                "mode": "LIVE",
                "modelId": policy.model_id,
                "modelChecksum": ctx["model"]["id"],
                "strategyVersion": version,
                "accountDigest": digest(account),
                "account": account,
                "marketAtMs": snapshot.get("bookMts", now),
                "planHash": result["plan_hash"],
                "action": action,
            }
        )

    try:
        cap_excess = max(D(0), result["existing_exposure"] - result["funding_cap"])
        long_excess = max(
            D(0),
            sum((D(v) for p, v in account.get("exposureByPeriod", {}).items() if int(p) >= policy.long_from_days), D(0))
            - D(account["total"]) * policy.long_max_share / 100,
        )
        for offer in snapshot["offers"]:
            oid = int(offer.get("offer_id") or offer.get("id"))
            if policy.strategy_engine == "adaptive_net_yield_v2":
                from StrategyV41 import adjustment as multi_adjustment

                decision = multi_adjustment(
                    policy,
                    ctx["model"],
                    offer,
                    result["candidates"],
                    snapshot["trades"],
                    snapshot["book"],
                    now,
                    ctx["frr"],
                )
            else:
                decision = adjustment(
                    policy, ctx["model"], offer, result["candidates"], snapshot["trades"], snapshot["book"], now
                )
            if offer.get("managed") and (
                cap_excess > 0 or (long_excess > 0 and int(offer["period"]) >= policy.long_from_days)
            ):
                decision = {"action": "CANCEL", "hard": True, "reason": "CAP_EXCEEDED"}
                cap_excess = max(D(0), cap_excess - D(offer["amount"]))
                if int(offer["period"]) >= policy.long_from_days:
                    long_excess = max(D(0), long_excess - D(offer["amount"]))
            decision["offerId"] = oid
            chain = store.reprice_chain_for_offer(oid)
            decision["cumulativeWaitMinutes"] = (
                now - int((chain or {}).get("started_at_ms") or offer.get("mts_created") or now)
            ) / 60000
            result["decisions"].append(decision)
            if decision["action"] != "CANCEL":
                confirmations.pop(oid, None)
                continue
            if not decision.get("hard"):
                key = digest(decision | {"cumulativeWaitMinutes": 0, "aprGain": 0, "p10InterestGain": 0})
                old_key, count = confirmations.get(oid, (None, 0))
                confirmations[oid] = (key, count + 1 if old_key == key else 1)
                if confirmations[oid][1] < 2 or store.reprice_count_since(now - 3600000) >= min(
                    12, policy.max_reprices_per_hour
                ):
                    continue
                if (
                    chain
                    and now - int(chain.get("last_reprice_at_ms") or 0)
                    < max(2, policy.reprice_cooldown_minutes) * 60000
                ):
                    continue
            chain = chain or store.ensure_reprice_chain(offer, version, now)
            if chain is None or chain.get("pending_action"):
                continue
            # Persist intent before the network operation. A crash or timeout cannot
            # cause an untracked repeat cancel or immediate replacement.
            journal(decision)
            target = decision.get("targetRate", offer.get("rate_real") or offer["rate"])
            store.mark_reprice_pending(
                chain["chain_key"], policy.strategy_engine, target, now_ms=now, source_offer_id=oid
            )
            store.record_reprice(
                oid,
                decision["reason"],
                offer.get("rate_real") or offer["rate"],
                target,
                strategy_version=version,
                plan_hash=result["plan_hash"],
                chain_key=chain["chain_key"],
            )
            write = _write_result(runtime.client, "cancel_funding_offer_result", "cancel_funding_offer", oid)
            if write.outcome == WriteOutcome.DEFINITE_REJECT:
                store.bind_reprice_replacement_chain(
                    chain["chain_key"], oid, offer.get("rate_real") or offer["rate"], now
                )
                store.record_ownership_event("CANCEL_REJECTED", offer_id=oid, details=json_decimal(decision))
                result["blockReasons"] = ["撤单被明确拒绝，保留原单并等待冷却"]
                return result
            store.record_ownership_event(
                "CANCEL_CONFIRMED" if write.outcome == WriteOutcome.CONFIRMED else "CANCEL_UNKNOWN",
                offer_id=oid,
                details=json_decimal(decision),
            )
            if write.outcome != WriteOutcome.CONFIRMED:
                store.enter_protected_pause(f"AMBIGUOUS_CANCEL:{oid}")
                return result
            runtime._pending_cancel_requested.add(oid)
            result["canceledForReprice"].append(oid)
        if result["canceledForReprice"]:
            return result
        journal(
            {
                "action": "SUBMIT" if result["plan"] else "WAIT",
                "reason": result.get("empty_reason"),
                "plan": result["plan"],
            }
        )
        result["submitted"] = runtime._submit_plan(result, account["wallet"], version)
        for chain, submitted in zip(pending, result["submitted"]):
            store.bind_reprice_replacement_chain(
                chain["chain_key"], submitted["offerId"], submitted["effective_rate"], now
            )
        return result
    except OSError as exc:
        result["blockReasons"] = [f"决策日志无法持久化: {exc}"]
        store.enter_protected_pause("ADAPTIVE_JOURNAL_FAILED")
        return result
