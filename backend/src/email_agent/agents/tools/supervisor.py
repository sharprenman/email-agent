"""监督代理的无副作用结果聚合 Tool。"""

from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from ...persistence import MemoryKind, UserMemoryService
from ...skills import prepare_skill_workflow
from ..results import AgentTaskResult, merge_task_results


def build_supervisor_tools(memory: UserMemoryService) -> tuple[BaseTool, ...]:
    """构建监督代理的编排和受审批长期记忆 Tool。"""

    async def merge_subagent_results(
        results: list[AgentTaskResult],
    ) -> dict[str, Any]:
        """按失败优先规则聚合多个子代理结果。"""
        return merge_task_results(results).model_dump(mode="json")

    async def prepare_workflow(
        skill_name: str,
        days: int | None = None,
        max_results: int | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        """生成受服务端限制的 Skill 查询、窗口和委派工具组合。"""
        return prepare_skill_workflow(
            skill_name,
            days=days,
            max_results=max_results,
            query=query,
        ).model_dump(mode="json")

    async def read_user_memory(kind: MemoryKind) -> dict[str, Any]:
        """读取当前可信用户的一类长期记忆和版本。"""
        record = memory.read(kind)
        if record is None:
            return {
                "kind": kind.value,
                "path": f"/memories/{kind.value}.md",
                "exists": False,
                "version": 0,
            }
        return {
            **record.model_dump(mode="json"),
            "exists": True,
        }

    async def save_user_memory(
        kind: MemoryKind,
        content: str,
        expected_version: int,
    ) -> dict[str, Any]:
        """审批后按乐观版本保存当前可信用户的稳定长期记忆。"""
        return memory.save(
            kind,
            content,
            expected_version=expected_version,
        ).model_dump(mode="json")

    return (
        StructuredTool.from_function(
            coroutine=prepare_workflow,
            name="prepare_skill_workflow",
            description="为内置业务 Skill 生成确定性的查询、窗口和委派工具计划。",
        ),
        StructuredTool.from_function(
            coroutine=merge_subagent_results,
            name="merge_subagent_results",
            description="聚合多步骤结果，禁止把失败或部分完成提升为成功。",
        ),
        StructuredTool.from_function(
            coroutine=read_user_memory,
            name="read_user_memory",
            description="读取当前可信用户的画像、习惯或写作风格及其版本。",
        ),
        StructuredTool.from_function(
            coroutine=save_user_memory,
            name="save_user_memory",
            description="审批后保存稳定的用户长期记忆；必须提供读取到的期望版本。",
        ),
    )
