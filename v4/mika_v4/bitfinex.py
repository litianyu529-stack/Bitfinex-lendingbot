from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from decimal import Decimal
from typing import Any, Callable

from .domain import AccountSnapshot, CreditSnapshot, OfferSnapshot, WriteOutcome, WriteResult


D = Decimal


class BitfinexError(RuntimeError):
    pass


class SlidingWindowLimiter:
    def __init__(self, limit: int = 45, window_seconds: float = 60.0) -> None:
        self.limit = int(limit)
        self.window_seconds = float(window_seconds)
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.window_seconds:
                    self._calls.popleft()
                if len(self._calls) < self.limit:
                    self._calls.append(now)
                    return
                wait = self.window_seconds - (now - self._calls[0]) + 0.01
            time.sleep(max(0.01, wait))


def _decimal(value: Any) -> D:
    return D(str(value or 0))


def _symbol_currency(symbol: Any) -> str:
    value = str(symbol or "").upper()
    return value[1:] if value.startswith("F") else value


def _parse_offer(row: list[Any]) -> OfferSnapshot:
    return OfferSnapshot(
        offer_id=int(row[0]),
        currency=_symbol_currency(row[1]),
        mts_created=int(row[2] or row[3] or 0),
        amount=abs(_decimal(row[4])),
        amount_original=abs(_decimal(row[5])),
        offer_type=str(row[6] or "LIMIT").upper(),
        flags=int(row[9] or 0),
        hidden=bool(row[17]) if len(row) > 17 else bool(int(row[9] or 0) & 64),
        status=str(row[10] or ""),
        rate=_decimal(row[14]),
        period=int(row[15] or 0),
    )


def _parse_credit(row: list[Any], state: str) -> CreditSnapshot:
    return CreditSnapshot(
        credit_id=int(row[0]),
        currency=_symbol_currency(row[1]),
        mts_opening=int(row[3] or row[4] or 0),
        amount=abs(_decimal(row[5])),
        funding_state=state,
        rate=_decimal(row[11]),
        period=int(row[12] or row[13] or 0),
    )


