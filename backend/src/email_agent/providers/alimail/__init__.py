"""阿里邮箱开放平台 Provider 装配。"""

import httpx

from ...calendar import ApprovalService
from ...config import AliMailApiEdition, Settings
from ...contracts import ProviderAuthenticationError
from .calendar import AliMailCalendarProvider
from .client import AliMailClient
from .mail import AliMailProvider

ALIMAIL_BASE_URLS = {
    AliMailApiEdition.STANDARD: "https://alimail-cn.aliyuncs.com",
    AliMailApiEdition.LOCALIZED: "https://mail-open.xc.aliyun.com",
}


def build_alimail_providers(
    settings: Settings,
    approvals: ApprovalService,
) -> tuple[AliMailProvider, AliMailCalendarProvider]:
    """使用企业应用凭证创建共享认证上下文的邮件和日历 Provider。"""
    client_id = settings.alimail_client_id
    client_secret = settings.alimail_client_secret
    email = settings.alimail_account_email
    required = {
        "ALIMAIL_CLIENT_ID": client_id,
        "ALIMAIL_CLIENT_SECRET": client_secret,
        "ALIMAIL_ACCOUNT_EMAIL": email,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise ProviderAuthenticationError("缺少阿里邮箱开放平台配置：" + ", ".join(missing))
    assert client_id is not None and client_secret is not None and email is not None

    http_client = httpx.AsyncClient(
        base_url=ALIMAIL_BASE_URLS[settings.alimail_api_edition],
        timeout=settings.provider_timeout_seconds,
        follow_redirects=False,
    )
    client = AliMailClient(
        http_client,
        client_id=client_id,
        client_secret=client_secret.get_secret_value(),
    )
    return (
        AliMailProvider(client, email=email),
        AliMailCalendarProvider(client, email=email, approvals=approvals),
    )


__all__ = [
    "ALIMAIL_BASE_URLS",
    "AliMailCalendarProvider",
    "AliMailClient",
    "AliMailProvider",
    "build_alimail_providers",
]
