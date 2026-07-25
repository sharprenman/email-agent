"""结构化脱敏审计、进程内指标与可选追踪接入。"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.:/-]+")
_UPSTREAM_TOOL_NAMES = frozenset(
    {
        "get_mailbox_identity",
        "search_emails",
        "get_email",
        "get_sent_emails",
        "get_unanswered_emails",
        "list_email_attachments",
        "list_contacts",
        "send_email",
        "discover_unsubscribe",
        "execute_unsubscribe",
        "list_calendar_events",
        "create_calendar_event",
        "update_calendar_event",
        "delete_calendar_event",
    }
)


@dataclass(frozen=True)
class ObservationContext:
    """一次请求中允许写入日志的非敏感关联字段。"""

    trace_id: str
    request_id: str
    user_ref: str
    thread_id: str | None = None

    def with_thread(self, thread_id: str) -> "ObservationContext":
        """为上下文补充线程标识。"""
        return ObservationContext(
            trace_id=self.trace_id,
            request_id=self.request_id,
            user_ref=self.user_ref,
            thread_id=thread_id,
        )


@dataclass(frozen=True)
class MetricPoint:
    """一个有界聚合后的指标点。"""

    category: str
    operation: str
    outcome: str
    count: int
    total_duration_ms: float
    max_duration_ms: float
    input_tokens: int
    output_tokens: int


@dataclass
class _MutableMetric:
    count: int = 0
    total_duration_ms: float = 0
    max_duration_ms: float = 0
    input_tokens: int = 0
    output_tokens: int = 0


class AuditSink(Protocol):
    """结构化审计输出端口。"""

    def emit(self, event: Mapping[str, Any]) -> None: ...


class JsonLogAuditSink:
    """将字段白名单事件输出为单行 JSON。"""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("email_agent.audit")
        if logger is None and not self._logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)
            self._logger.propagate = False
        self._logger.setLevel(logging.INFO)

    def emit(self, event: Mapping[str, Any]) -> None:
        self._logger.info(
            json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        )


class MemoryAuditSink:
    """供测试或私有部署内存采集使用的审计端口。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: Mapping[str, Any]) -> None:
        self.events.append(dict(event))


