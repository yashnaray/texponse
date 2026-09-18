from __future__ import annotations

import hashlib
import hmac

import pytest
from fastapi import HTTPException

from texponse.config import Settings
from texponse.security.webhook_validator import verify_aircall_webhook


async def test_verify_aircall_webhook_valid_signature():
    settings = Settings(
        aircall_api_token="secret-token-123",
        aircall_webhook_secret="secret-token-123",
    )
    payload = b'{"event": "message.received", "resource": {"id": "42"}}'
    signature = hmac.new(
        settings.aircall_api_token.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()

    request = _build_request(payload, signature)
    result = await verify_aircall_webhook(request, settings)
    assert result["event"] == "message.received"
    assert result["resource"]["id"] == "42"


async def test_verify_aircall_webhook_missing_signature():
    settings = Settings(aircall_api_token="secret")
    request = _build_request(b'{"event": "test"}', "")

    with pytest.raises(HTTPException):
        await verify_aircall_webhook(request, settings)


async def test_verify_aircall_webhook_invalid_signature():
    settings = Settings(
        aircall_api_token="secret-token-123",
        aircall_webhook_secret="secret-token-123",
    )
    payload = b'{"event": "message.received"}'
    bad_sig = "a" * 64

    request = _build_request(payload, bad_sig)
    with pytest.raises(HTTPException):
        await verify_aircall_webhook(request, settings)


def _build_request(body: bytes, signature: str):
    class MockRequest:
        def __init__(self, body, signature):
            self._body = body
            self.headers = {"X-Aircall-Signature": signature}

        async def body(self):
            return self._body

    return MockRequest(body, signature)
