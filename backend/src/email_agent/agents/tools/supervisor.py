"""监督代理的无副作用结果聚合 Tool。"""

from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from ..results import AgentTaskResult, merge_task_results


def build_supervisor_tools() -> tuple[BaseTool, ...]:
    """构建监督代理唯一可直接调用的结果聚合 Tool。"""

    async def merge_subagent_results(
        results: list[AgentTaskResult],
    ) -> dict[str, Any]:
        """按失败优先规则聚合多个子代理结果。"""
        return merge_task_results(results).model_dump(mode="json")

    return (
        StructuredTool.from_function(
            coroutine=merge_subagent_results,
            name="merge_subagent_results",
            description="聚合多步骤结果，禁止把失败或部分完成提升为成功。",
        ),
    )
