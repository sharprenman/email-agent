"""DeepAgents 主代理、最小权限子代理和业务工具装配。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from deepagents import create_deep_agent
from deepagents.middleware.subagents import SubAgent
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from ..calendar import ApprovalService
from ..config import AuthContext
from ..content_tools import AttachmentTextService, UnsubscribeService
from ..contracts import CalendarProvider, MailProvider
from ..model import build_model
from .loader import (
    CALENDAR_AGENT,
    MAIL_WRITER,
    MAILBOX_READER,
    LoadedAgentDefinition,
    LoadedAgentDefinitions,
    load_agent_definitions,
)
from .results import AgentTaskResult
from .tools import (
    ApprovedMailService,
    build_calendar_tools,
    build_mail_writer_tools,
    build_mailbox_tools,
    build_supervisor_tools,
)


@dataclass(frozen=True)
class EmailAgentRuntime:
    """可由 API 层持有的 Agent 图及可审计装配信息。"""

    agent: Any
    subagents: tuple[SubAgent, ...]
    main_tools: tuple[BaseTool, ...]
    interrupt_on: Mapping[str, bool]

    def subagent_tool_names(self, name: str) -> frozenset[str]:
        """返回指定自定义子代理的显式业务工具白名单。"""
        spec = next((item for item in self.subagents if item["name"] == name), None)
        if spec is None:
            raise KeyError(f"未知子代理：{name}")
        return frozenset(tool.name for tool in spec.get("tools", []))


def build_email_agent_runtime(
    *,
    mail_provider: MailProvider,
    calendar_provider: CalendarProvider,
    attachment_service: AttachmentTextService,
    approvals: ApprovalService,
    auth: AuthContext,
    unsubscribe_service: UnsubscribeService | None = None,
    model: BaseChatModel | None = None,
    definitions: LoadedAgentDefinitions | None = None,
) -> EmailAgentRuntime:
    """按受校验的外部定义装配 Supervisor 和三个最小权限子代理。"""
    effective_model = model or build_model()
    effective_definitions = definitions or load_agent_definitions()
    mail_writes = ApprovedMailService(mail_provider, approvals, auth)
    reader_tools = build_mailbox_tools(mail_provider, attachment_service)
    writer_tools = build_mail_writer_tools(mail_writes, unsubscribe_service, auth)
    calendar_tools = build_calendar_tools(calendar_provider, auth)
    supervisor_tools = build_supervisor_tools()
    registries = {
        MAILBOX_READER: _tool_registry(reader_tools),
        MAIL_WRITER: _tool_registry(writer_tools),
        CALENDAR_AGENT: _tool_registry(calendar_tools),
    }
    subagents = tuple(
        _build_subagent(definition, registries[definition.name])
        for definition in effective_definitions.subagents
    )
    main_tools = _resolve_tools(
        effective_definitions.supervisor,
        _tool_registry(supervisor_tools),
    )
    agent = create_deep_agent(
        model=effective_model,
        tools=list(main_tools),
        system_prompt=effective_definitions.supervisor.system_prompt,
        subagents=subagents,
        response_format=AgentTaskResult,
        name=effective_definitions.supervisor.name,
    )
    return EmailAgentRuntime(
        agent=agent,
        subagents=subagents,
        main_tools=main_tools,
        interrupt_on={
            tool_name: True
            for definition in effective_definitions.subagents
            for tool_name in definition.interrupt_on
            if tool_name in registries[definition.name]
        },
    )


def _build_subagent(
    definition: LoadedAgentDefinition,
    registry: Mapping[str, BaseTool],
) -> SubAgent:
    tools = _resolve_tools(definition, registry)
    interrupts = {tool_name: True for tool_name in definition.interrupt_on if tool_name in registry}
    subagent: SubAgent = {
        "name": definition.name,
        "description": definition.description,
        "system_prompt": definition.system_prompt,
        "tools": list(tools),
        "response_format": AgentTaskResult,
    }
    if interrupts:
        subagent["interrupt_on"] = interrupts
    return subagent


def _resolve_tools(
    definition: LoadedAgentDefinition,
    registry: Mapping[str, BaseTool],
) -> tuple[BaseTool, ...]:
    missing = [
        name
        for name in definition.tools
        if name not in registry and name not in definition.optional_tools
    ]
    if missing:
        raise RuntimeError(f"{definition.name} 缺少必需工具：{', '.join(sorted(missing))}")
    return tuple(registry[name] for name in definition.tools if name in registry)


def _tool_registry(tools: Sequence[BaseTool]) -> Mapping[str, BaseTool]:
    return {tool.name: tool for tool in tools}
