"""Adaptive integration: read-only planning and journal-before-write execution."""

from decimal import Decimal as D

from ResearchV4 import ModelRepository
from StrategyV3 import json_decimal
from StrategyV4 import adjustment, build_plan, digest
from AdaptiveEngines import REGISTRY, engine_module


def context(store, policy, book, trades, now_ms, stats=()):
    repo = ModelRepository(store.path, policy.currency)
    try:
        model = repo.load(policy.model_id, now_ms)
        expected = REGISTRY[policy.strategy_engine][0]
        if model["algorithm"] != expected:
            model = None
    except (ValueError, KeyError):
        model = None
    valid = [
        r
        for r in stats
        if 0 <= now_ms - int(r["mts"]) <= policy.rest_stale_seconds * 1000
        and D(str(r["frr_daily_rate"])).is_finite()
        and D(str(r["frr_daily_rate"])) > 0
    ]
    latest = max(valid, key=lambda r: int(r["mts"]), default=None)
    from OperationalV41 import status as operational_status

    result = {
        "model": model,
        "book": book,
        "trades": trades,
        "now_ms": now_ms,
        "frr": D(str(latest["frr_daily_rate"])) if latest else None,
        "researchValidated": eligible(store, model),
        **operational_status(repo, model, now_ms),
    }
    if policy.strategy_engine == "adaptive_net_yield_v3":
        from AdaptiveExecutionState import PassiveState

        try:
            state = PassiveState(store)
            result.update(
                passiveLeaseStates=list(state.value["leases"].values()), passiveQuarantines=state.value["quarantines"]
            )
        except OSError as exc:
            result["passiveStateError"] = str(exc)
    return result


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
    if policy.strategy_engine in ("adaptive_net_yield_v2", "adaptive_net_yield_v3"):
        if policy.strategy_engine == "adaptive_net_yield_v3":
            account = {
                **account,
                "passiveLeaseStates": account.get("passiveLeaseStates", ctx.get("passiveLeaseStates", [])),
                "passiveQuarantines": account.get("passiveQuarantines", ctx.get("passiveQuarantines", {})),
            }
        result = engine_module(policy.strategy_engine).build_plan(
            account,
            policy,
            ctx.get("model"),
            ctx.get("book", []),
            ctx.get("trades", []),
            ctx.get("now_ms", 0),
            version,
            ctx.get("frr"),
        )
        result.update({k: ctx.get(k) for k in ("operationalReady", "operationalReportHash", "operationalBlockReasons")})
        model = ctx.get("model") or {}
        result["dataBasis"] = model.get("dataBasis", {})
        result["eligibleForLiveCandidate"] = ctx.get("researchValidated", False)
        result["typeConfidence"] = {
            kind: "CALIBRATED" if count >= 20 else "LOW"
            for kind, count in model.get("typeObservationCounts", {}).items()
        }
        if ctx.get("passiveStateError"):
            result.update(
                plan=[], planned_amount=D(0), idle_amount=D(account["wallet"]), blockReasons=[ctx["passiveStateError"]]
            )
        return result
    return build_plan(
        account, policy, ctx.get("model"), ctx.get("book", []), ctx.get("trades", []), ctx.get("now_ms", 0), version
    )


def recovery_qualified(store, policy, snapshot, now_ms, stats=()):
    """Freshness may recover; invalid frozen models/reports cannot authorize it."""
    ctx = context(store, policy, snapshot.get("book", []), snapshot.get("trades", []), now_ms, stats)
    if policy.strategy_engine == "adaptive_net_yield_v1":
        return eligible(store, ctx["model"])
    return bool(
        ctx.get("operationalReady")
        and not ctx.get("passiveStateError")
        and ctx.get("frr") is not None
        and ctx["model"]
        and len(ctx["model"].get("frrDays", [])) >= 20
    )


