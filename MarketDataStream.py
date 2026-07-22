import hashlib
import hmac
import json
import threading
import time
from collections import deque
from decimal import Decimal

from bitfinex import symbol_to_currency


D = Decimal
PUBLIC_WS_URL = "wss://api-pub.bitfinex.com/ws/2"
AUTH_WS_URL = "wss://api.bitfinex.com/ws/2"

try:
    from websockets.sync.client import connect as websocket_connect
except ImportError:  # pragma: no cover - exercised by dependency preflight
    websocket_connect = None


def websocket_dependency_available():
    return websocket_connect is not None


def _now_ms():
    return int(time.time() * 1000)


def _as_decimal(value):
    return D(str(value))


class BitfinexMarketDataHub:
    def __init__(
        self,
        api_key="",
        api_secret="",
        symbol="fUSD",
        store=None,
        fallback_seconds=300,
        rest_stale_seconds=60,
        max_trades=20_000,
    ):
        self.api_key = api_key or ""
        self.api_secret = api_secret or ""
        self.symbol = symbol
        self.store = store
        self.fallback_ms = int(fallback_seconds) * 1000
        self.rest_stale_ms = int(rest_stale_seconds) * 1000
        self.max_trades = int(max_trades)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads = []
        self._public_channels = {}
        self._book = {}
        self._trades = deque(maxlen=self.max_trades)
        self._wallets = {}
        self._offers = {}
        self._credits = {}
        self._funding_trades = deque(maxlen=5000)
        self._public_connected = False
        self._auth_connected = False
        self._public_last_message_ms = None
        self._auth_last_message_ms = None
        self._public_disconnected_since_ms = None
        self._auth_disconnected_since_ms = None
        self._rest_last_sync_ms = None
        self._last_error = ""

    def start(self):
        if websocket_connect is None:
            raise RuntimeError("websockets dependency is not installed")
        if self._threads:
            return
        self._stop.clear()
        targets = [("bitfinex-public-ws", self._run_public)]
        if self.api_key and self.api_secret:
            targets.append(("bitfinex-auth-ws", self._run_auth))
        for name, target in targets:
            thread = threading.Thread(name=name, target=target, daemon=True)
            self._threads.append(thread)
            thread.start()

    def stop(self, timeout=3):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads = []

    def _set_connected(self, channel, connected, error=""):
        now = _now_ms()
        with self._lock:
            if channel == "public":
                if connected:
                    self._public_disconnected_since_ms = None
                elif self._public_disconnected_since_ms is None:
                    self._public_disconnected_since_ms = now
                self._public_connected = connected
            else:
                if connected:
                    self._auth_disconnected_since_ms = None
                elif self._auth_disconnected_since_ms is None:
                    self._auth_disconnected_since_ms = now
                self._auth_connected = connected
            if error:
                self._last_error = str(error)[:500]

    def _run_forever(self, channel, runner):
        delay = 1
        while not self._stop.is_set():
            try:
                runner()
                delay = 1
            except Exception as exc:
                self._set_connected(channel, False, exc)
                if self._stop.wait(delay):
                    break
                delay = min(30, delay * 2)

    def _run_public(self):
        self._run_forever("public", self._public_session)

    def _public_session(self):
        with websocket_connect(PUBLIC_WS_URL, open_timeout=15, close_timeout=3, ping_interval=20, ping_timeout=20) as socket:
            socket.send(json.dumps({"event": "subscribe", "channel": "book", "symbol": self.symbol, "prec": "P0", "freq": "F0", "len": "250"}))
            socket.send(json.dumps({"event": "subscribe", "channel": "trades", "symbol": self.symbol}))
            self._set_connected("public", True)
            while not self._stop.is_set():
                message = socket.recv(timeout=30)
                self.handle_public_message(message)

    def _run_auth(self):
        self._run_forever("auth", self._auth_session)

    def _auth_session(self):
        nonce = str(int(time.time() * 1_000_000))
        payload = "AUTH" + nonce
        signature = hmac.new(self.api_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha384).hexdigest()
        auth = {
            "event": "auth",
            "apiKey": self.api_key,
            "authSig": signature,
            "authNonce": nonce,
            "authPayload": payload,
            "filter": [f"funding-{self.symbol}", "wallet"],
        }
        with websocket_connect(AUTH_WS_URL, open_timeout=15, close_timeout=3, ping_interval=20, ping_timeout=20) as socket:
            socket.send(json.dumps(auth))
            while not self._stop.is_set():
                message = socket.recv(timeout=30)
                self.handle_auth_message(message)

    def handle_public_message(self, raw):
        message = json.loads(raw) if isinstance(raw, str) else raw
        now = _now_ms()
        with self._lock:
            self._public_last_message_ms = now
            self._public_connected = True
            self._public_disconnected_since_ms = None
            if isinstance(message, dict):
                if message.get("event") == "subscribed":
                    self._public_channels[int(message["chanId"])] = message.get("channel")
                elif message.get("event") == "error":
                    self._last_error = str(message.get("msg") or message)[:500]
                return
            if not isinstance(message, list) or len(message) < 2 or message[1] == "hb":
                return
            channel = self._public_channels.get(int(message[0]))
            if channel == "book":
                entries = message[1] if isinstance(message[1], list) and message[1] and isinstance(message[1][0], list) else [message[1]]
                for entry in entries:
                    self._apply_book_entry(entry)
            elif channel == "trades":
                if len(message) >= 3 and message[1] in {"fte", "ftu", "te", "tu"}:
                    entries = [message[2]]
                else:
                    entries = message[1] if isinstance(message[1], list) and message[1] and isinstance(message[1][0], list) else [message[1]]
                for entry in entries:
                    self._apply_public_trade(entry)

    def _apply_book_entry(self, entry):
        if not isinstance(entry, (list, tuple)) or len(entry) < 4:
            return
        rate, period, count, amount = _as_decimal(entry[0]), int(entry[1]), int(entry[2]), _as_decimal(entry[3])
        side = "offer" if amount > 0 else "bid"
        key = (format(rate, "f"), period, side)
        if count == 0:
            self._book.pop(key, None)
        else:
            self._book[key] = {"rate": rate, "period": period, "count": count, "amount": amount}

    def _apply_public_trade(self, entry):
        if not isinstance(entry, (list, tuple)) or len(entry) < 5:
            return
        trade = {
            "id": str(entry[0]),
            "mts": int(entry[1]),
            "amount": abs(_as_decimal(entry[2])),
            "rate": _as_decimal(entry[3]),
            "period": int(entry[4]),
        }
        self._merge_trades([trade])
        if self.store is not None:
            self.store.upsert_market_trades([trade])

    def _merge_trades(self, trades):
        """Merge public funding trades by id and retain the newest bounded set."""
        merged = {str(row["id"]): dict(row) for row in self._trades if row.get("id") is not None}
        for row in trades or []:
            normalized = dict(row)
            normalized["id"] = str(normalized["id"])
            normalized["amount"] = abs(D(normalized["amount"]))
            normalized["rate"] = D(normalized["rate"])
            normalized["mts"] = int(normalized["mts"])
            merged[normalized["id"]] = normalized
        newest = sorted(merged.values(), key=lambda row: (int(row["mts"]), str(row["id"])))
        self._trades = deque(newest[-self.max_trades :], maxlen=self.max_trades)

    def _merge_funding_trades(self, trades):
        """Merge authenticated funding trades by id without double-counting updates."""
        limit = self._funding_trades.maxlen or 5000
        merged = {str(row["id"]): dict(row) for row in self._funding_trades if row.get("id") is not None}
        for row in trades or []:
            normalized = dict(row)
            normalized["id"] = int(normalized["id"])
            merged[str(normalized["id"])] = normalized
        newest = sorted(merged.values(), key=lambda row: (int(row.get("mts") or 0), str(row["id"])))
        self._funding_trades = deque(newest[-limit:], maxlen=limit)

    def handle_auth_message(self, raw):
        message = json.loads(raw) if isinstance(raw, str) else raw
        now = _now_ms()
        with self._lock:
            self._auth_last_message_ms = now
            if isinstance(message, dict):
                if message.get("event") == "auth":
                    if message.get("status") == "OK":
                        self._auth_connected = True
                        self._auth_disconnected_since_ms = None
                    else:
                        self._auth_connected = False
                        self._last_error = f"auth failed: {message.get('code', 'unknown')}"
                return
            if not isinstance(message, list) or len(message) < 2 or message[0] != 0:
                return
            event = message[1]
            payload = message[2] if len(message) > 2 else None
            if event == "hb":
                return
            if event == "fos":
                self._offers = {int(row[0]): self._parse_offer(row) for row in payload or [] if len(row) >= 16}
            elif event in {"fon", "fou"} and payload:
                offer = self._parse_offer(payload)
                self._offers[offer["id"]] = offer
            elif event == "foc" and payload:
                self._offers.pop(int(payload[0]), None)
            elif event == "fcs":
                self._credits = {int(row[0]): self._parse_credit(row) for row in payload or [] if len(row) >= 13}
            elif event in {"fcn", "fcu"} and payload:
                credit = self._parse_credit(payload)
                self._credits[credit["id"]] = credit
            elif event == "fcc" and payload:
                self._credits.pop(int(payload[0]), None)
            elif event == "ws":
                self._wallets = {self._wallet_key(row): self._parse_wallet(row) for row in payload or [] if len(row) >= 3}
            elif event == "wu" and payload:
                self._wallets[self._wallet_key(payload)] = self._parse_wallet(payload)
            elif event in {"fte", "ftu"} and payload:
                self._merge_funding_trades([self._parse_funding_trade(payload)])

    @staticmethod
    def _wallet_key(row):
        return f"{str(row[0]).lower()}:{str(row[1]).upper()}"

    @staticmethod
    def _parse_wallet(row):
        return {
            "wallet_type": str(row[0]).lower(),
            "currency": str(row[1]).upper(),
            "balance": _as_decimal(row[2]),
            "unsettled_interest": _as_decimal(row[3] or 0) if len(row) > 3 else D("0"),
            "available": _as_decimal(row[4] if len(row) > 4 and row[4] is not None else row[2]),
        }

    @staticmethod
    def _parse_offer(row):
        return {
            "id": int(row[0]),
            "currency": symbol_to_currency(str(row[1])).upper(),
            "mts_created": int(row[2] or 0),
            "mts_updated": int(row[3] or 0),
            "amount": abs(_as_decimal(row[4])),
            "amount_original": abs(_as_decimal(row[5])),
            "offer_type": str(row[6]),
            "flags": int(row[9] or 0),
            "status": str(row[10]),
            "rate": _as_decimal(row[14]),
            "period": int(row[15]),
            "hidden": bool(row[17]) if len(row) > 17 else bool(int(row[9] or 0) & 64),
            "rate_real": _as_decimal(row[20]) if len(row) > 20 and row[20] is not None else None,
        }

    @staticmethod
    def _parse_credit(row):
        return {
            "id": int(row[0]),
            "currency": symbol_to_currency(str(row[1])).upper(),
            "amount": abs(_as_decimal(row[5])),
            "status": str(row[7]),
            "rate_type": str(row[8]) if len(row) > 8 and row[8] is not None else None,
            "rate": _as_decimal(row[11]),
            "period": int(row[12]),
            "mts_opening": int(row[13] or 0) if len(row) > 13 else 0,
            "mts_updated": int(row[4] or 0),
            "hidden": bool(row[16]) if len(row) > 16 else False,
            "rate_real": _as_decimal(row[19]) if len(row) > 19 and row[19] is not None else None,
        }

    @staticmethod
    def _parse_funding_trade(row):
        return {
            "id": int(row[0]),
            "currency": symbol_to_currency(str(row[1])).upper(),
            "mts": int(row[2]),
            "offer_id": int(row[3]),
            "amount": abs(_as_decimal(row[4])),
            "rate": _as_decimal(row[5]),
            "period": int(row[6]),
            "maker": row[7] if len(row) > 7 else None,
        }

    def apply_rest_snapshot(self, book=None, trades=None, wallets=None, offers=None, credits=None, synced_at_ms=None):
        now = int(synced_at_ms if synced_at_ms is not None else _now_ms())
        with self._lock:
            if book is not None:
                self._book = {}
                for entry in book:
                    if isinstance(entry, dict):
                        rate, period, amount = D(entry["rate"]), int(entry["period"]), D(entry["amount"])
                        key = (format(rate, "f"), period, "offer" if amount > 0 else "bid")
                        self._book[key] = {"rate": rate, "period": period, "count": int(entry.get("count", 1)), "amount": amount}
                    else:
                        self._apply_book_entry(entry)
            self._merge_trades(trades)
            if wallets is not None:
                self._wallets = {self._wallet_key(row): self._parse_wallet(row) for row in wallets}
            if offers is not None:
                self._offers = {
                    int(row["id"] if isinstance(row, dict) else row[0]): dict(row) if isinstance(row, dict) else self._parse_offer(row)
                    for row in offers
                }
            if credits is not None:
                self._credits = {
                    int(row["id"] if isinstance(row, dict) else row[0]): dict(row) if isinstance(row, dict) else self._parse_credit(row)
                    for row in credits
                }
            self._rest_last_sync_ms = now
        if self.store is not None and trades:
            self.store.upsert_market_trades(trades)

    def snapshot(self, now_ms=None):
        now = int(now_ms if now_ms is not None else _now_ms())
        with self._lock:
            public_age = None if self._public_last_message_ms is None else max(0, now - self._public_last_message_ms)
            auth_age = None if self._auth_last_message_ms is None else max(0, now - self._auth_last_message_ms)
            rest_age = None if self._rest_last_sync_ms is None else max(0, now - self._rest_last_sync_ms)
            ws_healthy = self._public_connected and (not self.api_key or self._auth_connected)
            rest_fresh = rest_age is not None and rest_age <= self.rest_stale_ms
            if ws_healthy:
                source = "WEBSOCKET"
            elif rest_fresh:
                source = "REST_FALLBACK"
            else:
                source = "STALE"
            disconnected = [value for value in (self._public_disconnected_since_ms, self._auth_disconnected_since_ms if self.api_key else None) if value is not None]
            disconnected_for = 0 if not disconnected else now - min(disconnected)
            return {
                "as_of": now,
                "source": source,
                "publicConnected": self._public_connected,
                "authConnected": self._auth_connected if self.api_key else None,
                "publicAgeMs": public_age,
                "authAgeMs": auth_age,
                "restAgeMs": rest_age,
                "disconnectedForMs": disconnected_for,
                "safeRequired": bool(disconnected_for >= self.fallback_ms or (not ws_healthy and not rest_fresh)),
                "lastError": self._last_error,
                "book": [dict(row) for row in self._book.values()],
                "trades": [dict(row) for row in self._trades],
                "wallets": [dict(row) for row in self._wallets.values()],
                "offers": [dict(row) for row in self._offers.values()],
                "credits": [dict(row) for row in self._credits.values()],
                "fundingTrades": [dict(row) for row in self._funding_trades],
            }
