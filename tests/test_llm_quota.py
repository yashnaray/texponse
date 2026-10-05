from __future__ import annotations

import pytest

from texponse.integrations.openrouter import OpenRouterClient, _is_rate_limit_error


def test_is_rate_limit_error():
    class RateLimitError(Exception):
        pass

    RateLimitError.__name__ = "RateLimitError"
    assert _is_rate_limit_error(RateLimitError("quota hit")) is True

    err_429 = RuntimeError("Error code: 429 - {'error': {'message': 'x'}}")
    assert _is_rate_limit_error(err_429) is True

    assert _is_rate_limit_error(RuntimeError("Error code: 404 - not found")) is False
    assert _is_rate_limit_error(ValueError("boom")) is False


async def test_429_skips_fallback_and_cools_down(monkeypatch):
    import texponse.integrations.openrouter as mod

    mod._shared_cooldown_until = 0.0
    client = OpenRouterClient(
        api_key="test",
        base_url="https://openrouter.ai/api/v1",
        default_model="primary",
        fallback_model="fallback",
        min_interval=0.0,
        cooldown_seconds=60.0,
    )

    calls: list[str] = []

    async def _fail_primary(messages, model, max_tokens, temperature):
        calls.append(model)
        raise RuntimeError("Error code: 429 - {'error': {'message': 'Rate limit'}}")

    async def _fail_fallback(*args, **kwargs):
        calls.append("FALLBACK-CALLED")
        raise AssertionError("fallback must not be attempted on 429")

    monkeypatch.setattr(client, "_chat_non_stream", _fail_primary)
    monkeypatch.setattr(client, "_retry_with_fallback", _fail_fallback)

    with pytest.raises(RuntimeError, match="429"):
        await client.chat(messages=[{"role": "user", "content": "hi"}])

    assert calls == ["primary"]  # no fallback burn
    assert mod.llm_cooldown_remaining() > 0

    # Next call fails fast without touching the provider.
    with pytest.raises(RuntimeError, match="cooling down"):
        await client.chat(messages=[{"role": "user", "content": "hi"}])
    assert calls == ["primary"]

    mod._shared_cooldown_until = 0.0  # reset global for other tests


async def test_transient_503_retries_then_succeeds(monkeypatch):
    import texponse.integrations.openrouter as mod

    mod._shared_cooldown_until = 0.0
    client = OpenRouterClient(
        api_key="test",
        base_url="https://gemini.example/v1",
        default_model="primary",
        fallback_model="fallback",
        min_interval=0.0,
        transient_attempts=2,
    )
    # Real backoff (1s + 2s) is fine here: patching asyncio.sleep globally
    # deadlocks pytest-asyncio's own internals.

    attempts: list[int] = []

    async def _flaky(messages, model, max_tokens, temperature):
        attempts.append(model)
        if len(attempts) < 3:
            raise RuntimeError("Error code: 503 - high demand")
        return {"content": "ok", "model": model}

    monkeypatch.setattr(client, "_chat_non_stream", _flaky)

    result = await client.chat(messages=[{"role": "user", "content": "hi"}])
    assert result["content"] == "ok"
    assert len(attempts) == 3  # 503 retried twice, no fallback burn
    assert mod.llm_cooldown_remaining() == 0  # 503 must NOT park the lane


async def test_per_model_quota_falls_back_on_429(monkeypatch):
    """Gemini meters each model separately: a drained default must still
    fall back, because the fallback has its own 20/day bucket."""
    import texponse.integrations.openrouter as mod

    mod._shared_cooldown_until = 0.0
    mod._model_cooldowns.clear()

    client = OpenRouterClient(
        api_key="test",
        base_url="https://gemini.example/v1",
        default_model="gemini-default",
        fallback_model="gemini-fallback",
        min_interval=0.0,
        per_model_quota=True,
    )

    tried: list[str] = []

    async def _only_default_drained(messages, model, max_tokens, temperature):
        tried.append(model)
        if model == "gemini-default":
            raise RuntimeError("Error code: 429 - RESOURCE_EXHAUSTED quota")
        return {"content": "served by fallback", "model": model}

    monkeypatch.setattr(client, "_chat_non_stream", _only_default_drained)

    result = await client.chat(messages=[{"role": "user", "content": "hi"}])
    assert result["content"] == "served by fallback"
    assert result["fallback_used"] is True
    assert tried == ["gemini-default", "gemini-fallback"]

    # Only the drained model is parked; the fallback model stays usable.
    assert mod.llm_cooldown_remaining("gemini-default") > 0
    assert mod.llm_cooldown_remaining("gemini-fallback") == 0
    assert mod.llm_cooldown_remaining() == 0  # global lane not blocked

    mod._model_cooldowns.clear()