def _fresh_before_write(runtime, snapshot, fallback_now):
    """Check the wall clock after valuation, not its captured start time."""
    now = int(runtime.clock() * 1000) if hasattr(runtime, "clock") else fallback_now
    ctx = context(
        runtime.store,
        runtime.policy,
        snapshot.get("book", []),
        snapshot.get("trades", []),
        now,
        getattr(runtime, "_stats", ()),
    )
    if not ctx.get("operationalReady"):
        runtime.store.enter_protected_pause("ADAPTIVE_MODEL_NOT_QUALIFIED")
        return False
    if ctx.get("passiveStateError"):
        runtime.store.enter_protected_pause("ADAPTIVE_JOURNAL_FAILED")
        return False
    if ctx["frr"] is None:
        runtime.store.enter_protected_pause("ADAPTIVE_FRR_STALE")
        return False
    delta = now - int(snapshot.get("as_of", fallback_now))
    source_age = snapshot.get("publicAgeMs") if snapshot.get("source") == "WEBSOCKET" else snapshot.get("restAgeMs")
    if (
        snapshot.get("safeRequired")
        or delta < 0
        or (source_age is not None and int(source_age) + delta > runtime.policy.rest_stale_seconds * 1000)
        or (source_age is None and snapshot.get("bookMts") is None)
    ):
        runtime.store.enter_protected_pause("MARKET_DATA_STALE")
        return False
    if (
        snapshot.get("bookMts") is not None
        and not 0 <= now - int(snapshot["bookMts"]) <= runtime.policy.rest_stale_seconds * 1000
    ):
        runtime.store.enter_protected_pause("MARKET_DATA_STALE")
        return False
    return True


def _target_quote(policy, decision, amount):
    from StrategyV41 import display_type

    kind = decision.get("targetType", "LIMIT")
    return dict(
        currency=policy.currency,
        display_type=display_type({"display_type": kind}),
        offer_type={
            "LIMIT": "LIMIT",
            "FRR": "FRRDELTAVAR",
            "FRR_DELTA_FIXED": "FRRDELTAFIX",
            "FRR_DELTA_VARIABLE": "FRRDELTAVAR",
        }[kind],
        submitted_rate=D(str(decision.get("targetSubmittedRate", decision.get("targetRate", 0)))),
        effective_rate=D(str(decision.get("targetRate", 0))),
        rate=D(str(decision.get("targetRate", 0))),
        period=int(decision.get("targetPeriod", 2)),
        amount=D(str(amount)),
        flags=0,
    )


def _confirmation_key(policy, decision, offer):
    from ExecutionSafety import execution_fingerprint

    return execution_fingerprint(_target_quote(policy, decision, offer["amount"]))


def _remaining_chain_amount(store, chain, target, now_ms):
    """Late fills belong to loans, never to replacement principal."""
    with store.read_connection() as connection:
        source = connection.execute(
            "SELECT amount_original FROM offers WHERE offer_id=?", (chain["pending_source_offer_id"],)
        ).fetchone()
        rows = connection.execute(
            "SELECT amount,mts FROM funding_trades WHERE currency=? AND offer_id=? AND mts<=?",
            (store.currency, chain["pending_source_offer_id"], now_ms),
        ).fetchall()
    if source and source["amount_original"] is not None:
        return max(
            D(0),
            min(
                D(chain["source_amount"]), D(source["amount_original"]) - sum((abs(D(r["amount"])) for r in rows), D(0))
            ),
        )
    return max(
        D(0), D(chain["source_amount"]) - sum((abs(D(r["amount"])) for r in rows if r["mts"] > target["atMs"]), D(0))
    )


