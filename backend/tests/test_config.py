"""应用配置和身份上下文测试。"""

import pytest
from pydantic import ValidationError

from email_agent.config import (
    AppEnvironment,
    Settings,
    build_default_auth_context,
)
from email_agent.main import create_app


def test_secret_values_are_masked() -> None:
    """敏感配置不能通过对象字符串意外泄漏。"""
    settings = Settings(openai_api_key="test-secret", service_auth_token="service-secret")

    rendered = repr(settings)
    assert "test-secret" not in rendered
    assert "service-secret" not in rendered
    assert "**********" in rendered


def test_production_requires_service_auth_token() -> None:
    """生产环境缺少服务间认证时快速失败。"""
    with pytest.raises(ValidationError, match="SERVICE_AUTH_TOKEN"):
        Settings(app_env=AppEnvironment.PRODUCTION, service_auth_token=None)


def test_limits_reject_unsafe_values() -> None:
    """请求大小和外部调用超时必须保持在确定边界内。"""
    with pytest.raises(ValidationError):
        Settings(provider_timeout_seconds=0)

    with pytest.raises(ValidationError):
        Settings(max_request_bytes=100)


def test_single_user_context_is_attached_to_application() -> None:
    """首期单用户也通过统一身份上下文进入业务层。"""
    settings = Settings(app_env=AppEnvironment.TEST, single_user_id="private-owner")
    app = create_app(settings)

    assert build_default_auth_context(settings).user_id == "private-owner"
    assert app.state.settings is settings
    assert app.state.default_auth_context.user_id == "private-owner"
