"""Agent 线程状态和用户级长期记忆。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

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


class ApplicationState(Protocol):
    """需要跨进程原子共享的应用状态。"""

    def health_check(self) -> None: ...

    def thread_lock(self, thread_id: str) -> AbstractContextManager[None]: ...

    def create_thread(self, thread_id: str, user_id: str) -> None: ...

    def get_thread_owner(self, thread_id: str) -> str | None: ...

    def delete_thread(self, thread_id: str, user_id: str) -> None: ...

    def reserve_idempotency(
        self,
        user_id: str,
        operation: str,
        key_hash: str,
        thread_id: str,
        fingerprint: str,
    ) -> bool: ...

    def release_idempotency(self, user_id: str, operation: str, key_hash: str) -> None: ...

    def consume_approval(
        self,
        jti: str,
        user_id: str,
        request_hash: str,
        expires_at: int,
    ) -> bool: ...

    def get_unsubscribe(self, user_id: str, target_hash: str) -> Mapping[str, Any] | None: ...

    def begin_unsubscribe(
        self,
        user_id: str,
        target_hash: str,
        record: Mapping[str, Any],
    ) -> tuple[bool, Mapping[str, Any]]: ...

    def finish_unsubscribe(
        self,
        user_id: str,
        target_hash: str,
        idempotency_hash: str,
        record: Mapping[str, Any],
    ) -> bool: ...

    def save_memory(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: Mapping[str, Any],
        expected_version: int,
    ) -> bool: ...


class StoreApplicationState:
    """供测试和无数据库开发使用的进程内状态实现。"""

    _THREADS = ("email-agent", "application", "thread-owners")
    _IDEMPOTENCY = ("email-agent", "application", "idempotency")
    _APPROVALS = ("email-agent", "application", "approvals")
    _UNSUBSCRIBE = ("email-agent", "application", "unsubscribe")

    def __init__(self, store: BaseStore) -> None:
        self._store = store
        self._lock = threading.RLock()

    def health_check(self) -> None:
        return None

    @contextmanager
    def thread_lock(self, thread_id: str) -> Iterator[None]:
        del thread_id
        yield

    def create_thread(self, thread_id: str, user_id: str) -> None:
        with self._lock:
            self._store.put(
                self._THREADS,
                thread_id,
                {"user_id": user_id, "created_at": datetime.now(UTC).isoformat()},
                index=False,
            )

    def get_thread_owner(self, thread_id: str) -> str | None:
        item = self._store.get(self._THREADS, thread_id)
        return str(item.value["user_id"]) if item is not None else None

    def delete_thread(self, thread_id: str, user_id: str) -> None:
        with self._lock:
            if self.get_thread_owner(thread_id) == user_id:
                self._store.delete(self._THREADS, thread_id)

    def reserve_idempotency(
        self,
        user_id: str,
        operation: str,
        key_hash: str,
        thread_id: str,
        fingerprint: str,
    ) -> bool:
        key = f"{user_id}:{operation}:{key_hash}"
        with self._lock:
            if self._store.get(self._IDEMPOTENCY, key) is not None:
                return False
            self._store.put(
                self._IDEMPOTENCY,
                key,
                {
                    "thread_id": thread_id,
                    "fingerprint": fingerprint,
                    "created_at": datetime.now(UTC).isoformat(),
                },
                index=False,
            )
            return True

    def release_idempotency(self, user_id: str, operation: str, key_hash: str) -> None:
        self._store.delete(self._IDEMPOTENCY, f"{user_id}:{operation}:{key_hash}")

    def consume_approval(
        self,
        jti: str,
        user_id: str,
        request_hash: str,
        expires_at: int,
    ) -> bool:
        with self._lock:
            operation_key = f"{user_id}:{request_hash}"
            if (
                self._store.get(self._APPROVALS, jti) is not None
                or self._store.get(self._APPROVALS, operation_key) is not None
            ):
                return False
            value = {"user_id": user_id, "request_hash": request_hash, "expires_at": expires_at}
            self._store.put(self._APPROVALS, jti, value, index=False)
            self._store.put(self._APPROVALS, operation_key, value, index=False)
            return True

    def get_unsubscribe(self, user_id: str, target_hash: str) -> Mapping[str, Any] | None:
        item = self._store.get(self._UNSUBSCRIBE, f"{user_id}:{target_hash}")
        return item.value if item is not None else None

    def begin_unsubscribe(
        self,
        user_id: str,
        target_hash: str,
        record: Mapping[str, Any],
    ) -> tuple[bool, Mapping[str, Any]]:
        key = f"{user_id}:{target_hash}"
        with self._lock:
            existing = self.get_unsubscribe(user_id, target_hash)
            if existing is not None and (
                existing["state"] != "failed"
                or existing["idempotency_hash"] == record["idempotency_hash"]
            ):
                return False, existing
            self._store.put(self._UNSUBSCRIBE, key, dict(record), index=False)
            return True, record

    def finish_unsubscribe(
        self,
        user_id: str,
        target_hash: str,
        idempotency_hash: str,
        record: Mapping[str, Any],
    ) -> bool:
        key = f"{user_id}:{target_hash}"
        with self._lock:
            existing = self.get_unsubscribe(user_id, target_hash)
            if existing is None or existing["idempotency_hash"] != idempotency_hash:
                return False
            self._store.put(self._UNSUBSCRIBE, key, dict(record), index=False)
            return True

    def save_memory(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: Mapping[str, Any],
        expected_version: int,
    ) -> bool:
        with self._lock:
            current = self._store.get(namespace, key)
            version = int(current.value["version"]) if current is not None else 0
            if version != expected_version:
                return False
            self._store.put(namespace, key, dict(value), index=False)
            return True


class PostgresApplicationState:
    """使用数据库约束和条件更新实现跨进程原子状态。"""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def setup(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS email_agent_thread_owners (
                thread_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS email_agent_idempotency (
                user_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, operation, key_hash)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS email_agent_approval_consumptions (
                jti TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                consumed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (user_id, request_hash)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS email_agent_unsubscribe_state (
                user_id TEXT NOT NULL,
                target_hash TEXT NOT NULL,
                record JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, target_hash)
            )
            """,
        )
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                for statement in statements:
                    cur.execute(statement)

    def health_check(self) -> None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                if cur.fetchone() != (1,):
                    raise RuntimeError("PostgreSQL 健康检查失败")

    @contextmanager
    def thread_lock(self, thread_id: str) -> Iterator[None]:
        lock_id = int.from_bytes(hashlib.sha256(thread_id.encode()).digest()[:8], signed=True)
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(%s)", (lock_id,))
                try:
                    yield
                finally:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))

    def create_thread(self, thread_id: str, user_id: str) -> None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO email_agent_thread_owners (thread_id, user_id)
                    VALUES (%s, %s)
                    ON CONFLICT (thread_id) DO NOTHING
                    """,
                    (thread_id, user_id),
                )
                if cur.rowcount != 1:
                    raise RuntimeError("线程标识已经存在")

    def get_thread_owner(self, thread_id: str) -> str | None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id FROM email_agent_thread_owners WHERE thread_id = %s",
                    (thread_id,),
                )
                row = cur.fetchone()
                return str(row[0]) if row is not None else None

    def delete_thread(self, thread_id: str, user_id: str) -> None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM email_agent_thread_owners WHERE thread_id = %s AND user_id = %s",
                    (thread_id, user_id),
                )

    def reserve_idempotency(
        self,
        user_id: str,
        operation: str,
        key_hash: str,
        thread_id: str,
        fingerprint: str,
    ) -> bool:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO email_agent_idempotency
                        (user_id, operation, key_hash, thread_id, fingerprint)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, operation, key_hash) DO NOTHING
                    """,
                    (user_id, operation, key_hash, thread_id, fingerprint),
                )
                return cur.rowcount == 1

    def release_idempotency(self, user_id: str, operation: str, key_hash: str) -> None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM email_agent_idempotency
                    WHERE user_id = %s AND operation = %s AND key_hash = %s
                    """,
                    (user_id, operation, key_hash),
                )

    def consume_approval(
        self,
        jti: str,
        user_id: str,
        request_hash: str,
        expires_at: int,
    ) -> bool:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM email_agent_approval_consumptions
                    WHERE expires_at < CURRENT_TIMESTAMP
                    """
                )
                cur.execute(
                    """
                    INSERT INTO email_agent_approval_consumptions
                        (jti, user_id, request_hash, expires_at)
                    VALUES (%s, %s, %s, to_timestamp(%s))
                    ON CONFLICT DO NOTHING
                    """,
                    (jti, user_id, request_hash, expires_at),
                )
                return cur.rowcount == 1

    def get_unsubscribe(self, user_id: str, target_hash: str) -> Mapping[str, Any] | None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT record FROM email_agent_unsubscribe_state
                    WHERE user_id = %s AND target_hash = %s
                    """,
                    (user_id, target_hash),
                )
                row = cur.fetchone()
                return row[0] if row is not None else None

    def begin_unsubscribe(
        self,
        user_id: str,
        target_hash: str,
        record: Mapping[str, Any],
    ) -> tuple[bool, Mapping[str, Any]]:
        payload = json.dumps(dict(record), ensure_ascii=False)
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO email_agent_unsubscribe_state (user_id, target_hash, record)
                    VALUES (%s, %s, %s::jsonb)
                    ON CONFLICT (user_id, target_hash) DO UPDATE
                    SET record = EXCLUDED.record, updated_at = CURRENT_TIMESTAMP
                    WHERE email_agent_unsubscribe_state.record->>'state' = 'failed'
                      AND email_agent_unsubscribe_state.record->>'idempotency_hash'
                          <> EXCLUDED.record->>'idempotency_hash'
                    RETURNING record
                    """,
                    (user_id, target_hash, payload),
                )
                row = cur.fetchone()
                if row is not None:
                    return True, row[0]
                cur.execute(
                    """
                    SELECT record FROM email_agent_unsubscribe_state
                    WHERE user_id = %s AND target_hash = %s
                    """,
                    (user_id, target_hash),
                )
                return False, cur.fetchone()[0]

    def finish_unsubscribe(
        self,
        user_id: str,
        target_hash: str,
        idempotency_hash: str,
        record: Mapping[str, Any],
    ) -> bool:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE email_agent_unsubscribe_state
                    SET record = %s::jsonb, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = %s AND target_hash = %s
                      AND record->>'idempotency_hash' = %s
                    """,
                    (
                        json.dumps(dict(record), ensure_ascii=False),
                        user_id,
                        target_hash,
                        idempotency_hash,
                    ),
                )
                return cur.rowcount == 1

    def save_memory(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: Mapping[str, Any],
        expected_version: int,
    ) -> bool:
        prefix = ".".join(namespace)
        payload = json.dumps(dict(value), ensure_ascii=False)
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                if expected_version == 0:
                    cur.execute(
                        """
                        INSERT INTO store (prefix, key, value)
                        VALUES (%s, %s, %s::jsonb)
                        ON CONFLICT (prefix, key) DO NOTHING
                        """,
                        (prefix, key, payload),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE store
                        SET value = %s::jsonb, updated_at = CURRENT_TIMESTAMP
                        WHERE prefix = %s AND key = %s
                          AND (value->>'version')::integer = %s
                        """,
                        (payload, prefix, key, expected_version),
                    )
                return cur.rowcount == 1


