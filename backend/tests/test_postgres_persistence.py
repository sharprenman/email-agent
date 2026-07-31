"""真实 PostgreSQL 持久化、并发与恢复测试。"""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, TypedDict

import psycopg
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from email_agent.config import AuthContext
from email_agent.persistence import (
    MemoryConflictError,
    MemoryKind,
    UserMemoryService,
    open_postgres_persistence,
)


class _MessageState(TypedDict):
    messages: Annotated[list, add_messages]


def _graph(checkpointer):
    graph = StateGraph(_MessageState)
    graph.add_node("reply", lambda _state: {"messages": [AIMessage(content="已处理")]})
    graph.add_edge(START, "reply")
    graph.add_edge("reply", END)
    return graph.compile(checkpointer=checkpointer)


def _interrupt_graph(checkpointer):
    def wait_for_approval(state: _MessageState):
        interrupt(
            {
                "action_requests": [{"name": "send_email", "args": {"subject": "测试"}}],
                "review_configs": [{"action_name": "send_email"}],
            }
        )
        return state

    graph = StateGraph(_MessageState)
    graph.add_node("wait", wait_for_approval)
    graph.add_edge(START, "wait")
    graph.add_edge("wait", END)
    return graph.compile(checkpointer=checkpointer)


def test_postgres_multi_instance_atomicity_and_restart_recovery(postgres_test_url: str) -> None:
    run_id = uuid.uuid4().hex
    user_id = f"pg-owner-{run_id}"
    other_user = f"pg-other-{run_id}"
    thread_id = f"pg-thread-{run_id}"
    interrupt_thread = f"pg-interrupt-{run_id}"
    key_hash = f"key-{run_id}"
    request_hash = f"request-{run_id}"
    target_hash = f"target-{run_id}"
    memory_key = "/profile.md"

    async def run() -> None:
        async with open_postgres_persistence(postgres_test_url) as first:
            async with open_postgres_persistence(postgres_test_url) as second:
                first.state.health_check()
                second.state.health_check()

                first.state.create_thread(thread_id, user_id)
                assert second.state.get_thread_owner(thread_id) == user_id
                assert second.state.get_thread_owner(thread_id) != other_user

                def reserve(state) -> bool:
                    return state.reserve_idempotency(
                        user_id,
                        "resume",
                        key_hash,
                        thread_id,
                        f"fingerprint-{run_id}",
                    )

                with ThreadPoolExecutor(max_workers=2) as executor:
                    reservations = list(executor.map(reserve, (first.state, second.state)))
                assert sorted(reservations) == [False, True]

                def consume(args) -> bool:
                    state, jti = args
                    return state.consume_approval(jti, user_id, request_hash, 4_102_444_800)

                with ThreadPoolExecutor(max_workers=2) as executor:
                    consumptions = list(
                        executor.map(
                            consume,
                            (
                                (first.state, f"jti-a-{run_id}"),
                                (second.state, f"jti-b-{run_id}"),
                            ),
                        )
                    )
                assert sorted(consumptions) == [False, True]

                unsubscribe = {
                    "state": "pending",
                    "method": "one_click",
                    "target_hash": target_hash,
                    "idempotency_hash": key_hash,
                    "updated_at": "2026-07-30T00:00:00+00:00",
                    "evidence_hash": None,
                    "status_code": None,
                }
                with ThreadPoolExecutor(max_workers=2) as executor:
                    begins = list(
                        executor.map(
                            lambda state: state.begin_unsubscribe(
                                user_id, target_hash, unsubscribe
                            )[0],
                            (first.state, second.state),
                        )
                    )
                assert sorted(begins) == [False, True]
                assert second.state.get_unsubscribe(other_user, target_hash) is None

                first_memory = UserMemoryService(
                    first.store,
                    AuthContext(user_id=user_id),
                    first.state,
                )
                second_memory = UserMemoryService(
                    second.store,
                    AuthContext(user_id=user_id),
                    second.state,
                )

                def save(memory: UserMemoryService, name: str):
                    try:
                        return memory.save(
                            MemoryKind.PROFILE,
                            f"# 用户画像\n\n## 基本偏好\n- 称呼：{name}",
                            expected_version=0,
                        )
                    except MemoryConflictError as exc:
                        return exc

                with ThreadPoolExecutor(max_workers=2) as executor:
                    memories = list(
                        executor.map(
                            lambda args: save(*args),
                            ((first_memory, "小王"), (second_memory, "小李")),
                        )
                    )
                assert sum(not isinstance(item, Exception) for item in memories) == 1
                assert sum(isinstance(item, MemoryConflictError) for item in memories) == 1

                graph = _graph(first.checkpointer)
                config = {"configurable": {"thread_id": thread_id}}
                await graph.ainvoke({"messages": [HumanMessage(content="第一轮")]}, config)

                pending_graph = _interrupt_graph(first.checkpointer)
                pending_config = {"configurable": {"thread_id": interrupt_thread}}
                result = await pending_graph.ainvoke(
                    {"messages": [HumanMessage(content="请发送")]},
                    pending_config,
                )
                assert result["__interrupt__"]

        async with open_postgres_persistence(postgres_test_url) as restarted:
            restarted_graph = _graph(restarted.checkpointer)
            state = await restarted_graph.aget_state(
                {"configurable": {"thread_id": thread_id}}
            )
            assert [message.content for message in state.values["messages"]] == [
                "第一轮",
                "已处理",
            ]

            restarted_pending = _interrupt_graph(restarted.checkpointer)
            pending_state = await restarted_pending.aget_state(
                {"configurable": {"thread_id": interrupt_thread}}
            )
            assert len(pending_state.interrupts) == 1

            restarted_memory = UserMemoryService(
                restarted.store,
                AuthContext(user_id=user_id),
                restarted.state,
            )
            assert restarted_memory.read(MemoryKind.PROFILE).version == 1
            assert (
                UserMemoryService(
                    restarted.store,
                    AuthContext(user_id=other_user),
                    restarted.state,
                ).read(MemoryKind.PROFILE)
                is None
            )

            await restarted.checkpointer.adelete_thread(thread_id)
            await restarted.checkpointer.adelete_thread(interrupt_thread)
            restarted.store.delete(
                ("email-agent", "users", user_id, "memories"),
                memory_key,
            )

    try:
        asyncio.run(run())
    finally:
        with psycopg.connect(postgres_test_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM email_agent_thread_owners WHERE thread_id = %s",
                    (thread_id,),
                )
                cur.execute(
                    """
                    DELETE FROM email_agent_idempotency
                    WHERE user_id = %s AND operation = 'resume' AND key_hash = %s
                    """,
                    (user_id, key_hash),
                )
                cur.execute(
                    """
                    DELETE FROM email_agent_approval_consumptions
                    WHERE user_id = %s AND request_hash = %s
                    """,
                    (user_id, request_hash),
                )
                cur.execute(
                    """
                    DELETE FROM email_agent_unsubscribe_state
                    WHERE user_id = %s AND target_hash = %s
                    """,
                    (user_id, target_hash),
                )
