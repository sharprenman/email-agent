"""邮件 Agent 的配置加载和运行时装配入口。"""

from .loader import (
    CALENDAR_AGENT,
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
    "MAILBOX_READER",
    "MAIL_WRITER",
    "AgentDefinitionError",
    "AgentTaskResult",
    "AgentTaskStatus",
    "ApprovedMailService",
    "EmailAgentRuntime",
    "LoadedAgentDefinition",
    "LoadedAgentDefinitions",
    "build_email_agent_runtime",
    "load_agent_definitions",
    "mail_approval_payload",
    "merge_task_results",
]
