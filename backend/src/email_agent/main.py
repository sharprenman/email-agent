"""FastAPI 应用入口。"""

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from .config import Settings, build_default_auth_context, get_settings


class HealthData(BaseModel):
    """存活检查数据。"""

    status: Literal["ok"]


class HealthResponse(BaseModel):
    """统一的存活检查响应。"""

    code: Literal[0]
    message: Literal["success"]
    data: HealthData


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建 FastAPI 应用。"""
    effective_settings = settings or get_settings()
    application = FastAPI(
        title="DeepAgents 邮件智能体 API",
        version="0.1.0",
    )
    application.state.settings = effective_settings
    application.state.default_auth_context = build_default_auth_context(effective_settings)

    @application.get("/health/live", response_model=HealthResponse, tags=["健康检查"])
    def health_live() -> HealthResponse:
        """返回进程存活状态，不访问任何外部依赖。"""
        return HealthResponse(
            code=0,
            message="success",
            data=HealthData(status="ok"),
        )

    return application


app = create_app()
