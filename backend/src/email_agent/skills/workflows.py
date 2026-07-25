"""为九类 Skill 生成确定性的查询、窗口和工具组合。"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from pydantic import Field

from ..contracts import ContractModel

URGENT_QUERY_TERMS = (
    "urgent",
    "asap",
    "critical",
    "priority",
    '"action required"',
    '"security alert"',
    "deadline",
    "overdue",
)
BUG_QUERY_TERMS = (
    "bug",
    "defect",
    "regression",
    '"build failed"',
    '"failing tests"',
    "incident",
    "outage",
    "blocker",
)
RESUME_QUERY_TERMS = (
    "candidate",
    "applicant",
    "application",
    "resume",
    "cv",
    "portfolio",
    '"cover letter"',
)
UNSUBSCRIBE_QUERY_TERMS = (
    "unsubscribe",
    "newsletter",
    "subscription",
    '"manage preferences"',
    "marketing",
)

SKILL_DELEGATED_TOOLS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "weekly-email-summary": frozenset(
            {
                "get_mailbox_identity",
                "search_emails",
                "get_unanswered_emails",
                "list_calendar_events",
            }
        ),
        "urgent-email-triage": frozenset({"search_emails", "get_email", "get_unanswered_emails"}),
        "bug-issue-triage": frozenset({"search_emails", "get_email"}),
        "resume-candidate-review": frozenset(
            {
                "search_emails",
                "list_email_attachments",
                "extract_attachment_text",
            }
        ),
        "draft-reply-from-email-context": frozenset(
            {
                "search_emails",
                "get_unanswered_emails",
                "get_email",
                "prepare_email_draft",
            }
        ),
        "send-prepared-email": frozenset({"send_email"}),
        "unsubscribe-discovery": frozenset(
            {"search_emails", "get_email", "discover_email_unsubscribe"}
        ),
        "unsubscribe-execute": frozenset(
            {
                "search_emails",
                "get_email",
                "discover_email_unsubscribe",
                "execute_unsubscribe",
            }
        ),
        "writing-style-profile": frozenset({"get_sent_emails"}),
    }
)

_WINDOWS: Mapping[str, tuple[int | None, int | None, int]] = MappingProxyType(
    {
        "weekly-email-summary": (7, 30, 100),
        "urgent-email-triage": (7, 7, 100),
        "bug-issue-triage": (7, 7, 100),
        "resume-candidate-review": (7, 7, 100),
        "draft-reply-from-email-context": (30, 30, 20),
        "send-prepared-email": (None, None, 1),
        "unsubscribe-discovery": (30, 90, 100),
        "unsubscribe-execute": (30, 90, 100),
        "writing-style-profile": (None, None, 30),
    }
)


class SkillWorkflowPlan(ContractModel):
    """交给 Supervisor 的确定性 Skill 执行计划。"""

    skill_name: str
    days: int | None = None
    max_results: int = Field(ge=1, le=250)
    search_query: str | None = None
    delegated_tools: tuple[str, ...]
    notes: tuple[str, ...] = ()


def prepare_skill_workflow(
    skill_name: str,
    *,
    days: int | None = None,
    max_results: int | None = None,
    query: str | None = None,
) -> SkillWorkflowPlan:
    """规范化 Skill 参数并生成不可由 Prompt 随意放大的查询计划。"""
    if skill_name not in _WINDOWS:
        raise ValueError(f"未知 Skill：{skill_name}")
    default_days, maximum_days, default_results = _WINDOWS[skill_name]
    normalized_days = _clamp_optional(days, default_days, maximum_days)
    normalized_results = max(1, min(max_results or default_results, 250))
    normalized_query = " ".join((query or "").strip().split())
    return SkillWorkflowPlan(
        skill_name=skill_name,
        days=normalized_days,
        max_results=normalized_results,
        search_query=_build_query(
            skill_name,
            days=normalized_days,
            query=normalized_query,
        ),
        delegated_tools=tuple(sorted(SKILL_DELEGATED_TOOLS[skill_name])),
        notes=_workflow_notes(skill_name),
    )


def _clamp_optional(
    value: int | None,
    default: int | None,
    maximum: int | None,
) -> int | None:
    if default is None or maximum is None:
        return None
    return max(1, min(value if value is not None else default, maximum))


def _build_query(
    skill_name: str,
    *,
    days: int | None,
    query: str,
) -> str | None:
    if skill_name == "weekly-email-summary":
        return f"newer_than:{days}d"
    if skill_name == "urgent-email-triage":
        return _inbox_query(days, URGENT_QUERY_TERMS)
    if skill_name == "bug-issue-triage":
        return _inbox_query(days, BUG_QUERY_TERMS)
    if skill_name == "resume-candidate-review":
        return query or _inbox_query(days, RESUME_QUERY_TERMS)
    if skill_name == "draft-reply-from-email-context":
        return _scoped_user_query(query, days) if query else None
    if skill_name == "unsubscribe-discovery":
        return _inbox_query(days, UNSUBSCRIBE_QUERY_TERMS)
    if skill_name == "unsubscribe-execute" and query:
        return _scoped_user_query(
            f"({query}) ({' OR '.join(UNSUBSCRIBE_QUERY_TERMS)})",
            days,
        )
    return None


def _inbox_query(days: int | None, terms: tuple[str, ...]) -> str:
    return f"in:inbox newer_than:{days}d ({' OR '.join(terms)})"


def _scoped_user_query(query: str, days: int | None) -> str:
    lowered = query.casefold()
    if any(
        token in lowered
        for token in ("newer_than:", "after:", "before:", "in:inbox", "in:anywhere")
    ):
        return query
    return f"in:inbox newer_than:{days}d ({query})"


def _workflow_notes(skill_name: str) -> tuple[str, ...]:
    if skill_name == "send-prepared-email":
        return ("缺少收件人、主题或正文时不得调用发送工具。",)
    if skill_name == "unsubscribe-execute":
        return ("目标必须唯一；每个候选分别审批并使用独立幂等键。",)
    if skill_name == "writing-style-profile":
        return ("画像保存前必须读取当前版本，并通过受审批的长期记忆工具写入。",)
    return ()
