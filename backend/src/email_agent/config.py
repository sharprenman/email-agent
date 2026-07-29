"""应用配置与单用户身份上下文。"""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnvironment(StrEnum):
    """应用运行环境。"""

    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class MailProviderKind(StrEnum):
    """生产运行时支持的邮箱 Provider。"""

    GMAIL = "gmail"
    OUTLOOK = "outlook"
    ALIMAIL = "alimail"


class AliMailApiEdition(StrEnum):
    """阿里邮箱开放平台版本。"""

    STANDARD = "standard"
    LOCALIZED = "localized"


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
    service_auth_token: SecretStr | None = Field(default=None, max_length=512)
    approval_signing_secret: SecretStr | None = Field(default=None, min_length=32)
    database_url: SecretStr | None = None
    mail_provider: MailProviderKind | None = None
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
    alimail_api_edition: AliMailApiEdition = AliMailApiEdition.STANDARD
    alimail_client_id: str | None = Field(default=None, max_length=512)
    alimail_client_secret: SecretStr | None = None
    alimail_account_email: str | None = Field(default=None, min_length=3, max_length=320)
    model: str = Field(default="openai/gpt-5.1", min_length=1, max_length=255)
    user_timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=100)
    unsubscribe_state_path: Path = Path(".data/unsubscribe-state.json")
    provider_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    agent_timeout_seconds: float = Field(default=120.0, ge=1, le=600)
    max_request_bytes: int = Field(default=1_048_576, ge=1024, le=10_485_760)
    max_attachment_bytes: int = Field(default=26_214_400, ge=1024, le=104_857_600)

    @field_validator("service_auth_token")
    @classmethod
    def validate_service_auth_token(cls, value: SecretStr | None) -> SecretStr | None:
        """服务令牌必须可由恒定时间字符串比较安全处理。"""
        if value is None:
            return None
        token = value.get_secret_value()
        if not token or not token.isascii():
            raise ValueError("SERVICE_AUTH_TOKEN 必须是非空 ASCII 字符串")
        return value

    @field_validator("user_timezone")
    @classmethod
    def validate_user_timezone(cls, value: str) -> str:
        """要求使用服务端可解析的 IANA 时区。"""
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("USER_TIMEZONE 必须是有效的 IANA 时区") from exc
        return value

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        """生产环境必须配置服务间认证。"""
        missing: list[str] = []
        if self.app_env is AppEnvironment.PRODUCTION:
            if self.service_auth_token is None:
                missing.append("SERVICE_AUTH_TOKEN")
            if self.approval_signing_secret is None:
                missing.append("APPROVAL_SIGNING_SECRET")
            if self.database_url is None:
                missing.append("DATABASE_URL")
            if self.mail_provider is None:
                missing.append("MAIL_PROVIDER")
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
    return Settings(_env_file=".env", _env_file_encoding="utf-8")


def build_default_auth_context(settings: Settings) -> AuthContext:
    """为首期单用户部署生成统一身份上下文。"""
    return AuthContext(user_id=settings.single_user_id)
