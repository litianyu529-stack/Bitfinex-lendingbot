from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Sequence


class WriteOutcome(str, Enum):
    CONFIRMED = "CONFIRMED"
    DEFINITE_REJECT = "DEFINITE_REJECT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class WriteResult:
    outcome: WriteOutcome
    response: Any = None
    error: str = ""
    category: str = ""
    retryable: bool = False


@dataclass(frozen=True)
class AccountSnapshot:
    total: Decimal
    wallet: Decimal
    offers: Decimal
    credits: Decimal
    exposure_by_pool: Mapping[str, Decimal] = field(default_factory=dict)
    exposure_by_layer: Mapping[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketSnapshot:
    as_of_ms: int
    source: str
    book: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    trades: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    wallets: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    offers: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    credits: Sequence[Mapping[str, Any]] = field(default_factory=tuple)


@dataclass(frozen=True)
class StrategyPlan:
    version: str
    plan_hash: str
    orders: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    variant: str = "baseline"
