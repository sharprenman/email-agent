"""Agent API 应用服务的线程、审批和幂等测试。"""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command, Interrupt, StateSnapshot

from email_agent.agents import AgentTaskResult, AgentTaskStatus
from email_agent.api.errors import ApiError
from email_agent.api.schemas import (
    ChatRequest,
    ResumeRequest,
    StreamEventType,
    ThreadStatus,
)
from email_agent.api.service import AgentApplicationService
from email_agent.calendar import ApprovalService
from email_agent.config import AuthContext
from email_agent.observability import MemoryAuditSink, Observability, hash_reference
from email_agent.persistence import build_in_memory_persistence


class _FakeAgent:
    def __init__(self, *, interrupt_tool: str | None = None) -> None:
        self._states: dict[str, StateSnapshot] = {}
        self.interrupt_tool = interrupt_tool
        self.last_command: Command | None = None

    async def ainvoke(self, payload, config, *, context):
        del context
        thread_id = config["configurable"]["thread_id"]
        if isinstance(payload, Command):
            self.last_command = payload
            self._states[thread_id] = _snapshot(
                thread_id,
                messages=[
                    HumanMessage(content="原请求"),
                    AIMessage(content="审批后完成"),
                ],
                result=AgentTaskResult(
                    status=AgentTaskStatus.SUCCESS,
                    summary="审批后完成",
                ),
            )
            return self._states[thread_id].values
        if self.interrupt_tool:
            interrupt = _interrupt(self.interrupt_tool)
            self._states[thread_id] = _snapshot(
                thread_id,
                messages=[HumanMessage(content="需要审批"), AIMessage(content="")],
                interrupts=(interrupt,),
                next_nodes=("human_review",),
            )
        else:
            self._states[thread_id] = _snapshot(
                thread_id,
                messages=[
                    HumanMessage(content="测试请求"),
                    AIMessage(content="测试完成", id="ai-final"),
                ],
                result=AgentTaskResult(
                    status=AgentTaskStatus.SUCCESS,
                    summary="测试完成",
                ),
            )
        return self._states[thread_id].values

    async def astream(self, payload, config, *, context, **kwargs):
        del payload, context, kwargs
        thread_id = config["configurable"]["thread_id"]
        message = AIMessage(
            content="流式回复",
            id="ai-stream",
            tool_calls=[
                {
                    "name": "search_emails",
                    "args": {"query": "secret subject"},
                    "id": "tool-1",
                }
            ],
        )
        yield {"type": "messages", "data": (message, {})}
        self._states[thread_id] = _snapshot(
            thread_id,
            messages=[HumanMessage(content="流式请求"), message],
            result=AgentTaskResult(
                status=AgentTaskStatus.SUCCESS,
                summary="流式回复",
            ),
        )

    async def aget_state(self, config):
        thread_id = config["configurable"]["thread_id"]
        return self._states.get(thread_id, _snapshot(thread_id))


class _FakeRuntime:
    def __init__(
        self,
        agent: _FakeAgent,
        *,
        user_id: str = "owner",
        persistence=None,
    ) -> None:
        self.agent = agent
        self.auth = AuthContext(user_id=user_id)
        self.persistence = persistence or build_in_memory_persistence()
        self.approvals = ApprovalService("a" * 32)

    @property
    def context(self) -> AuthContext:
        return self.auth

    @staticmethod
    def prepare_input(payload):
        return dict(payload)


def _snapshot(
    thread_id: str,
    *,
    messages=(),
    result: AgentTaskResult | None = None,
    interrupts=(),
    next_nodes=(),
) -> StateSnapshot:
    values = {"messages": list(messages)} if messages else {}
    if result is not None:
        values["structured_response"] = result
    return StateSnapshot(
        values=values,
        next=tuple(next_nodes),
        config={"configurable": {"thread_id": thread_id}},
        metadata=None,
        created_at="2026-07-24T10:00:00+00:00",
        parent_config=None,
        tasks=(),
        interrupts=tuple(interrupts),
    )


