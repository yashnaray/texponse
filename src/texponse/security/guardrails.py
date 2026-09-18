from __future__ import annotations

import functools
import logging
import re
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_TAG_RE = re.compile(r"<script[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_EVENT_HANDLER_RE = re.compile(r"on\w+\s*=\s*[\"']?[^\"'>\s]*[\"']?", re.IGNORECASE)
_JS_PROTOCOL_RE = re.compile(r"javascript\s*:[^\\s]*", re.IGNORECASE)


def sanitize_input(text: str, max_length: int = 100_000) -> str:
    if text is None:
        return ""

    text = _SCRIPT_TAG_RE.sub("", text)
    text = _HTML_TAG_RE.sub("", text)
    text = _EVENT_HANDLER_RE.sub("", text)
    text = _JS_PROTOCOL_RE.sub("", text)

    if len(text) > max_length:
        logger.warning("Input truncated from %d to %d characters", len(text), max_length)
        text = text[:max_length]

    return text.strip()


def check_input_toxicity(text: str) -> bool:
    toxic_patterns = [
        re.compile(r"\b(fuck|shit|damn|bastard|crap)\b", re.IGNORECASE),
        re.compile(r"(hate\s+speech|slur|bigot)", re.IGNORECASE),
        re.compile(r"\b(kill\s+yourself|suicide)\b", re.IGNORECASE),
    ]
    return any(pattern.search(text) for pattern in toxic_patterns)


def require_role(role: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            request: Request | None = kwargs.get("request")
            if request is None:
                for arg in args:
                    if isinstance(arg, Request):
                        request = arg
                        break

            if request is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Request object not found",
                )

            user = getattr(request.state, "user", None)
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication required",
                )

            if user.role.value != role and user.role.value != "admin":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Role '{role}' required",
                )

            return await func(*args, **kwargs)

        return wrapper

    return decorator


def redact_pii(text: str) -> str:
    email_re = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")
    phone_re = re.compile(r"\b(\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
    cc_re = re.compile(r"\b(?:\d[ -]*?){13,16}\b")

    text = email_re.sub("[EMAIL_REDACTED]", text)
    text = phone_re.sub("[PHONE_REDACTED]", text)
    text = cc_re.sub("[CARD_REDACTED]", text)
    return text
