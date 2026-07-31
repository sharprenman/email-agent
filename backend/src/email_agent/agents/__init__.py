"""邮件 Agent 的配置加载和运行时装配入口。"""

from ..persistence import (
    AgentPersistence,
    MemoryConflictError,
    MemoryKind,
    MemoryRecord,
    MemoryValidationError,
    UserMemoryService,
    build_in_memory_persistence,
    open_postgres_persistence,
)
from ..skills import SkillBundle, SkillCatalogError, load_skill_bundle
from .loader import (
    CALENDAR_AGENT,
    CRM_AGENT,
    MAIL_WRITER,
    MAILBOX_READER,
    AgentDefinitionError,
    LoadedAgentDefinition,
    LoadedAgentDefinitions,
    load_agent_definitions,
)
from .results import AgentTaskResult, AgentTaskStatus, merge_task_results
from .runtime import (
    EmailAgentRuntime,
    build_email_agent_runtime,
)
from .tools import ApprovedMailService, mail_approval_payload

__all__ = [
    "CALENDAR_AGENT",
    "CRM_AGENT",
    "MAILBOX_READER",
    "MAIL_WRITER",
    "AgentDefinitionError",
    "AgentTaskResult",
    "AgentTaskStatus",
    "AgentPersistence",
    "ApprovedMailService",
    "EmailAgentRuntime",
    "LoadedAgentDefinition",
    "LoadedAgentDefinitions",
    "MemoryConflictError",
    "MemoryKind",
    "MemoryRecord",
    "MemoryValidationError",
    "SkillBundle",
    "SkillCatalogError",
    "UserMemoryService",
    "build_in_memory_persistence",
    "build_email_agent_runtime",
    "load_agent_definitions",
    "load_skill_bundle",
    "mail_approval_payload",
    "merge_task_results",
    "open_postgres_persistence",
]
