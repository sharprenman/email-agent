"""Agent 线程状态和用户级长期记忆。"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from deepagents.backends import StoreBackend
from deepagents.backends.protocol import EditResult, ReadResult, WriteResult
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from pydantic import BaseModel, ConfigDict, Field

from .agents.results import AgentTaskResult, AgentTaskStatus
from .config import AuthContext

MEMORY_ROOT = "/memories/"
_MEMORY_NAMESPACE_ROOT = ("email-agent", "users")
_MAX_MEMORY_BYTES = 16_384
_MEMORY_LOCKS = tuple(threading.RLock() for _ in range(64))
_FORBIDDEN_MEMORY_TEXT = (
    "ignore previous",
    "ignore all previous",
    "system prompt",
    "developer message",
    "assistant:",
    "调用工具",
    "执行命令",
    "忽略以上",
    "忽略之前",
    "覆盖系统",
    "<|",
    "|>",
)
_CHECKPOINT_SERIALIZER = JsonPlusSerializer(
    allowed_msgpack_modules=(AgentTaskResult, AgentTaskStatus),
)


class MemoryKind(StrEnum):
    """允许持久化的用户记忆类型。"""

    PROFILE = "profile"
    HABITS = "habits"
    WRITING_STYLE = "writing-style"


_MEMORY_FILES = {
    MemoryKind.PROFILE: "/profile.md",
    MemoryKind.HABITS: "/habits.md",
    MemoryKind.WRITING_STYLE: "/writing-style.md",
}
_MEMORY_HEADINGS = {
    MemoryKind.PROFILE: "# 用户画像",
    MemoryKind.HABITS: "# 使用习惯",
    MemoryKind.WRITING_STYLE: "# 写作风格",
}
MEMORY_PATHS = tuple(f"{MEMORY_ROOT.rstrip('/')}{path}" for path in _MEMORY_FILES.values())


class MemoryError(RuntimeError):
    """长期记忆操作失败。"""


class MemoryConflictError(MemoryError):
    """调用方基于旧版本写入，已有内容保持不变。"""


class MemoryValidationError(MemoryError):
    """长期记忆路径或内容不符合安全规则。"""


class MemoryRecord(BaseModel):
    """返回给 Agent 的长期记忆记录。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: MemoryKind
    path: str
    content: str
    version: int = Field(ge=1)
    updated_at: datetime


@dataclass(frozen=True)
class AgentPersistence:
    """一次应用生命周期内共享的 Checkpointer 和 Store。"""

    checkpointer: BaseCheckpointSaver[Any]
    store: BaseStore


def build_in_memory_persistence() -> AgentPersistence:
    """构建仅供测试或本地进程使用的非持久化资源。"""
    return AgentPersistence(
        checkpointer=InMemorySaver(serde=_CHECKPOINT_SERIALIZER),
        store=InMemoryStore(),
    )


@asynccontextmanager
async def open_postgres_persistence(database_url: str) -> AsyncIterator[AgentPersistence]:
    """打开 PostgreSQL 持久化资源，并幂等初始化 LangGraph 表。"""
    if not database_url.strip():
        raise ValueError("DATABASE_URL 不能为空")

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.postgres import PostgresStore

    async with AsyncPostgresSaver.from_conn_string(
        database_url,
        serde=_CHECKPOINT_SERIALIZER,
    ) as checkpointer:
        with PostgresStore.from_conn_string(
            database_url,
            pool_config={"min_size": 1, "max_size": 10},
        ) as store:
            await checkpointer.setup()
            await asyncio.to_thread(store.setup)
            yield AgentPersistence(checkpointer=checkpointer, store=store)


def user_memory_namespace(user_id: str) -> tuple[str, ...]:
    """生成与可信用户绑定、可扩展到多用户的 Store namespace。"""
    normalized = user_id.strip()
    if not normalized:
        raise MemoryValidationError("用户标识不能为空")
    return (*_MEMORY_NAMESPACE_ROOT, normalized, "memories")


