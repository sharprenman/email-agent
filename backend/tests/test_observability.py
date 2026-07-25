"""可观测性脱敏、指标与回调测试。"""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from email_agent.observability import (
    MemoryAuditSink,
    MetricsRegistry,
    Observability,
    hash_reference,
)


def _observability() -> tuple[Observability, MemoryAuditSink, MetricsRegistry]:
    sink = MemoryAuditSink()
    metrics = MetricsRegistry()
    return Observability(audit_sink=sink, metrics=metrics), sink, metrics


class _TraceSink:
    def __init__(self) -> None:
        self.started = []
        self.finished = []

    def start_span(self, **payload):
        self.started.append(payload)
        return "span-1"

    def finish_span(self, span, **payload) -> None:
        self.finished.append((span, payload))


def test_operation_logs_fixed_fields_without_exception_message() -> None:
    observability, sink, metrics = _observability()
    context = observability.context(
        user_id="owner@example.com",
        request_id="request-1",
        trace_id="trace-1",
        thread_id="th_12345678",
    )

    with pytest.raises(RuntimeError):
        with observability.operation(
            context,
            category="agent",
            operation="chat",
            agent_name="email_supervisor",
        ):
            raise RuntimeError(
                "receiver@example.com 机密主题 正文内容 oauth-token model-api-key"
            )

    encoded = json.dumps(sink.events, ensure_ascii=False)
    assert "owner@example.com" not in encoded
    assert "receiver@example.com" not in encoded
    assert "机密主题" not in encoded
    assert "正文内容" not in encoded
    assert "oauth-token" not in encoded
    assert "model-api-key" not in encoded
    assert sink.events[0]["trace_id"] == "trace-1"
    assert sink.events[0]["thread_id"] == "th_12345678"
    assert sink.events[0]["error_type"] == "RuntimeError"
    assert metrics.snapshot()[0].outcome == "error"


def test_langchain_callback_ignores_inputs_outputs_and_counts_tokens() -> None:
    observability, sink, metrics = _observability()
    context = observability.context(
        user_id="owner",
        request_id="request-2",
        trace_id="trace-2",
        thread_id="th_abcdefgh",
    )
    callback = observability.callback(context)

    model_run = uuid4()
    callback.on_chat_model_start(
        {"name": "gpt-5.1"},
        [["邮件正文和模型密钥"]],
        run_id=model_run,
    )
    callback.on_llm_end(
        SimpleNamespace(
            llm_output={
                "token_usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 5,
                }
            }
        ),
        run_id=model_run,
    )

    tool_run = uuid4()
    callback.on_tool_start(
        {"name": "send_email"},
        "receiver@example.com 机密正文 approval-token",
        run_id=tool_run,
    )
    callback.on_tool_end(
        {"message_id": "secret-provider-result"},
        run_id=tool_run,
    )

    encoded = json.dumps(sink.events, ensure_ascii=False)
    assert "邮件正文" not in encoded
    assert "receiver@example.com" not in encoded
    assert "approval-token" not in encoded
    assert "secret-provider-result" not in encoded
    assert {event["category"] for event in sink.events} == {
        "model",
        "tool",
        "upstream",
    }
    model_metric = next(point for point in metrics.snapshot() if point.category == "model")
    assert model_metric.input_tokens == 12
    assert model_metric.output_tokens == 5
    assert any(point.category == "upstream" for point in metrics.snapshot())


def test_write_audit_only_accepts_hashed_references() -> None:
    observability, sink, metrics = _observability()
    context = observability.context(
        user_id="approver@example.com",
        request_id="request-3",
        trace_id="trace-3",
        thread_id="th_write123",
    )

    observability.record_write_audit(
        context,
        tool_name="send_email",
        phase="approval",
        outcome="approve",
        approval_decision="approve",
        preview_hash=hash_reference("receiver@example.com|主题|正文"),
        idempotency_hash=hash_reference("send-operation-0001"),
    )

    event = sink.events[0]
    encoded = json.dumps(event, ensure_ascii=False)
    assert event["event"] == "write.audit"
    assert len(event["approval_actor_ref"]) == 16
    assert len(event["preview_hash"]) == 64
    assert len(event["idempotency_hash"]) == 64
    assert "receiver@example.com" not in encoded
    assert "主题" not in encoded
    assert "正文" not in encoded
    assert metrics.snapshot()[0].category == "approval"


def test_optional_trace_sink_receives_only_safe_attributes() -> None:
    sink = MemoryAuditSink()
    trace_sink = _TraceSink()
    observability = Observability(
        audit_sink=sink,
        trace_sink=trace_sink,
        user_hash_key=b"x" * 32,
    )
    context = observability.context(
        user_id="owner@example.com",
        request_id="request-4",
        trace_id="trace-4",
        thread_id="th_trace123",
    )

    with observability.operation(
        context,
        category="agent",
        operation="chat",
        agent_name="email_supervisor",
    ):
        pass

    encoded = json.dumps(trace_sink.started, ensure_ascii=False)
    assert trace_sink.finished == [
        ("span-1", {"outcome": "success", "error_type": None})
    ]
    assert "owner@example.com" not in encoded
    assert "trace-4" in encoded
    assert "th_trace123" in encoded
