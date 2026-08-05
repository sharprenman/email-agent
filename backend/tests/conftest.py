"""隔离自动化测试与开发机配置。"""

import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

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


def pytest_addoption(parser: pytest.Parser) -> None:
    """真实数据库测试必须由命令行显式指定配置文件。"""
    parser.addoption(
        "--postgres-env-file",
        default=None,
        help="显式启用 PostgreSQL 集成测试的 dotenv 文件",
    )
    parser.addoption(
        "--real-model-env-file",
        default=None,
        help="显式启用真实模型 Agent 回归的 dotenv 文件",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_model: 需要显式模型配置且会产生外部模型 API 调用的回归测试",
    )


@pytest.fixture(scope="session")
def postgres_test_url(request: pytest.FixtureRequest) -> str:
    """只从显式指定的文件读取 DATABASE_URL，避免隐式污染测试。"""
    env_file = request.config.getoption("--postgres-env-file")
    if env_file is None:
        pytest.skip("未显式启用 PostgreSQL 集成测试")
    values = dotenv_values(Path(env_file).expanduser().resolve())
    database_url = values.get("DATABASE_URL")
    if not database_url:
        pytest.fail("指定文件缺少 DATABASE_URL")
    return database_url