@dataclass(frozen=True)
class AgentPersistence:
    """一次应用生命周期内共享的 Checkpointer 和 Store。"""

    checkpointer: BaseCheckpointSaver[Any]
    store: BaseStore
    state: ApplicationState


def build_in_memory_persistence() -> AgentPersistence:
    """构建仅供测试或本地进程使用的非持久化资源。"""
    persistence_store = InMemoryStore()
    return AgentPersistence(
        checkpointer=InMemorySaver(serde=_CHECKPOINT_SERIALIZER),
        store=persistence_store,
        state=StoreApplicationState(persistence_store),
    )


@asynccontextmanager
async def open_postgres_persistence(database_url: str) -> AsyncIterator[AgentPersistence]:
    """打开 PostgreSQL 持久化资源，并幂等初始化 LangGraph 表。"""
    if not database_url.strip():
        raise ValueError("DATABASE_URL 不能为空")

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.postgres import PostgresStore
    from psycopg_pool import ConnectionPool

    async with AsyncPostgresSaver.from_conn_string(
        database_url,
        serde=_CHECKPOINT_SERIALIZER,
    ) as checkpointer:
        with PostgresStore.from_conn_string(
            database_url,
            pool_config={"min_size": 1, "max_size": 10},
        ) as store:
            with ConnectionPool(
                conninfo=database_url,
                min_size=1,
                max_size=10,
                open=True,
            ) as application_pool:
                state = PostgresApplicationState(application_pool)
                await checkpointer.setup()
                await asyncio.to_thread(store.setup)
                await asyncio.to_thread(state.setup)
                yield AgentPersistence(checkpointer=checkpointer, store=store, state=state)


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

    def __init__(
        self,
        store: BaseStore,
        auth: AuthContext,
        state: ApplicationState | None = None,
    ) -> None:
        self._store = store
        self._namespace = user_memory_namespace(auth.user_id)
        self._state = state

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
        if self._state is not None:
            return self._save_with_state(kind, key, content, expected_version)
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

    def _save_with_state(
        self,
        kind: MemoryKind,
        key: str,
        content: str,
        expected_version: int,
    ) -> MemoryRecord:
        now = datetime.now(UTC)
        value = {
            "content": content.strip() + "\n",
            "encoding": "utf-8",
            "version": expected_version + 1,
            "updated_at": now.isoformat(),
        }
        if not self._state.save_memory(self._namespace, key, value, expected_version):
            current = self._store.get(self._namespace, key)
            current_version = int(current.value["version"]) if current is not None else 0
            raise MemoryConflictError(
                f"{kind.value} 版本冲突：期望 {expected_version}，当前 {current_version}"
            )
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