async def test_per_model_quota_parked_model_fails_fast(monkeypatch):
    """Once a model is parked, calling it again must not burn a request
    on THAT model (its sibling bucket may still be tried)."""
    import texponse.integrations.openrouter as mod

    mod._shared_cooldown_until = 0.0
    mod._model_cooldowns.clear()

    client = OpenRouterClient(
        api_key="test",
        base_url="https://gemini.example/v1",
        default_model="gemini-default",
        fallback_model="gemini-fallback",
        min_interval=0.0,
        per_model_quota=True,
    )
    calls: list[str] = []

    async def _record(messages, model, max_tokens, temperature):
        calls.append(model)
        raise RuntimeError("Error code: 429 - RESOURCE_EXHAUSTED quota")

    monkeypatch.setattr(client, "_chat_non_stream", _record)

    # First call: default is tried, then its distinct fallback bucket.
    with pytest.raises(RuntimeError, match="429"):
        await client.chat(messages=[{"role": "user", "content": "hi"}], model="gemini-default")
    assert calls == ["gemini-default", "gemini-fallback"]

    # Both are parked now, so a repeat call must hit no provider at all.
    calls.clear()
    with pytest.raises(RuntimeError, match="cooling down"):
        await client.chat(messages=[{"role": "user", "content": "hi"}], model="gemini-default")
    assert calls == []

    mod._model_cooldowns.clear()


async def test_non_429_still_uses_fallback(monkeypatch):
    import texponse.integrations.openrouter as mod

    mod._shared_cooldown_until = 0.0
    client = OpenRouterClient(
        api_key="test",
        base_url="https://openrouter.ai/api/v1",
        default_model="primary",
        fallback_model="fallback",
        min_interval=0.0,
    )

    tried: list[str] = []

    async def _primary_404(messages, model, max_tokens, temperature):
        tried.append(model)
        if model == "primary":
            raise RuntimeError("Error code: 404 - no endpoints")
        return {"content": "ok", "model": model}

    monkeypatch.setattr(client, "_chat_non_stream", _primary_404)

    result = await client.chat(messages=[{"role": "user", "content": "hi"}])
    assert result["fallback_used"] is True
    assert tried == ["primary", "fallback"]
    assert mod.llm_cooldown_remaining() == 0


async def test_shared_pool_does_not_rotate_after_429(monkeypatch):
    """One shared bucket (openrouter router / proxy strategy): a 429 means
    the pool is dry, so no further provider calls are made."""
    import texponse.integrations.openrouter as mod

    mod._shared_cooldown_until = 0.0
    mod._model_cooldowns.clear()

    client = OpenRouterClient(
        api_key="test",
        base_url="https://openrouter.ai/api/v1",
        default_model="primary",
        fallback_model="fallback",
        min_interval=0.0,
        per_model_quota=False,
    )
    calls: list[str] = []

    async def _quota(messages, model, max_tokens, temperature):
        calls.append(model)
        raise RuntimeError("Error code: 429 - free-models-per-day")

    monkeypatch.setattr(client, "_chat_non_stream", _quota)

    with pytest.raises(RuntimeError, match="429"):
        await client.chat(messages=[{"role": "user", "content": "hi"}])
    assert calls == ["primary"]  # fallback NOT burned
    assert mod.llm_cooldown_remaining() > 0

    mod._shared_cooldown_until = 0.0
    mod._model_cooldowns.clear()
