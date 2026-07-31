"""生产运行时及外部资源的生命周期装配。"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import httpx

from .agents import build_email_agent_runtime, build_in_memory_persistence
from .api import AgentApplicationService
from .calendar import (
    build_approval_service,
    build_google_calendar_provider,
    build_microsoft_calendar_provider,
)
from .config import MailProviderKind, Settings, build_default_auth_context
from .content_tools import (
    AttachmentTextService,
    DatabaseUnsubscribeStateStore,
    JsonUnsubscribeStateStore,
    UnsubscribeService,
)
from .gmail import build_gmail_provider
from .model import build_model
from .observability import Observability
from .outlook import build_outlook_provider
from .persistence import open_postgres_persistence
from .providers.alimail import build_alimail_providers


@asynccontextmanager
async def open_agent_service(
    settings: Settings,
    observability: Observability,
) -> AsyncIterator[AgentApplicationService]:
    """按配置装配应用服务，并在退出时关闭全部外部资源。"""
    if settings.mail_provider is None:
        raise RuntimeError("缺少 MAIL_PROVIDER，无法装配邮件智能体")

    auth = build_default_auth_context(settings)

    async with AsyncExitStack() as stack:
        if settings.database_url is None:
            persistence = build_in_memory_persistence()
        else:
            persistence = await stack.enter_async_context(
                open_postgres_persistence(settings.database_url.get_secret_value())
            )
        approvals = build_approval_service(settings, persistence.state)

        if settings.mail_provider is MailProviderKind.GMAIL:
            mail_provider = build_gmail_provider(settings)
            calendar_provider = build_google_calendar_provider(settings, approvals)
        elif settings.mail_provider is MailProviderKind.OUTLOOK:
            mail_provider = build_outlook_provider(settings)
            calendar_provider = build_microsoft_calendar_provider(settings, approvals)
            stack.push_async_callback(mail_provider.aclose)
            stack.push_async_callback(calendar_provider.aclose)
        else:
            mail_provider, calendar_provider = build_alimail_providers(settings, approvals)
            stack.push_async_callback(mail_provider.aclose)

        unsubscribe_client = httpx.AsyncClient(
            timeout=settings.provider_timeout_seconds,
            follow_redirects=False,
        )
        stack.push_async_callback(unsubscribe_client.aclose)
        unsubscribe_service = UnsubscribeService(
            approvals=approvals,
            store=(
                JsonUnsubscribeStateStore(settings.unsubscribe_state_path)
                if settings.database_url is None
                else DatabaseUnsubscribeStateStore(persistence.state)
            ),
            http_client=unsubscribe_client,
            mail_provider=mail_provider,
        )
        runtime = build_email_agent_runtime(
            mail_provider=mail_provider,
            calendar_provider=calendar_provider,
            attachment_service=AttachmentTextService(
                max_attachment_bytes=settings.max_attachment_bytes,
            ),
            approvals=approvals,
            auth=auth,
            unsubscribe_service=unsubscribe_service,
            persistence=persistence,
            model=build_model(settings),
            user_timezone=settings.user_timezone,
        )
        yield AgentApplicationService(
            runtime,
            timeout_seconds=settings.agent_timeout_seconds,
            observability=observability,
        )
