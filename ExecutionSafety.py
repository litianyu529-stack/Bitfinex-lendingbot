"""Durable exact-payload rejection barriers; never retry an uncertain exchange write."""

import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from FileUtils import atomic_write_text

CONTRACT_VERSION = "funding-payload-v42"
PERMANENT_CATEGORIES = {"PARAMETER_INVALID", "WRITE_PARAMETER_INVALID", "WRITE_REJECTED", "BITFINEX_HTTP"}


def _checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def execution_fingerprint(order):
    def number(name, default=0):
        try:
            value = Decimal(str(order.get(name, default)))
        except InvalidOperation:
            return str(order.get(name, default))
        return str(value.normalize()) if value.is_finite() else str(value)

    if order.get("action") == "CANCEL":
        value = {
            "contract": CONTRACT_VERSION,
            "currency": order["currency"],
            "action": "CANCEL",
            "offerId": int(order["offerId"]),
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()
    value = {
        "contract": CONTRACT_VERSION,
        "currency": order["currency"],
        "strategyVersion": str(order.get("strategy_version") or ""),
        "type": str(order["offer_type"]).upper(),
        "amount": number("amount"),
        "rate": number("submitted_rate"),
        "period": number("period"),
        "flags": number("flags"),
    }
    if value["type"] == "FRR":
        value.update(type="FRRDELTAVAR", rate="0")
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def _path(store):
    return Path(store.path).resolve().parent / "research" / store.currency / "execution-rejections.json"


def _load(store):
    try:
        value = json.loads(_path(store).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"currency": store.currency, "entries": {}}
    except (OSError, ValueError) as exc:
        raise OSError("Cannot verify durable execution rejection barriers") from exc
    if not isinstance(value, dict):
        raise OSError("Invalid durable execution rejection barriers")
    checksum = value.pop("checksum", None)
    if checksum != _checksum(value):
        raise OSError("Invalid durable execution rejection checksum")
    if (
        not isinstance(value, dict)
        or value.get("currency") != store.currency
        or not isinstance(value.get("entries"), dict)
    ):
        raise OSError("Invalid durable execution rejection barriers")
    if any(not isinstance(row, dict) or not isinstance(row.get("category"), str) for row in value["entries"].values()):
        raise OSError("Invalid durable execution rejection entries")
    return value


def blocked(store, order):
    return _load(store)["entries"].get(execution_fingerprint(order))


def record_rejection(store, order, result, now_ms):
    if result.retryable or result.category not in PERMANENT_CATEGORIES:
        return False
    value = _load(store)
    value["entries"][execution_fingerprint(order)] = {
        "category": result.category,
        "atMs": int(now_ms),
        "strategyVersion": str(order.get("strategy_version") or ""),
        "contract": CONTRACT_VERSION,
    }
    atomic_write_text(str(_path(store)), json.dumps({**value, "checksum": _checksum(value)}, sort_keys=True))
    return True
