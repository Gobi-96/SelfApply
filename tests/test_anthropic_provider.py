from unittest.mock import patch

import httpx

from selfapply import llm


def test_anthropic_provider_detection():
    with patch.dict("os.environ", {
        "ANTHROPIC_API_KEY": "anthropic-test",
        "LLM_MODEL": "claude-sonnet-4-5",
    }, clear=True):
        base, model, key = llm._detect_provider()
    assert base == "https://api.anthropic.com/v1"
    assert model == "claude-sonnet-4-5"
    assert key == "anthropic-test"


def test_anthropic_messages_request_and_response_mapping():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["payload"] = __import__("json").loads(request.content)
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "SelfApply OK"}],
            "usage": {"input_tokens": 10, "output_tokens": 3},
        })

    client = llm.LLMClient(
        "https://api.anthropic.com/v1", "claude-sonnet-4-5", "test-key",
    )
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        result = client.chat([
            {"role": "system", "content": "Be concise"},
            {"role": "user", "content": "Test"},
        ])
    finally:
        client.close()
    assert result == "SelfApply OK"
    assert captured["headers"]["x-api-key"] == "test-key"
    assert captured["payload"]["system"] == "Be concise"
    assert captured["payload"]["messages"] == [{"role": "user", "content": "Test"}]


def test_codex_cost_estimate_is_nonzero_and_configurable():
    usage = {"input_tokens": 1_000_000, "cached_input_tokens": 500_000,
             "output_tokens": 100_000}
    with patch.dict("os.environ", {
        "CODEX_INPUT_USD_PER_MILLION": "2",
        "CODEX_CACHED_INPUT_USD_PER_MILLION": "0.2",
        "CODEX_OUTPUT_USD_PER_MILLION": "10",
    }, clear=False):
        assert llm.estimate_codex_cost(usage) == 2.1


def test_provider_inventory_makes_priority_visible():
    with patch.dict("os.environ", {
        "GEMINI_API_KEY": "g", "OPENAI_API_KEY": "o",
        "ANTHROPIC_API_KEY": "a", "LLM_URL": "http://localhost:1234/v1",
    }, clear=True):
        assert llm.configured_providers() == ["Gemini", "OpenAI", "Anthropic", "local"]
