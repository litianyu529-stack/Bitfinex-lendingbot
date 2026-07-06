import hashlib
import hmac
import json
import time
from decimal import Decimal
from urllib import error, parse, request


class BitfinexApiError(Exception):
    pass


class Bitfinex:
    AUTH_BASE_URL = "https://api.bitfinex.com"
    PUBLIC_BASE_URL = "https://api-pub.bitfinex.com"

    def __init__(self, api_key="", api_secret="", timeout=30):
        self.api_key = api_key or ""
        self.api_secret = api_secret or ""
        self.timeout = timeout
        self._last_nonce = 0

    @staticmethod
    def is_placeholder_credential(value):
        if not value:
            return True
        lowered = value.strip().lower()
        return lowered in {"yourapikey", "yoursecret", "yourbitfinexapikey", "yourbitfinexsecret"}

    def has_credentials(self):
        return not (
            self.is_placeholder_credential(self.api_key)
            or self.is_placeholder_credential(self.api_secret)
        )

    def _nonce(self):
        nonce = int(time.time() * 1000000)
        if nonce <= self._last_nonce:
            nonce = self._last_nonce + 1
        self._last_nonce = nonce
        return str(nonce)

    @staticmethod
    def _body(payload):
        return json.dumps(payload or {}, separators=(",", ":"))

    def _headers(self, path, nonce, body):
        signature_payload = f"/api/{path}{nonce}{body}"
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            signature_payload.encode("utf-8"),
            hashlib.sha384,
        ).hexdigest()
        return {
            "bfx-nonce": nonce,
            "bfx-apikey": self.api_key,
            "bfx-signature": signature,
            "content-type": "application/json",
        }

    def auth_headers_for_test(self, path, nonce, payload=None):
        body = self._body(payload)
        return self._headers(path, nonce, body)

    def _request_json(self, url, method="GET", data=None, headers=None):
        data_bytes = None
        if data is not None:
            data_bytes = data.encode("utf-8")
        request_headers = {
            "User-Agent": "MikaLendingBot/0.4 Python",
            "Accept": "application/json",
        }
        request_headers.update(headers or {})
        req = request.Request(url, data=data_bytes, headers=request_headers, method=method)
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                content = json.loads(raw)
            except ValueError:
                content = raw
            raise BitfinexApiError(f"HTTP {exc.code} {exc.reason}: {content}") from exc
        except error.URLError as exc:
            raise BitfinexApiError(str(exc)) from exc

        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise BitfinexApiError(f"Invalid JSON response: {raw[:200]}") from exc
        if isinstance(payload, list) and payload and payload[0] == "error":
            raise BitfinexApiError(str(payload))
        return payload

    def _auth_post(self, path, payload=None):
        if not self.has_credentials():
            raise BitfinexApiError("Bitfinex API key/secret are not configured")
        body = self._body(payload)
        nonce = self._nonce()
        headers = self._headers(path, nonce, body)
        return self._request_json(
            f"{self.AUTH_BASE_URL}/{path}",
            method="POST",
            data=body,
            headers=headers,
        )

    def _public_get(self, path, query=None):
        url = f"{self.PUBLIC_BASE_URL}/{path}"
        if query:
            url += "?" + parse.urlencode(query)
        return self._request_json(url, method="GET")

    def wallets(self):
        return self._auth_post("v2/auth/r/wallets")

    def active_funding_offers(self, symbol=None):
        path = "v2/auth/r/funding/offers"
        if symbol:
            path += f"/{symbol}"
        return self._auth_post(path)

    def active_funding_loans(self, symbol=None):
        path = "v2/auth/r/funding/loans"
        if symbol:
            path += f"/{symbol}"
        return self._auth_post(path)

    def active_funding_credits(self, symbol=None):
        path = "v2/auth/r/funding/credits"
        if symbol:
            path += f"/{symbol}"
        return self._auth_post(path)

    def submit_funding_offer(self, symbol, amount, rate, period):
        return self._auth_post(
            "v2/auth/w/funding/offer/submit",
            {
                "type": "LIMIT",
                "symbol": symbol,
                "amount": str(amount),
                "rate": str(rate),
                "period": int(period),
                "flags": 0,
            },
        )

    def cancel_all_funding_offers(self, currency):
        return self._auth_post(
            "v2/auth/w/funding/offer/cancel/all",
            {"currency": currency},
        )

    def cancel_funding_offer(self, offer_id):
        return self._auth_post(
            "v2/auth/w/funding/offer/cancel",
            {"id": int(offer_id)},
        )

    def ledgers(self, currency=None, start=None, end=None, limit=None, wallet=None, category=None):
        path = "v2/auth/r/ledgers"
        if currency:
            path += f"/{currency.strip().upper()}"
        path += "/hist"
        payload = {}
        for key, value in (
            ("start", start),
            ("end", end),
            ("limit", limit),
            ("wallet", wallet),
            ("category", category),
        ):
            if value is not None:
                payload[key] = value
        return self._auth_post(path, payload)

    def transfer_between_wallets(self, from_wallet, to_wallet, currency, amount):
        return self._auth_post(
            "v2/auth/w/transfer",
            {
                "from": from_wallet,
                "to": to_wallet,
                "currency": currency,
                "currency_to": currency,
                "amount": str(amount),
            },
        )

    def funding_book(self, symbol, length=250):
        return self._public_get(f"v2/book/{symbol}/P0", {"len": int(length)})

    def ticker(self, symbol):
        return self._public_get(f"v2/ticker/{symbol}")


def currency_to_symbol(currency):
    currency = currency.strip().upper()
    if currency.startswith("F"):
        return currency
    return f"f{currency}"


def symbol_to_currency(symbol):
    symbol = symbol.strip().upper()
    if symbol.startswith("F"):
        return symbol[1:]
    return symbol


def decimal_from_api(value):
    return Decimal(str(value))
