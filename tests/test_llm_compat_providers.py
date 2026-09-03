import httpx

from selfapply import llm


def _client(base: str, model: str, handler) -> llm.LLMClient:
    client = llm.LLMClient(base, model, "test-key")
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_openai_compatible_chat_path():
    def handler(request):
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "OK"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        })

    client = _client("https://api.openai.com/v1", "gpt-4o-mini", handler)
    try:
        assert client.ask("test") == "OK"
    finally:
        client.close()


def test_gemini_compat_403_falls_back_to_native():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(403, text="not on compatibility endpoint")
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "OK"}]}}],
            "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 1},
        })

    client = _client(llm._GEMINI_COMPAT_BASE, "gemini-preview", handler)
    try:
        assert client.ask("test") == "OK"
        assert client._use_native_gemini is True
        assert len(calls) == 2
    finally:
        client.close()
