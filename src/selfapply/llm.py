"""
Unified LLM client for SelfApply.

Auto-detects provider from environment:
  GEMINI_API_KEY  -> Google Gemini (default: gemini-2.0-flash)
  OPENAI_API_KEY  -> OpenAI (default: gpt-4o-mini)
  ANTHROPIC_API_KEY -> Anthropic Claude (default: claude-sonnet-4-5)
  LLM_URL         -> Local llama.cpp / Ollama compatible endpoint

LLM_MODEL env var overrides the model name for any provider.
"""

import logging
import os
import threading
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

log = logging.getLogger(__name__)


def normalize_local_url(value: str) -> str:
    """Normalize a bare local server address to its OpenAI-compatible v1 base."""
    url = value.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme in {"http", "https"} and parts.netloc and parts.path in {"", "/"}:
        return urlunsplit((parts.scheme, parts.netloc, "/v1", parts.query, parts.fragment))
    return url

# ---------------------------------------------------------------------------
# Cost tracking — a soft spend guardrail, not billing-accurate accounting.
# Unrecognized/local models price at $0 and simply don't count against a cap.
# ---------------------------------------------------------------------------

_cost_lock = threading.Lock()
_cumulative_cost_usd = 0.0
_cost_limit_usd: float | None = None

_PRICING_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    "gemini-2.0-flash": (0.10, 0.40),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def _price_for(model: str) -> tuple[float, float]:
    model_lower = model.lower()
    for key, price in _PRICING_PER_MILLION_TOKENS.items():
        if key in model_lower:
            return price
    return (0.0, 0.0)


def estimate_codex_cost(usage: dict, model: str = "") -> float:
    """Return a configurable estimate for Codex CLI token usage.

    Codex JSONL exposes tokens but not a billed-dollar total. A zero here
    makes SelfApply's spend guard ineffective, so use conservative defaults
    that users can override when their plan/model pricing differs.
    """
    input_rate = float(os.environ.get("CODEX_INPUT_USD_PER_MILLION", "2.00"))
    cached_rate = float(os.environ.get("CODEX_CACHED_INPUT_USD_PER_MILLION", "0.20"))
    output_rate = float(os.environ.get("CODEX_OUTPUT_USD_PER_MILLION", "10.00"))
    input_tokens = max(0, int(usage.get("input_tokens", 0) or 0))
    cached_tokens = min(input_tokens, max(0, int(usage.get("cached_input_tokens", 0) or 0)))
    uncached_tokens = input_tokens - cached_tokens
    output_tokens = max(0, int(usage.get("output_tokens", 0) or 0))
    return (
        uncached_tokens * input_rate
        + cached_tokens * cached_rate
        + output_tokens * output_rate
    ) / 1_000_000


def configured_providers() -> list[str]:
    """Return every configured provider, in selection-priority order."""
    providers = []
    if os.environ.get("GEMINI_API_KEY"):
        providers.append("Gemini")
    if os.environ.get("OPENAI_API_KEY"):
        providers.append("OpenAI")
    if os.environ.get("ANTHROPIC_API_KEY"):
        providers.append("Anthropic")
    if os.environ.get("LLM_URL"):
        providers.append("local")
    return providers


def smoke_test() -> tuple[str, str]:
    """Verify the selected provider/key/model with one tiny real request."""
    providers = configured_providers()
    if len(providers) > 1:
        log.warning(
            "Multiple LLM providers are configured (%s); SelfApply selected %s. "
            "Remove unused keys to make provider selection explicit.",
            ", ".join(providers), providers[0],
        )
    client = get_client()
    reply = client.ask("Reply with exactly OK.", max_tokens=8, temperature=0)
    if not reply.strip():
        raise RuntimeError("The configured LLM returned an empty response.")
    return providers[0] if providers else "local", client.model


def record_cost(usd: float) -> float:
    """Add to the running cost total and return the new cumulative total."""
    global _cumulative_cost_usd
    with _cost_lock:
        _cumulative_cost_usd += usd
        return _cumulative_cost_usd


def get_cumulative_cost_usd() -> float:
    with _cost_lock:
        return _cumulative_cost_usd


def reset_cumulative_cost_usd() -> None:
    global _cumulative_cost_usd
    with _cost_lock:
        _cumulative_cost_usd = 0.0


def set_cost_limit(usd: float | None) -> None:
    """Set a process-local hard boundary checked before every LLM request."""
    global _cost_limit_usd
    _cost_limit_usd = usd


