"""聊天、审批、线程和统一响应的 HTTP 契约。"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..agents import AgentTaskResult

THREAD_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$"
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
INTERRUPT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$"
FILE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"

DataT = TypeVar("DataT")


class ApiModel(BaseModel):
    """禁止额外字段并清理字符串空白的 API 基础模型。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ApiResponse(ApiModel, Generic[DataT]):
    """统一成功响应。"""

    code: Literal[0] = 0
    message: Literal["success"] = "success"
    data: DataT
    request_id: str
    trace_id: str


class ErrorResponse(ApiModel):
    """不暴露内部实现的统一错误响应。"""

    code: int
    message: str
    data: None = None
    request_id: str
    trace_id: str


class AttachmentReference(ApiModel):
    """仅接受受控文件 ID，不接受客户端或服务器文件路径。"""

    file_id: str = Field(min_length=1, max_length=256, pattern=FILE_ID_PATTERN)
    display_name: str | None = Field(default=None, max_length=255)


class ChatRequest(ApiModel):
    """同步或流式聊天请求。"""

    message: str = Field(min_length=1, max_length=20_000)
    thread_id: str | None = Field(default=None, pattern=THREAD_ID_PATTERN)
    attachments: tuple[AttachmentReference, ...] = Field(default=(), max_length=10)
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=IDEMPOTENCY_KEY_PATTERN,
    )

    @field_validator("attachments")
    @classmethod
    def validate_unique_attachments(
        cls,
        values: tuple[AttachmentReference, ...],
    ) -> tuple[AttachmentReference, ...]:
        """同一请求不能重复引用同一附件。"""
        file_ids = [item.file_id for item in values]
        if len(file_ids) != len(set(file_ids)):
            raise ValueError("attachments 不能包含重复 file_id")
        return values


class ApprovalDecisionType(StrEnum):
    """首期支持的人工审批决策。"""

    APPROVE = "approve"
    EDIT = "edit"
    REJECT = "reject"


class ApprovalDecision(ApiModel):
    """针对一个待审批动作的决定。"""

    type: ApprovalDecisionType
    edited_args: dict[str, Any] | None = Field(default=None, max_length=50)
    message: str | None = Field(default=None, max_length=1000)
    operation_idempotency_key: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        pattern=IDEMPOTENCY_KEY_PATTERN,
    )

    @model_validator(mode="after")
    def validate_decision_fields(self) -> ApprovalDecision:
        """审批、编辑和拒绝使用互不混淆的字段。"""
        if self.type is ApprovalDecisionType.EDIT and self.edited_args is None:
            raise ValueError("edit 决策必须提供 edited_args")
        if self.type is not ApprovalDecisionType.EDIT and self.edited_args is not None:
            raise ValueError("只有 edit 决策可以提供 edited_args")
        if self.type is ApprovalDecisionType.REJECT:
            if self.operation_idempotency_key is not None:
                raise ValueError("reject 决策不能提供 operation_idempotency_key")
        elif self.operation_idempotency_key is None:
            raise ValueError("approve/edit 决策必须提供 operation_idempotency_key")
        if self.type is not ApprovalDecisionType.REJECT and self.message is not None:
            raise ValueError("只有 reject 决策可以提供 message")
        return self


class ResumeRequest(ApiModel):
    """恢复一个指定 interrupt 的审批请求。"""

    interrupt_id: str = Field(min_length=1, max_length=256, pattern=INTERRUPT_ID_PATTERN)
    decisions: tuple[ApprovalDecision, ...] = Field(min_length=1, max_length=20)
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=IDEMPOTENCY_KEY_PATTERN,
    )


class PendingAction(ApiModel):
    """返回给前端审批卡片的待执行动作。"""

    name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any]
    allowed_decisions: tuple[ApprovalDecisionType, ...]


class PendingApproval(ApiModel):
    """一个可由恢复接口处理的 DeepAgents interrupt。"""

    interrupt_id: str
    actions: tuple[PendingAction, ...]


class ThreadStatus(StrEnum):
    """对外公开的线程状态。"""

    IDLE = "idle"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"


class ThreadData(ApiModel):
    """不返回完整消息正文的线程公开状态。"""

    thread_id: str
    status: ThreadStatus
    message_count: int = Field(ge=0)
    pending_approvals: tuple[PendingApproval, ...] = ()
    result: AgentTaskResult | None = None
    updated_at: str | None = None


class ChatData(ThreadData):
    """聊天和恢复接口返回的数据。"""


class DeleteThreadData(ApiModel):
    """线程删除结果。"""

    thread_id: str
    deleted: Literal[True] = True


class ReadyData(ApiModel):
    """就绪检查结果。"""

    status: Literal["ready"]
    agent_runtime: Literal["configured"]
    persistence: Literal["configured"]


class StreamEventType(StrEnum):
    """SSE 对外事件类型。"""

    THREAD = "thread"
    MESSAGE = "message"
    TOOL = "tool"
    APPROVAL_REQUIRED = "approval_required"
    COMPLETED = "completed"
    ERROR = "error"


class StreamEvent(ApiModel):
    """SSE 单个事件的稳定结构。"""

    type: StreamEventType
    thread_id: str
    data: dict[str, Any]
