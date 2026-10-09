"""Local adaptive artifacts and resumable research jobs; no exchange write client."""

import json
import os
import sqlite3
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from FileUtils import atomic_write_text
from StrategyV3 import json_decimal, pool_for_period
from StrategyV4 import DAY, adaptive_template, condition, fit_model, validate_model, weighted_rate


class ModelRepository:
    def __init__(self, db_path, currency):
        from Currency import require_currency

        self.currency = require_currency(currency)
        self.directory = Path(db_path).resolve().parent / "research" / currency

    def save(self, model):
        validate_model(model, self.currency, model["trainedUntilMs"], allow_empty=True)
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            str(self.directory / (model["id"] + ".json")), json.dumps(json_decimal(model), sort_keys=True)
        )
        atomic_write_text(str(self.directory / "candidate.json"), json.dumps({"modelId": model["id"]}))
        return model["id"]

    def load(self, model_id, now_ms):
        if len(str(model_id)) != 64 or any(c not in "0123456789abcdef" for c in str(model_id)):
            raise ValueError("model ID must be a SHA256 digest")
        try:
            model = json.loads((self.directory / (model_id + ".json")).read_text(encoding="utf-8"))
            return validate_model(model, self.currency, now_ms)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("model unavailable or corrupted") from exc

    def candidate(self, now_ms):
        try:
            pointer = json.loads((self.directory / "candidate.json").read_text(encoding="utf-8"))
            return self.load(pointer["modelId"], now_ms)
        except (OSError, ValueError, KeyError):
            return None

    def journal(self, decision):
        self.directory.mkdir(parents=True, exist_ok=True)
        record = {"currency": self.currency, **decision}
        if record["currency"] != self.currency:
            raise ValueError("cross-currency journal")
        with (self.directory / "decisions.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(json_decimal(record), sort_keys=True, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


@contextmanager
def connect_readonly(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        yield connection
    finally:
        connection.close()


def training_data(path, currency, now_ms, cancelled=lambda: False, lookback_days=90):
    """Stream raw trades into day histograms, keeping memory bounded on multi-GB DBs."""
    with connect_readonly(path) as c:
        c.execute("BEGIN")
        tag = c.execute("SELECT value FROM schema_meta WHERE key='currency'").fetchone()
        if tag and tag[0] != currency:
            raise ValueError("database currency mismatch")
        hist = defaultdict(lambda: defaultdict(float))
        count, earliest, latest = 0, None, None
        for r in c.execute(
            "SELECT mts,rate,amount,period FROM market_trades WHERE mts>=? AND mts<=? ORDER BY mts",
            (now_ms - lookback_days * DAY, now_ms),
        ):
            if count % 10000 == 0 and cancelled():
                raise InterruptedError("research cancelled")
            mts = r["mts"]
            earliest = mts if earliest is None else earliest
            latest = mts
            count += 1
            hist[(mts // DAY, int(r["period"]))][str(r["rate"])] += abs(float(r["amount"]))
        daily = []
        for (day, period), levels in sorted(hist.items()):
            total, cumulative = sum(levels.values()), 0.0
            for rate, volume in sorted(levels.items(), key=lambda pair: float(pair[0])):
                cumulative += volume
                if cumulative >= total / 2:
                    daily.append(
                        {"mts": min(now_ms, (day + 1) * DAY - 1), "rate": rate, "amount": total, "period": period}
                    )
                    break
        trades = [
            dict(r) for r in c.execute("SELECT * FROM funding_trades WHERE currency=? AND mts<=?", (currency, now_ms))
        ]
        grouped = defaultdict(list)
        for row in trades:
            grouped[row["offer_id"]].append((row["mts"], abs(float(row["amount"]))))
        observations = []
        chain_ids = {
            r["offer_id"]: r["chain_key"]
            for r in c.execute(
                "SELECT offer_id,chain_key FROM reprice_events WHERE chain_key IS NOT NULL AND created_at_ms<=?",
                (now_ms,),
            )
        }
        for row in c.execute("SELECT * FROM reprice_chains WHERE updated_at_ms<=?", (now_ms,)):
            for oid in (row["current_offer_id"], row["pending_source_offer_id"]):
                if oid is not None:
                    chain_ids[oid] = row["chain_key"]
        # Duplicate exchange IDs are ambiguous; exclude rather than assigning arbitrary ancestry.
        intents = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM order_intents WHERE created_at_ms>=? AND created_at_ms<=?",
                (now_ms - lookback_days * DAY, now_ms),
            )
        ]
        occurrences = defaultdict(int)
        for row in intents:
            occurrences[row["exchange_offer_id"]] += 1
        for row in intents:
            oid = row["exchange_offer_id"]
            if oid is None or occurrences[oid] != 1:
                continue
            end = min(now_ms, row["updated_at_ms"]) if row["state"] == "CLOSED" else now_ms
            # Features are measured at quote creation, never using a later book.
            observed_condition = pool_for_period(row["period"])
            observed_book = c.execute(
                "SELECT book_json,mts FROM book_snapshots WHERE mts<=? ORDER BY mts DESC LIMIT 1",
                (row["created_at_ms"],),
            ).fetchone()
            if observed_book and row["created_at_ms"] - observed_book["mts"] <= 60000:
                from ExchangeModels import parse_book
                from decimal import Decimal as D

                raw = json.loads(observed_book["book_json"])
                quote_book = raw if not raw or isinstance(raw[0], dict) else parse_book(raw)
                recent = [
                    dict(r)
                    for r in c.execute(
                        "SELECT rate,sum(abs(CAST(amount AS REAL))) AS amount FROM market_trades "
                        "WHERE mts>=? AND mts<=? AND period<=? GROUP BY rate",
                        (row["created_at_ms"] - DAY, row["created_at_ms"], row["period"]),
                    )
                ]
                near = [
                    dict(r)
                    for r in c.execute(
                        "SELECT rate,sum(abs(CAST(amount AS REAL))) AS amount FROM market_trades "
                        "WHERE mts>=? AND mts<=? AND period<=? GROUP BY rate",
                        (row["created_at_ms"] - 3600000, row["created_at_ms"], row["period"]),
                    )
                ]
                ref = weighted_rate(recent)
                price = D(str(row["effective_rate"]))
                queue = sum(
                    abs(D(str(r["amount"])))
                    for r in quote_book
                    if 0 < D(str(r["amount"])) and 0 < D(str(r["rate"])) <= price
                )
                flow = sum(float(r["amount"]) for r in recent if D(str(r["rate"])) >= price) / 1440
                ratio = float(queue) / max(abs(float(row["amount"])), flow * 60, 1)
                recent_rate = weighted_rate(near) or ref
                trend = "up" if recent_rate > ref * D("1.05") else "down" if recent_rate < ref * D(".95") else "flat"
                observed_condition = condition(row["period"], price, ref, ratio, trend)
            observations.append(
                {
                    "currency": currency,
                    "offerId": oid,
                    "chainId": chain_ids.get(oid, f"intent:{row['id']}"),
                    "period": row["period"],
                    "start_ms": row["created_at_ms"],
                    "end_ms": end,
                    "amount": abs(float(row["amount"])),
                    "fills": grouped.get(oid, []),
                    "condition": observed_condition,
                }
            )
        holdings = [
            {
                "id": r["credit_id"],
                "currency": currency,
                "period": r["period"],
                "opened_ms": r["opened_at_ms"],
                "closed_ms": r["closed_at_ms"],
            }
            for r in c.execute("SELECT * FROM credit_closures WHERE currency=? AND opened_at_ms<=?", (currency, now_ms))
        ]
        closed_ids = {r[0] for r in c.execute("SELECT credit_id FROM credit_closures WHERE closed_at_ms<=?", (now_ms,))}
        seen_ids = {h["id"] for h in holdings}
        for row in c.execute("SELECT * FROM credit_history WHERE currency=? AND mts_opening<=?", (currency, now_ms)):
            if row["credit_id"] not in closed_ids:
                seen_ids.add(row["credit_id"])
                holdings.append(
                    {"currency": currency, "period": row["period"], "opened_ms": row["mts_opening"], "closed_ms": None}
                )
        for row in c.execute("SELECT * FROM credits WHERE currency=? AND mts_opening<=?", (currency, now_ms)):
            if row["credit_id"] not in closed_ids and row["credit_id"] not in seen_ids:
                holdings.append(
                    {"currency": currency, "period": row["period"], "opened_ms": row["mts_opening"], "closed_ms": None}
                )
        books = c.execute(
            "SELECT min(mts),max(mts),count(*) FROM book_snapshots WHERE mts>=? AND mts<=?",
            (now_ms - lookback_days * DAY, now_ms),
        ).fetchone()
        coverage = {
            "publicTrades": {"count": count, "earliestMs": earliest, "latestMs": latest},
            "bookSnapshots": {"earliestMs": books[0], "latestMs": books[1], "count": books[2]},
            "historicalBookBackfillable": False,
            "publicComplete": False,
            "gaps": ["公共逐笔历史缺少可核实的分页覆盖凭证；时间范围不能证明完整"],
            "queueIdentifiable": False,
        }
        manifest_path = Path(path).resolve().parent / "research" / currency / "public-coverage.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            coverage["pagination"] = manifest
            coverage["publicComplete"] = bool(
                manifest.get("complete")
                and not manifest.get("gaps")
                and manifest.get("startMs", now_ms) <= now_ms - lookback_days * DAY
                and manifest.get("endMs", 0) >= now_ms
            )
            if coverage["publicComplete"]:
                coverage["gaps"] = []
        except (OSError, ValueError):
            pass
        return daily, observations, holdings, coverage


def build_from_store(store, now_ms, cancelled=lambda: False, lookback_days=90):
    data = training_data(store.path, store.currency, now_ms, cancelled, lookback_days)
    return fit_model(store.currency, *data[:3], now_ms=now_ms, coverage=data[3])


def moving_block_interval(candidate, baseline, seed=409, iterations=2000):
    import random

    differences = [float(a) - float(b) for a, b in zip(candidate, baseline)]
    if len(differences) < 3:
        return {"lower": 0.0, "upper": 0.0, "valid": False}
    rng, values = random.Random(seed), []
    for _ in range(iterations):
        sample = []
        while len(sample) < len(differences):
            start = rng.randrange(len(differences) - 2)
            sample.extend(differences[start : start + 3])
        values.append(sum(sample[: len(differences)]) / len(differences))
    values.sort()
    return {
        "lower": values[int(iterations * 0.025)],
        "upper": values[int(iterations * 0.975)],
        "valid": True,
        "blockDays": 3,
        "iterations": iterations,
    }


class ResearchJobs:
    """Daemon jobs have only SQLite inputs, never an authenticated exchange client."""

    def __init__(self, store_factory, clock=time.time):
        self.store_factory, self.clock = store_factory, clock
        self.lock = threading.RLock()
        self.tasks = {}
        self.stops = {}
        self.shadow_observed_until = {}

    def status(self, currency):
        from Currency import require_currency

        require_currency(currency)
        with self.lock:
            if currency in self.tasks:
                return dict(self.tasks[currency])
        store = self.store_factory(currency)
        repo = ModelRepository(store.path, currency)
        try:
            state = json.loads((repo.directory / "job.json").read_text(encoding="utf-8"))
            if state.get("state") == "RUNNING":
                state["state"] = "INTERRUPTED"
            return state
        except (OSError, ValueError):
            return {"currency": currency, "state": "IDLE"}

    def start(self, currency, kind="evaluate", resume=False):
        if kind not in ("evaluate", "shadow"):
            raise ValueError("unknown research task")
        store = self.store_factory(currency)
        previous = self.status(currency) if resume else {}
        with self.lock:
            current = self.tasks.get(currency, {})
            if current.get("state") == "RUNNING":
                return dict(current)
            stop = threading.Event()
            self.stops[currency] = stop
            self.tasks[currency] = {
                "currency": currency,
                "kind": kind,
                "state": "RUNNING",
                "startedAtMs": previous.get("startedAtMs", int(self.clock() * 1000)),
                "phase": "TRAINING",
            }
        thread = threading.Thread(target=self._run, args=(store, kind, stop), daemon=True)
        thread.start()
        return self.status(currency)

    def stop(self, currency):
        from Currency import require_currency

        require_currency(currency)
        with self.lock:
            event = self.stops.get(currency)
            if event:
                event.set()
        return self.status(currency)

    def _publish(self, store, patch):
        repo = ModelRepository(store.path, store.currency)
        with self.lock:
            self.tasks[store.currency].update(patch)
            value = dict(self.tasks[store.currency])
            repo.directory.mkdir(parents=True, exist_ok=True)
            atomic_write_text(str(repo.directory / "job.json"), json.dumps(json_decimal(value)))

    def _run(self, store, kind, stop):
        repo = ModelRepository(store.path, store.currency)
        try:
            now = self.tasks[store.currency]["startedAtMs"] if kind == "evaluate" else int(self.clock() * 1000)
            if kind == "evaluate":
                from ReplayV4 import evaluate

                self._publish(store, {"phase": "REPLAY"})
                report, model = evaluate(store, now, stop.is_set)
                repo.save(model)
                atomic_write_text(str(repo.directory / "evaluation.json"), json.dumps(json_decimal(report)))
                self._publish(store, {"state": "COMPLETED", "phase": "DONE", "modelId": model["id"], "report": report})
            else:
                while not stop.is_set():
                    candidate = repo.candidate(int(self.clock() * 1000))
                    if candidate is None or candidate["trainedUntilMs"] // DAY < int(self.clock() * 1000) // DAY:
                        candidate = build_from_store(store, int(self.clock() * 1000), stop.is_set)
                        repo.save(candidate)
                    self._shadow(store, candidate, repo)
                    self._publish(
                        store, {"phase": "SHADOW", "modelId": candidate["id"], "confidence": candidate["confidence"]}
                    )
                    stop.wait(10)
                self._publish(store, {"state": "CANCELLED", "phase": "STOPPED"})
        except InterruptedError:
            self._publish(store, {"state": "CANCELLED", "phase": "STOPPED"})
        except Exception as exc:
            self._publish(store, {"state": "FAILED", "error": f"{type(exc).__name__}: {exc}"})

    def _shadow(self, store, model, repo):
        from RuntimeV3 import LendingRuntimeV3
        from StrategyV4 import build_plan
        from Configuration import strategy_v3_from_record

        now = int(self.clock() * 1000)
        with connect_readonly(store.path) as c:
            c.execute("BEGIN")
            sample = c.execute("SELECT * FROM account_samples ORDER BY mts DESC LIMIT 1").fetchone()
            book = c.execute("SELECT * FROM book_snapshots ORDER BY mts DESC LIMIT 1").fetchone()
            if sample is None or book is None or now - book["mts"] > 60000 or now - sample["mts"] > 60000:
                repo.journal({"atMs": now, "mode": "SHADOW", "reason": "ACCOUNT_OR_MARKET_STALE"})
                return
            policy = adaptive_template(strategy_v3_from_record(store.strategy("ACTIVE")))
            policy = replace(policy, model_id=model["id"])
            offers = [dict(r) for r in c.execute("SELECT * FROM offers WHERE status='ACTIVE'")]
            credits = [dict(r) for r in c.execute("SELECT * FROM credits WHERE status='ACTIVE'")]
            snapshot = {
                "wallets": [
                    {
                        "wallet_type": "funding",
                        "currency": store.currency,
                        "available": sample["wallet_available"],
                        "balance": sample["total_principal"],
                    }
                ],
                "offers": offers,
                "credits": credits,
                "loans": [],
            }
            account = LendingRuntimeV3._account(snapshot, store.currency)
            trades = [
                dict(r) for r in c.execute("SELECT * FROM market_trades WHERE mts>=? AND mts<=?", (now - 7 * DAY, now))
            ]
        from ExchangeModels import parse_book

        raw = json.loads(book["book_json"])
        normalized = raw if not raw or isinstance(raw[0], dict) else parse_book(raw)
        plan = build_plan(account, policy, model, normalized, trades, now, model["id"])
        after = self.shadow_observed_until.get(store.currency, now)
        observed = [row for row in trades if after < row["mts"] <= now]
        self.shadow_observed_until[store.currency] = now
        repo.journal(
            {
                "atMs": now,
                "mode": "SHADOW",
                "modelId": model["id"],
                "plan": plan,
                "marketObservation": {
                    "afterMs": after,
                    "untilMs": now,
                    "tradeCount": len(observed),
                    "volume": sum(abs(float(r["amount"])) for r in observed),
                    "medianRate": weighted_rate(observed),
                },
                "note": "公共成交不证明影子订单成交",
            }
        )
