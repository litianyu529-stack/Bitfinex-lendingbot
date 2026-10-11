"""Durable, currency-local passive offer leases. No exchange operations."""

import json
from decimal import Decimal
from pathlib import Path

from FileUtils import atomic_write_text
from StrategyV3 import json_decimal
from StrategyV4 import digest

MAX_PASSIVE_MS = 360 * 60000


def quote_key(currency, row):
    from decimal import Decimal

    return digest(
        {
            "currency": currency,
            "type": row.get("offer_type", "LIMIT"),
            "rate": str(Decimal(str(row.get("submitted_rate", row.get("rate", 0)))).normalize()),
            "period": int(row["period"]),
        }
    )


def evidence(trades, book, now_ms, after_ms=0):
    """Only deduplicable observations count; refresh time alone never counts."""
    result = set()
    for row in trades:
        identifier = row.get("id", row.get("trade_id"))
        mts = int(row.get("mts", 0))
        if identifier is not None and after_ms < mts <= now_ms:
            result.add(f"trade:{identifier}")
    for row in book:
        identifier = row.get("offer_id", row.get("id"))
        # Book entries without first-seen evidence are not independent renewal evidence.
        seen = int(row.get("firstSeenMs", 0))
        if identifier is not None and after_ms < seen <= now_ms and float(row.get("amount", 0)) < 0:
            result.add(f"book:{identifier}:{row['period']}:{row['rate']}:{row['amount']}")
    return result


