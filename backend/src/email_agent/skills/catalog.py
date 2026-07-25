"""加载、校验并打包项目内置的 DeepAgents Skill。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Any

from deepagents.backends.protocol import FileData

from .workflows import SKILL_DELEGATED_TOOLS

EMAIL_SKILL_SOURCE = "/skills/email/"
EMAIL_SKILLS = (
    "weekly-email-summary",
    "urgent-email-triage",
    "bug-issue-triage",
    "resume-candidate-review",
    "draft-reply-from-email-context",
    "send-prepared-email",
    "unsubscribe-discovery",
    "unsubscribe-execute",
    "writing-style-profile",
)
_DIRECT_TOOLS = frozenset(
    {
        "task",
        "prepare_skill_workflow",
        "merge_subagent_results",
        "read_user_memory",
        "save_user_memory",
    }
)
_MEMORY_TOOLS = frozenset({"read_user_memory", "save_user_memory"})
_REQUIRED_SECTIONS = (
    "## 适用条件",
    "## 输入规则",
    "## 执行流程",
    "## 安全边界",
    "## 空结果",
    "## 上游失败",
    "## 结果要求",
)


class SkillCatalogError(RuntimeError):
    """内置 Skill 资源缺失、格式错误或越权时阻止服务启动。"""


@dataclass(frozen=True)
class SkillBundle:
    """可直接注入 DeepAgents StateBackend 的只读 Skill 资源。"""

    source: str
    names: tuple[str, ...]
    files: Mapping[str, FileData]

    @property
    def sources(self) -> tuple[str, ...]:
        """返回传给 DeepAgents `skills` 参数的虚拟目录。"""
        return (self.source,)

    def inject(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """把内置 Skill 注入调用输入，并拒绝同路径覆盖。"""
        raw_files = payload.get("files", {})
        if not isinstance(raw_files, Mapping):
            raise ValueError("调用输入中的 files 必须是映射")
        collisions = set(raw_files) & set(self.files)
        if collisions:
            raise ValueError(f"调用输入不能覆盖内置 Skill：{', '.join(sorted(collisions))}")
        injected_files = {path: dict(file_data) for path, file_data in raw_files.items()}
        injected_files.update({path: dict(file_data) for path, file_data in self.files.items()})
        return {**payload, "files": injected_files}


def load_skill_bundle(root: Path | Any | None = None) -> SkillBundle:
    """从包资源加载九个 Skill，并执行启动时安全校验。"""
    resource_root = root or files("email_agent.skills")
    bundled_files: dict[str, FileData] = {}
    loaded_names: list[str] = []
    for expected_name in EMAIL_SKILLS:
        skill_path = resource_root.joinpath(expected_name, "SKILL.md")
        try:
            content = skill_path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError) as exc:
            raise SkillCatalogError(f"无法读取 Skill {expected_name}：{exc}") from exc
        metadata = _parse_frontmatter(content, expected_name)
        _validate_skill(expected_name, content, metadata)
        virtual_path = f"{EMAIL_SKILL_SOURCE}{expected_name}/SKILL.md"
        bundled_files[virtual_path] = FileData(
            content=content + "\n",
            encoding="utf-8",
        )
        loaded_names.append(expected_name)

    return SkillBundle(
        source=EMAIL_SKILL_SOURCE,
        names=tuple(loaded_names),
        files=MappingProxyType(bundled_files),
    )


def _parse_frontmatter(content: str, expected_name: str) -> dict[str, str]:
    lines = content.splitlines()
    if len(lines) < 4 or lines[0].strip() != "---":
        raise SkillCatalogError(f"{expected_name} 缺少 YAML frontmatter")
    try:
        closing_index = lines[1:].index("---") + 1
    except ValueError as exc:
        raise SkillCatalogError(f"{expected_name} 的 YAML frontmatter 未闭合") from exc

    metadata: dict[str, str] = {}
    for raw_line in lines[1:closing_index]:
        if not raw_line.strip() or raw_line.startswith((" ", "\t")):
            continue
        key, separator, value = raw_line.partition(":")
        if separator:
            metadata[key.strip()] = value.strip().strip("\"'")
    return metadata


def _validate_skill(
    expected_name: str,
    content: str,
    metadata: Mapping[str, str],
) -> None:
    if metadata.get("name") != expected_name:
        raise SkillCatalogError(f"{expected_name} 的 name 必须与目录名一致")
    description = metadata.get("description", "")
    if not description or len(description) > 1024:
        raise SkillCatalogError(f"{expected_name} 的 description 长度无效")

    allowed_tools = frozenset(metadata.get("allowed-tools", "").split())
    if not allowed_tools or allowed_tools - _DIRECT_TOOLS:
        raise SkillCatalogError(
            f"{expected_name} 声明了越权的直接工具："
            f"{', '.join(sorted(allowed_tools - _DIRECT_TOOLS))}"
        )
    if expected_name != "writing-style-profile" and allowed_tools & _MEMORY_TOOLS:
        raise SkillCatalogError(f"{expected_name} 不允许声明长期记忆工具")
    if expected_name == "writing-style-profile" and not _MEMORY_TOOLS <= allowed_tools:
        raise SkillCatalogError("writing-style-profile 必须声明完整的长期记忆工具")
    missing_sections = [section for section in _REQUIRED_SECTIONS if section not in content]
    if missing_sections:
        raise SkillCatalogError(f"{expected_name} 缺少章节：{', '.join(missing_sections)}")
    missing_tools = [
        tool for tool in SKILL_DELEGATED_TOOLS[expected_name] if f"`{tool}`" not in content
    ]
    if missing_tools:
        raise SkillCatalogError(
            f"{expected_name} 未声明必需的委派工具：{', '.join(sorted(missing_tools))}"
        )
