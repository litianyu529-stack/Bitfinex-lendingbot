"""Fail-closed recovery classification shared by the V3.2 runtime and supervisor."""

from __future__ import annotations

import http.client
import socket
import sqlite3
from dataclasses import dataclass
from urllib import error

from bitfinex import BitfinexAmbiguousWriteError, BitfinexApiError


RECOVERY_BACKOFF_SECONDS = (30, 60, 120, 300)
RECOVERY_REQUIRED_SNAPSHOTS = 2
RECOVERY_MINIMUM_GAP_MS = 30_000
WORKER_HEARTBEAT_TIMEOUT_MS = 300_000


@dataclass(frozen=True)
class RecoveryDecision:
    category: str
    retryable: bool
    manual_required: bool = False


def recovery_delay_seconds(attempts: int) -> int:
    index = max(0, min(int(attempts), len(RECOVERY_BACKOFF_SECONDS) - 1))
    return RECOVERY_BACKOFF_SECONDS[index]


def classify_runtime_error(exc: BaseException) -> RecoveryDecision:
    """Classify only errors that are safe to retry without a trading write."""

    if isinstance(exc, BitfinexAmbiguousWriteError):
        return RecoveryDecision("AMBIGUOUS_WRITE", False, True)
    if isinstance(exc, BitfinexApiError):
        return RecoveryDecision(
            getattr(exc, "category", "BITFINEX_API"),
            bool(getattr(exc, "retryable", False)),
            bool(getattr(exc, "manual_required", False)),
        )
    if isinstance(
        exc,
        (
            error.URLError,
            TimeoutError,
            socket.timeout,
            ConnectionError,
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
        ),
    ):
        return RecoveryDecision("NETWORK_TRANSPORT", True)
    if isinstance(exc, sqlite3.OperationalError):
        text = str(exc).lower()
        if "locked" in text or "busy" in text:
            return RecoveryDecision("DATABASE_BUSY", True)
        return RecoveryDecision("DATABASE_ERROR", False, True)
    if isinstance(exc, (TypeError, AssertionError, ValueError)):
        return RecoveryDecision("PROGRAM_ERROR", False, True)
    return RecoveryDecision("UNEXPECTED_RUNTIME_ERROR", False, True)


def recovery_category_for_reason(reason: str) -> str | None:
    value = str(reason or "")
    if value == "MARKET_DATA_STALE":
        return "MARKET_DATA"
    if value in {"ACCOUNT_AVAILABLE_BALANCE_UNKNOWN", "ACCOUNT_RECONCILIATION_MISMATCH"}:
        return "ACCOUNT_DATA"
    if value == "AMBIGUOUS_WALLET_TRANSFER":
        return "AMBIGUOUS_WRITE"
    if value.startswith("AMBIGUOUS_SUBMIT:") or value.startswith("AMBIGUOUS_CANCEL:"):
        return "AMBIGUOUS_WRITE"
    return None
