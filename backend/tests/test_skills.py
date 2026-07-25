"""DeepAgents 九类业务 Skill 的资源、契约与安全测试。"""

from pathlib import Path

import pytest
from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware.skills import SkillsMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from email_agent.skills import (
    EMAIL_SKILL_SOURCE,
    EMAIL_SKILLS,
    SkillCatalogError,
    load_skill_bundle,
    prepare_skill_workflow,
)

_CAPTURED_MODEL_MESSAGES = []


class _ToolCapableFakeModel(GenericFakeChatModel):
    """记录模型请求，并提供 DeepAgents 构图需要的 Tool 绑定能力。"""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        del tools, tool_choice, kwargs
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        _CAPTURED_MODEL_MESSAGES.extend(messages)
        return super()._generate(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )


def _materialize_bundle(root: Path) -> None:
    bundle = load_skill_bundle()
    for virtual_path, file_data in bundle.files.items():
        target = root / virtual_path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file_data["content"], encoding="utf-8")


def test_deepagents_native_middleware_loads_all_nine_skills(tmp_path: Path) -> None:
    _materialize_bundle(tmp_path)
    middleware = SkillsMiddleware(
        backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        sources=[EMAIL_SKILL_SOURCE],
    )

    update = middleware.before_agent({}, None, {})

    assert update is not None
    assert "skills_load_errors" not in update
    assert {item["name"] for item in update["skills_metadata"]} == set(EMAIL_SKILLS)
    metadata = {item["name"]: item["allowed_tools"] for item in update["skills_metadata"]}
    assert metadata["weekly-email-summary"] == [
        "task",
        "prepare_skill_workflow",
        "merge_subagent_results",
    ]
    assert metadata["writing-style-profile"] == [
        "task",
        "prepare_skill_workflow",
        "merge_subagent_results",
        "read_user_memory",
        "save_user_memory",
    ]


def test_state_backend_injects_skill_catalog_into_model_prompt() -> None:
    _CAPTURED_MODEL_MESSAGES.clear()
    bundle = load_skill_bundle()
    agent = create_deep_agent(
        model=_ToolCapableFakeModel(messages=iter([AIMessage(content="已读取技能目录")])),
        skills=list(bundle.sources),
    )

    result = agent.invoke(bundle.inject({"messages": [HumanMessage(content="生成一份周报")]}))

    system_text = "\n".join(
        str(message.content) for message in _CAPTURED_MODEL_MESSAGES if message.type == "system"
    )
    assert result["messages"][-1].content == "已读取技能目录"
    assert "weekly-email-summary" in system_text
    assert "writing-style-profile" in system_text
    assert EMAIL_SKILL_SOURCE in system_text


@pytest.mark.parametrize("skill_name", EMAIL_SKILLS)
def test_each_skill_declares_empty_failure_and_result_rules(skill_name: str) -> None:
    bundle = load_skill_bundle()
    content = bundle.files[f"{EMAIL_SKILL_SOURCE}{skill_name}/SKILL.md"]["content"]

    assert "## 空结果" in content
    assert "## 上游失败" in content
    assert "## 结果要求" in content
    assert "## 安全边界" in content


def test_skill_files_are_injected_without_overwriting_caller_files() -> None:
    bundle = load_skill_bundle()

    payload = bundle.inject(
        {
            "messages": ["测试"],
            "files": {"/workspace/note.md": {"content": "备注", "encoding": "utf-8"}},
        }
    )

    assert payload["files"]["/workspace/note.md"]["content"] == "备注"
    assert len(payload["files"]) == len(EMAIL_SKILLS) + 1


@pytest.mark.parametrize("skill_name", EMAIL_SKILLS)
def test_each_skill_has_a_deterministic_workflow_plan(skill_name: str) -> None:
    plan = prepare_skill_workflow(
        skill_name,
        days=999,
        max_results=999,
        query="Alice",
    )

    assert plan.skill_name == skill_name
    assert plan.max_results <= 250
    assert plan.delegated_tools


def test_workflow_plan_clamps_windows_and_builds_queries() -> None:
    urgent = prepare_skill_workflow("urgent-email-triage", days=60)
    draft = prepare_skill_workflow(
        "draft-reply-from-email-context",
        days=60,
        query="subject:项目截止",
    )

    assert urgent.days == 7
    assert urgent.search_query is not None
    assert urgent.search_query.startswith("in:inbox newer_than:7d")
    assert draft.days == 30
    assert draft.search_query == "in:inbox newer_than:30d (subject:项目截止)"


def test_workflow_plan_rejects_unknown_skill() -> None:
    with pytest.raises(ValueError, match="未知 Skill"):
        prepare_skill_workflow("unknown-skill")


def test_caller_cannot_override_builtin_skill(tmp_path: Path) -> None:
    del tmp_path
    bundle = load_skill_bundle()
    protected_path = next(iter(bundle.files))

    with pytest.raises(ValueError, match="不能覆盖内置 Skill"):
        bundle.inject(
            {
                "files": {
                    protected_path: {
                        "content": "恶意覆盖",
                        "encoding": "utf-8",
                    }
                }
            }
        )


def test_skill_catalog_rejects_direct_tool_privilege_escalation(
    tmp_path: Path,
) -> None:
    _materialize_bundle(tmp_path)
    target = tmp_path / "skills/email/send-prepared-email/SKILL.md"
    content = target.read_text(encoding="utf-8")
    target.write_text(
        content.replace(
            "allowed-tools: task prepare_skill_workflow merge_subagent_results",
            "allowed-tools: task send_email",
        ),
        encoding="utf-8",
    )

    with pytest.raises(SkillCatalogError, match="越权的直接工具"):
        load_skill_bundle(tmp_path / "skills/email")


def test_skill_catalog_rejects_missing_failure_contract(tmp_path: Path) -> None:
    _materialize_bundle(tmp_path)
    target = tmp_path / "skills/email/weekly-email-summary/SKILL.md"
    content = target.read_text(encoding="utf-8")
    target.write_text(
        content.replace("## 上游失败", "## 失败信息"),
        encoding="utf-8",
    )

    with pytest.raises(SkillCatalogError, match="缺少章节"):
        load_skill_bundle(tmp_path / "skills/email")
