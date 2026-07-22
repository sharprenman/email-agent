"""FastAPI 存活检查测试。"""

import asyncio

import httpx

from email_agent.main import app


def test_health_live() -> None:
    """存活检查不依赖模型、数据库或邮件服务。"""
    async def request_health() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/health/live")

    response = asyncio.run(request_health())

    assert response.status_code == 200
    assert response.json() == {
        "code": 0,
        "message": "success",
        "data": {"status": "ok"},
    }
