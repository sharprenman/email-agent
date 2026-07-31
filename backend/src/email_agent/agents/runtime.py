"""DeepAgents 主代理、最小权限子代理和业务工具装配。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend
from deepagents.middleware.subagents import SubAgent
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from ..calendar import ApprovalService
from ..config import AuthContext
from ..content_tools import AttachmentTextService, UnsubscribeService
from ..contracts import CalendarProvider, MailProvider
from ..crm import CrmService
from ..persistence import (
    MEMORY_PATHS,
    AgentPersistence,
    ReadOnlyMemoryBackend,
    UserMemoryService,
    build_in_memory_persistence,
    trusted_memory_namespace,
)
from ..skills import SkillBundle, load_skill_bundle
from .loader import (
    CALENDAR_AGENT,
    CRM_AGENT,
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
    build_crm_tools,
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
    skill_bundle: SkillBundle
    persistence: AgentPersistence
    memory_service: UserMemoryService
    auth: AuthContext
    approvals: ApprovalService
    crm: CrmService

    def subagent_tool_names(self, name: str) -> frozenset[str]:
        """返回指定自定义子代理的显式业务工具白名单。"""
        spec = next((item for item in self.subagents if item["name"] == name), None)
        if spec is None:
            raise KeyError(f"未知子代理：{name}")
        return frozenset(tool.name for tool in spec.get("tools", []))

    def prepare_input(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """为一次 Agent 调用注入不可由调用方覆盖的内置 Skill。"""
        return self.skill_bundle.inject(payload)

    @property
    def context(self) -> AuthContext:
        """返回调用图时必须使用的可信运行时上下文。"""
        return self.auth


def build_email_agent_runtime(
    *,
    mail_provider: MailProvider,
    calendar_provider: CalendarProvider,
    attachment_service: AttachmentTextService,
    approvals: ApprovalService,
    auth: AuthContext,
    model: BaseChatModel,
    user_timezone: str = "Asia/Shanghai",
    unsubscribe_service: UnsubscribeService | None = None,
    definitions: LoadedAgentDefinitions | None = None,
    skill_bundle: SkillBundle | None = None,
    persistence: AgentPersistence | None = None,
) -> EmailAgentRuntime:
    """按受校验的外部定义装配 Supervisor 和四个最小权限子代理。"""
    effective_definitions = definitions or load_agent_definitions()
    effective_skills = skill_bundle or load_skill_bundle()
    effective_persistence = persistence or build_in_memory_persistence()
    memory_service = UserMemoryService(
        effective_persistence.store,
        auth,
        effective_persistence.state,
    )
    crm = CrmService(mail_provider, effective_persistence.state, auth)
    mail_writes = ApprovedMailService(mail_provider, approvals, auth, crm)
    reader_tools = build_mailbox_tools(
        mail_provider,
        attachment_service,
        user_timezone=user_timezone,
    )
    writer_tools = build_mail_writer_tools(mail_writes, unsubscribe_service, auth)
    calendar_tools = build_calendar_tools(calendar_provider, auth)
    crm_tools = build_crm_tools(crm)
    supervisor_tools = build_supervisor_tools(
        memory_service,
        user_timezone=user_timezone,
    )
    registries = {
        MAILBOX_READER: _tool_registry(reader_tools),
        MAIL_WRITER: _tool_registry(writer_tools),
        CALENDAR_AGENT: _tool_registry(calendar_tools),
        CRM_AGENT: _tool_registry(crm_tools),
    }
    subagents = tuple(
        _build_subagent(definition, registries[definition.name])
        for definition in effective_definitions.subagents
    )
    main_tools = _resolve_tools(
        effective_definitions.supervisor,
        _tool_registry(supervisor_tools),
    )
    supervisor_interrupts = {
        tool_name: True
        for tool_name in effective_definitions.supervisor.interrupt_on
        if tool_name in {tool.name for tool in main_tools}
    }
    all_interrupts = dict(supervisor_interrupts)
    all_interrupts.update(
        {
            tool_name: True
            for definition in effective_definitions.subagents
            for tool_name in definition.interrupt_on
            if tool_name in registries[definition.name]
        }
    )
    agent = create_deep_agent(
        model=model,
        tools=list(main_tools),
        system_prompt=effective_definitions.supervisor.system_prompt,
        subagents=subagents,
        skills=list(effective_skills.sources),
        memory=list(MEMORY_PATHS),
        backend=CompositeBackend(
            default=StateBackend(),
            routes={
                "/memories/": ReadOnlyMemoryBackend(
                    store=effective_persistence.store,
                    namespace=trusted_memory_namespace(auth.user_id),
                )
            },
        ),
        permissions=[
            FilesystemPermission(
                operations=["write"],
                paths=["/memories/**"],
                mode="deny",
            )
        ],
        interrupt_on=supervisor_interrupts,
        response_format=AgentTaskResult,
        context_schema=AuthContext,
        checkpointer=effective_persistence.checkpointer,
        store=effective_persistence.store,
        name=effective_definitions.supervisor.name,
    )
    return EmailAgentRuntime(
        agent=agent,
        subagents=subagents,
        main_tools=main_tools,
        interrupt_on=all_interrupts,
        skill_bundle=effective_skills,
        persistence=effective_persistence,
        memory_service=memory_service,
        auth=auth,
        approvals=approvals,
        crm=crm,
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
