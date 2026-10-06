from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator
from typing import Any, cast

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message import ChatCompletionMessage

logger = logging.getLogger(__name__)

# Process-wide throttle state. Every Celery task builds its own client, so a
# per-instance timestamp cannot pace bursts from concurrent threads. These
# module globals make ALL instances in this worker process share one lane.
_THROTTLE_LOCK = threading.Lock()
_shared_last_request: float = 0.0
_shared_cooldown_until: float = 0.0
# Per-model cooldown. Gemini meters each model separately (20/day each), so
# draining gemini-3.8-flash must NOT park gemini-flash-latest. Shared pools
# (openrouter/free router, a proxy strategy) keep a "*" bucket.
_model_cooldowns: dict[str, float] = {}


def _is_rate_limit_error(exc: BaseException) -> bool:
    """True if the provider says quota exhausted (429 / RateLimitError)."""
    if exc.__class__.__name__ == "RateLimitError":
        return True
    status = getattr(exc, "status_code", None)
    if status == 429:
        return True
    text = str(exc).lower()
    return "error code: 429" in text or "rate limit" in text or "http/1.1 429" in text


def _is_transient_error(exc: BaseException) -> bool:
    """True for retryable upstream hiccups (503/529/overloaded/timeouts).

    Gemini's free tier returns 503 "high demand" under load; OpenRouter and
    friends return 529/503 for the same reason. These are worth retrying —
    unlike 429, the quota is not spent.
    """
    status = getattr(exc, "status_code", None)
    if status in (408, 500, 502, 503, 504, 529):
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "error code: 503",
            "error code: 529",
            "error code: 502",
            "error code: 500",
            "unavailable",
            "overloaded",
            "high demand",
            "timeout",
            "timed out",
            "connection reset",
        )
    )


def _cooldown_remaining_locked(model: str | None = None) -> float:
    """Caller MUST already hold _THROTTLE_LOCK (it is not reentrant)."""
    now = time.time()
    remaining = max(0.0, _shared_cooldown_until - now)
    if model:
        remaining = max(remaining, max(0.0, _model_cooldowns.get(model, 0.0) - now))
    return remaining


def _cooldown_remaining(model: str | None = None) -> float:
    with _THROTTLE_LOCK:
        return _cooldown_remaining_locked(model)


def llm_cooldown_remaining(model: str | None = None) -> float:
    """Seconds left on the cooldown for `model` (or the shared lane)."""
    return _cooldown_remaining(model)

MODEL_CONFIGS: dict[str, ModelConfig] = {}


class ModelConfig:
    def __init__(
        self,
        name: str,
        context_window: int,
        max_output_tokens: int,
        supports_streaming: bool = True,
        is_free: bool = True,
    ) -> None:
        self.name = name
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.supports_streaming = supports_streaming
        self.is_free = is_free


