"""DeepAgents 主代理装配与最小权限测试。"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from email_agent.agents import (
    CALENDAR_AGENT,
    MAIL_WRITER,
    MAILBOX_READER,
    AgentTaskResult,
    AgentTaskStatus,
    build_email_agent_runtime,
    mail_approval_payload,
    merge_task_results,
)
from email_agent.api.schemas import ChatRequest, ResumeRequest, ThreadStatus
from email_agent.api.service import AgentApplicationService
from email_agent.calendar import ApprovalAction, ApprovalRequiredError, ApprovalService
from email_agent.config import AuthContext
from email_agent.content_tools import (
    AttachmentTextService,
    UnsubscribeCandidate,
    UnsubscribeMethod,
    UnsubscribeResult,
    UnsubscribeResultStatus,
    UnsubscribeSource,
)
from email_agent.contracts import CalendarEvent, CalendarEventInput, SendEmailRequest
from email_agent.skills import EMAIL_SKILL_SOURCE, EMAIL_SKILLS


class _ToolCapableFakeModel(GenericFakeChatModel):
    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        del tools, tool_choice, kwargs
        return self


def _fake_model() -> GenericFakeChatModel:
    return _ToolCapableFakeModel(messages=iter([AIMessage(content="测试完成")]))


def _build_runtime(unsubscribe_service=None, *, model=None):
    mail = SimpleNamespace(
        get_identity=AsyncMock(),
        read_inbox=AsyncMock(return_value=[]),
        search_emails=AsyncMock(return_value=[]),
        get_email=AsyncMock(),
        get_sent_emails=AsyncMock(return_value=[]),
        get_unanswered_emails=AsyncMock(return_value=[]),
        list_attachments=AsyncMock(return_value=[]),
        download_attachment=AsyncMock(),
        list_contacts=AsyncMock(return_value=[]),
        send_email=AsyncMock(return_value="message-1"),
        mark_read=AsyncMock(),
    )
    calendar = SimpleNamespace(
        list_events=AsyncMock(return_value=[]),
        create_event=AsyncMock(),
        update_event=AsyncMock(),
        delete_event=AsyncMock(),
    )
    approvals = ApprovalService("a" * 32)
    auth = AuthContext(user_id="trusted-user")
    runtime = build_email_agent_runtime(
        mail_provider=mail,
        calendar_provider=calendar,
        attachment_service=AttachmentTextService(max_attachment_bytes=1024),
        approvals=approvals,
        auth=auth,
        unsubscribe_service=unsubscribe_service,
        model=model or _fake_model(),
    )
    return runtime, mail, calendar, approvals, auth


def _tool(runtime, subagent_name: str, tool_name: str):
    spec = next(item for item in runtime.subagents if item["name"] == subagent_name)
    return next(tool for tool in spec["tools"] if tool.name == tool_name)


def test_runtime_builds_supervisor_and_three_explicit_subagents() -> None:
    runtime, _, _, _, _ = _build_runtime()

    assert runtime.agent.name == "email-supervisor"
    assert [spec["name"] for spec in runtime.subagents] == [
        MAILBOX_READER,
        MAIL_WRITER,
        CALENDAR_AGENT,
    ]
    assert {tool.name for tool in runtime.main_tools} == {
        "prepare_skill_workflow",
        "merge_subagent_results",
        "read_user_memory",
        "save_user_memory",
    }
    assert all(spec["response_format"] is AgentTaskResult for spec in runtime.subagents)
    assert runtime.skill_bundle.names == EMAIL_SKILLS
    assert runtime.skill_bundle.sources == (EMAIL_SKILL_SOURCE,)
    assert runtime.agent.checkpointer is runtime.persistence.checkpointer
    assert runtime.agent.store is runtime.persistence.store
    assert runtime.context.user_id == "trusted-user"
    assert runtime.approvals is not None


def test_runtime_injects_builtin_skill_files() -> None:
    runtime, _, _, _, _ = _build_runtime()

    payload = runtime.prepare_input({"messages": ["生成一份周报"]})

    assert len(payload["files"]) == len(EMAIL_SKILLS)
    assert all(path.endswith("/SKILL.md") for path in payload["files"])


def test_supervisor_workflow_tool_enforces_skill_limits() -> None:
    runtime, _, _, _, _ = _build_runtime()
    tool = next(item for item in runtime.main_tools if item.name == "prepare_skill_workflow")

    result = asyncio.run(
        tool.ainvoke(
            {
                "skill_name": "urgent-email-triage",
                "days": 90,
                "max_results": 999,
            }
        )
    )

    assert result["days"] == 7
    assert result["max_results"] == 250
    assert result["search_query"].startswith("in:inbox newer_than:7d")


def test_subagent_business_tool_whitelists_prevent_privilege_escalation() -> None:
    runtime, _, _, _, _ = _build_runtime()
    reader_tools = runtime.subagent_tool_names(MAILBOX_READER)
    writer_tools = runtime.subagent_tool_names(MAIL_WRITER)
    calendar_tools = runtime.subagent_tool_names(CALENDAR_AGENT)

    assert {
        "read_inbox",
        "search_emails",
        "get_email",
        "extract_attachment_text",
        "discover_email_unsubscribe",
    } <= reader_tools
    assert reader_tools.isdisjoint(
        {
            "prepare_email_draft",
            "send_email",
            "execute_unsubscribe",
            "create_calendar_event",
            "update_calendar_event",
            "delete_calendar_event",
        }
    )
    assert writer_tools == {"prepare_email_draft", "send_email"}
    assert calendar_tools == {
        "list_calendar_events",
        "create_calendar_event",
        "update_calendar_event",
        "delete_calendar_event",
    }
    assert runtime.interrupt_on == {
        "save_user_memory": True,
        "send_email": True,
        "create_calendar_event": True,
        "update_calendar_event": True,
        "delete_calendar_event": True,
    }


def test_memory_tool_uses_versioned_current_user_store() -> None:
    runtime, _, _, _, _ = _build_runtime()
    read_tool = next(item for item in runtime.main_tools if item.name == "read_user_memory")
    save_tool = next(item for item in runtime.main_tools if item.name == "save_user_memory")

    empty = asyncio.run(read_tool.ainvoke({"kind": "writing-style"}))
    saved = asyncio.run(
        save_tool.ainvoke(
            {
                "kind": "writing-style",
                "content": "# 写作风格\n\n## 语气\n- 偏好简洁、正式的中文表达",
                "expected_version": 0,
            }
        )
    )

    assert empty["exists"] is False
    assert saved["version"] == 1
    assert runtime.memory_service.read("writing-style").content.startswith("# 写作风格")


def test_memory_write_interrupt_is_checkpointed_before_store_change() -> None:
    model = _ToolCapableFakeModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "save_user_memory",
                            "args": {
                                "kind": "profile",
                                "content": "# 用户画像\n- 称呼：小王",
                                "expected_version": 0,
                            },
                            "id": "memory-write-1",
                        }
                    ],
                )
            ]
        )
    )
    runtime, _, _, _, _ = _build_runtime(model=model)
    config = {"configurable": {"thread_id": "memory-interrupt-thread"}}

    result = runtime.agent.invoke(
        runtime.prepare_input({"messages": ["请记住称呼"]}),
        config,
        context=runtime.context,
    )

    assert result["__interrupt__"]
    assert runtime.agent.get_state(config).next
    assert runtime.memory_service.read("profile") is None


def test_application_service_resumes_real_deepagent_memory_interrupt() -> None:
    model = _ToolCapableFakeModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "save_user_memory",
                            "args": {
                                "kind": "profile",
                                "content": "# 用户画像\n- 称呼：小王",
                                "expected_version": 0,
                            },
                            "id": "memory-write-api-1",
                        }
                    ],
                ),
                AIMessage(content="长期记忆已经保存"),
            ]
        )
    )
    runtime, _, _, _, _ = _build_runtime(model=model)
    service = AgentApplicationService(runtime)
    interrupted = asyncio.run(
        service.chat(
            ChatRequest(
                message="请记住我的称呼",
                idempotency_key="real-chat-request-0001",
            )
        )
    )

    assert interrupted.status is ThreadStatus.INTERRUPTED

    resumed = asyncio.run(
        service.resume(
            interrupted.thread_id,
            ResumeRequest.model_validate(
                {
                    "interrupt_id": interrupted.pending_approvals[0].interrupt_id,
                    "idempotency_key": "real-resume-request-0001",
                    "decisions": [
                        {
                            "type": "approve",
                            "operation_idempotency_key": "real-memory-operation-0001",
                        }
                    ],
                }
            ),
        )
    )

    assert resumed.status is ThreadStatus.COMPLETED
    assert runtime.memory_service.read("profile").content.startswith("# 用户画像")


def test_draft_is_explicitly_not_sent() -> None:
    runtime, mail, _, _, _ = _build_runtime()
    tool = _tool(runtime, MAIL_WRITER, "prepare_email_draft")

    result = asyncio.run(
        tool.ainvoke(
            {
                "to": ["receiver@example.com"],
                "subject": "项目进展",
                "body": "这是草稿。",
            }
        )
    )

    assert result["status"] == "draft"
    assert result["sent"] is False
    mail.send_email.assert_not_awaited()


def test_send_email_cannot_bypass_approval_and_approved_payload_is_exact() -> None:
    runtime, mail, _, approvals, auth = _build_runtime()
    tool = _tool(runtime, MAIL_WRITER, "send_email")
    arguments = {
        "to": ["receiver@example.com"],
        "subject": "项目进展",
        "body": "审批后的正文。",
        "idempotency_key": "send-1",
    }

    with pytest.raises(ApprovalRequiredError):
        asyncio.run(tool.ainvoke(arguments))
    mail.send_email.assert_not_awaited()

    request = SendEmailRequest(
        to=("receiver@example.com",),
        subject="项目进展",
        body="审批后的正文。",
    )
    token = approvals.mint_after_interrupt(
        auth,
        action=ApprovalAction.SEND_EMAIL,
        target_id=None,
        payload=mail_approval_payload(request),
        idempotency_key="send-1",
    )
    result = asyncio.run(tool.ainvoke({**arguments, "approval_token": token}))

    assert result == {"status": "sent", "message_id": "message-1"}
    mail.send_email.assert_awaited_once_with(request, idempotency_key="send-1")


def test_calendar_tool_uses_captured_trusted_user_id() -> None:
    runtime, _, calendar, _, _ = _build_runtime()
    tool = _tool(runtime, CALENDAR_AGENT, "create_calendar_event")
    start_at = datetime(2026, 7, 24, 9, tzinfo=UTC)
    event = CalendarEventInput(
        title="项目会议",
        start_at=start_at,
        end_at=start_at + timedelta(hours=1),
        timezone="Asia/Shanghai",
    )
    calendar.create_event.return_value = CalendarEvent(id="event-1", **event.model_dump())

    result = asyncio.run(
        tool.ainvoke(
            {
                "event": event.model_dump(mode="json"),
                "idempotency_key": "calendar-1",
                "approval_token": "server-token",
            }
        )
    )

    assert result["id"] == "event-1"
    calendar.create_event.assert_awaited_once_with(
        event,
        user_id="trusted-user",
        approval_token="server-token",
        idempotency_key="calendar-1",
    )


def test_unsubscribe_tool_is_writer_only_and_uses_trusted_user_id() -> None:
    unsubscribe = SimpleNamespace(
        execute=AsyncMock(
            return_value=UnsubscribeResult(
                method=UnsubscribeMethod.ONE_CLICK,
                status=UnsubscribeResultStatus.CONFIRMED,
                message="退订请求已接受",
            )
        )
    )
    runtime, _, _, _, _ = _build_runtime(unsubscribe)
    candidate = UnsubscribeCandidate(
        method=UnsubscribeMethod.ONE_CLICK,
        source=UnsubscribeSource.HEADER,
        target="https://example.com/unsubscribe/token",
        dkim_evidence=True,
    )
    tool = _tool(runtime, MAIL_WRITER, "execute_unsubscribe")

    result = asyncio.run(
        tool.ainvoke(
            {
                "candidate": candidate.model_dump(mode="json"),
                "idempotency_key": "unsubscribe-1",
                "approval_token": "server-token",
            }
        )
    )

    assert result["status"] == "confirmed"
    assert "execute_unsubscribe" not in runtime.subagent_tool_names(MAILBOX_READER)
    assert runtime.interrupt_on["execute_unsubscribe"] is True
    unsubscribe.execute.assert_awaited_once_with(
        candidate,
        user_id="trusted-user",
        approval_token="server-token",
        idempotency_key="unsubscribe-1",
    )


def test_merge_task_results_preserves_failed_and_partial_states() -> None:
    success = AgentTaskResult(
        status=AgentTaskStatus.SUCCESS,
        summary="邮件已找到",
        evidence=("mail-1",),
    )
    failed = AgentTaskResult(
        status=AgentTaskStatus.FAILED,
        summary="日历写入失败",
        failures=("审批凭证无效",),
    )

    partial = merge_task_results([success, failed])
    all_failed = merge_task_results([failed, failed])

    assert partial.status is AgentTaskStatus.PARTIAL
    assert partial.evidence == ("mail-1",)
    assert partial.failures == ("审批凭证无效",)
    assert "[failed] 日历写入失败" in partial.summary
    assert all_failed.status is AgentTaskStatus.FAILED


def test_merge_task_results_rejects_empty_execution() -> None:
    with pytest.raises(ValueError, match="至少需要一个任务结果"):
        merge_task_results([])
