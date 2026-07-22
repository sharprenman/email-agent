"""应用配置与单用户身份上下文。"""

from enum import StrEnum
from functools import lru_cache

import dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnvironment(StrEnum):
    """应用运行环境。"""

    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """从环境变量读取并校验应用配置。"""

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    app_env: AppEnvironment = AppEnvironment.DEVELOPMENT
    single_user_id: str = Field(
        default="local-user",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.@+-]*$",
    )
    service_auth_token: SecretStr | None = None
    approval_signing_secret: SecretStr | None = Field(default=None, min_length=32)
    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = Field(default=None, max_length=2048)
    google_client_id: str | None = Field(default=None, max_length=512)
    google_client_secret: SecretStr | None = None
    google_access_token: SecretStr | None = None
    google_refresh_token: SecretStr | None = None
    microsoft_tenant_id: str = Field(default="common", min_length=1, max_length=128)
    microsoft_client_id: str | None = Field(default=None, max_length=128)
    microsoft_client_secret: SecretStr | None = None
    microsoft_access_token: SecretStr | None = None
    microsoft_refresh_token: SecretStr | None = None
    model: str = Field(default="openai/gpt-5.1", min_length=1, max_length=255)
    provider_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    max_request_bytes: int = Field(default=1_048_576, ge=1024, le=10_485_760)
    max_attachment_bytes: int = Field(default=26_214_400, ge=1024, le=104_857_600)

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        """生产环境必须配置服务间认证。"""
        missing: list[str] = []
        if self.app_env is AppEnvironment.PRODUCTION:
            if self.service_auth_token is None:
                missing.append("SERVICE_AUTH_TOKEN")
            if self.approval_signing_secret is None:
                missing.append("APPROVAL_SIGNING_SECRET")
        if missing:
            raise ValueError("生产环境必须配置：" + ", ".join(missing))
        return self


class AuthContext(BaseModel):
    """传递给业务层和存储命名空间的可信身份。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: str = Field(min_length=1, max_length=128)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """加载一次根目录环境配置，避免请求期间重复解析。"""
    dotenv.load_dotenv()
    return Settings()


def build_default_auth_context(settings: Settings) -> AuthContext:
    """为首期单用户部署生成统一身份上下文。"""
    return AuthContext(user_id=settings.single_user_id)
