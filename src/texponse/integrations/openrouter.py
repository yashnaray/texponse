from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any, cast

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message import ChatCompletionMessage

logger = logging.getLogger(__name__)

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
    "meta-llama/llama-3.1-8b-instruct:free": ModelConfig(
        name="meta-llama/llama-3.1-8b-instruct:free",
        context_window=131072,
        max_output_tokens=4096,
        is_free=True,
    ),
    "mistralai/mistral-7b-instruct:free": ModelConfig(
        name="mistralai/mistral-7b-instruct:free",
        context_window=32768,
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
        default_model: str = "meta-llama/llama-3.1-8b-instruct:free",
        fallback_model: str = "mistralai/mistral-7b-instruct:free",
        timeout: float = 60.0,
        app_url: str = "https://texponse.ai",
        app_name: str = "Texponse",
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._default_model = default_model
        self._fallback_model = fallback_model
        self._timeout = timeout
        self._app_url = app_url
        self._app_name = app_name

        self._client = AsyncOpenAI(
            api_key=api_key,
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

    @property
    def circuit_open(self) -> bool:
        return time.time() < self._circuit_open_until

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def _get_model_config(self, model: str) -> ModelConfig:
        return MODEL_CONFIGS.get(model, MODEL_CONFIGS[self._default_model])

    async def _check_rate_limit(self) -> None:
        min_interval = 1.0
        elapsed = time.time() - self._last_request_time
        if elapsed < min_interval:
            await asyncio.sleep(min_interval - elapsed)
        self._last_request_time = time.time()

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

        await self._check_rate_limit()

        try:
            if stream:
                return await self._chat_stream(messages, model, max_tokens, temperature)
            return await self._chat_non_stream(messages, model, max_tokens, temperature)
        except Exception as exc:
            logger.warning("LLM call failed for model %s: %s", model, exc)
            self._consecutive_failures += 1
            if self._consecutive_failures >= 5:
                self._circuit_open_until = time.time() + 60

            if self._fallback_model != model:
                logger.info("Retrying with fallback model: %s", self._fallback_model)
                return await self._retry_with_fallback(
                    messages, max_tokens, temperature, stream, str(exc)
                )
            raise

    async def _chat_non_stream(
        self,
        messages: list[dict[str, str]],
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        resp: ChatCompletion = await self._client.chat.completions.create(
            model=model,
            messages=cast(Any, messages),
            max_tokens=max_tokens,
            temperature=temperature,
            stream=False,
        )

        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

        choice: Choice = resp.choices[0]
        msg: ChatCompletionMessage = choice.message
        usage = resp.usage or None

        return {
            "model": resp.model,
            "content": msg.content or "",
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
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
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
    ) -> dict[str, Any] | AsyncIterator[str]:
        await asyncio.sleep(1.0)

        try:
            if stream:
                return await self._chat_stream(
                    messages,
                    self._fallback_model,
                    max_tokens,
                    temperature,
                )

            result = await self._chat_non_stream(
                messages,
                self._fallback_model,
                max_tokens,
                temperature,
            )
            result["fallback_used"] = True
            result["original_error"] = original_error
            self._consecutive_failures = 0
            self._circuit_open_until = 0.0
            return result
        except Exception as exc:
            logger.error("Fallback model also failed: %s", exc)
            self._consecutive_failures += 1
            raise

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
