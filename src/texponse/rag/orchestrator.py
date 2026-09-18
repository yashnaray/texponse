from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from texponse.config import Settings
from texponse.db.models import Response as ResponseModel
from texponse.integrations.openrouter import OpenRouterClient
from texponse.rag.prompt_builder import PromptBuilder, PromptContext

logger = logging.getLogger(__name__)


PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("PHONE", re.compile(r"\b(\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")),
    ("CREDIT_CARD", re.compile(r"\b(?:\d[ -]*?){13,16}\b")),
]

INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(ignore\s+(all|your)\s+(instructions|prompts|previous))", re.IGNORECASE),
    re.compile(r"(you are now\s+(a different|not)\s+(an?|a))", re.IGNORECASE),
    re.compile(r"system\s*prompt|reveal.*secret|show.*internal", re.IGNORECASE),
    re.compile(r"forget.*previous|disregard.*above|override.*instructions", re.IGNORECASE),
]


@dataclass
class GeneratedResponse:
    content: str
    model_used: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: int
    was_fallback: bool
    retrieved_passage_count: int


class LLMOrchestrator:
    def __init__(
        self,
        settings: Settings,
        openrouter: OpenRouterClient,
        prompt_builder: PromptBuilder,
    ) -> None:
        self._settings = settings
        self._openrouter = openrouter
        self._prompt_builder = prompt_builder

    async def generate(
        self,
        query: str,
        context: PromptContext,
        company_name: str = "Texponse Customer",
    ) -> GeneratedResponse:
        start = time.perf_counter()

        if self._detect_injection(query):
            logger.warning("Prompt injection detected in query, refusing to process")
            return GeneratedResponse(
                content="I'm sorry, I can't comply with that request.",
                model_used="guardrail",
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                latency_ms=int((time.perf_counter() - start) * 1000),
                was_fallback=False,
                retrieved_passage_count=len(context.passages),
            )

        messages = self._prompt_builder.build_prompt(query, context, company_name)

        result = await self._openrouter.chat(
            messages=messages,
            model=self._settings.openrouter_default_model,
            max_tokens=self._settings.max_response_tokens,
            temperature=0.7,
        )

        if not isinstance(result, dict):
            raise TypeError("Expected non-streaming response from LLM")

        content = result.get("content", "")
        content = self._redact_pii(content)

        latency_ms = int((time.perf_counter() - start) * 1000)

        return GeneratedResponse(
            content=content,
            model_used=result.get("model", self._settings.openrouter_default_model),
            prompt_tokens=result.get("prompt_tokens", 0),
            completion_tokens=result.get("completion_tokens", 0),
            total_tokens=result.get("total_tokens", 0),
            latency_ms=latency_ms,
            was_fallback=result.get("fallback_used", False),
            retrieved_passage_count=len(context.passages),
        )

    def _detect_injection(self, text: str) -> bool:
        return any(pattern.search(text) for pattern in INJECTION_PATTERNS)

    def _redact_pii(self, text: str) -> str:
        for label, pattern in PII_PATTERNS:
            text = pattern.sub(f"[{label}_REDACTED]", text)
        return text

    def to_db_model(
        self,
        response: GeneratedResponse,
        conversation_id: str,
    ) -> ResponseModel:
        return ResponseModel(
            conversation_id=conversation_id,
            content=response.content,
            model_used=response.model_used,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            total_tokens=response.total_tokens,
            latency_ms=response.latency_ms,
            retrieved_passage_count=response.retrieved_passage_count,
            was_fallback=response.was_fallback,
        )
