"""Agent 跨步骤结果契约与确定性聚合规则。"""

from collections.abc import Sequence
from enum import StrEnum

from pydantic import Field

from ..contracts import ContractModel


class AgentTaskStatus(StrEnum):
    """子代理和 Supervisor 的统一任务状态。"""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class AgentTaskResult(ContractModel):
    """用于跨子代理传递且不会隐藏失败的结构化结果。"""

    status: AgentTaskStatus
    summary: str = Field(min_length=1, max_length=5000)
    evidence: tuple[str, ...] = Field(default=(), max_length=100)
    failures: tuple[str, ...] = Field(default=(), max_length=100)


def merge_task_results(results: Sequence[AgentTaskResult]) -> AgentTaskResult:
    """聚合多步骤结果，任何失败都不能被静默提升为成功。"""
    if not results:
        raise ValueError("至少需要一个任务结果")
    statuses = {result.status for result in results}
    if statuses == {AgentTaskStatus.SUCCESS}:
        status = AgentTaskStatus.SUCCESS
    elif statuses == {AgentTaskStatus.FAILED}:
        status = AgentTaskStatus.FAILED
    else:
        status = AgentTaskStatus.PARTIAL

    failures = [
        failure
        for result in results
        for failure in (
            result.failures
            or ((result.summary,) if result.status is AgentTaskStatus.FAILED else ())
        )
    ]
    return AgentTaskResult(
        status=status,
        summary="\n".join(f"[{result.status.value}] {result.summary}" for result in results),
        evidence=tuple(
            dict.fromkeys(evidence for result in results for evidence in result.evidence)
        ),
        failures=tuple(dict.fromkeys(failures)),
    )
