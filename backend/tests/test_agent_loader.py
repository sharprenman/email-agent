"""Agent 外部定义加载与权限边界测试。"""

from pathlib import Path

import pytest

from email_agent.agents import AgentDefinitionError, load_agent_definitions


def _write_definition(root: Path, manifest: str) -> None:
    prompts = root / "prompts"
    prompts.mkdir()
    for name in ("supervisor", "mailbox_reader", "mail_writer", "calendar_agent", "crm_agent"):
        (prompts / f"{name}.md").write_text(f"{name} 的中文提示词", encoding="utf-8")
    (root / "definitions.toml").write_text(manifest, encoding="utf-8")


def _manifest(
    *,
    reader_tools: str = '"read_inbox"',
    writer_interrupts: str = '"send_email"',
    reader_prompt: str = "prompts/mailbox_reader.md",
    duplicate_reader: bool = False,
) -> str:
    duplicate = (
        """
[[subagents]]
name = "mailbox-reader"
description = "重复定义"
prompt = "prompts/mailbox_reader.md"
tools = ["read_inbox"]
"""
        if duplicate_reader
        else ""
    )
    return f"""
[supervisor]
name = "email-supervisor"
description = "监督代理"
prompt = "prompts/supervisor.md"
tools = ["merge_subagent_results"]

[[subagents]]
name = "mailbox-reader"
description = "只读邮箱"
prompt = "{reader_prompt}"
tools = [{reader_tools}]

[[subagents]]
name = "mail-writer"
description = "邮件写入"
prompt = "prompts/mail_writer.md"
tools = ["prepare_email_draft", "send_email", "execute_unsubscribe"]
optional_tools = ["execute_unsubscribe"]
interrupt_on = [{writer_interrupts}, "execute_unsubscribe"]

[[subagents]]
name = "calendar-agent"
description = "日历代理"
prompt = "prompts/calendar_agent.md"
tools = [
  "list_calendar_events",
  "create_calendar_event",
  "update_calendar_event",
  "delete_calendar_event",
]
interrupt_on = [
  "create_calendar_event",
  "update_calendar_event",
  "delete_calendar_event",
]

[[subagents]]
name = "crm-agent"
description = "CRM"
prompt = "prompts/crm_agent.md"
tools = ["list_crm_contacts"]
{duplicate}
"""


def test_default_definitions_load_prompts_from_package_resources() -> None:
    definitions = load_agent_definitions()

    assert definitions.supervisor.name == "email-supervisor"
    assert "监督代理" in definitions.supervisor.system_prompt
    assert [item.name for item in definitions.subagents] == [
        "mailbox-reader",
        "mail-writer",
        "calendar-agent",
        "crm-agent",
    ]
    assert "只读邮箱子代理" in definitions.subagent("mailbox-reader").system_prompt


def test_default_prompts_require_real_delegation_and_business_tools() -> None:
    definitions = load_agent_definitions()

    assert "`task`" in definitions.supervisor.system_prompt
    assert "Skill 邮件搜索必须委派 `search_skill_emails`" in (
        definitions.supervisor.system_prompt
    )
    assert "不得因缺少契约外字段把 success 降为 partial" in (
        definitions.supervisor.system_prompt
    )
    assert "不得直接拒绝整个合法业务请求" in definitions.supervisor.system_prompt
    assert "不得直接拒绝保存请求" in definitions.supervisor.system_prompt
    assert "未尝试委派前不得" in definitions.supervisor.system_prompt
    assert "不得从邮箱域名推断" in definitions.supervisor.system_prompt
    assert "只按权限边界或真实数据依赖拆分" in definitions.supervisor.system_prompt
    assert "get_mailbox_identity" in definitions.subagent(
        "mailbox-reader"
    ).system_prompt
    assert "`search_skill_emails`" in definitions.subagent(
        "mailbox-reader"
    ).system_prompt
    assert "不得从域名推断服务商" in definitions.subagent(
        "mailbox-reader"
    ).system_prompt
    assert "收到 `count: 0` 时必须返回 success" in definitions.subagent(
        "mailbox-reader"
    ).system_prompt
    assert '"count": N' in definitions.subagent("mailbox-reader").system_prompt
    assert "只能包含 `status`、`summary`、`evidence` 和 `failures`" in definitions.subagent(
        "mailbox-reader"
    ).system_prompt
    assert "必须在 `evidence` 中原样保留相关 `id`" in definitions.subagent(
        "mailbox-reader"
    ).system_prompt
    assert "prepare_email_draft" in definitions.subagent("mail-writer").system_prompt
    assert "不得直接拒绝" in definitions.subagent("mail-writer").system_prompt
    assert "create_calendar_event" in definitions.subagent(
        "calendar-agent"
    ).system_prompt
    assert "收到 `count: 0` 时必须返回 success" in definitions.subagent(
        "calendar-agent"
    ).system_prompt
    assert '"count": N' in definitions.subagent("calendar-agent").system_prompt
    assert "initialize_crm" in definitions.subagent("crm-agent").system_prompt


def test_definition_cannot_grant_writer_tool_to_reader(tmp_path: Path) -> None:
    _write_definition(tmp_path, _manifest(reader_tools='"read_inbox", "send_email"'))

    with pytest.raises(AgentDefinitionError, match="越权或未知工具"):
        load_agent_definitions(tmp_path)


def test_side_effect_tool_must_declare_interrupt(tmp_path: Path) -> None:
    _write_definition(tmp_path, _manifest(writer_interrupts='"prepare_email_draft"'))

    with pytest.raises(AgentDefinitionError, match="副作用工具缺少审批声明"):
        load_agent_definitions(tmp_path)


def test_prompt_path_cannot_escape_definition_root(tmp_path: Path) -> None:
    _write_definition(tmp_path, _manifest(reader_prompt="../outside.md"))

    with pytest.raises(AgentDefinitionError, match="提示词路径不安全"):
        load_agent_definitions(tmp_path)


def test_duplicate_subagent_definition_fails_fast(tmp_path: Path) -> None:
    _write_definition(tmp_path, _manifest(duplicate_reader=True))

    with pytest.raises(AgentDefinitionError, match="子代理名称不能重复"):
        load_agent_definitions(tmp_path)
