"""日历查询和受审批写入 Tool。"""

from datetime import datetime
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from ...config import AuthContext
from ...contracts import CalendarEventInput, CalendarProvider


def build_calendar_tools(
    provider: CalendarProvider,
    auth: AuthContext,
) -> tuple[BaseTool, ...]:
    """构建日历读取和受审批写入 Tool。"""

    async def list_calendar_events(
        start_at: datetime,
        end_at: datetime,
    ) -> list[dict[str, Any]]:
        """读取指定时间窗口的日历事件。"""
        return [
            item.model_dump(mode="json")
            for item in await provider.list_events(start_at=start_at, end_at=end_at)
        ]

    async def create_calendar_event(
        event: CalendarEventInput,
        idempotency_key: str,
        approval_token: str = "",
    ) -> dict[str, Any]:
        """审批后创建日历事件。"""
        result = await provider.create_event(
            event,
            user_id=auth.user_id,
            approval_token=approval_token,
            idempotency_key=idempotency_key,
        )
        return result.model_dump(mode="json")

    async def update_calendar_event(
        event_id: str,
        event: CalendarEventInput,
        idempotency_key: str,
        approval_token: str = "",
    ) -> dict[str, Any]:
        """审批后修改指定日历事件。"""
        result = await provider.update_event(
            event_id,
            event,
            user_id=auth.user_id,
            approval_token=approval_token,
            idempotency_key=idempotency_key,
        )
        return result.model_dump(mode="json")

    async def delete_calendar_event(
        event_id: str,
        idempotency_key: str,
        approval_token: str = "",
    ) -> dict[str, str]:
        """审批后删除指定日历事件。"""
        await provider.delete_event(
            event_id,
            user_id=auth.user_id,
            approval_token=approval_token,
            idempotency_key=idempotency_key,
        )
        return {"status": "deleted", "event_id": event_id}

    definitions = (
        (
            list_calendar_events,
            "list_calendar_events",
            "读取指定时间窗口的日历事件。",
        ),
        (
            create_calendar_event,
            "create_calendar_event",
            "人工审批后创建日历事件。",
        ),
        (
            update_calendar_event,
            "update_calendar_event",
            "人工审批后修改日历事件。",
        ),
        (
            delete_calendar_event,
            "delete_calendar_event",
            "人工审批后删除日历事件。",
        ),
    )
    return tuple(
        StructuredTool.from_function(
            coroutine=function,
            name=name,
            description=description,
        )
        for function, name, description in definitions
    )