def trusted_memory_namespace(
    expected_user_id: str,
) -> Callable[[Any], tuple[str, ...]]:
    """创建拒绝运行时身份替换的 StoreBackend namespace 工厂。"""
    expected = expected_user_id.strip()

    def resolve(runtime: Any) -> tuple[str, ...]:
        context = runtime.context
        actual = context.get("user_id") if isinstance(context, dict) else context.user_id
        if actual != expected:
            raise PermissionError("运行时用户与 Agent 可信身份不一致")
        return user_memory_namespace(expected)

    return resolve


class ReadOnlyMemoryBackend(StoreBackend):
    """只开放白名单记忆读取，写入必须经过受审批的业务 Tool。"""

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        if file_path not in _MEMORY_FILES.values():
            return ReadResult(error=f"长期记忆路径不在白名单：{file_path}")
        return super().read(file_path, offset=offset, limit=limit)

    def write(self, file_path: str, content: str) -> WriteResult:
        del content
        return WriteResult(error=f"长期记忆写入必须通过 save_user_memory：{file_path}")

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        del old_string, new_string, replace_all
        return EditResult(error=f"长期记忆修改必须通过 save_user_memory：{file_path}")


class UserMemoryService:
    """以乐观版本和进程内串行化保护用户级记忆。"""

    def __init__(self, store: BaseStore, auth: AuthContext) -> None:
        self._store = store
        self._namespace = user_memory_namespace(auth.user_id)

    def read(self, kind: MemoryKind) -> MemoryRecord | None:
        """读取一个记忆文件及其当前版本。"""
        item = self._store.get(self._namespace, _MEMORY_FILES[kind])
        if item is None:
            return None
        value = item.value
        return MemoryRecord(
            kind=kind,
            path=_public_path(kind),
            content=str(value["content"]),
            version=int(value["version"]),
            updated_at=datetime.fromisoformat(str(value["updated_at"])),
        )

    def save(
        self,
        kind: MemoryKind,
        content: str,
        *,
        expected_version: int,
    ) -> MemoryRecord:
        """仅在版本匹配时保存稳定记忆，冲突时不覆盖已有内容。"""
        _validate_memory_content(kind, content)
        key = _MEMORY_FILES[kind]
        lock = _lock_for((*self._namespace, key))
        with lock:
            current = self._store.get(self._namespace, key)
            current_version = int(current.value["version"]) if current is not None else 0
            if expected_version != current_version:
                raise MemoryConflictError(
                    f"{kind.value} 版本冲突：期望 {expected_version}，当前 {current_version}"
                )
            now = datetime.now(UTC)
            value = {
                "content": content.strip() + "\n",
                "encoding": "utf-8",
                "version": current_version + 1,
                "updated_at": now.isoformat(),
            }
            self._store.put(self._namespace, key, value, index=False)
            return MemoryRecord(
                kind=kind,
                path=_public_path(kind),
                content=value["content"],
                version=value["version"],
                updated_at=now,
            )


def _public_path(kind: MemoryKind) -> str:
    return f"{MEMORY_ROOT.rstrip('/')}{_MEMORY_FILES[kind]}"


def _lock_for(parts: tuple[str, ...]) -> threading.RLock:
    digest = hashlib.sha256("\0".join(parts).encode()).digest()
    return _MEMORY_LOCKS[int.from_bytes(digest[:2]) % len(_MEMORY_LOCKS)]


def _validate_memory_content(kind: MemoryKind, content: str) -> None:
    normalized = content.strip()
    if not normalized:
        raise MemoryValidationError("长期记忆内容不能为空")
    if len(normalized.encode("utf-8")) > _MAX_MEMORY_BYTES:
        raise MemoryValidationError("长期记忆内容超过 16 KiB")
    lines = normalized.splitlines()
    if lines[0] != _MEMORY_HEADINGS[kind]:
        raise MemoryValidationError(f"{kind.value} 必须以“{_MEMORY_HEADINGS[kind]}”开头")
    for line in lines[1:]:
        if len(line) > 500:
            raise MemoryValidationError("长期记忆单行不能超过 500 个字符")
        if line and not line.startswith(("## ", "- ")):
            raise MemoryValidationError("长期记忆只能使用二级标题和无序事实条目")
    lowered = normalized.casefold()
    if any(token in lowered for token in _FORBIDDEN_MEMORY_TEXT):
        raise MemoryValidationError("长期记忆包含指令注入特征")
