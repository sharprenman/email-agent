"""聊天模型工厂配置测试。"""

from email_agent.config import Settings
from email_agent.model import build_model


def test_build_model_uses_single_settings_source_and_gateway_safe_options(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    expected_model = object()

    def fake_init_chat_model(model: str, **options):
        captured["model"] = model
        captured["options"] = options
        return expected_model

    monkeypatch.setattr("email_agent.model.init_chat_model", fake_init_chat_model)
    settings = Settings(
        model="openai/gpt-5.1",
        openai_api_key="gateway-key",
        openai_base_url="https://gateway.example/v1",
    )

    assert build_model(settings) is expected_model
    assert captured == {
        "model": "openai:openai/gpt-5.1",
        "options": {
            "api_key": "gateway-key",
            "base_url": "https://gateway.example/v1",
            "use_responses_api": False,
            "disable_streaming": True,
        },
    }
