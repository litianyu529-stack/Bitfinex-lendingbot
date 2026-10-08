"""Canonical funding currencies and per-cycle amount sizing.

Context variables keep pure strategy sizing local to one runtime or HTTP request.
No account balances or permissions are shared through this context.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal, ROUND_CEILING


SUPPORTED_CURRENCIES = ("USD", "USDT")
_minimum_amount = ContextVar("funding_minimum_amount", default=Decimal("150"))


def normalize_currency(value):
    currency = str(value).strip().upper()
    if currency.startswith("F"):
        currency = currency[1:]
    return "USDT" if currency in {"UST", "USDT"} else currency


def require_currency(value):
    currency = normalize_currency(value)
    if currency not in SUPPORTED_CURRENCIES:
        raise ValueError("currency must be USD or USDT")
    return currency


def wallet_currency(value):
    currency = require_currency(value)
    return "UST" if currency == "USDT" else currency


def funding_symbol(value):
    return "f" + wallet_currency(value)


def funding_minimum():
    return _minimum_amount.get()


def usdt_minimum(bid):
    price = Decimal(str(bid))
    if not price.is_finite() or price <= 0:
        raise ValueError("USDT/USD bid must be positive and finite")
    return max(Decimal("150"), Decimal("150") / price).quantize(Decimal("0.00000001"), rounding=ROUND_CEILING)


@contextmanager
def funding_sizing(minimum):
    value = Decimal(minimum)
    if not value.is_finite() or value < 150:
        raise ValueError("minimum funding amount must be at least 150")
    token = _minimum_amount.set(value)
    try:
        yield
    finally:
        _minimum_amount.reset(token)
