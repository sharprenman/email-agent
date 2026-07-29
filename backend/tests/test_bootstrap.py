"""生产运行时生命周期装配测试。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock

from email_agent import bootstrap
from email_agent import main as main_module
from email_agent.config import AppEnvironment, MailProviderKind, Settings
from email_agent.main import create_app
from email_agent.observability import Observability
from email_agent.persistence import build_in_memory_persistence


class _ClosableProvider:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _settings(provider: MailProviderKind) -> Settings:
    common = {
        "app_env": AppEnvironment.TEST,
        "mail_provider": provider,
        "approval_signing_secret": "a" * 32,
    }
    if provider is MailProviderKind.GMAIL:
        return Settings(
            **common,
            google_client_id="google-client",
            google_client_secret="google-secret",
            google_refresh_token="google-refresh",
        )
    if provider is MailProviderKind.ALIMAIL:
        return Settings(
            **common,
            alimail_client_id="alimail-client",
            alimail_client_secret="alimail-secret",
            alimail_account_email="owner@example.com",
        )
    return Settings(
        **common,
        microsoft_client_id="microsoft-client",
        microsoft_client_secret="microsoft-secret",
        microsoft_refresh_token="microsoft-refresh",
    )


def _patch_runtime(monkeypatch, mail_provider, calendar_provider) -> Mock:
    model = object()
    monkeypatch.setattr(bootstrap, "build_model", lambda _settings: model)
    monkeypatch.setattr(
        bootstrap,
        "build_gmail_provider",
        lambda _settings: mail_provider,
    )
    monkeypatch.setattr(
        bootstrap,
        "build_google_calendar_provider",
        lambda _settings, _approvals: calendar_provider,
    )
    monkeypatch.setattr(
        bootstrap,
        "build_outlook_provider",
        lambda _settings: mail_provider,
    )
    monkeypatch.setattr(
        bootstrap,
        "build_microsoft_calendar_provider",
        lambda _settings, _approvals: calendar_provider,
    )
    monkeypatch.setattr(
        bootstrap,
        "build_alimail_providers",
        lambda _settings, _approvals: (mail_provider, calendar_provider),
    )
    build_runtime = Mock(
        return_value=SimpleNamespace(
            agent=object(),
            persistence=build_in_memory_persistence(),
            auth=SimpleNamespace(user_id="local-user"),
        )
    )
    monkeypatch.setattr(bootstrap, "build_email_agent_runtime", build_runtime)
    build_runtime.expected_model = model
    return build_runtime


def test_gmail_runtime_uses_in_memory_persistence(monkeypatch) -> None:
    mail_provider = object()
    calendar_provider = object()
    build_runtime = _patch_runtime(monkeypatch, mail_provider, calendar_provider)

    async def run() -> None:
        async with bootstrap.open_agent_service(
            _settings(MailProviderKind.GMAIL),
            Observability(),
        ) as service:
            assert service.is_ready()

    asyncio.run(run())
    arguments = build_runtime.call_args.kwargs
    assert arguments["mail_provider"] is mail_provider
    assert arguments["calendar_provider"] is calendar_provider
    assert arguments["persistence"].checkpointer is not None
    assert arguments["model"] is build_runtime.expected_model
    assert arguments["user_timezone"] == "Asia/Shanghai"


def test_outlook_runtime_closes_provider_clients(monkeypatch) -> None:
    mail_provider = _ClosableProvider()
    calendar_provider = _ClosableProvider()
    _patch_runtime(monkeypatch, mail_provider, calendar_provider)

    async def run() -> None:
        async with bootstrap.open_agent_service(
            _settings(MailProviderKind.OUTLOOK),
            Observability(),
        ):
            assert not mail_provider.closed
            assert not calendar_provider.closed

    asyncio.run(run())
    assert mail_provider.closed
    assert calendar_provider.closed


def test_alimail_runtime_closes_shared_provider_client_once(monkeypatch) -> None:
    mail_provider = _ClosableProvider()
    calendar_provider = object()
    _patch_runtime(monkeypatch, mail_provider, calendar_provider)

    async def run() -> None:
        async with bootstrap.open_agent_service(
            _settings(MailProviderKind.ALIMAIL),
            Observability(),
        ):
            assert not mail_provider.closed

    asyncio.run(run())
    assert mail_provider.closed


def test_application_lifespan_injects_and_releases_service(monkeypatch) -> None:
    service = SimpleNamespace()

    @asynccontextmanager
    async def open_service(_settings, _observability):
        yield service

    monkeypatch.setattr(main_module, "open_agent_service", open_service)
    application = create_app(
        _settings(MailProviderKind.GMAIL),
        auto_configure=True,
    )

    async def run() -> None:
        assert application.state.agent_service is None
        async with application.router.lifespan_context(application):
            assert application.state.agent_service is service
        assert application.state.agent_service is None

    asyncio.run(run())
