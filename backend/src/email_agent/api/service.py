"""Agent HTTP 接口的应用服务。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command, Interrupt
from pydantic import ValidationError

from ..agents import AgentTaskResult, AgentTaskStatus, EmailAgentRuntime, mail_approval_payload
from ..calendar import ApprovalAction
from ..content_tools import UnsubscribeCandidate, UnsubscribeMethod
from ..contracts import CalendarEventInput, SendEmailRequest
from ..files import UploadedFileError, UploadedFileService
from ..observability import Observability, ObservationContext, hash_reference
from .errors import (
    bad_request,
    conflict,
    forbidden,
    gateway_timeout,
    not_found,
    service_unavailable,
)
from .schemas import (
    ApprovalDecision,
    ApprovalDecisionType,
    ChatData,
    ChatRequest,
    DeleteFileData,
    DeleteThreadData,
    PendingAction,
    PendingApproval,
    ResumeRequest,
    StreamEvent,
    StreamEventType,
    ThreadData,
    ThreadStatus,
    UploadedFileData,
)

_SENSITIVE_ARGUMENT_PARTS = ("token", "secret", "password", "credential")
_EXTERNAL_APPROVAL_TOOLS = frozenset(
    {
        "send_email",
        "execute_unsubscribe",
        "create_calendar_event",
        "update_calendar_event",
        "delete_calendar_event",
    }
)
_MEMORY_APPROVAL_TOOL = "save_user_memory"
_INTERNAL_APPROVAL_TOOLS = frozenset(
    {_MEMORY_APPROVAL_TOOL, "initialize_crm", "update_crm_contact"}
)
_WRITE_APPROVAL_TOOLS = _EXTERNAL_APPROVAL_TOOLS | _INTERNAL_APPROVAL_TOOLS


@dataclass(frozen=True)
class _WriteAudit:
    tool_name: str
    approval_decision: str
    preview_hash: str
    idempotency_hash: str | None


class _ThreadLockContext:
    """组合进程锁与数据库 advisory lock，不改写业务异常。"""

    def __init__(self, service: AgentApplicationService, thread_id: str) -> None:
        self._local = service._thread_locks.setdefault(thread_id, asyncio.Lock())
        self._database = service._runtime.persistence.state.thread_lock(thread_id)

    async def __aenter__(self) -> None:
        await self._local.acquire()
        try:
            await asyncio.to_thread(self._database.__enter__)
        except Exception:
            self._local.release()
            raise

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        del exc_type, exc, traceback
        try:
            await asyncio.to_thread(self._database.__exit__, None, None, None)
        finally:
            self._local.release()
        return False


class AgentApplicationService:
    """编排线程所有权、Agent 执行、审批恢复和持久化。"""

    def __init__(
        self,
        runtime: EmailAgentRuntime,
        *,
        timeout_seconds: float = 120,
        observability: Observability | None = None,
        uploaded_files: UploadedFileService | None = None,
    ) -> None:
        if not 1 <= timeout_seconds <= 600:
            raise ValueError("Agent 超时必须在 1 到 600 秒之间")
        self._runtime = runtime
        self._timeout_seconds = timeout_seconds
        self._observability = observability or Observability()
        self._uploaded_files = uploaded_files
        self._thread_locks: dict[str, asyncio.Lock] = {}

    @property
    def user_id(self) -> str:
        """返回该服务绑定的可信用户。"""
        return self._runtime.auth.user_id

    @property
    def observability(self) -> Observability:
        """返回 API 和 Agent 共用的可观测性实例。"""
        return self._observability

    def is_ready(self) -> bool:
        """检查应用服务已装配 Agent、Checkpointer 和 Store。"""
        return all(
            (
                self._runtime.agent is not None,
                self._runtime.persistence.checkpointer is not None,
                self._runtime.persistence.store is not None,
            )
        )

    async def check_ready(self) -> bool:
        """确认运行时结构和数据库连接均可用。"""
        if not self.is_ready():
            return False
        try:
            await asyncio.to_thread(self._runtime.persistence.state.health_check)
        except Exception:
            return False
        return True

    async def chat(
        self,
        request: ChatRequest,
        *,
        observation: ObservationContext | None = None,
    ) -> ChatData:
        """执行一次同步 Agent 对话，并返回完成或待审批状态。"""
        message = await self._build_human_message(request)
        thread_id, is_new = await self._prepare_thread(request.thread_id)
        context = self._context(observation, thread_id)
        reservation = await self._reserve_idempotency(
            "chat",
            request.idempotency_key,
            thread_id=thread_id,
            fingerprint=_fingerprint(request.model_dump(mode="json")),
        )
        try:
            with self._observability.operation(
                context,
                category="agent",
                operation="chat",
                agent_name="email_supervisor",
            ):
                async with self._thread_lock(thread_id):
                    async with asyncio.timeout(self._timeout_seconds):
                        await self._runtime.agent.ainvoke(
                            self._runtime.prepare_input(
                                {
                                    "messages": [
                                        HumanMessage(
                                            content=message,
                                            additional_kwargs={
                                                "request_idempotency_key": (
                                                    request.idempotency_key
                                                )
                                            },
                                        )
                                    ]
                                }
                            ),
                            self._agent_config(context),
                            context=self._runtime.context,
                        )
                result = ChatData.model_validate(
                    (
                        await self.get_thread(
                            thread_id,
                            observation=context,
                        )
                    ).model_dump(mode="json")
                )
                if result.status is ThreadStatus.INTERRUPTED:
                    self._record_approval_wait(context, result)
                return result
        except TimeoutError as exc:
            await self._release_idempotency(reservation)
            await self._cleanup_failed_new_thread(thread_id, is_new)
            raise gateway_timeout() from exc
        except Exception:
            await self._release_idempotency(reservation)
            await self._cleanup_failed_new_thread(thread_id, is_new)
            raise

    async def stream_chat(
        self,
        request: ChatRequest,
        *,
        observation: ObservationContext | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """以稳定且脱敏的事件结构流式执行 Agent。"""
        message = await self._build_human_message(request)
        thread_id, is_new = await self._prepare_thread(request.thread_id)
        context = self._context(observation, thread_id)
        reservation = await self._reserve_idempotency(
            "stream",
            request.idempotency_key,
            thread_id=thread_id,
            fingerprint=_fingerprint(request.model_dump(mode="json")),
        )
        yield StreamEvent(
            type=StreamEventType.THREAD,
            thread_id=thread_id,
            data={"status": "started"},
        )
        emitted_messages: set[str] = set()
        emitted_tools: set[str] = set()
        try:
            with self._observability.operation(
                context,
                category="agent",
                operation="stream_chat",
                agent_name="email_supervisor",
            ):
                async with self._thread_lock(thread_id):
                    async with asyncio.timeout(self._timeout_seconds):
                        async for raw_event in self._runtime.agent.astream(
                            self._runtime.prepare_input(
                                {
                                    "messages": [
                                        HumanMessage(
                                            content=message,
                                            additional_kwargs={
                                                "request_idempotency_key": (
                                                    request.idempotency_key
                                                )
                                            },
                                        )
                                    ]
                                }
                            ),
                            self._agent_config(context),
                            context=self._runtime.context,
                            stream_mode=["messages", "values"],
                            version="v2",
                        ):
                            for event in _translate_graph_event(
                                raw_event,
                                thread_id=thread_id,
                                emitted_messages=emitted_messages,
                                emitted_tools=emitted_tools,
                            ):
                                yield event

                state = await self.get_thread(thread_id, observation=context)
                terminal_type = (
                    StreamEventType.APPROVAL_REQUIRED
                    if state.status is ThreadStatus.INTERRUPTED
                    else StreamEventType.COMPLETED
                )
                if state.status is ThreadStatus.INTERRUPTED:
                    self._record_approval_wait(context, state)
                yield StreamEvent(
                    type=terminal_type,
                    thread_id=thread_id,
                    data=state.model_dump(mode="json"),
                )
        except TimeoutError as exc:
            await self._release_idempotency(reservation)
            await self._cleanup_failed_new_thread(thread_id, is_new)
            raise gateway_timeout() from exc
        except Exception:
            await self._release_idempotency(reservation)
            await self._cleanup_failed_new_thread(thread_id, is_new)
            raise

    async def resume(
        self,
        thread_id: str,
        request: ResumeRequest,
        *,
        observation: ObservationContext | None = None,
    ) -> ChatData:
        """校验线程和 interrupt 后恢复人工审批。"""
        await self._assert_thread_owner(thread_id)
        context = self._context(observation, thread_id)
        reservation = await self._reserve_idempotency(
            "resume",
            request.idempotency_key,
            thread_id=thread_id,
            fingerprint=_fingerprint(request.model_dump(mode="json")),
        )
        write_audits: tuple[_WriteAudit, ...] = ()
        try:
            with self._observability.operation(
                context,
                category="agent",
                operation="resume",
                agent_name="email_supervisor",
            ):
                async with self._thread_lock(thread_id):
                    snapshot = await self._runtime.agent.aget_state(_thread_config(thread_id))
                    pending = _pending_approvals(snapshot.interrupts)
                    selected = next(
                        (
                            approval
                            for approval in pending
                            if approval.interrupt_id == request.interrupt_id
                        ),
                        None,
                    )
                    if selected is None:
                        raise conflict("指定 interrupt 不存在或已经处理")
                    if len(selected.actions) != len(request.decisions):
                        raise bad_request("decisions 数量必须与待审批动作数量一致")
                    wait_duration_ms = _approval_wait_ms(snapshot.created_at)
                    for action in selected.actions:
                        self._observability.record_operation(
                            context,
                            category="approval",
                            operation=action.name,
                            outcome="resumed",
                            duration_ms=wait_duration_ms,
                            tool_name=action.name,
                        )

                    raw_interrupt = next(
                        item
                        for item in snapshot.interrupts
                        if item.id == request.interrupt_id
                    )
                    decisions, write_audits = self._build_resume_decisions(
                        raw_interrupt,
                        request.decisions,
                    )
                    for audit in write_audits:
                        self._record_write_audit(
                            context,
                            audit,
                            phase="approval",
                            outcome=audit.approval_decision,
                        )
                    async with asyncio.timeout(self._timeout_seconds):
                        await self._runtime.agent.ainvoke(
                            Command(
                                resume={
                                    request.interrupt_id: {
                                        "decisions": decisions,
                                    }
                                }
                            ),
                            self._agent_config(context),
                            context=self._runtime.context,
                        )
                result = ChatData.model_validate(
                    (
                        await self.get_thread(
                            thread_id,
                            observation=context,
                        )
                    ).model_dump(mode="json")
                )
                for audit in write_audits:
                    outcome = (
                        "not_executed"
                        if audit.approval_decision
                        == ApprovalDecisionType.REJECT.value
                        else "success"
                    )
                    self._record_write_audit(
                        context,
                        audit,
                        phase="result",
                        outcome=outcome,
                    )
                return result
        except TimeoutError as exc:
            self._record_failed_write_audits(context, write_audits, "timeout")
            await self._release_idempotency(reservation)
            raise gateway_timeout() from exc
        except Exception:
            self._record_failed_write_audits(context, write_audits, "error")
            await self._release_idempotency(reservation)
            raise

    async def get_thread(
        self,
        thread_id: str,
        *,
        observation: ObservationContext | None = None,
    ) -> ThreadData:
        """读取当前可信用户可见的线程公开状态。"""
        del observation
        await self._assert_thread_owner(thread_id)
        snapshot = await self._runtime.agent.aget_state(_thread_config(thread_id))
        values = snapshot.values if isinstance(snapshot.values, Mapping) else {}
        pending = _pending_approvals(snapshot.interrupts)
        result = _extract_result(values)
        if pending:
            status = ThreadStatus.INTERRUPTED
        elif values:
            status = ThreadStatus.COMPLETED
        else:
            status = ThreadStatus.IDLE
        messages = values.get("messages", ())
        return ThreadData(
            thread_id=thread_id,
            status=status,
            message_count=len(messages) if isinstance(messages, Sequence) else 0,
            pending_approvals=pending,
            result=result,
            updated_at=snapshot.created_at,
        )

    async def delete_thread(
        self,
        thread_id: str,
        *,
        observation: ObservationContext | None = None,
    ) -> DeleteThreadData:
        """只删除指定线程检查点，不删除用户长期记忆。"""
        context = self._context(observation, thread_id)
        await self._assert_thread_owner(thread_id)
        with self._observability.operation(
            context,
            category="storage",
            operation="delete_thread",
        ):
            async with self._thread_lock(thread_id):
                await self._runtime.persistence.checkpointer.adelete_thread(thread_id)
                await asyncio.to_thread(
                    self._runtime.persistence.state.delete_thread,
                    thread_id,
                    self.user_id,
                )
        self._thread_locks.pop(thread_id, None)
        return DeleteThreadData(thread_id=thread_id)

    async def upload_file(
        self,
        *,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> UploadedFileData:
        if self._uploaded_files is None:
            raise service_unavailable("受控文件服务尚未装配")
        try:
            record = await self._uploaded_files.upload(filename, content_type, content)
        except UploadedFileError as exc:
            raise bad_request(str(exc)) from exc
        return UploadedFileData(
            file_id=record.file_id,
            filename=record.filename,
            content_type=record.content_type,
            size_bytes=record.size_bytes,
            truncated=record.truncated,
            expires_at=record.expires_at,
        )

    async def delete_file(self, file_id: str) -> DeleteFileData:
        if self._uploaded_files is None:
            raise service_unavailable("受控文件服务尚未装配")
        if not await self._uploaded_files.delete(file_id):
            raise not_found("上传文件不存在、已过期或无权访问")
        return DeleteFileData(file_id=file_id)

    def _build_resume_decisions(
        self,
        interrupt: Interrupt,
        decisions: Sequence[ApprovalDecision],
    ) -> tuple[list[dict[str, Any]], tuple[_WriteAudit, ...]]:
        value = interrupt.value if isinstance(interrupt.value, Mapping) else {}
        actions = value.get("action_requests", ())
        configs = value.get("review_configs", ())
        if not isinstance(actions, Sequence) or len(actions) != len(decisions):
            raise bad_request("interrupt 数据与 decisions 不匹配")

        built: list[dict[str, Any]] = []
        audits: list[_WriteAudit] = []
        for action, decision in zip(actions, decisions, strict=True):
            if not isinstance(action, Mapping):
                raise bad_request("interrupt 动作格式无效")
            name = str(action.get("name") or "")
            original_args = action.get("args")
            if not isinstance(original_args, Mapping):
                raise bad_request("interrupt 动作参数无效")
            if decision.type not in _allowed_decisions(name, configs):
                raise bad_request("审批决策不在该动作允许范围内")
            if decision.type is ApprovalDecisionType.REJECT:
                payload: dict[str, Any] = {"type": "reject"}
                if decision.message:
                    payload["message"] = decision.message
                built.append(payload)
                if name in _WRITE_APPROVAL_TOOLS:
                    audits.append(
                        _WriteAudit(
                            tool_name=name,
                            approval_decision=decision.type.value,
                            preview_hash=_fingerprint(original_args),
                            idempotency_hash=None,
                        )
                    )
                continue

            effective_args = (
                dict(decision.edited_args)
                if decision.type is ApprovalDecisionType.EDIT
                else dict(original_args)
            )
            operation_key = decision.operation_idempotency_key
            if operation_key is None:
                raise bad_request("批准操作缺少 operation_idempotency_key")
            preview_hash = _fingerprint(effective_args)
            try:
                effective_args = self._inject_approval(
                    name,
                    effective_args,
                    operation_key,
                )
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                raise bad_request("审批动作参数无效") from exc
            built.append(
                {
                    "type": "edit",
                    "edited_action": {
                        "name": name,
                        "args": effective_args,
                    },
                }
            )
            if name in _WRITE_APPROVAL_TOOLS:
                audits.append(
                    _WriteAudit(
                        tool_name=name,
                        approval_decision=decision.type.value,
                        preview_hash=preview_hash,
                        idempotency_hash=hash_reference(operation_key),
                    )
                )
        return built, tuple(audits)

    def _inject_approval(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        arguments.pop("approval_token", None)
        if tool_name in _INTERNAL_APPROVAL_TOOLS:
            return arguments
        if tool_name not in _EXTERNAL_APPROVAL_TOOLS:
            raise ValueError("不支持的审批工具")

        arguments["idempotency_key"] = idempotency_key
        action, target_id, payload = _approval_claim(tool_name, arguments)
        arguments["approval_token"] = self._runtime.approvals.mint_after_interrupt(
            self._runtime.auth,
            action=action,
            target_id=target_id,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        return arguments

    async def _prepare_thread(self, requested_thread_id: str | None) -> tuple[str, bool]:
        if requested_thread_id is not None:
            await self._assert_thread_owner(requested_thread_id)
            return requested_thread_id, False
        thread_id = f"th_{uuid.uuid4().hex}"
        await asyncio.to_thread(
            self._runtime.persistence.state.create_thread,
            thread_id,
            self.user_id,
        )
        return thread_id, True

    async def _assert_thread_owner(self, thread_id: str) -> None:
        owner = await asyncio.to_thread(
            self._runtime.persistence.state.get_thread_owner,
            thread_id,
        )
        if owner is None:
            raise not_found("线程不存在")
        if owner != self.user_id:
            raise forbidden("无权访问该线程")

    async def _reserve_idempotency(
        self,
        operation: str,
        key: str,
        *,
        thread_id: str,
        fingerprint: str,
    ) -> tuple[str, str]:
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        reserved = await asyncio.to_thread(
            self._runtime.persistence.state.reserve_idempotency,
            self.user_id,
            operation,
            key_hash,
            thread_id,
            fingerprint,
        )
        if not reserved:
            raise conflict("幂等键已经使用，不能重复执行")
        return operation, key_hash

    async def _release_idempotency(
        self,
        reservation: tuple[str, str],
    ) -> None:
        operation, key_hash = reservation
        await asyncio.to_thread(
            self._runtime.persistence.state.release_idempotency,
            self.user_id,
            operation,
            key_hash,
        )

    async def _cleanup_failed_new_thread(self, thread_id: str, is_new: bool) -> None:
        if not is_new:
            return
        await self._runtime.persistence.checkpointer.adelete_thread(thread_id)
        await asyncio.to_thread(
            self._runtime.persistence.state.delete_thread,
            thread_id,
            self.user_id,
        )

    def _thread_lock(self, thread_id: str) -> _ThreadLockContext:
        return _ThreadLockContext(self, thread_id)

    def _context(
        self,
        observation: ObservationContext | None,
        thread_id: str,
    ) -> ObservationContext:
        if observation is not None:
            return observation.with_thread(thread_id)
        return self._observability.context(
            user_id=self.user_id,
            thread_id=thread_id,
        )

    def _agent_config(self, context: ObservationContext) -> dict[str, Any]:
        """向 LangChain 回调链注入脱敏关联字段。

        部署环境若启用 LangSmith 标准追踪，会自动复用同一回调链和 metadata。
        """
        return {
            "configurable": {"thread_id": context.thread_id},
            "callbacks": [self._observability.callback(context)],
            "metadata": {
                "trace_id": context.trace_id,
                "request_id": context.request_id,
                "user_ref": context.user_ref,
                "thread_id": context.thread_id,
            },
        }

    def _record_approval_wait(
        self,
        context: ObservationContext,
        thread: ThreadData,
    ) -> None:
        for approval in thread.pending_approvals:
            for action in approval.actions:
                self._observability.record_operation(
                    context,
                    category="approval",
                    operation=action.name,
                    outcome="waiting",
                    duration_ms=0,
                    tool_name=action.name,
                )

    def _record_write_audit(
        self,
        context: ObservationContext,
        audit: _WriteAudit,
        *,
        phase: str,
        outcome: str,
    ) -> None:
        self._observability.record_write_audit(
            context,
            tool_name=audit.tool_name,
            phase=phase,
            outcome=outcome,
            approval_decision=audit.approval_decision,
            preview_hash=audit.preview_hash,
            idempotency_hash=audit.idempotency_hash,
        )

    def _record_failed_write_audits(
        self,
        context: ObservationContext,
        audits: Sequence[_WriteAudit],
        outcome: str,
    ) -> None:
        for audit in audits:
            if audit.approval_decision != ApprovalDecisionType.REJECT.value:
                self._record_write_audit(
                    context,
                    audit,
                    phase="result",
                    outcome=outcome,
                )

    async def _build_human_message(self, request: ChatRequest) -> str:
        if not request.attachments:
            return request.message
        if self._uploaded_files is None:
            raise service_unavailable("受控文件服务尚未装配")
        try:
            context = await self._uploaded_files.build_context(
                tuple(item.file_id for item in request.attachments)
            )
        except UploadedFileError as exc:
            raise not_found(str(exc)) from exc
        return request.message + context


def _thread_config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _fingerprint(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _approval_wait_ms(created_at: str | None) -> float:
    if not created_at:
        return 0
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return 0
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return max(0, (datetime.now(UTC) - created).total_seconds() * 1000)


def _approval_claim(
    tool_name: str,
    arguments: Mapping[str, Any],
) -> tuple[ApprovalAction, str | None, Mapping[str, Any]]:
    if tool_name == "send_email":
        request = SendEmailRequest(
            to=tuple(arguments["to"]),
            subject=arguments["subject"],
            body=arguments["body"],
            cc=tuple(arguments.get("cc") or ()),
            bcc=tuple(arguments.get("bcc") or ()),
            reply_to_email_id=arguments.get("reply_to_email_id"),
        )
        return (
            ApprovalAction.SEND_EMAIL,
            request.reply_to_email_id,
            mail_approval_payload(request),
        )
    if tool_name in {"create_calendar_event", "update_calendar_event"}:
        event = CalendarEventInput.model_validate(arguments["event"])
        action = (
            ApprovalAction.CREATE
            if tool_name == "create_calendar_event"
            else ApprovalAction.UPDATE
        )
        target = None if action is ApprovalAction.CREATE else str(arguments["event_id"])
        return action, target, event.model_dump(mode="json")
    if tool_name == "delete_calendar_event":
        return ApprovalAction.DELETE, str(arguments["event_id"]), {}
    if tool_name == "execute_unsubscribe":
        candidate = UnsubscribeCandidate.model_validate(arguments["candidate"])
        if candidate.method is UnsubscribeMethod.ONE_CLICK:
            action = ApprovalAction.UNSUBSCRIBE_ONE_CLICK
        elif candidate.method is UnsubscribeMethod.MAILTO:
            action = ApprovalAction.UNSUBSCRIBE_MAILTO
        else:
            raise ValueError("该退订方式不能自动执行")
        return action, candidate.fingerprint, candidate.model_dump(mode="json")
    raise ValueError("不支持的审批工具")


def _pending_approvals(interrupts: Sequence[Interrupt]) -> tuple[PendingApproval, ...]:
    pending: list[PendingApproval] = []
    for interrupt in interrupts:
        value = interrupt.value if isinstance(interrupt.value, Mapping) else {}
        requests = value.get("action_requests", ())
        configs = value.get("review_configs", ())
        if not isinstance(requests, Sequence) or not isinstance(configs, Sequence):
            continue
        actions = []
        for request in requests:
            if not isinstance(request, Mapping):
                continue
            name = str(request.get("name") or "")
            raw_arguments = request.get("args")
            if not name or not isinstance(raw_arguments, Mapping):
                continue
            allowed = _allowed_decisions(name, configs)
            actions.append(
                PendingAction(
                    name=name,
                    arguments=_sanitize_arguments(raw_arguments),
                    allowed_decisions=allowed,
                )
            )
        if actions:
            pending.append(
                PendingApproval(
                    interrupt_id=interrupt.id,
                    actions=tuple(actions),
                )
            )
    return tuple(pending)


def _allowed_decisions(
    action_name: str,
    configs: Sequence[Any],
) -> tuple[ApprovalDecisionType, ...]:
    for config in configs:
        if not isinstance(config, Mapping) or config.get("action_name") != action_name:
            continue
        return tuple(
            ApprovalDecisionType(item)
            for item in config.get("allowed_decisions", ())
            if item in {decision.value for decision in ApprovalDecisionType}
        )
    return ()


def _sanitize_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in arguments.items():
        lowered = str(key).casefold()
        if any(part in lowered for part in _SENSITIVE_ARGUMENT_PARTS):
            continue
        if isinstance(value, Mapping):
            sanitized[str(key)] = _sanitize_arguments(value)
        elif isinstance(value, list):
            sanitized[str(key)] = [
                _sanitize_arguments(item) if isinstance(item, Mapping) else item
                for item in value
            ]
        else:
            sanitized[str(key)] = value
    return sanitized


def _extract_result(values: Mapping[str, Any]) -> AgentTaskResult | None:
    structured = values.get("structured_response")
    if structured is not None:
        try:
            return AgentTaskResult.model_validate(structured)
        except ValidationError:
            return None
    messages = values.get("messages")
    if not isinstance(messages, Sequence):
        return None
    last_ai = next(
        (message for message in reversed(messages) if isinstance(message, AIMessage)),
        None,
    )
    text = _message_text(last_ai) if last_ai is not None else ""
    if not text:
        return None
    return AgentTaskResult(
        status=AgentTaskStatus.SUCCESS,
        summary=text[:5000],
    )


def _translate_graph_event(
    raw_event: Any,
    *,
    thread_id: str,
    emitted_messages: set[str],
    emitted_tools: set[str],
) -> tuple[StreamEvent, ...]:
    if not isinstance(raw_event, Mapping) or raw_event.get("type") != "messages":
        return ()
    data = raw_event.get("data")
    message = data[0] if isinstance(data, tuple) and data else None
    if not isinstance(message, AIMessage):
        return ()

    events: list[StreamEvent] = []
    message_id = str(message.id or "")
    text = _message_text(message)
    if text and message_id not in emitted_messages:
        emitted_messages.add(message_id)
        events.append(
            StreamEvent(
                type=StreamEventType.MESSAGE,
                thread_id=thread_id,
                data={"message_id": message_id, "content": text},
            )
        )
    for tool_call in message.tool_calls:
        call_id = str(tool_call.get("id") or "")
        if call_id in emitted_tools:
            continue
        emitted_tools.add(call_id)
        events.append(
            StreamEvent(
                type=StreamEventType.TOOL,
                thread_id=thread_id,
                data={
                    "call_id": call_id,
                    "name": str(tool_call.get("name") or ""),
                    "status": "requested",
                },
            )
        )
    return tuple(events)


def _message_text(message: AIMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, Mapping) and block.get("type") == "text"
        ).strip()
    return ""