class PassiveState:
    def __init__(self, store):
        self.store = store
        self.path = Path(store.path).resolve().parent / "research" / store.currency / "passive-state.json"
        self.value = {"schemaVersion": 2, "currency": store.currency, "leases": {}, "quarantines": {}}
        if self.path.exists():
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                checksum = value.pop("checksum")
                if (
                    digest(value) != checksum
                    or value["currency"] != store.currency
                    or value["schemaVersion"] not in (1, 2)
                ):
                    raise ValueError("invalid lease checksum or currency")
                if not isinstance(value["leases"], dict) or not isinstance(value["quarantines"], dict):
                    raise ValueError("invalid lease state")
                self.value = value
                if value["schemaVersion"] == 1:
                    self._migrate()
                self._validate_leases()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise OSError("被动挂单状态缺失字段或损坏，禁止新增动作") from exc

    def _lease_id(self, slice_key):
        return digest({"currency": self.store.currency, "sliceKey": str(slice_key)})

    def _migrate(self):
        """Read old quote-keyed leases without changing their waiting clocks."""
        migrated, intents = {}, self.store.intents()
        for lease in self.value["leases"].values():
            base = lease["baseSliceKey"]
            if lease.get("intentId") is not None:
                matches = [r for r in intents if int(r["id"]) == int(lease["intentId"])]
            elif lease.get("offerId") is not None:
                matches = [r for r in intents if r.get("exchange_offer_id") == int(lease["offerId"])]
            else:
                matches = [r for r in intents if r["slice_key"] == base or r["slice_key"].startswith(base + ":r")]
                matches = sorted(matches, key=lambda r: int(r["id"]))[-1:]
            if len(matches) > 1:
                raise ValueError("ambiguous legacy lease intent")
            if matches:
                intent = matches[0]
                if not self._matches_intent(lease, intent):
                    raise ValueError("legacy lease intent does not match")
                expected = intent["slice_key"]
            else:
                expected = base
            identifier = self._lease_id(expected)
            if identifier in migrated:
                raise ValueError("duplicate legacy lease intent")
            lease.update(leaseId=identifier, expectedSliceKey=expected)
            migrated[identifier] = lease
        # Persist schema 2 on the next normal mutation. Reading a preflight
        # never rewrites a live file or resets its original deadlines.
        self.value.update(schemaVersion=2, leases=migrated)

    def _validate_leases(self):
        offer_ids = set()
        for identifier, lease in self.value["leases"].items():
            if (
                not isinstance(lease, dict)
                or lease["leaseId"] != identifier
                or self._lease_id(lease["expectedSliceKey"]) != identifier
                or quote_key(self.store.currency, lease["order"]) != lease["quoteKey"]
                or not 0 <= int(lease["chainStartMs"]) <= int(lease["startedAtMs"])
                or not int(lease["startedAtMs"])
                <= int(lease["expiresAtMs"])
                <= int(lease["chainStartMs"]) + MAX_PASSIVE_MS
                or not isinstance(lease["evidenceWatermark"], list)
            ):
                raise ValueError("invalid lease identity or deadline")
            offer_id = lease.get("offerId")
            if offer_id is not None:
                if int(offer_id) in offer_ids:
                    raise ValueError("duplicate lease offer id")
                offer_ids.add(int(offer_id))

    def _matches_intent(self, lease, intent):
        return (
            str(intent.get("currency", "")).upper() == self.store.currency
            and str(intent["strategy_version"]) == str(lease["strategyVersion"])
            and quote_key(self.store.currency, intent) == lease["quoteKey"]
        )

    def persist(self):
        body = json_decimal(self.value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(str(self.path), json.dumps({**body, "checksum": digest(body)}, ensure_ascii=False))

    def augment(self, account):
        return {
            **account,
            "passiveLeaseStates": list(self.value["leases"].values()),
            "passiveQuarantines": self.value["quarantines"],
        }

    def prepare(self, row, plan_hash, version, now_ms, policy, continuation=None):
        key = quote_key(self.store.currency, row)
        base_slice = f"{version}:{plan_hash}:{row['pool']}:{row['layer']}:{row['slice_index']}"
        expected_slice = self.store.replenishment_slice_key(base_slice)
        identifier = self._lease_id(expected_slice)
        existing = self.value["leases"].get(identifier)
        if existing is not None:
            if existing["quoteKey"] != key or Decimal(str(existing["order"]["amount"])) != Decimal(str(row["amount"])):
                raise OSError("同一被动挂单意图的报价或本金发生变化，禁止覆盖租约")
            return existing
        chain_start = int((continuation or {}).get("chainStartMs", now_ms))
        end = min(now_ms + policy.passive_wait_minutes * 60000, chain_start + MAX_PASSIVE_MS)
        if chain_start > now_ms or end <= now_ms:
            raise ValueError("被动等待已到期或起点无效；禁止重新开始等待")
        lease = {
            "leaseId": identifier,
            "quoteKey": key,
            "offerId": None,
            "intentId": None,
            "startedAtMs": now_ms,
            "chainStartMs": chain_start,
            "expiresAtMs": end,
            "status": "PENDING",
            "baseSliceKey": base_slice,
            "expectedSliceKey": expected_slice,
            "strategyVersion": version,
            "evidenceWatermark": list(row.get("evidenceWatermark") or []),
            "order": json_decimal(row),
        }
        self.value["leases"][identifier] = lease
        self.persist()
        return lease

    def bind(self, submitted):
        intent = next((r for r in self.store.intents() if int(r["id"]) == int(submitted["intentId"])), None)
        lease = self.value["leases"].get(self._lease_id(intent["slice_key"])) if intent else None
        if lease is None:
            if submitted.get("passive"):
                raise OSError("被动挂单缺少精确意图租约，禁止猜测绑定")
            return
        offer_id = int(submitted["offerId"])
        if (
            not self._matches_intent(lease, intent)
            or quote_key(self.store.currency, submitted) != lease["quoteKey"]
            or int(intent.get("exchange_offer_id") or 0) != offer_id
            or lease.get("offerId") not in (None, offer_id)
        ):
            raise OSError("被动挂单意图或订单编号不匹配，禁止覆盖租约")
        lease.update(offerId=offer_id, intentId=int(intent["id"]), status="ACTIVE")
        self.persist()

    def reconcile(self, offers, now_ms):
        active_ids = {int(r.get("offer_id", r.get("id"))) for r in offers}
        changed = False
        for key, lease in list(self.value["leases"].items()):
            if lease.get("offerId") is None:
                intents = [r for r in self.store.intents() if str(r["slice_key"]) == lease["expectedSliceKey"]]
                if not intents:
                    # No durable intent means the exchange call never began.
                    del self.value["leases"][key]
                    changed = True
                    continue
                if len(intents) != 1 or not self._matches_intent(lease, intents[0]):
                    raise OSError("被动挂单对应多个或不匹配意图，禁止猜测")
                intent = intents[0]
                changed = changed or lease.get("intentId") != intent["id"]
                lease["intentId"] = intent["id"]
                if intent.get("exchange_offer_id"):
                    lease.update(offerId=int(intent["exchange_offer_id"]), status="ACTIVE")
                    changed = True
                elif intent["state"] == "CLOSED":
                    del self.value["leases"][key]
                    changed = True
                    continue
            if lease.get("offerId") is not None and lease["offerId"] not in active_ids:
                if lease.get("status") != "CANCELLING":
                    # A just-confirmed write may not appear in the first REST
                    # view. Require two separated account absences.
                    first = lease.get("absentFirstMs")
                    if first is None:
                        lease["absentFirstMs"] = now_ms
                        changed = True
                        continue
                    if now_ms - first < 30000:
                        continue
                quote = lease["quoteKey"]
                previous = self.value["quarantines"].get(quote, {})
                self.value["quarantines"][quote] = {
                    "closedAtMs": max(now_ms, int(previous.get("closedAtMs", 0))),
                    "evidenceWatermark": sorted(
                        set(lease["evidenceWatermark"]) | set(previous.get("evidenceWatermark", []))
                    ),
                    "chainStartMs": min(
                        int(lease["chainStartMs"]), int(previous.get("chainStartMs", lease["chainStartMs"]))
                    ),
                }
                del self.value["leases"][key]
                changed = True
            elif lease.get("offerId") in active_ids and lease.pop("absentFirstMs", None) is not None:
                changed = True
        if changed:
            self.persist()

    def lease_for(self, offer_id):
        return next((r for r in self.value["leases"].values() if r.get("offerId") == int(offer_id)), None)

    def promote(self, lease):
        self.value["leases"].pop(lease["leaseId"], None)
        self.persist()

    def renew(self, lease, now_ms, tokens, policy):
        if now_ms >= lease["chainStartMs"] + MAX_PASSIVE_MS:
            return False
        fresh = set(tokens) - set(lease["evidenceWatermark"])
        if not fresh:
            return False
        lease["evidenceWatermark"] = sorted(set(lease["evidenceWatermark"]) | fresh)
        lease["expiresAtMs"] = min(now_ms + policy.passive_wait_minutes * 60000, lease["chainStartMs"] + MAX_PASSIVE_MS)
        self.persist()
        return True

    def closing(self, lease):
        lease["status"] = "CANCELLING"
        self.persist()

    def target(self, chain_key, row, now_ms, chain_start_ms):
        self.value.setdefault("replacements", {})[chain_key] = {
            "order": json_decimal(row) if row else None,
            "atMs": now_ms,
            "chainStartMs": chain_start_ms,
            "cancelPhase": "PREPARED",
        }
        self.persist()

    def replacement(self, chain_key):
        return self.value.get("replacements", {}).get(chain_key)

    def cancel_phase(self, chain_key, phase):
        self.value["replacements"][chain_key]["cancelPhase"] = phase
        self.persist()

    def submission(self, chain_key, row, plan_hash, version):
        base = f"{version}:{plan_hash}:{row['pool']}:{row['layer']}:{row['slice_index']}"
        self.value["replacements"][chain_key]["expectedSliceKey"] = self.store.replenishment_slice_key(base)
        self.persist()

    def complete(self, chain_key):
        self.value.get("replacements", {}).pop(chain_key, None)
        self.persist()

    def continuation(self, row, now_ms, tokens):
        """Repricing/cash do not erase accumulated passive-chain waiting."""
        closed = list(self.value["quarantines"].values())
        if not closed:
            return None
        # Returned principal is fungible in the funding wallet. Without a
        # separate cash-lot ledger, a younger simultaneous lease must not erase
        # the oldest possible waiting chain when that cash is merged/requoted.
        chain_start = min(int(q["chainStartMs"]) for q in closed)
        observed = set().union(*(set(q["evidenceWatermark"]) for q in closed))
        if now_ms < chain_start + MAX_PASSIVE_MS:
            return {"chainStartMs": chain_start}
        if set(tokens) - observed:
            return None
        raise ValueError("被动等待已满6小时；需结束之后的新需求证据")