def _interrupt(tool_name: str) -> Interrupt:
    arguments = {
        "to": ["receiver@example.com"],
        "subject": "项目进展",
        "body": "审批后的正文",
        "idempotency_key": "model-generated-key",
        "approval_token": "must-not-leak",
    }
    if tool_name == "save_user_memory":
        arguments = {
            "kind": "profile",
            "content": "# 用户画像\n- 称呼：小王",
            "expected_version": 0,
        }
    return Interrupt(
        id="interrupt-1",
        value={
            "action_requests": [
                {
                    "name": tool_name,
                    "args": arguments,
                }
            ],
            "review_configs": [
                {
                    "action_name": tool_name,
                    "allowed_decisions": ["approve", "edit", "reject", "respond"],
                }
            ],
        },
    )


def _chat_request(**updates) -> ChatRequest:
    return ChatRequest(
        message="测试请求",
        idempotency_key="chat-request-0001",
        **updates,
    )


def test_chat_creates_owned_thread_and_delete_removes_access() -> None:
    service = AgentApplicationService(_FakeRuntime(_FakeAgent()))

    result = asyncio.run(service.chat(_chat_request()))
    fetched = asyncio.run(service.get_thread(result.thread_id))
    deleted = asyncio.run(service.delete_thread(result.thread_id))

    assert result.thread_id.startswith("th_")
    assert result.status is ThreadStatus.COMPLETED
    assert result.result.summary == "测试完成"
    assert fetched.message_count == 2
    assert deleted.deleted is True
    with pytest.raises(ApiError) as exc_info:
        asyncio.run(service.get_thread(result.thread_id))
    assert exc_info.value.status_code == 404


def test_unknown_and_foreign_threads_are_distinguished() -> None:
    persistence = build_in_memory_persistence()
    owner = AgentApplicationService(
        _FakeRuntime(_FakeAgent(), user_id="owner", persistence=persistence)
    )
    other = AgentApplicationService(
        _FakeRuntime(_FakeAgent(), user_id="other", persistence=persistence)
    )
    thread_id = asyncio.run(owner.chat(_chat_request())).thread_id

    with pytest.raises(ApiError) as missing:
        asyncio.run(owner.get_thread("th_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"))
    with pytest.raises(ApiError) as denied:
        asyncio.run(other.get_thread(thread_id))

    assert missing.value.status_code == 404
    assert denied.value.status_code == 403


def test_chat_idempotency_key_cannot_execute_twice() -> None:
    service = AgentApplicationService(_FakeRuntime(_FakeAgent()))
    first = asyncio.run(service.chat(_chat_request()))

    with pytest.raises(ApiError) as exc_info:
        asyncio.run(
            service.chat(
                _chat_request(
                    thread_id=first.thread_id,
                )
            )
        )

    assert exc_info.value.status_code == 409


def test_approved_email_resume_injects_server_token_and_operation_key() -> None:
    agent = _FakeAgent(interrupt_tool="send_email")
    service = AgentApplicationService(_FakeRuntime(agent))
    interrupted = asyncio.run(service.chat(_chat_request()))
    approval = interrupted.pending_approvals[0]

    assert interrupted.status is ThreadStatus.INTERRUPTED
    assert approval.actions[0].name == "send_email"
    assert "approval_token" not in approval.actions[0].arguments

    resumed = asyncio.run(
        service.resume(
            interrupted.thread_id,
            ResumeRequest.model_validate(
                {
                    "interrupt_id": approval.interrupt_id,
                    "idempotency_key": "resume-request-0001",
                    "decisions": [
                        {
                            "type": "approve",
                            "operation_idempotency_key": "send-operation-0001",
                        }
                    ],
                }
            ),
        )
    )

    decision = agent.last_command.resume["interrupt-1"]["decisions"][0]
    arguments = decision["edited_action"]["args"]
    assert resumed.status is ThreadStatus.COMPLETED
    assert decision["type"] == "edit"
    assert arguments["idempotency_key"] == "send-operation-0001"
    assert arguments["approval_token"]
    assert arguments["approval_token"] != "must-not-leak"


