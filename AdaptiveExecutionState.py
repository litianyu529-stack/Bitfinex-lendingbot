"""Durable, currency-local passive offer leases. No exchange operations."""

import json
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
        self.value = {"schemaVersion": 1, "currency": store.currency, "leases": {}, "quarantines": {}}
        if self.path.exists():
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                checksum = value.pop("checksum")
                if digest(value) != checksum or value["currency"] != store.currency or value["schemaVersion"] != 1:
                    raise ValueError("invalid lease checksum or currency")
                if not isinstance(value["leases"], dict) or not isinstance(value["quarantines"], dict):
                    raise ValueError("invalid lease state")
                self.value = value
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise OSError("被动挂单状态缺失字段或损坏，禁止新增动作") from exc

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
        if self.value["leases"]:
            return next(iter(self.value["leases"].values()))
        chain_start = int((continuation or {}).get("chainStartMs", now_ms))
        end = min(now_ms + policy.passive_wait_minutes * 60000, chain_start + MAX_PASSIVE_MS)
        lease = {
            "quoteKey": key,
            "offerId": None,
            "intentId": None,
            "startedAtMs": now_ms,
            "chainStartMs": chain_start,
            "expiresAtMs": end,
            "status": "PENDING",
            "baseSliceKey": f"{version}:{plan_hash}:{row['pool']}:{row['layer']}:{row['slice_index']}",
            "strategyVersion": version,
            "evidenceWatermark": list(row.get("evidenceWatermark") or []),
            "order": json_decimal(row),
        }
        self.value["leases"][key] = lease
        self.persist()
        return lease

    def bind(self, submitted):
        key = quote_key(self.store.currency, submitted)
        lease = self.value["leases"].get(key)
        if lease:
            lease.update(offerId=int(submitted["offerId"]), intentId=submitted["intentId"], status="ACTIVE")
            self.persist()

    def reconcile(self, offers, now_ms):
        active_ids = {int(r.get("offer_id", r.get("id"))) for r in offers}
        changed = False
        for key, lease in list(self.value["leases"].items()):
            if lease.get("offerId") is None:
                intents = [
                    r
                    for r in self.store.intents()
                    if str(r["slice_key"]).startswith(lease["baseSliceKey"] + ":")
                    or str(r["slice_key"]) == lease["baseSliceKey"]
                ]
                if not intents:
                    # No durable intent means the exchange call never began.
                    del self.value["leases"][key]
                    changed = True
                    continue
                intent = max(intents, key=lambda r: r["id"])
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
                self.value["quarantines"][key] = {
                    "closedAtMs": now_ms,
                    "evidenceWatermark": lease["evidenceWatermark"],
                    "chainStartMs": lease["chainStartMs"],
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
        self.value["leases"].pop(lease["quoteKey"], None)
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
        closed = sorted(self.value["quarantines"].values(), key=lambda q: q["closedAtMs"], reverse=True)
        if not closed:
            return None
        last = closed[0]
        if now_ms < last["chainStartMs"] + MAX_PASSIVE_MS:
            return {"chainStartMs": last["chainStartMs"]}
        if set(tokens) - set(last["evidenceWatermark"]):
            return None
        raise ValueError("被动等待已满6小时；需结束之后的新需求证据")