class MetricsRegistry:
    """只保存聚合值，避免标签和调用明细无限增长。"""

    def __init__(self) -> None:
        self._values: dict[tuple[str, str, str], _MutableMetric] = {}
        self._lock = threading.Lock()

    def observe(
        self,
        *,
        category: str,
        operation: str,
        outcome: str,
        duration_ms: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        key = (category, operation, outcome)
        with self._lock:
            metric = self._values.setdefault(key, _MutableMetric())
            metric.count += 1
            metric.total_duration_ms += duration_ms
            metric.max_duration_ms = max(metric.max_duration_ms, duration_ms)
            metric.input_tokens += input_tokens
            metric.output_tokens += output_tokens

    def snapshot(self) -> tuple[MetricPoint, ...]:
        """返回不可变快照，供监控导出器读取。"""
        with self._lock:
            return tuple(
                MetricPoint(
                    category=category,
                    operation=operation,
                    outcome=outcome,
                    count=value.count,
                    total_duration_ms=round(value.total_duration_ms, 3),
                    max_duration_ms=round(value.max_duration_ms, 3),
                    input_tokens=value.input_tokens,
                    output_tokens=value.output_tokens,
                )
                for (category, operation, outcome), value in sorted(self._values.items())
            )


class TraceSink(Protocol):
    """外部追踪端口；默认实现不会发送任何数据。"""

    def start_span(
        self,
        *,
        category: str,
        operation: str,
        attributes: Mapping[str, str],
    ) -> Any: ...

    def finish_span(self, span: Any, *, outcome: str, error_type: str | None) -> None: ...


class NoopTraceSink:
    """未配置外部追踪时使用的空实现。"""

    def start_span(
        self,
        *,
        category: str,
        operation: str,
        attributes: Mapping[str, str],
    ) -> Any:
        del category, operation, attributes
        return nullcontext()

    def finish_span(self, span: Any, *, outcome: str, error_type: str | None) -> None:
        del outcome, error_type
        span.__exit__(None, None, None)


class OpenTelemetryTraceSink:
    """复用已安装 OpenTelemetry SDK 的可选追踪适配器。"""

    def __init__(self, tracer: Any) -> None:
        self._tracer = tracer

    @classmethod
    def from_installed_sdk(cls, service_name: str = "deepagents-email") -> Any:
        """从部署环境加载 OTel；未安装时给出明确配置错误。"""
        try:
            from opentelemetry import trace
        except ImportError as exc:
            raise RuntimeError(
                "启用 OTel 前必须在部署环境安装 opentelemetry-api"
            ) from exc
        return cls(trace.get_tracer(service_name))

    def start_span(
        self,
        *,
        category: str,
        operation: str,
        attributes: Mapping[str, str],
    ) -> Any:
        manager = self._tracer.start_as_current_span(
            f"{category}.{operation}",
            attributes=dict(attributes),
        )
        span = manager.__enter__()
        return manager, span

    def finish_span(self, span: Any, *, outcome: str, error_type: str | None) -> None:
        manager, active_span = span
        active_span.set_attribute("email_agent.outcome", outcome)
        if error_type:
            active_span.set_attribute("error.type", error_type)
        manager.__exit__(None, None, None)


class Observability:
    """统一记录固定字段事件，拒绝接收正文或任意业务载荷。"""

    def __init__(
        self,
        *,
        audit_sink: AuditSink | None = None,
        metrics: MetricsRegistry | None = None,
        trace_sink: TraceSink | None = None,
        user_hash_key: bytes | None = None,
    ) -> None:
        self.audit_sink = audit_sink or JsonLogAuditSink()
        self.metrics = metrics or MetricsRegistry()
        self.trace_sink = trace_sink or NoopTraceSink()
        self._user_hash_key = user_hash_key or secrets.token_bytes(32)

    def context(
        self,
        *,
        user_id: str,
        trace_id: str = "internal",
        request_id: str = "internal",
        thread_id: str | None = None,
    ) -> ObservationContext:
        """构造脱敏上下文，原始用户标识不会进入日志。"""
        return ObservationContext(
            trace_id=trace_id,
            request_id=request_id,
            user_ref=hmac.new(
                self._user_hash_key,
                user_id.encode(),
                hashlib.sha256,
            ).hexdigest()[:16],
            thread_id=thread_id,
        )

    def operation(
        self,
        context: ObservationContext,
        *,
        category: str,
        operation: str,
        agent_name: str | None = None,
        tool_name: str | None = None,
    ) -> "_ObservedOperation":
        """统计并追踪一次操作，异常日志只记录异常类型。"""
        return _ObservedOperation(
            self,
            context,
            category=category,
            operation=operation,
            agent_name=agent_name,
            tool_name=tool_name,
        )

    def record_operation(
        self,
        context: ObservationContext,
        *,
        category: str,
        operation: str,
        outcome: str,
        duration_ms: float,
        agent_name: str | None = None,
        tool_name: str | None = None,
        error_type: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """记录一个不含业务载荷的完成事件。"""
        safe_category = _safe_component(category)
        safe_operation = _safe_component(operation)
        safe_outcome = _safe_component(outcome)
        self.metrics.observe(
            category=safe_category,
            operation=safe_operation,
            outcome=safe_outcome,
            duration_ms=duration_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        self.audit_sink.emit(
            _event(
                context,
                event="operation.completed",
                category=safe_category,
                operation=safe_operation,
                outcome=safe_outcome,
                duration_ms=duration_ms,
                agent_name=agent_name,
                tool_name=tool_name,
                error_type=error_type,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        )

    def record_write_audit(
        self,
        context: ObservationContext,
        *,
        tool_name: str,
        phase: str,
        outcome: str,
        approval_decision: str,
        preview_hash: str,
        idempotency_hash: str | None,
    ) -> None:
        """记录副作用审批与结果，哈希之外不接收动作参数。"""
        self.audit_sink.emit(
            _event(
                context,
                event="write.audit",
                category="approval",
                operation="write",
                outcome=_safe_component(outcome),
                duration_ms=0,
                tool_name=tool_name,
                approval_actor_ref=context.user_ref,
                approval_decision=_safe_component(approval_decision),
                preview_hash=preview_hash,
                idempotency_hash=idempotency_hash,
                phase=_safe_component(phase),
            )
        )
        self.metrics.observe(
            category="approval",
            operation=_safe_component(tool_name),
            outcome=_safe_component(outcome),
            duration_ms=0,
        )

    def callback(self, context: ObservationContext) -> "ObservabilityCallbackHandler":
        """创建只观察名称和耗时的 LangChain 回调。"""
        return ObservabilityCallbackHandler(self, context)


class _ObservedOperation:
    """不改写异常对象的同步计时上下文。"""

    def __init__(
        self,
        observability: Observability,
        context: ObservationContext,
        *,
        category: str,
        operation: str,
        agent_name: str | None,
        tool_name: str | None,
    ) -> None:
        self._observability = observability
        self._context = context
        self._category = category
        self._operation = operation
        self._agent_name = agent_name
        self._tool_name = tool_name
        self._started = 0.0
        self._span: Any = None

    def __enter__(self) -> None:
        self._started = time.perf_counter()
        self._span = self._observability.trace_sink.start_span(
            category=self._category,
            operation=self._operation,
            attributes=_trace_attributes(
                self._context,
                self._agent_name,
                self._tool_name,
            ),
        )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> bool:
        del traceback
        error_type = exc_type.__name__ if exc_type else None
        outcome = "error" if exc is not None else "success"
        self._observability.record_operation(
            self._context,
            category=self._category,
            operation=self._operation,
            outcome=outcome,
            duration_ms=_elapsed_ms(self._started),
            agent_name=self._agent_name,
            tool_name=self._tool_name,
            error_type=error_type,
        )
        self._observability.trace_sink.finish_span(
            self._span,
            outcome=outcome,
            error_type=error_type,
        )
        return False


class ObservabilityCallbackHandler(BaseCallbackHandler):
    """采集模型和工具执行，不读取提示词、参数或输出。"""

    def __init__(self, observability: Observability, context: ObservationContext) -> None:
        self._observability = observability
        self._context = context
        self._runs: dict[str, tuple[str, str, float, Any]] = {}
        self._lock = threading.Lock()

    def on_chat_model_start(
        self,
        serialized: Mapping[str, Any],
        messages: Any,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del messages, kwargs
        self._start(run_id, "model", _serialized_name(serialized, "chat_model"))

    def on_llm_start(
        self,
        serialized: Mapping[str, Any],
        prompts: Any,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del prompts, kwargs
        self._start(run_id, "model", _serialized_name(serialized, "llm"))

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        del kwargs
        input_tokens, output_tokens = _token_usage(response)
        self._finish(
            run_id,
            outcome="success",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        del kwargs
        self._finish(run_id, outcome="error", error_type=type(error).__name__)

    def on_tool_start(
        self,
        serialized: Mapping[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del input_str, kwargs
        self._start(run_id, "tool", _serialized_name(serialized, "tool"))

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        del output, kwargs
        self._finish(run_id, outcome="success")

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        del kwargs
        self._finish(run_id, outcome="error", error_type=type(error).__name__)

    def _start(self, run_id: UUID, category: str, operation: str) -> None:
        span = self._observability.trace_sink.start_span(
            category=category,
            operation=operation,
            attributes=_trace_attributes(
                self._context,
                "email_supervisor" if category == "model" else None,
                operation if category == "tool" else None,
            ),
        )
        with self._lock:
            self._runs[str(run_id)] = (category, operation, time.perf_counter(), span)

    def _finish(
        self,
        run_id: UUID,
        *,
        outcome: str,
        error_type: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        with self._lock:
            run = self._runs.pop(str(run_id), None)
        if run is None:
            return
        category, operation, started, span = run
        self._observability.record_operation(
            self._context,
            category=category,
            operation=operation,
            outcome=outcome,
            duration_ms=_elapsed_ms(started),
            agent_name="email_supervisor" if category == "model" else None,
            tool_name=operation if category == "tool" else None,
            error_type=error_type,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        if category == "tool" and operation in _UPSTREAM_TOOL_NAMES:
            self._observability.record_operation(
                self._context,
                category="upstream",
                operation=operation,
                outcome=outcome,
                duration_ms=_elapsed_ms(started),
                tool_name=operation,
                error_type=error_type,
            )
        self._observability.trace_sink.finish_span(
            span,
            outcome=outcome,
            error_type=error_type,
        )


def hash_reference(value: str) -> str:
    """生成仅用于关联审计记录的单向摘要。"""
    return hashlib.sha256(value.encode()).hexdigest()


def _event(
    context: ObservationContext,
    *,
    event: str,
    category: str,
    operation: str,
    outcome: str,
    duration_ms: float,
    agent_name: str | None = None,
    tool_name: str | None = None,
    error_type: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    approval_actor_ref: str | None = None,
    approval_decision: str | None = None,
    preview_hash: str | None = None,
    idempotency_hash: str | None = None,
    phase: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event": event,
        "category": category,
        "operation": operation,
        "outcome": outcome,
        "duration_ms": round(max(0, duration_ms), 3),
        "trace_id": context.trace_id,
        "request_id": context.request_id,
        "user_ref": context.user_ref,
    }
    optional = {
        "thread_id": context.thread_id,
        "agent_name": _safe_component(agent_name) if agent_name else None,
        "tool_name": _safe_component(tool_name) if tool_name else None,
        "error_type": _safe_component(error_type) if error_type else None,
        "approval_actor_ref": approval_actor_ref,
        "approval_decision": approval_decision,
        "preview_hash": preview_hash,
        "idempotency_hash": idempotency_hash,
        "phase": phase,
        "input_tokens": input_tokens or None,
        "output_tokens": output_tokens or None,
    }
    payload.update({key: value for key, value in optional.items() if value is not None})
    return payload


def _trace_attributes(
    context: ObservationContext,
    agent_name: str | None,
    tool_name: str | None,
) -> dict[str, str]:
    attributes = {
        "email_agent.trace_id": context.trace_id,
        "email_agent.request_id": context.request_id,
        "email_agent.user_ref": context.user_ref,
    }
    if context.thread_id:
        attributes["email_agent.thread_id"] = context.thread_id
    if agent_name:
        attributes["email_agent.agent_name"] = _safe_component(agent_name)
    if tool_name:
        attributes["email_agent.tool_name"] = _safe_component(tool_name)
    return attributes


def _serialized_name(serialized: Mapping[str, Any], fallback: str) -> str:
    name = serialized.get("name")
    if not name:
        identifier = serialized.get("id")
        if isinstance(identifier, list) and identifier:
            name = identifier[-1]
    return _safe_component(str(name or fallback))


def _safe_component(value: str) -> str:
    cleaned = _SAFE_NAME.sub("_", value.strip())
    return cleaned[:128] or "unknown"


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


def _token_usage(response: Any) -> tuple[int, int]:
    llm_output = getattr(response, "llm_output", None)
    if isinstance(llm_output, Mapping):
        usage = llm_output.get("token_usage") or llm_output.get("usage")
        if isinstance(usage, Mapping):
            input_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
            output_tokens = (
                usage.get("completion_tokens") or usage.get("output_tokens") or 0
            )
            return _safe_token_count(input_tokens), _safe_token_count(output_tokens)
    generations = getattr(response, "generations", ()) or ()
    for batch in generations:
        for generation in batch:
            message = getattr(generation, "message", None)
            usage = getattr(message, "usage_metadata", None)
            if isinstance(usage, Mapping):
                return (
                    _safe_token_count(usage.get("input_tokens")),
                    _safe_token_count(usage.get("output_tokens")),
                )
    return 0, 0


def _safe_token_count(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0
