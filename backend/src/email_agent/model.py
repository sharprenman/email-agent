"""构造邮件智能体使用的聊天模型。"""

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from .config import Settings


def build_model(settings: Settings) -> BaseChatModel:
    """使用唯一的应用配置构造 OpenAI 兼容聊天模型。"""
    options: dict[str, str] = {}
    if settings.openai_api_key is not None:
        options["api_key"] = settings.openai_api_key.get_secret_value()
    if settings.openai_base_url is not None:
        options["base_url"] = settings.openai_base_url
    return init_chat_model(
        f"openai:{settings.model}",
        use_responses_api=False,
        disable_streaming=True,
        **options,
    )