MODEL_CONFIGS = {
    "openrouter/free": ModelConfig(
        name="openrouter/free",
        context_window=200000,
        max_output_tokens=4096,
        is_free=True,
    ),
    "google/gemma-4-31b-it:free": ModelConfig(
        name="google/gemma-4-31b-it:free",
        context_window=262144,
        max_output_tokens=4096,
        is_free=True,
    ),
    "qwen/qwen3.8-27b:free": ModelConfig(
        name="qwen/qwen3.8-27b:free",
        context_window=262144,
        max_output_tokens=4096,
        is_free=True,
    ),
    "nvidia/nemotron-3.5-lightning:free": ModelConfig(
        name="nvidia/nemotron-3.5-lightning:free",
        context_window=1000000,
        max_output_tokens=4096,
        is_free=True,
    ),
    "nvidia/nemotron-3-ultra-550b-a55b:free": ModelConfig(
        name="nvidia/nemotron-3-ultra-550b-a55b:free",
        context_window=1000000,
        max_output_tokens=4096,
        is_free=True,
    ),
    "liquid/lfm-2.5-2.6b:free": ModelConfig(
        name="liquid/lfm-2.5-2.6b:free",
        context_window=131072,
        max_output_tokens=4096,
        is_free=True,
    ),
    "google/gemma-4-26b-a4b-it:free": ModelConfig(
        name="google/gemma-4-26b-a4b-it:free",
        context_window=262144,
        max_output_tokens=4096,
        is_free=True,
    ),
    "google/gemma-2-9b-it:free": ModelConfig(
        name="google/gemma-2-9b-it:free",
        context_window=8192,
        max_output_tokens=2048,
        is_free=True,
    ),
    "microsoft/phi-3-mini-128k-instruct:free": ModelConfig(
        name="microsoft/phi-3-mini-128k-instruct:free",
        context_window=131072,
        max_output_tokens=4096,
        is_free=True,
    ),
    "llama3.1:8b": ModelConfig(
        name="llama3.1:8b",
        context_window=131072,
        max_output_tokens=4096,
        is_free=True,
    ),
    "qwen2.5:7b": ModelConfig(
        name="qwen2.5:7b",
        context_window=32768,
        max_output_tokens=4096,
        is_free=True,
    ),
    "mistral:7b": ModelConfig(
        name="mistral:7b",
        context_window=32768,
        max_output_tokens=4096,
        is_free=True,
    ),
    "qwen/qwen3-32b": ModelConfig(
        name="qwen/qwen3-32b",
        context_window=128000,
        max_output_tokens=4096,
        is_free=True,
    ),
    "openai/gpt-oss-120b": ModelConfig(
        name="openai/gpt-oss-120b",
        context_window=131072,
        max_output_tokens=4096,
        is_free=True,
    ),
    "openai/gpt-oss-20b": ModelConfig(
        name="openai/gpt-oss-20b",
        context_window=131072,
        max_output_tokens=4096,
        is_free=True,
    ),
    # Gemini direct models. NOTE: 2.0-flash / 2.5-flash are retired for new
    # users (404 "no longer available"); 3.8-flash + the -latest alias are live.
    "gemini-3.8-flash": ModelConfig(
        name="gemini-3.8-flash",
        context_window=1048576,
        max_output_tokens=4096,
        is_free=True,
    ),
    "gemini-flash-latest": ModelConfig(
        name="gemini-flash-latest",
        context_window=1048576,
        max_output_tokens=4096,
        is_free=True,
    ),
    # free-llm-proxy-router routing strategies (resolved server-side to a
    # free model; budgets assume a mid-size context window).
    "small": ModelConfig(
        name="small",
        context_window=32768,
        max_output_tokens=4096,
        is_free=True,
    ),
    "speed": ModelConfig(
        name="speed",
        context_window=32768,
        max_output_tokens=4096,
        is_free=True,
    ),
    "openai/text-embedding-3-small": ModelConfig(
        name="openai/text-embedding-3-small",
        context_window=8192,
        max_output_tokens=0,
        is_free=True,
    ),
}


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        default_model: str = "google/gemma-4-31b-it:free",
        fallback_model: str = "liquid/lfm-2.5-2.6b:free",
        timeout: float = 60.0,
        app_url: str = "https://texponse.ai",
        app_name: str = "Texponse",
        local_mode: bool = False,
        min_interval: float = 1.0,
        cooldown_seconds: float = 90.0,
        transient_attempts: int = 2,
        reasoning_effort: str | None = None,
        per_model_quota: bool = False,
        model_pool: list[str] | None = None,
    ) -> None:
        self._api_key = api_key or "ollama"  # Ollama ignores the key but SDK requires one
        self._base_url = base_url
        self._local_mode = local_mode or "11434" in base_url
        self._default_model = default_model
        self._fallback_model = fallback_model
        self._timeout = timeout
        self._app_url = app_url
        self._app_name = app_name
        self._min_interval = min_interval
        self._cooldown_seconds = cooldown_seconds
        self._transient_attempts = transient_attempts
        self._reasoning_effort = reasoning_effort
        self._per_model_quota = per_model_quota
        # Ordered rotation pool (drained-model failover). Defaults to
        # [default, fallback] when the provider meters a shared bucket.
        self._model_pool = [
            m for m in (model_pool or [default_model, fallback_model]) if m
        ]

        self._client = AsyncOpenAI(
            api_key=self._api_key,
            base_url=base_url,
            default_headers={
                "HTTP-Referer": app_url,
                "X-OpenRouter-Title": app_name,
            },
            timeout=timeout,
        )

        self._last_request_time: float = 0.0
        self._circuit_open_until: float = 0.0
        self._consecutive_failures: int = 0

    async def close(self) -> None:
        await self._client.close()

    @property
    def circuit_open(self) -> bool:
        return time.time() < self._circuit_open_until

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def _get_model_config(self, model: str) -> ModelConfig:
        if model in MODEL_CONFIGS:
            return MODEL_CONFIGS[model]
        default = MODEL_CONFIGS.get(self._default_model)
        if default is not None:
            return default
        return next(iter(MODEL_CONFIGS.values()))

    async def _check_rate_limit(self, model: str | None = None) -> None:
        global _shared_last_request
        with _THROTTLE_LOCK:
            remaining = _cooldown_remaining_locked(model)
            if remaining > 0:
                # Fail fast: caller serves the static SMS instead of
                # queueing behind an exhausted quota.
                target = model or "provider"
                raise RuntimeError(f"LLM provider cooling down ({remaining:.0f}s left): {target}")
            gap = _shared_last_request + self._min_interval - time.time()
            if self._local_mode:
                return  # local/proxied inference: no pacing needed
        # Shared lane: brief pacing sleep OUTSIDE the lock.
        if gap > 0:
            await asyncio.sleep(min(gap, 5.0))
        with _THROTTLE_LOCK:
            _shared_last_request = time.time()

    def _enter_cooldown(self, model: str | None = None) -> None:
        """Park this model (and the lane) after a 429.

        A provider that meters per-model (Gemini) only gets `model` parked;
        a shared-pool provider also gets the global lane parked.
        """
        global _shared_cooldown_until
        until = time.time() + self._cooldown_seconds
        with _THROTTLE_LOCK:
            if model and self._per_model_quota:
                _model_cooldowns[model] = max(_model_cooldowns.get(model, 0.0), until)
            else:
                _shared_cooldown_until = max(_shared_cooldown_until, until)

    async def embed(
        self,
        texts: list[str],
        model: str = "openai/text-embedding-3-small",
    ) -> list[list[float]]:
        await self._check_rate_limit()

        try:
            resp = await self._client.embeddings.create(
                model=model,
                input=texts,
            )
        except Exception as exc:
            logger.error("Embedding API failed: %s", exc)
            self._consecutive_failures += 1
            if self._consecutive_failures >= 5:
                self._circuit_open_until = time.time() + 60
            raise

        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        return [list(item.embedding) for item in resp.data]

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.7,
        stream: bool = False,
    ) -> dict[str, Any] | AsyncIterator[str]:
        if model is None:
            model = self._default_model

        await self._check_rate_limit(model)

        failure: Exception | None = None
        for attempt in range(self._transient_attempts + 1):
            try:
                if stream:
                    return await self._chat_stream(messages, model, max_tokens, temperature)
                return await self._chat_non_stream(messages, model, max_tokens, temperature)
            except Exception as retry_exc:
                failure = retry_exc
                if _is_transient_error(retry_exc) and attempt < self._transient_attempts:
                    delay = min(4.0, 1.0 * (2**attempt))
                    logger.warning(
                        "Transient LLM error for model %s (attempt %d/%d), retrying in %.1fs: %s",
                        model,
                        attempt + 1,
                        self._transient_attempts + 1,
                        delay,
                        retry_exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                break

        assert failure is not None
        logger.warning("LLM call failed for model %s: %s", model, failure)
        self._consecutive_failures += 1
        if self._consecutive_failures >= 5:
            self._circuit_open_until = time.time() + 60

        if _is_rate_limit_error(failure):
            # Park this model so the rest of the process stops hitting a dry
            # bucket. Under per-model metering its siblings stay usable;
            # a shared pool parks the whole lane.
            self._enter_cooldown(model)

        candidates = self._candidate_models(model, quota_exhausted=_is_rate_limit_error(failure))
        if candidates:
            next_model = candidates[0]
            logger.info("Retrying with %s", next_model)
            return await self._retry_with_fallback(
                messages, max_tokens, temperature, stream, str(failure), next_model
            )
        raise failure

    def _candidate_models(self, failed_model: str, quota_exhausted: bool) -> list[str]:
        """Next models to try after `failed_model`.

        Gemini meters 20 free requests/day PER MODEL, so a drained model has
        siblings with untouched buckets — rotating is free capacity. A shared
        pool (openrouter router, proxy strategy) has one bucket, so after a
        429 nothing is left to try and we fail fast to the static SMS.
        """
        pool = [m for m in self._model_pool if m != failed_model]
        if not self._per_model_quota:
            # Shared pool: only the single configured fallback is worth a try,
            # and not at all when the quota itself is dry.
            fb = [m for m in self._model_pool if m == self._fallback_model]
            if quota_exhausted:
                return []
            return fb
        # Skip anything already parked by a previous 429.
        return [m for m in pool if _cooldown_remaining(m) <= 0]

    async def _chat_non_stream(
        self,
        messages: list[dict[str, str]],
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        extra_body: dict[str, Any] = {}
        if self._reasoning_effort:
            # Gemini 3.x flash thinks by default and burns max_tokens on
            # invisible reasoning (we saw 60 tokens in -> 0 content out).
            # "none" makes it answer directly, which is what an SMS needs.
            extra_body["reasoning_effort"] = self._reasoning_effort
        resp: ChatCompletion = await self._client.chat.completions.create(
            model=model,
            messages=cast(Any, messages),
            max_tokens=max_tokens,
            temperature=temperature,
            stream=False,
            **({"extra_body": extra_body} if extra_body else {}),
        )

        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

        choice: Choice = resp.choices[0]
        msg: ChatCompletionMessage = choice.message
        usage = resp.usage or None

        from texponse.security.guardrails import strip_reasoning

        # Reasoning models (DeepSeek R1, QwQ, Nemotron) may put chain-of-thought
        # in `reasoning`/`reasoning_content` extras or inline <think> blocks.
        # Never surface those: only `content` goes out, stripped of think tags.
        content = strip_reasoning(msg.content or "")

        return {
            "model": resp.model,
            "content": content,
            "prompt_tokens": usage.prompt_tokens if usage else 0,
            "completion_tokens": usage.completion_tokens if usage else 0,
            "total_tokens": usage.total_tokens if usage else 0,
            "finish_reason": choice.finish_reason,
            "fallback_used": False,
        }

    async def _chat_stream(
        self,
        messages: list[dict[str, str]],
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        stream = await self._client.chat.completions.create(
            model=model,
            messages=cast(Any, messages),
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
        )

        async def _generator() -> AsyncIterator[str]:
            import re as _re

            _think_re = _re.compile(r"<(think|reasoning)>.*?(</\1>|$)", _re.IGNORECASE | _re.DOTALL)
            buf = ""
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    # Drop reasoning deltas if the provider exposes them separately.
                    delta = chunk.choices[0].delta
                    if getattr(delta, "reasoning", None) or getattr(
                        delta, "reasoning_content", None
                    ):
                        continue
                    text = _think_re.sub("", delta.content)
                    buf += text
                    # Hold back while inside an unclosed <think> block.
                    if _re.search(r"<(think|reasoning)>", buf, _re.IGNORECASE) and not _re.search(
                        r"</(think|reasoning)>", buf, _re.IGNORECASE
                    ):
                        continue
                    yield text
                    buf = ""
            self._consecutive_failures = 0
            self._circuit_open_until = 0.0

        return _generator()

    async def _retry_with_fallback(
        self,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        stream: bool,
        original_error: str,
        next_model: str | None = None,
    ) -> dict[str, Any] | AsyncIterator[str]:
        # Walk the remaining pool: a sibling model may still have quota even
        # when the previous one 429'd.
        candidates = (
            [next_model, *self._candidate_models(next_model, quota_exhausted=True)]
            if next_model
            else [self._fallback_model]
        )
        last_exc: Exception | None = None
        for idx, candidate in enumerate(dict.fromkeys(candidates)):
            if _cooldown_remaining(candidate) > 0:
                continue
            if idx > 0:
                await asyncio.sleep(1.0)
            try:
                if stream:
                    return await self._chat_stream(
                        messages, candidate, max_tokens, temperature
                    )
                result = await self._chat_non_stream(
                    messages, candidate, max_tokens, temperature
                )
                result["fallback_used"] = True
                result["original_error"] = original_error
                self._consecutive_failures = 0
                self._circuit_open_until = 0.0
                return result
            except Exception as exc:
                last_exc = exc
                logger.warning("Fallback model %s also failed: %s", candidate, exc)
                if _is_rate_limit_error(exc):
                    self._enter_cooldown(candidate)
                self._consecutive_failures += 1
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"No viable model left after {original_error}")

    def estimate_tokens(self, text: str) -> int:
        if not text:
            return 0
        return len(text) // 3

    def get_context_budget(
        self,
        model_name: str,
        reserved_output_tokens: int = 512,
    ) -> int:
        cfg = self._get_model_config(model_name)
        overhead = 200
        return max(100, cfg.context_window - reserved_output_tokens - overhead)


def build_llm_client(settings: Any) -> OpenRouterClient:
    """Factory: hosted OpenRouter vs local Ollama vs free-llm-proxy sidecar.

    Ollama and free-llm-proxy both speak the OpenAI chat API, so the same
    client class works — they just need their base_url, a dummy key, a
    longer timeout, and no shared-quota throttling (local_mode: the proxy
    does its own cooldown/skip logic per provider).
    """
    provider = getattr(settings, "llm_provider", "openrouter")
    min_interval = float(getattr(settings, "llm_min_interval", 1.0))
    cooldown = float(getattr(settings, "llm_cooldown_seconds", 90.0))
    if provider == "gemini":
        # Gemini's OpenAI-compatible endpoint speaks the same chat API.
        return OpenRouterClient(
            api_key=settings.gemini_api_key,
            base_url=settings.gemini_base_url,
            default_model=settings.gemini_default_model,
            fallback_model=settings.gemini_fallback_model,
            timeout=settings.gemini_timeout,
            app_url="https://texponse.ai",
            app_name="Texponse",
            min_interval=min_interval,
            cooldown_seconds=cooldown,
            reasoning_effort=getattr(settings, "gemini_reasoning_effort", "none") or None,
            # Gemini meters each model separately (20/day each), so a drained
            # default must not park the fallback.
            per_model_quota=True,
            model_pool=list(getattr(settings, "gemini_models", []) or []) or None,
        )
    if provider == "ollama":
        return OpenRouterClient(
            api_key="ollama",
            base_url=settings.ollama_base_url,
            default_model=settings.ollama_default_model,
            fallback_model=settings.ollama_fallback_model,
            timeout=settings.ollama_timeout,
            local_mode=True,
        )
    if provider == "free-proxy":
        return OpenRouterClient(
            api_key="free-proxy",
            base_url=settings.free_proxy_base_url,
            default_model=settings.free_proxy_default_model,
            fallback_model=settings.free_proxy_fallback_model,
            timeout=settings.free_proxy_timeout,
            local_mode=True,
        )
    if provider == "groq":
        # Groq speaks the OpenAI chat API; per-minute limits are handled by
        # the shared pacing lane (llm_min_interval).
        return OpenRouterClient(
            api_key=settings.groq_api_key,
            base_url=settings.groq_base_url,
            default_model=settings.groq_default_model,
            fallback_model=settings.groq_fallback_model,
            timeout=settings.groq_timeout,
            min_interval=min_interval,
            cooldown_seconds=cooldown,
        )
    return OpenRouterClient(
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        default_model=settings.openrouter_default_model,
        fallback_model=settings.openrouter_fallback_model,
        timeout=settings.openrouter_timeout,
        min_interval=min_interval,
        cooldown_seconds=cooldown,
    )
