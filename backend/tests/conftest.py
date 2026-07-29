"""隔离自动化测试与开发机配置。"""

import os
from pathlib import Path

_CONFIG_ENVIRONMENT_VARIABLES = (
    "APP_ENV",
    "SINGLE_USER_ID",
    "SERVICE_AUTH_TOKEN",
    "APPROVAL_SIGNING_SECRET",
    "DATABASE_URL",
    "MAIL_PROVIDER",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_ACCESS_TOKEN",
    "GOOGLE_REFRESH_TOKEN",
    "MICROSOFT_TENANT_ID",
    "MICROSOFT_CLIENT_ID",
    "MICROSOFT_CLIENT_SECRET",
    "MICROSOFT_ACCESS_TOKEN",
    "MICROSOFT_REFRESH_TOKEN",
    "ALIMAIL_API_EDITION",
    "ALIMAIL_CLIENT_ID",
    "ALIMAIL_CLIENT_SECRET",
    "ALIMAIL_ACCOUNT_EMAIL",
    "MODEL",
    "USER_TIMEZONE",
    "UNSUBSCRIBE_STATE_PATH",
    "PROVIDER_TIMEOUT_SECONDS",
    "AGENT_TIMEOUT_SECONDS",
    "MAX_REQUEST_BYTES",
    "MAX_ATTACHMENT_BYTES",
)
_ORIGINAL_WORKING_DIRECTORY = Path.cwd()
_ISOLATED_WORKING_DIRECTORY = Path(__file__).parent

for _variable in _CONFIG_ENVIRONMENT_VARIABLES:
    os.environ.pop(_variable, None)


def pytest_sessionstart() -> None:
    """测试收集前离开包含本地 .env 的后端目录。"""
    os.chdir(_ISOLATED_WORKING_DIRECTORY)


def pytest_sessionfinish() -> None:
    """测试会话结束后恢复调用方的工作目录。"""
    os.chdir(_ORIGINAL_WORKING_DIRECTORY)