def _cycle_v42(runtime, snapshot, account, now, resume_barrier):
    """Currency-local leases and executable targets survive cancellation/restart."""
    from AdaptiveExecutionState import PassiveState, evidence, quote_key
    from DomainTypes import WriteOutcome
    from RuntimeV3 import _write_result
    from StrategyV41 import display_type
    from StrategyV4 import CandidateMarket, gross_floor

    policy, store = runtime.policy, runtime.store
    version = (store.strategy("ACTIVE") or {}).get("version_id", "4")
    ctx = context(store, policy, snapshot["book"], snapshot["trades"], now, getattr(runtime, "_stats", ()))
    core = engine_module(policy.strategy_engine)
    result = {"submitted": [], "canceledForReprice": [], "decisions": [], "plan": [], "engine": policy.strategy_engine}
    try:
        leases = PassiveState(store)
        # Reconcile only against the authoritative account offers already read by Worker.
        if not snapshot.get("safeRequired") and (
            snapshot.get("source") != "WEBSOCKET" or snapshot.get("accountSnapshotsReady")
        ):
            leases.reconcile(snapshot["offers"], now)
        account = leases.augment(account)
        result.update(plan(account, policy, {"adaptiveContext": ctx}, version))
        if store.runtime()["mode"] != "LIVE" or resume_barrier:
            result["recoveryResumeBarrier"] = bool(resume_barrier)
            return result
        if not ctx["operationalReady"]:
            result["blockReasons"] = ctx["operationalBlockReasons"]
            store.enter_protected_pause("ADAPTIVE_MODEL_NOT_QUALIFIED")
            return result
        if result.get("blockReasons"):
            store.enter_protected_pause("ADAPTIVE_FRR_STALE" if ctx["frr"] is None else "ADAPTIVE_DATA_UNAVAILABLE")
            return result
        pending = store.pending_reprices(version)
        active_ids = {int(o.get("offer_id", o.get("id"))) for o in snapshot["offers"]}
        for chain in pending:
            oid = int(chain["pending_source_offer_id"])
            if oid not in active_ids:
                continue
            target = leases.replacement(chain["chain_key"]) or {}
            with store.read_connection() as connection:
                reconciled = connection.execute(
                    "SELECT 1 FROM ownership_events WHERE offer_id=? AND event_type='CANCEL_RECONCILED_PRESENT' "
                    "AND created_at_ms>=? LIMIT 1",
                    (oid, target.get("atMs", now)),
                ).fetchone()
            if target.get("cancelPhase") == "PREPARED" or reconciled:
                source = next(o for o in snapshot["offers"] if int(o.get("offer_id", o.get("id"))) == oid)
                store.bind_reprice_replacement_chain(
                    chain["chain_key"], oid, source.get("rate_real") or source["rate"], now
                )
                leases.complete(chain["chain_key"])
                lease = leases.lease_for(oid)
                if lease:
                    lease["status"] = "ACTIVE"
                    leases.persist()
                result["decisions"].append({"action": "KEEP", "reason": "CANCEL_NOT_EFFECTIVE", "offerId": oid})
                return result
            if target.get("cancelPhase") in ("SENDING", "UNKNOWN"):
                store.enter_protected_pause(f"AMBIGUOUS_CANCEL:{oid}")
            result["blockReasons"] = ["等待账户确认撤单，禁止提交替代单"]
            return result
        signature = digest({"account": account, "book": snapshot["book"], "trades": snapshot["trades"]})
        last = getattr(runtime, "_adaptive_at", 0)
        if now - last < (60000 if signature == getattr(runtime, "_adaptive_signature", None) else 10000):
            result["decisions"] = getattr(runtime, "_adaptive_last_decisions", []) or [
                {"action": "WAIT", "reason": "EVALUATION_INTERVAL"}
            ]
            return result
        runtime._adaptive_at, runtime._adaptive_signature = now, signature
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
                    "operationalReportHash": ctx["operationalReportHash"],
                    "strategyVersion": version,
                    "accountDigest": digest(account),
                    "account": account,
                    "marketAtMs": snapshot.get("bookMts", now),
                    "planHash": result["plan_hash"],
                    "action": action,
                }
            )

        market = CandidateMarket(snapshot["trades"], now)
        import time

        market.deadline = time.monotonic() + core.VALUATION_BUDGET_SECONDS
        cap_excess = max(D(0), result["existing_exposure"] - result["funding_cap"])
        long_excess = max(
            D(0),
            sum((D(v) for p, v in account.get("exposureByPeriod", {}).items() if int(p) >= policy.long_from_days), D(0))
            - D(account["total"]) * policy.long_max_share / 100,
        )
        floating_excess = max(
            D(0),
            D(account.get("existingExposure", {}).get("variable", 0))
            - D(account["total"]) * policy.variable_max_share / 100,
        )
        for original in snapshot["offers"]:
            offer = dict(original)
            oid = int(offer.get("offer_id", offer.get("id")))
            lease = leases.lease_for(oid)
            if lease:
                offer["passiveLease"] = lease
            candidates = result["candidates"]
            # The second observation revalues the previously executable quote,
            # even when FRR or the current best quote changes slightly.
            locked = confirmations.get(oid)
            if locked:
                quote = dict(locked["quote"])
                quote["rate"] = (
                    quote["submitted_rate"]
                    if quote["display_type"] == "LIMIT"
                    else ctx["frr"] + quote["submitted_rate"]
                )
                candidates = [quote] if quote["rate"] >= gross_floor(policy, quote["period"]) else []
            decision = core.adjustment(
                policy, ctx["model"], offer, candidates, snapshot["trades"], snapshot["book"], now, ctx["frr"], market
            )
            if decision.get("blockReasons"):
                result["blockReasons"] = decision["blockReasons"]
                return result
            if offer.get("managed") and (
                cap_excess > 0
                or long_excess > 0
                and int(offer["period"]) >= policy.long_from_days
                or floating_excess > 0
                and offer["offer_type"] == "FRRDELTAVAR"
            ):
                decision = {"action": "CANCEL", "hard": True, "reason": "CAP_EXCEEDED"}
                cap_excess = max(D(0), cap_excess - D(offer["amount"]))
                long_excess = (
                    max(D(0), long_excess - D(offer["amount"]))
                    if int(offer["period"]) >= policy.long_from_days
                    else long_excess
                )
                floating_excess = (
                    max(D(0), floating_excess - D(offer["amount"]))
                    if offer["offer_type"] == "FRRDELTAVAR"
                    else floating_excess
                )
            decision["offerId"] = oid
            chain = store.reprice_chain_for_offer(oid)
            decision["cumulativeWaitMinutes"] = max(
                0,
                (
                    now
                    - int(
                        (lease or {}).get("chainStartMs")
                        or (chain or {}).get("started_at_ms")
                        or offer.get("mts_created")
                        or now
                    )
                )
                / 60000,
            )
            if lease:
                decision["leaseExpiresAtMs"] = lease["expiresAtMs"]
                if decision["reason"] == "PASSIVE_TO_NORMAL":
                    leases.promote(lease)
                    decision["leaseExpiresAtMs"] = None
                if decision["reason"] == "PASSIVE_LEASE_RENEW":
                    compatible = [
                        r
                        for r in snapshot["trades"]
                        if int(r["period"]) <= int(offer["period"])
                        and D(str(r["rate"])) >= D(str(offer.get("rate_real") or offer["rate"]))
                        and core.compatibility(display_type(offer), r) != "INCOMPATIBLE"
                    ]
                    tokens = evidence(compatible, [], now, lease["startedAtMs"])
                    if leases.renew(lease, now, tokens, policy):
                        decision["leaseExpiresAtMs"] = lease["expiresAtMs"]
                    else:
                        decision.update(action="CANCEL", hard=True, reason="LEASE_EXPIRED")
            result["decisions"].append(decision)
            if decision["action"] != "CANCEL":
                confirmations.pop(oid, None)
                continue
            if not decision.get("hard"):
                key = _confirmation_key(policy, decision, offer)
                count = locked["count"] + 1 if locked and locked["key"] == key else 1
                quote = _target_quote(policy, decision, offer["amount"])
                match = next(
                    (
                        c
                        for c in result["candidates"]
                        if quote_key(policy.currency, c) == quote_key(policy.currency, quote)
                    ),
                    {},
                )
                quote.update(
                    {
                        k: match[k]
                        for k in ("passive", "leaseMinutes", "evidenceWatermark", "demandCompatibility")
                        if k in match
                    }
                )
                confirmations[oid] = {"key": key, "count": count, "quote": quote}
                if (
                    count < 2
                    or store.reprice_count_since(now - 3600000) >= min(12, policy.max_reprices_per_hour)
                    or (
                        chain
                        and now - int(chain.get("last_reprice_at_ms") or 0)
                        < max(2, policy.reprice_cooldown_minutes) * 60000
                    )
                ):
                    if not lease or now < lease["expiresAtMs"]:
                        continue
                    decision.update(hard=True, reason="LEASE_EXPIRED")
                    quote = None
            else:
                quote = None
            chain = chain or store.ensure_reprice_chain(offer, version, now)
            if chain is None or chain.get("pending_action"):
                continue
            if not _fresh_before_write(runtime, snapshot, now):
                result["blockReasons"] = ["模型或行情已过期；等待重新核对后恢复"]
                return result
            journal(decision)
            leases.target(chain["chain_key"], quote, now, (lease or {}).get("chainStartMs", now))
            if lease:
                leases.closing(lease)
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
            leases.cancel_phase(chain["chain_key"], "SENDING")
            write = _write_result(runtime.client, "cancel_funding_offer_result", "cancel_funding_offer", oid)
            if write.outcome == WriteOutcome.DEFINITE_REJECT:
                store.bind_reprice_replacement_chain(
                    chain["chain_key"], oid, offer.get("rate_real") or offer["rate"], now
                )
                leases.complete(chain["chain_key"])
                if lease:
                    lease["status"] = "ACTIVE"
                    leases.persist()
                result["blockReasons"] = ["撤单被明确拒绝；保留旧单"]
                return result
            store.record_ownership_event(
                "CANCEL_CONFIRMED" if write.outcome == WriteOutcome.CONFIRMED else "CANCEL_UNKNOWN",
                offer_id=oid,
                details=json_decimal(decision),
            )
            if write.outcome != WriteOutcome.CONFIRMED:
                leases.cancel_phase(chain["chain_key"], "UNKNOWN")
                store.enter_protected_pause(f"AMBIGUOUS_CANCEL:{oid}")
                return result
            leases.cancel_phase(chain["chain_key"], "CONFIRMED")
            runtime._pending_cancel_requested.add(oid)
            result["canceledForReprice"].append(oid)
        runtime._adaptive_last_decisions = result["decisions"]
        if result["canceledForReprice"]:
            return result
        if pending:
            # Safe lease expiry returns to cash. Never bind an unrelated later
            # submission to the expired order's waiting chain.
            for chain in pending:
                target = leases.replacement(chain["chain_key"])
                if target is None:
                    result["blockReasons"] = ["撤单目标记录缺失，禁止猜测替代单"]
                    return result
                if target["order"] is None:
                    with store.transaction(immediate=True) as connection:
                        connection.execute(
                            "UPDATE reprice_chains SET status='CLOSED',pending_action=NULL,"
                            "pending_target_rate=NULL,pending_source_offer_id=NULL WHERE chain_key=?",
                            (chain["chain_key"],),
                        )
                    leases.complete(chain["chain_key"])
                    continue
                key = quote_key(policy.currency, target["order"])
                # A confirmed intent may have survived a crash between exchange
                # submission and chain binding. Bind it before considering cash.
                confirmed = [
                    i
                    for i in store.intents()
                    if i.get("exchange_offer_id")
                    and i["strategy_version"] == version
                    and i["slice_key"] == target.get("expectedSliceKey")
                    and int(i["exchange_offer_id"]) != int(chain["pending_source_offer_id"])
                    and quote_key(policy.currency, i) == key
                ]
                if len(confirmed) == 1:
                    row = confirmed[0]
                    store.bind_reprice_replacement_chain(
                        chain["chain_key"], row["exchange_offer_id"], row["effective_rate"], now
                    )
                    leases.complete(chain["chain_key"])
                    result["decisions"].append({"action": "KEEP", "reason": "RESTART_REPLACEMENT_BOUND"})
                    return result
                if len(confirmed) > 1:
                    result["blockReasons"] = ["存在多个相同替代单凭证，需要核对"]
                    store.enter_protected_pause("ADAPTIVE_DATA_UNAVAILABLE")
                    return result
                result["plan"] = [r for r in result["plan"] if quote_key(policy.currency, r) == key]
                if not result["plan"]:
                    with store.transaction(immediate=True) as connection:
                        connection.execute(
                            "UPDATE reprice_chains SET status='CLOSED',pending_action=NULL,"
                            "pending_target_rate=NULL,pending_source_offer_id=NULL WHERE chain_key=?",
                            (chain["chain_key"],),
                        )
                    leases.complete(chain["chain_key"])
                    result["decisions"].append(
                        {
                            "action": "WAIT",
                            "reason": "REPLACEMENT_RETURN_CASH",
                            "offerId": chain["pending_source_offer_id"],
                        }
                    )
                    return result
                row = result["plan"][0]
                row["amount"] = min(
                    D(row["amount"]), _remaining_chain_amount(store, chain, target, now), D(account["wallet"])
                )
                from Currency import funding_minimum

                if row["amount"] < funding_minimum():
                    leases.target(chain["chain_key"], None, now, target["chainStartMs"])
                    result["plan"] = []
                    return result
                result["plan"] = [row]
                leases.submission(chain["chain_key"], row, result["plan_hash"], version)
                break
        for row in result["plan"]:
            if row.get("passive"):
                closed = sorted(leases.value["quarantines"].values(), key=lambda q: q["closedAtMs"], reverse=True)
                after = closed[0]["closedAtMs"] if closed else 0
                compatible = [
                    r
                    for r in snapshot["trades"]
                    if int(r["period"]) <= row["period"]
                    and D(str(r["rate"])) >= row["effective_rate"]
                    and core.compatibility(row["display_type"], r) != "INCOMPATIBLE"
                ]
                continuation = leases.continuation(row, now, evidence(compatible, [], now, after))
                leases.prepare(row, result["plan_hash"], version, now, policy, continuation)
        if not _fresh_before_write(runtime, snapshot, now):
            result["blockReasons"] = ["决策后行情已过期，未执行新动作"]
            return result
        journal(
            {
                "action": "SUBMIT" if result["plan"] else "WAIT",
                "plan": result["plan"],
                "reason": result.get("empty_reason"),
            }
        )
        result["submitted"] = runtime._submit_plan(result, account["wallet"], version)
        for submitted in result["submitted"]:
            leases.bind(submitted)
            for chain in pending:
                target = leases.replacement(chain["chain_key"])
                if (
                    target
                    and target["order"]
                    and quote_key(policy.currency, target["order"]) == quote_key(policy.currency, submitted)
                ):
                    store.bind_reprice_replacement_chain(
                        chain["chain_key"], submitted["offerId"], submitted["effective_rate"], now
                    )
                    leases.complete(chain["chain_key"])
                    break
        return result
    except ValueError as exc:
        result["blockReasons"] = [str(exc)]
        return result
    except OSError as exc:
        result["blockReasons"] = [f"执行状态或决策日志无法持久化: {exc}"]
        store.enter_protected_pause("ADAPTIVE_JOURNAL_FAILED")
        return result


