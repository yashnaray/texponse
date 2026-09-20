from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from fastapi import HTTPException, Request, status

from texponse.config import Settings


async def verify_aircall_webhook(
    request: Request,
    settings: Settings,
) -> dict[str, Any]:
    body = await request.body()
    signature = request.headers.get("X-Aircall-Signature", "")

    if not signature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing webhook signature",
        )

    secret = settings.aircall_webhook_secret or settings.aircall_api_token
    expected = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )

    try:
        payload: dict[str, Any] = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        ) from None

    return payload