class BitfinexClient:
    """Small, V4-local Bitfinex v2 client with conservative write classification."""

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        *,
        auth_limit_per_minute: int = 45,
        timeout_seconds: float = 20.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout_seconds = timeout_seconds
        self.limiter = SlidingWindowLimiter(auth_limit_per_minute)
        self._opener = opener or urllib.request.urlopen
        self._nonce_lock = threading.RLock()
        self._last_nonce = 0

    def _nonce(self) -> str:
        with self._nonce_lock:
            value = int(time.time_ns() // 1_000)
            self._last_nonce = max(value, self._last_nonce + 1)
            return str(self._last_nonce)

    def _decode(self, response: Any) -> Any:
        raw = response.read()
        return json.loads(raw.decode("utf-8"))

    def public(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = urllib.parse.urlencode(params or {})
        url = f"https://api-pub.bitfinex.com/v2/{path.lstrip('/')}"
        if query:
            url += f"?{query}"
        request = urllib.request.Request(url, headers={"User-Agent": "MikaLendingBot-V4/0.4.0"})
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                return self._decode(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise BitfinexError(f"public request failed: {path}: {exc}") from exc

    def auth(self, path: str, body: dict[str, Any] | None = None) -> Any:
        if not self.api_key or not self.api_secret:
            raise BitfinexError("Bitfinex API credentials are not configured")
        self.limiter.acquire()
        # Bitfinex nonces are scoped to an API key. Serialize nonce creation
        # through response receipt so a later request cannot overtake it.
        with self._nonce_lock:
            return self._auth_locked(path, body)

    def _auth_locked(self, path: str, body: dict[str, Any] | None) -> Any:
        normalized = path.lstrip("/")
        encoded = json.dumps(body or {}, separators=(",", ":"))
        nonce = self._nonce()
        signature_payload = f"/api/{normalized}{nonce}{encoded}".encode()
        signature = hmac.new(self.api_secret.encode(), signature_payload, hashlib.sha384).hexdigest()
        request = urllib.request.Request(
            f"https://api.bitfinex.com/{normalized}",
            data=encoded.encode(),
            headers={
                "Content-Type": "application/json",
                "bfx-nonce": nonce,
                "bfx-apikey": self.api_key,
                "bfx-signature": signature,
                "User-Agent": "MikaLendingBot-V4/0.4.0",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                return self._decode(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise BitfinexError(f"authenticated request rejected ({exc.code}): {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise BitfinexError(f"authenticated request outcome unknown: {path}: {exc}") from exc

    def funding_book(self, symbol: str = "fUSD", length: int = 100) -> list[list[Any]]:
        result = self.public(f"book/{symbol}/P0", {"len": length})
        return result if isinstance(result, list) else []

    def funding_trades(
        self,
        symbol: str = "fUSD",
        start: int | None = None,
        limit: int = 10_000,
        sort: int = -1,
    ) -> list[list[Any]]:
        params: dict[str, Any] = {"limit": limit, "sort": int(sort)}
        if start is not None:
            params["start"] = int(start)
        result = self.public(f"trades/{symbol}/hist", params)
        return result if isinstance(result, list) else []

    def funding_stats(
        self,
        symbol: str = "fUSD",
        start: int | None = None,
        end: int | None = None,
        limit: int = 10_000,
    ) -> list[list[Any]]:
        params: dict[str, Any] = {"limit": min(250, limit), "sort": 1}
        if start is not None:
            params["start"] = int(start)
        if end is not None:
            params["end"] = int(end)
        result = self.public(f"funding/stats/{symbol}/hist", params)
        return result if isinstance(result, list) else []

    def account_snapshot(self, currency: str = "USD") -> AccountSnapshot:
        wallets = self.auth("v2/auth/r/wallets")
        offer_rows = self.auth("v2/auth/r/funding/offers")
        credit_rows = self.auth("v2/auth/r/funding/credits")
        loan_rows = self.auth("v2/auth/r/funding/loans")
        available = D("0")
        total = D("0")
        wallet_found = False
        availability_known = True
        for row in wallets if isinstance(wallets, list) else []:
            if len(row) >= 5 and str(row[0]).lower() == "funding" and str(row[1]).upper() == currency:
                wallet_found = True
                total += _decimal(row[2])
                if row[4] is None:
                    availability_known = False
                else:
                    available += _decimal(row[4])
        offers = tuple(
            _parse_offer(row)
            for row in offer_rows
            if isinstance(offer_rows, list)
            if isinstance(row, list) and len(row) > 15 and _symbol_currency(row[1]) == currency
        )
        credits = tuple(
            _parse_credit(row, "credit")
            for row in credit_rows
            if isinstance(credit_rows, list)
            if isinstance(row, list) and len(row) > 12 and _symbol_currency(row[1]) == currency
        )
        loans = tuple(
            _parse_credit(row, "loan")
            for row in loan_rows
            if isinstance(loan_rows, list)
            if isinstance(row, list) and len(row) > 12 and _symbol_currency(row[1]) == currency
        )
        return AccountSnapshot(
            as_of_ms=int(time.time() * 1000),
            wallet_available=max(D("0"), available),
            wallet_total=max(D("0"), total),
            offers=offers,
            credits=credits,
            loans=loans,
            authoritative=wallet_found and availability_known,
        )

    @staticmethod
    def _write_result(call: Callable[[], Any]) -> WriteResult:
        try:
            response = call()
        except BitfinexError as exc:
            text = str(exc)
            outcome = WriteOutcome.UNKNOWN if "outcome unknown" in text else WriteOutcome.DEFINITE_REJECT
            return WriteResult(outcome=outcome, error=text)
        if isinstance(response, list) and len(response) >= 8:
            status = str(response[6] or "").upper()
            if status == "SUCCESS":
                return WriteResult(WriteOutcome.CONFIRMED, response=response)
            return WriteResult(WriteOutcome.DEFINITE_REJECT, response=response, error=str(response[7]))
        return WriteResult(WriteOutcome.UNKNOWN, response=response, error="unrecognized Bitfinex write response")

    def submit_offer(self, amount: D, rate: D, period: int, symbol: str = "fUSD") -> WriteResult:
        body = {
            "type": "LIMIT",
            "symbol": symbol,
            "amount": format(D(amount), "f"),
            "rate": format(D(rate), "f"),
            "period": int(period),
            "flags": 0,
        }
        return self._write_result(lambda: self.auth("v2/auth/w/funding/offer/submit", body))

    def cancel_offer(self, offer_id: int) -> WriteResult:
        return self._write_result(lambda: self.auth("v2/auth/w/funding/offer/cancel", {"id": int(offer_id)}))


def submitted_offer_id(response: Any) -> int | None:
    if not isinstance(response, list) or len(response) < 5:
        return None
    payload = response[4]
    candidates = payload if isinstance(payload, list) and payload and isinstance(payload[0], list) else [payload]
    for row in candidates:
        if isinstance(row, list) and row and isinstance(row[0], (int, float, str)):
            try:
                return int(row[0])
            except (TypeError, ValueError):
                pass
    return None