def cycle(runtime, snapshot, account, signals, now, resume_barrier):
    """Cancel confirmation is an authoritative REST barrier, including after restart."""
    if runtime.policy.strategy_engine == "adaptive_net_yield_v3":
        return _cycle_v42(runtime, snapshot, account, now, resume_barrier)
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
    runnable = (
        ctx["operationalReady"] if policy.strategy_engine == "adaptive_net_yield_v2" else eligible(store, ctx["model"])
    )
    if not runnable:
        result["blockReasons"] = (
            ctx["operationalBlockReasons"]
            if policy.strategy_engine == "adaptive_net_yield_v2"
            else ["模型尚未通过收益研究验收；保持研究状态"]
        )
        store.enter_protected_pause("ADAPTIVE_MODEL_NOT_QUALIFIED")
        return result
    if result.get("blockReasons"):
        store.enter_protected_pause(
            "ADAPTIVE_FRR_STALE"
            if policy.strategy_engine == "adaptive_net_yield_v2" and ctx["frr"] is None
            else "ADAPTIVE_DATA_UNAVAILABLE"
        )
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
                "operationalReportHash": ctx.get("operationalReportHash"),
                "dataBasis": ctx["model"].get("dataBasis", {}),
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
            if policy.strategy_engine == "adaptive_net_yield_v2" and not _fresh_before_write(runtime, snapshot, now):
                result["blockReasons"] = ["决策后行情已过期，未执行撤单"]
                return result
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
        if policy.strategy_engine == "adaptive_net_yield_v2" and not _fresh_before_write(runtime, snapshot, now):
            result["blockReasons"] = ["决策后行情已过期，未执行新动作"]
            return result
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
