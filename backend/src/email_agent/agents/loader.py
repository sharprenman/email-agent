"""加载并校验 Agent 定义、提示词和工具权限。"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

MAILBOX_READER = "mailbox-reader"
MAIL_WRITER = "mail-writer"
CALENDAR_AGENT = "calendar-agent"

_EXPECTED_SUBAGENTS = frozenset({MAILBOX_READER, MAIL_WRITER, CALENDAR_AGENT})
_MAXIMUM_TOOLS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "email-supervisor": frozenset(
            {
                "prepare_skill_workflow",
                "merge_subagent_results",
                "read_user_memory",
                "save_user_memory",
            }
        ),
        MAILBOX_READER: frozenset(
            {
                "get_mailbox_identity",
                "read_inbox",
                "search_emails",
                "search_skill_emails",
                "get_email",
                "get_sent_emails",
                "get_unanswered_emails",
                "list_email_attachments",
                "extract_attachment_text",
                "list_contacts",
                "discover_email_unsubscribe",
            }
        ),
        MAIL_WRITER: frozenset({"prepare_email_draft", "send_email", "execute_unsubscribe"}),
        CALENDAR_AGENT: frozenset(
            {
                "list_calendar_events",
                "create_calendar_event",
                "update_calendar_event",
                "delete_calendar_event",
            }
        ),
    }
)
_SIDE_EFFECT_TOOLS = frozenset(
    {
        "send_email",
        "execute_unsubscribe",
        "create_calendar_event",
        "update_calendar_event",
        "delete_calendar_event",
        "save_user_memory",
    }
)


class AgentDefinitionError(RuntimeError):
    """Agent 定义无效，服务不得带着不完整或越权配置启动。"""


@dataclass(frozen=True)
class LoadedAgentDefinition:
    """一个完成安全校验并加载提示词的 Agent 定义。"""

    name: str
    description: str
    system_prompt: str
    tools: tuple[str, ...]
    optional_tools: frozenset[str]
    interrupt_on: frozenset[str]


@dataclass(frozen=True)
class LoadedAgentDefinitions:
    """监督代理及固定三个子代理的完整定义。"""

    supervisor: LoadedAgentDefinition
    subagents: tuple[LoadedAgentDefinition, ...]

    def subagent(self, name: str) -> LoadedAgentDefinition:
        """按名称读取子代理定义。"""
        definition = next((item for item in self.subagents if item.name == name), None)
        if definition is None:
            raise KeyError(f"未知子代理：{name}")
        return definition


def load_agent_definitions(root: Path | Any | None = None) -> LoadedAgentDefinitions:
    """从包资源或指定目录加载 Agent 定义并执行权限校验。"""
    resource_root = root or files("email_agent.agents")
    try:
        manifest_text = resource_root.joinpath("definitions.toml").read_text(encoding="utf-8")
        data = tomllib.loads(manifest_text)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError) as exc:
        raise AgentDefinitionError(f"无法加载 Agent 定义：{exc}") from exc

    supervisor = _load_definition(data.get("supervisor"), resource_root)
    if supervisor.name != "email-supervisor":
        raise AgentDefinitionError("监督代理名称必须是 email-supervisor")

    raw_subagents = data.get("subagents")
    if not isinstance(raw_subagents, list):
        raise AgentDefinitionError("subagents 必须是数组")
    subagents = tuple(_load_definition(item, resource_root) for item in raw_subagents)
    names = [item.name for item in subagents]
    if len(names) != len(set(names)):
        raise AgentDefinitionError("子代理名称不能重复")
    if set(names) != _EXPECTED_SUBAGENTS:
        raise AgentDefinitionError(
            "子代理必须且只能包含 mailbox-reader、mail-writer、calendar-agent"
        )
    return LoadedAgentDefinitions(supervisor=supervisor, subagents=subagents)


def _load_definition(raw: Any, root: Any) -> LoadedAgentDefinition:
    if not isinstance(raw, dict):
        raise AgentDefinitionError("Agent 定义必须是 TOML 表")

    name = _required_text(raw, "name")
    description = _required_text(raw, "description")
    prompt_path = _required_text(raw, "prompt")
    tools = _string_tuple(raw.get("tools"), "tools")
    optional_tools = frozenset(_string_tuple(raw.get("optional_tools", []), "optional_tools"))
    interrupt_on = frozenset(_string_tuple(raw.get("interrupt_on", []), "interrupt_on"))

    allowed_tools = _MAXIMUM_TOOLS.get(name)
    if allowed_tools is None:
        raise AgentDefinitionError(f"未知 Agent：{name}")
    undeclared = set(tools) - allowed_tools
    if undeclared:
        raise AgentDefinitionError(f"{name} 声明了越权或未知工具：{', '.join(sorted(undeclared))}")
    if optional_tools - set(tools):
        raise AgentDefinitionError(f"{name} 的可选工具必须包含在 tools 中")
    if interrupt_on - set(tools):
        raise AgentDefinitionError(f"{name} 的审批工具必须包含在 tools 中")
    missing_interrupts = (set(tools) & _SIDE_EFFECT_TOOLS) - interrupt_on
    if missing_interrupts:
        raise AgentDefinitionError(
            f"{name} 的副作用工具缺少审批声明：{', '.join(sorted(missing_interrupts))}"
        )

    return LoadedAgentDefinition(
        name=name,
        description=description,
        system_prompt=_read_prompt(root, prompt_path),
        tools=tools,
        optional_tools=optional_tools,
        interrupt_on=interrupt_on,
    )


def _read_prompt(root: Any, raw_path: str) -> str:
    path = PurePosixPath(raw_path)
    if path.is_absolute() or ".." in path.parts or path.suffix != ".md":
        raise AgentDefinitionError(f"提示词路径不安全：{raw_path}")
    try:
        prompt = root.joinpath(*path.parts).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError) as exc:
        raise AgentDefinitionError(f"无法读取提示词 {raw_path}：{exc}") from exc
    if not prompt:
        raise AgentDefinitionError(f"提示词不能为空：{raw_path}")
    return prompt


def _required_text(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AgentDefinitionError(f"{key} 必须是非空字符串")
    return value.strip()


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise AgentDefinitionError(f"{field} 必须是非空字符串数组")
    normalized = tuple(item.strip() for item in value)
    if len(normalized) != len(set(normalized)):
        raise AgentDefinitionError(f"{field} 不能包含重复值")
    return normalized
