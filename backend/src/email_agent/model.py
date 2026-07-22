"""构造邮件智能体使用的聊天模型。"""

import os

import dotenv
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel


def build_model() -> BaseChatModel:
    """按项目已验证的兼容方式读取环境并构造模型。"""
    dotenv.load_dotenv()
    raw_model = os.getenv("MODEL", "openai/gpt-5.1")
    return init_chat_model(
        f"openai:{raw_model}",
        use_responses_api=False,
    )
