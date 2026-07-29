"""应用配置和身份上下文测试。"""

import os

import pytest
from pydantic import ValidationError

from email_agent.config import (
    AliMailApiEdition,
    AppEnvironment,
    MailProviderKind,
    Settings,
    build_default_auth_context,
    get_settings,
)
from email_agent.main import create_app


def test_secret_values_are_masked() -> None:
    """敏感配置不能通过对象字符串意外泄漏。"""
    settings = Settings(
        openai_api_key="test-secret",
        service_auth_token="service-secret",
        approval_signing_secret="approval-signing-secret-32-bytes-long",
        database_url="postgresql://user:database-secret@localhost/email",
        microsoft_client_secret="microsoft-secret",
        microsoft_access_token="microsoft-access-token",
        microsoft_refresh_token="microsoft-refresh-token",
        alimail_client_secret="alimail-secret",
    )

    rendered = repr(settings)
    assert "test-secret" not in rendered
    assert "service-secret" not in rendered
    assert "approval-signing-secret-32-bytes-long" not in rendered
    assert "database-secret" not in rendered
    assert "microsoft-secret" not in rendered
    assert "microsoft-access-token" not in rendered
    assert "microsoft-refresh-token" not in rendered
    assert "alimail-secret" not in rendered
    assert "**********" in rendered


def test_production_requires_service_auth_token() -> None:
    """生产环境缺少服务间认证时快速失败。"""
    with pytest.raises(ValidationError, match="SERVICE_AUTH_TOKEN"):
        Settings(app_env=AppEnvironment.PRODUCTION, service_auth_token=None)


def test_production_requires_postgresql() -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings(
            app_env=AppEnvironment.PRODUCTION,
            service_auth_token="service-secret",
            approval_signing_secret="approval-signing-secret-32-bytes-long",
            database_url=None,
        )


def test_production_requires_explicit_mail_provider() -> None:
    with pytest.raises(ValidationError, match="MAIL_PROVIDER"):
        Settings(
            app_env=AppEnvironment.PRODUCTION,
            service_auth_token="service-secret",
            approval_signing_secret="approval-signing-secret-32-bytes-long",
            database_url="postgresql://localhost/email",
            mail_provider=None,
        )

    settings = Settings(
        app_env=AppEnvironment.PRODUCTION,
        service_auth_token="service-secret",
        approval_signing_secret="approval-signing-secret-32-bytes-long",
        database_url="postgresql://localhost/email",
        mail_provider=MailProviderKind.GMAIL,
    )
    assert settings.mail_provider is MailProviderKind.GMAIL


def test_limits_reject_unsafe_values() -> None:
    """请求大小和外部调用超时必须保持在确定边界内。"""
    with pytest.raises(ValidationError):
        Settings(provider_timeout_seconds=0)

    with pytest.raises(ValidationError):
        Settings(max_request_bytes=100)

    with pytest.raises(ValidationError):
        Settings(agent_timeout_seconds=0)


def test_service_auth_token_requires_ascii() -> None:
    with pytest.raises(ValidationError, match="ASCII"):
        Settings(service_auth_token="中文令牌")


def test_user_timezone_requires_valid_iana_name() -> None:
    with pytest.raises(ValidationError, match="IANA"):
        Settings(user_timezone="Mars/Phobos")


def test_get_settings_loads_dotenv_without_mutating_process_environment(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MODEL", raising=False)
    (tmp_path / ".env").write_text("MODEL=openai/test-model\n", encoding="utf-8")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.model == "openai/test-model"
        assert os.getenv("MODEL") is None
    finally:
        get_settings.cache_clear()


def test_alimail_provider_and_api_edition_are_validated() -> None:
    settings = Settings(
        mail_provider="alimail",
        alimail_api_edition="localized",
    )

    assert settings.mail_provider is MailProviderKind.ALIMAIL
    assert settings.alimail_api_edition is AliMailApiEdition.LOCALIZED


def test_single_user_context_is_attached_to_application() -> None:
    """首期单用户也通过统一身份上下文进入业务层。"""
    settings = Settings(app_env=AppEnvironment.TEST, single_user_id="private-owner")
    app = create_app(settings)

    assert build_default_auth_context(settings).user_id == "private-owner"
    assert app.state.settings is settings
    assert app.state.default_auth_context.user_id == "private-owner"