def _check_cost_limit() -> None:
    if _cost_limit_usd is not None and get_cumulative_cost_usd() >= _cost_limit_usd:
        raise RuntimeError(
            f"LLM spend cap reached (${get_cumulative_cost_usd():.2f} >= "
            f"${_cost_limit_usd:.2f})."
        )

# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def _detect_provider() -> tuple[str, str, str]:
    """Return (base_url, model, api_key) based on environment variables.

    Reads env at call time (not module import time) so that load_env() called
    in _bootstrap() is always visible here.
    """
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    local_url = os.environ.get("LLM_URL", "")
    model_override = os.environ.get("LLM_MODEL", "")

    if gemini_key and not local_url:
        return (
            "https://generativelanguage.googleapis.com/v1beta/openai",
            model_override or "gemini-2.0-flash",
            gemini_key,
        )

    if openai_key and not local_url:
        return (
            "https://api.openai.com/v1",
            model_override or "gpt-4o-mini",
            openai_key,
        )

    if anthropic_key and not local_url:
        return (
            "https://api.anthropic.com/v1",
            model_override or "claude-sonnet-4-5",
            anthropic_key,
        )

    if local_url:
        return (
            normalize_local_url(local_url),
            model_override or "local-model",
            os.environ.get("LLM_API_KEY", ""),
        )

    raise RuntimeError(
        "No LLM provider configured. "
        "Set GEMINI_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, or LLM_URL "
        "in your environment."
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_MAX_RETRIES = 5
_TIMEOUT = 120  # seconds

# Base wait on first 429/503 (doubles each retry, caps at 60s).
# Gemini free tier is 15 RPM = 4s minimum between requests; 10s gives headroom.
_RATE_LIMIT_BASE_WAIT = 10


_GEMINI_COMPAT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
_GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"


class LLMClient:
    """Thin LLM client supporting OpenAI-compatible and native Gemini endpoints.

    For Gemini keys, starts on the OpenAI-compat layer. On a 403 (which
    happens with preview/experimental models not exposed via compat), it
    automatically switches to the native generateContent API and stays there
    for the lifetime of the process.
    """

    def __init__(self, base_url: str, model: str, api_key: str) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self._client = httpx.Client(timeout=_TIMEOUT)
        # True once we've confirmed the native Gemini API works for this model
        self._use_native_gemini: bool = False
        self._is_gemini: bool = base_url.startswith(_GEMINI_COMPAT_BASE)
        self._is_anthropic: bool = base_url.startswith("https://api.anthropic.com")

    def _chat_anthropic(
        self, messages: list[dict], temperature: float, max_tokens: int,
    ) -> str:
        """Call Anthropic's Messages API using the shared client contract."""
        system = "\n\n".join(
            str(message.get("content", ""))
            for message in messages if message.get("role") == "system"
        )
        conversation = [
            {"role": message["role"], "content": message.get("content", "")}
            for message in messages if message.get("role") in {"user", "assistant"}
        ]
        payload: dict = {
            "model": self.model, "messages": conversation,
            "temperature": temperature, "max_tokens": max_tokens,
        }
        if system:
            payload["system"] = system
        response = self._client.post(
            f"{self.base_url}/messages", json=payload,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        response.raise_for_status()
        data = response.json()
        usage = data.get("usage", {})
        in_price, out_price = _price_for(self.model)
        cost = (
            usage.get("input_tokens", 0) / 1_000_000 * in_price
            + usage.get("output_tokens", 0) / 1_000_000 * out_price
        )
        if cost:
            record_cost(cost)
        return "".join(
            block.get("text", "") for block in data.get("content", [])
            if block.get("type") == "text"
        )

    # -- Native Gemini API --------------------------------------------------

    def _chat_native_gemini(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the native Gemini generateContent API.

        Used automatically when the OpenAI-compat endpoint returns 403,
        which happens for preview/experimental models not exposed via compat.

        Converts OpenAI-style messages to Gemini's contents/systemInstruction
        format transparently.
        """
        contents: list[dict] = []
        system_parts: list[dict] = []

        for msg in messages:
            role = msg["role"]
            text = msg.get("content", "")
            if role == "system":
                system_parts.append({"text": text})
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": text}]})
            elif role == "assistant":
                # Gemini uses "model" instead of "assistant"
                contents.append({"role": "model", "parts": [{"text": text}]})

        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}

        url = f"{_GEMINI_NATIVE_BASE}/models/{self.model}:generateContent"
        resp = self._client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            params={"key": self.api_key},
        )
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usageMetadata", {})
        in_price, out_price = _price_for(self.model)
        cost = (
            usage.get("promptTokenCount", 0) / 1_000_000 * in_price
            + usage.get("candidatesTokenCount", 0) / 1_000_000 * out_price
        )
        if cost:
            record_cost(cost)
        return data["candidates"][0]["content"]["parts"][0]["text"]

    # -- OpenAI-compat API --------------------------------------------------

    def _chat_compat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the OpenAI-compatible endpoint."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )

        # 403 on Gemini compat = model not available on compat layer.
        # Raise a specific sentinel so chat() can switch to native API.
        if resp.status_code == 403 and self._is_gemini:
            raise _GeminiCompatForbidden(resp)

        return self._handle_compat_response(resp)

    def _handle_compat_response(self, resp: httpx.Response) -> str:
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage", {})
        in_price, out_price = _price_for(self.model)
        cost = (
            usage.get("prompt_tokens", 0) / 1_000_000 * in_price
            + usage.get("completion_tokens", 0) / 1_000_000 * out_price
        )
        if cost:
            record_cost(cost)
        return data["choices"][0]["message"]["content"]

    # -- public API ---------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        """Send a chat completion request and return the assistant message text."""
        # Qwen3 optimization: prepend /no_think to skip chain-of-thought
        # reasoning, saving tokens on structured extraction tasks.
        if "qwen" in self.model.lower() and messages:
            first = messages[0]
            if first.get("role") == "user" and not first["content"].startswith("/no_think"):
                messages = [{"role": first["role"], "content": f"/no_think\n{first['content']}"}] + messages[1:]

        for attempt in range(_MAX_RETRIES):
            _check_cost_limit()
            try:
                if self._is_anthropic:
                    return self._chat_anthropic(messages, temperature, max_tokens)
                # Route to native Gemini if we've already confirmed it's needed
                if self._use_native_gemini:
                    return self._chat_native_gemini(messages, temperature, max_tokens)

                return self._chat_compat(messages, temperature, max_tokens)

            except _GeminiCompatForbidden as exc:
                # Model not available on OpenAI-compat layer — switch to native.
                log.warning(
                    "Gemini compat endpoint returned 403 for model '%s'. "
                    "Switching to native generateContent API. "
                    "(Preview/experimental models are often compat-only on native.)",
                    self.model,
                )
                self._use_native_gemini = True
                # Retry immediately with native — don't count as a rate-limit wait
                try:
                    return self._chat_native_gemini(messages, temperature, max_tokens)
                except httpx.HTTPStatusError as native_exc:
                    raise RuntimeError(
                        f"Both Gemini endpoints failed. Compat: 403 Forbidden. "
                        f"Native: {native_exc.response.status_code} — "
                        f"{native_exc.response.text[:200]}"
                    ) from native_exc

            except httpx.HTTPStatusError as exc:
                resp = exc.response
                if resp.status_code in (429, 503) and attempt < _MAX_RETRIES - 1:
                    # Respect Retry-After header if provided (Gemini sends this).
                    retry_after = (
                        resp.headers.get("Retry-After")
                        or resp.headers.get("X-RateLimit-Reset-Requests")
                    )
                    if retry_after:
                        try:
                            wait = float(retry_after)
                        except (ValueError, TypeError):
                            wait = _RATE_LIMIT_BASE_WAIT * (2 ** attempt)
                    else:
                        wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)

                    log.warning(
                        "LLM rate limited (HTTP %s). Waiting %ds before retry %d/%d. "
                        "Tip: Gemini free tier = 15 RPM. Consider a paid account "
                        "or switching to a local model.",
                        resp.status_code, wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

            except (httpx.TimeoutException, httpx.RequestError):
                if attempt < _MAX_RETRIES - 1:
                    wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)
                    log.warning(
                        "LLM network request failed, retrying in %ds (attempt %d/%d)",
                        wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

        raise RuntimeError("LLM request failed after all retries")

    def ask(self, prompt: str, **kwargs) -> str:
        """Convenience: single user prompt -> assistant response."""
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        self._client.close()


class _GeminiCompatForbidden(Exception):
    """Sentinel: Gemini OpenAI-compat returned 403. Switch to native API."""
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"Gemini compat 403: {response.text[:200]}")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: LLMClient | None = None


def reset_client() -> None:
    """Forget the cached provider so newly saved UI settings take effect."""
    global _instance
    _instance = None


def get_client() -> LLMClient:
    """Return (or create) the module-level LLMClient singleton."""
    global _instance
    if _instance is None:
        base_url, model, api_key = _detect_provider()
        log.info("LLM provider: %s  model: %s", base_url, model)
        _instance = LLMClient(base_url, model, api_key)
    return _instance
