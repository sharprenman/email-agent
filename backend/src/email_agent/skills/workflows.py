"""为九类 Skill 生成确定性的查询、窗口和工具组合。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from types import MappingProxyType
from zoneinfo import ZoneInfo

from pydantic import Field

from ..contracts import (
    ContractModel,
    EmailSearchCriteria,
    EmailSearchFolder,
)

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
                "search_skill_emails",
                "get_unanswered_emails",
                "list_calendar_events",
            }
        ),
        "urgent-email-triage": frozenset(
            {"search_skill_emails", "get_email", "get_unanswered_emails"}
        ),
        "bug-issue-triage": frozenset({"search_skill_emails", "get_email"}),
        "resume-candidate-review": frozenset(
            {
                "search_skill_emails",
                "list_email_attachments",
                "extract_attachment_text",
            }
        ),
        "draft-reply-from-email-context": frozenset(
            {
                "search_skill_emails",
                "get_unanswered_emails",
                "get_email",
                "prepare_email_draft",
            }
        ),
        "send-prepared-email": frozenset({"send_email"}),
        "unsubscribe-discovery": frozenset(
            {"search_skill_emails", "get_email", "discover_email_unsubscribe"}
        ),
        "unsubscribe-execute": frozenset(
            {
                "search_skill_emails",
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
    search_criteria: EmailSearchCriteria | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    delegated_tools: tuple[str, ...]
    notes: tuple[str, ...] = ()


def prepare_skill_workflow(
    skill_name: str,
    *,
    days: int | None = None,
    max_results: int | None = None,
    query: str | None = None,
    timezone: str = "Asia/Shanghai",
    now: datetime | None = None,
) -> SkillWorkflowPlan:
    """规范化 Skill 参数并生成不可由 Prompt 随意放大的查询计划。"""
    if skill_name not in _WINDOWS:
        raise ValueError(f"未知 Skill：{skill_name}")
    default_days, maximum_days, default_results = _WINDOWS[skill_name]
    normalized_days = _clamp_optional(days, default_days, maximum_days)
    normalized_results = max(1, min(max_results or default_results, 250))
    normalized_query = " ".join((query or "").strip().split())
    window_start, window_end = _time_window(
        normalized_days,
        timezone=timezone,
        now=now,
    )
    return SkillWorkflowPlan(
        skill_name=skill_name,
        days=normalized_days,
        max_results=normalized_results,
        search_criteria=_build_search_criteria(
            skill_name,
            since=window_start,
            query=normalized_query,
        ),
        window_start=window_start,
        window_end=window_end,
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


def _build_search_criteria(
    skill_name: str,
    *,
    since: datetime | None,
    query: str,
) -> EmailSearchCriteria | None:
    if skill_name == "weekly-email-summary":
        return EmailSearchCriteria(folder=EmailSearchFolder.ANY, since=since)
    if skill_name == "urgent-email-triage":
        return _inbox_criteria(since, URGENT_QUERY_TERMS)
    if skill_name == "bug-issue-triage":
        return _inbox_criteria(since, BUG_QUERY_TERMS)
    if skill_name == "resume-candidate-review":
        return _inbox_criteria(
            since,
            () if query else RESUME_QUERY_TERMS,
            query=query or None,
        )
    if skill_name == "draft-reply-from-email-context":
        return (
            EmailSearchCriteria(
                folder=EmailSearchFolder.INBOX,
                since=since,
                query=query,
            )
            if query
            else None
        )
    if skill_name == "unsubscribe-discovery":
        return _inbox_criteria(since, UNSUBSCRIBE_QUERY_TERMS)
    if skill_name == "unsubscribe-execute" and query:
        return _inbox_criteria(
            since,
            UNSUBSCRIBE_QUERY_TERMS,
            query=query,
        )
    return None


def _inbox_criteria(
    since: datetime | None,
    terms: tuple[str, ...],
    *,
    query: str | None = None,
) -> EmailSearchCriteria:
    return EmailSearchCriteria(
        folder=EmailSearchFolder.INBOX,
        since=since,
        query=query,
        keywords=tuple(term.strip('"') for term in terms),
    )


def _time_window(
    days: int | None,
    *,
    timezone: str,
    now: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    if days is None:
        return None, None
    zone = ZoneInfo(timezone)
    if now is None:
        end = datetime.now(zone)
    else:
        if now.tzinfo is None:
            raise ValueError("工作流当前时间必须包含时区")
        end = now.astimezone(zone)
    return end - timedelta(days=days), end


def _workflow_notes(skill_name: str) -> tuple[str, ...]:
    if skill_name == "send-prepared-email":
        return ("缺少收件人、主题或正文时不得调用发送工具。",)
    if skill_name == "unsubscribe-execute":
        return ("目标必须唯一；每个候选分别审批并使用独立幂等键。",)
    if skill_name == "writing-style-profile":
        return ("画像保存前必须读取当前版本，并通过受审批的长期记忆工具写入。",)
    return ()