def test_memory_resume_uses_interrupt_without_external_approval_token() -> None:
    agent = _FakeAgent(interrupt_tool="save_user_memory")
    service = AgentApplicationService(_FakeRuntime(agent))
    interrupted = asyncio.run(service.chat(_chat_request()))

    asyncio.run(
        service.resume(
            interrupted.thread_id,
            ResumeRequest.model_validate(
                {
                    "interrupt_id": "interrupt-1",
                    "idempotency_key": "resume-memory-0001",
                    "decisions": [
                        {
                            "type": "approve",
                            "operation_idempotency_key": "memory-operation-0001",
                        }
                    ],
                }
            ),
        )
    )

    arguments = agent.last_command.resume["interrupt-1"]["decisions"][0]["edited_action"][
        "args"
    ]
    assert "approval_token" not in arguments
    assert "idempotency_key" not in arguments


def test_resume_audits_approval_and_result_without_sensitive_arguments() -> None:
    agent = _FakeAgent(interrupt_tool="send_email")
    sink = MemoryAuditSink()
    observability = Observability(audit_sink=sink)
    service = AgentApplicationService(
        _FakeRuntime(agent),
        observability=observability,
    )
    context = observability.context(
        user_id="owner",
        request_id="request-audit",
        trace_id="trace-audit",
    )
    interrupted = asyncio.run(
        service.chat(
            _chat_request(),
            observation=context,
        )
    )

    asyncio.run(
        service.resume(
            interrupted.thread_id,
            ResumeRequest.model_validate(
                {
                    "interrupt_id": "interrupt-1",
                    "idempotency_key": "resume-audit-0001",
                    "decisions": [
                        {
                            "type": "approve",
                            "operation_idempotency_key": "send-audit-0001",
                        }
                    ],
                }
            ),
            observation=context,
        )
    )

    audits = [event for event in sink.events if event["event"] == "write.audit"]
    encoded = json.dumps(audits, ensure_ascii=False)
    assert [event["phase"] for event in audits] == ["approval", "result"]
    assert [event["outcome"] for event in audits] == ["approve", "success"]
    assert all(event["trace_id"] == "trace-audit" for event in audits)
    assert all(event["thread_id"] == interrupted.thread_id for event in audits)
    assert audits[0]["idempotency_hash"] == hash_reference("send-audit-0001")
    assert "receiver@example.com" not in encoded
    assert "项目进展" not in encoded
    assert "审批后的正文" not in encoded
    assert "must-not-leak" not in encoded


def test_resume_rejects_wrong_decision_count_without_advancing_thread() -> None:
    agent = _FakeAgent(interrupt_tool="send_email")
    service = AgentApplicationService(_FakeRuntime(agent))
    interrupted = asyncio.run(service.chat(_chat_request()))

    with pytest.raises(ApiError) as exc_info:
        asyncio.run(
            service.resume(
                interrupted.thread_id,
                ResumeRequest.model_validate(
                    {
                        "interrupt_id": "interrupt-1",
                        "idempotency_key": "resume-request-0002",
                        "decisions": [
                            {
                                "type": "reject",
                                "message": "不发送",
                            },
                            {
                                "type": "reject",
                                "message": "重复决定",
                            },
                        ],
                    }
                ),
            )
        )

    assert exc_info.value.status_code == 400
    assert agent.last_command is None


def test_stream_exposes_message_and_tool_name_but_not_tool_arguments() -> None:
    service = AgentApplicationService(_FakeRuntime(_FakeAgent()))

    async def collect():
        return [event async for event in service.stream_chat(_chat_request())]

    events = asyncio.run(collect())

    assert [event.type for event in events] == [
        StreamEventType.THREAD,
        StreamEventType.MESSAGE,
        StreamEventType.TOOL,
        StreamEventType.COMPLETED,
    ]
    tool_event = next(event for event in events if event.type is StreamEventType.TOOL)
    assert tool_event.data["name"] == "search_emails"
    assert "secret subject" not in str(tool_event.data)


def test_nonempty_attachment_reference_fails_before_agent_execution() -> None:
    agent = _FakeAgent()
    service = AgentApplicationService(_FakeRuntime(agent))
    request = ChatRequest.model_validate(
        {
            "message": "分析附件",
            "idempotency_key": "attachment-request-0001",
            "attachments": [{"file_id": "file-1"}],
        }
    )

    with pytest.raises(ApiError) as exc_info:
        asyncio.run(service.chat(request))

    assert exc_info.value.status_code == 503
    assert agent._states == {}
