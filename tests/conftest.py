import pytest


@pytest.fixture(autouse=True)
def isolate_credentials_and_runtime(monkeypatch, tmp_path):
    """Real credentials are never visible to tests; runtime paths come from AppContext."""
    import lendingbot

    monkeypatch.delenv("BITFINEX_API_KEY", raising=False)
    monkeypatch.delenv("BITFINEX_API_SECRET", raising=False)

    def isolated_context(config_path, context=None, client_factory=None, now=None):
        if context is not None:
            return context
        return lendingbot.AppContext.for_project(
            tmp_path,
            config_path=config_path,
            client_factory=client_factory or lendingbot.Bitfinex,
            now=now or lendingbot.time.time,
        )

    monkeypatch.setattr(lendingbot, "process_context", isolated_context)
