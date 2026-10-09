"""Isolated V4 browser fixture. Exchange reads and writes stay in a fake account."""

import json
import time
import os
import sys
import tempfile
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lendingbot  # noqa: E402
from AppContext import AppContext  # noqa: E402
from FileUtils import atomic_write_text  # noqa: E402
from RuntimeV3 import LendingRuntimeV3  # noqa: E402
from MarketDataStream import BitfinexMarketDataHub  # noqa: E402
from V4Service import stores_for_profiles  # noqa: E402
from test_bitfinex_bot import FakeControlledProcess  # noqa: E402
from test_v4 import FundingClient, configuration, policy  # noqa: E402
from RuntimeV4 import V4Coordinator  # noqa: E402
from StrategyV3 import json_decimal  # noqa: E402
from dataclasses import replace  # noqa: E402


def main():
    with tempfile.TemporaryDirectory(prefix="mika-v4-browser-") as directory:
        path, settings = configuration(Path(directory))
        client = FundingClient()
        client.now = int(time.time() * 1000)
        if "--v41" in sys.argv:
            from test_operational_v41 import PublicAccount

            client = PublicAccount()
            client.now = int(time.time() * 1000)
        context = AppContext.for_project(
            directory, config_path=str(path), client_factory=lambda *_args: client, now=lambda: client.now / 1000
        )
        stores = stores_for_profiles(settings, clock=lambda: client.now / 1000)
        # Seed actual V4 runtime output using only the fixture account.
        BitfinexMarketDataHub.start = lambda _self: None
        LendingRuntimeV3.start_income_history_sync = lambda _self: None
        for store in stores.values():
            store.set_mode("LIVE")
        coordinator = V4Coordinator(
            client,
            stores,
            {currency: policy(currency) for currency in stores},
            settings,
            clock=lambda: client.now / 1000,
        )
        statuses = coordinator.cycle()
        if "--v41" in sys.argv:
            from Configuration import strategy_v3_from_record
            from OperationalV41 import accept
            from ResearchV4 import ModelRepository
            from StrategyV41 import fit_model, template

            for currency, store in stores.items():
                repo = ModelRepository(store.path, currency)
                model = fit_model(
                    currency,
                    [],
                    now_ms=client.now,
                    frr=[dict(mts=client.now - d * 86400000, frr_daily_rate=".0008") for d in range(30)],
                    allow_frr_reference=True,
                )
                repo.save(model)
                accept(repo, model, client.now)
                proposed = replace(template(strategy_v3_from_record(store.strategy("ACTIVE"))), model_id=model["id"])
                store.save_strategy(json_decimal(proposed.__dict__), "DRAFT")
        for store in stores.values():
            store.pause_currency()
        client.now += 31_000
        statuses = coordinator.cycle()
        atomic_write_text(
            context.status_path,
            json.dumps(
                {
                    **statuses["USD"],
                    "currencies": json_decimal(statuses),
                    "last_update": lendingbot.timestamp(),
                    "operationMode": "PAUSED",
                    "schemaVersion": 3,
                    "v4SchemaVersion": 4,
                },
                default=str,
            ),
        )
        base_handler = lendingbot.make_dashboard_handler(
            str(ROOT / "www"), str(path), context.status_path, context=context
        )
        service = base_handler.application.v4_service
        if "--usd-live" in sys.argv:
            stores["USD"].set_mode("LIVE")
            context.process_state.process = FakeControlledProcess(os.getpid())

        def fixture_launch(selected):
            for currency in selected:
                stores[currency].authorize_live_after_preflight()
            context.process_state.process = FakeControlledProcess(os.getpid())
            context.process_state.started_at = lendingbot.timestamp()
            return lendingbot.controlled_bot_status(str(path), context)

        service._launch = fixture_launch

        class Handler(base_handler):
            def _validate_write_request(self):
                if (
                    self.headers.get("Host") == "127.0.0.1:8124"
                    and self.headers.get("Origin") == "http://127.0.0.1:8124"
                ):
                    self.headers.replace_header("Host", "127.0.0.1:8000")
                    self.headers.replace_header("Origin", "http://127.0.0.1:8000")
                super()._validate_write_request()

        server = ThreadingHTTPServer(("127.0.0.1", 8124), Handler)
        try:
            server.serve_forever()
        finally:
            server.server_close()
            coordinator.shutdown()


if __name__ == "__main__":
    main()
