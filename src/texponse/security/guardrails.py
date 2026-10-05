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
_JS_PROTOCOL_RE = re.compile(r"javascript\s*:[^\s]*", re.IGNORECASE)


def sanitize_input(text: str | None, max_length: int = 100_000) -> str:
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


def phone_hash(phone: str | None, pepper: str = "") -> str:
    """Stable SHA-256 over normalized digits (last 10) for caller-scoping.

    Returns "" for missing/short numbers so those points never match a caller.
    When `pepper` is set, uses HMAC-SHA256 so hashes are not brute-forceable
    from the 10-digit space (~10B candidates for unsalted SHA-256).
    """
    import hashlib
    import hmac

    if not phone:
        return ""
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) < 10:
        return ""
    normalized = digits[-10:].encode()
    if pepper:
        return hmac.new(pepper.encode(), normalized, hashlib.sha256).hexdigest()
    return hashlib.sha256(normalized).hexdigest()


def phone_hash_match(phone: str | None, candidate: str, peppers: list[str]) -> bool:
    """True if `candidate` equals the hash under any of `peppers`.

    Used during pepper rotation: old points still match via the previous
    pepper until a re-sync rewrites them with the primary pepper.
    """
    import hmac as _hmac

    return any(
        _hmac.compare_digest(phone_hash(phone, pepper=p), candidate) for p in peppers if candidate
    )


def active_peppers(secret_key: str, primary: str = "", previous: str = "") -> list[str]:
    """Ordered [primary, previous] peppers, falling back to secret_key."""
    pep = primary or secret_key
    prev = previous or ""
    return [pep] + ([prev] if prev and prev != pep else [])


def strip_reasoning(text: str) -> str:
    """Remove chain-of-thought / reasoning blocks leaked by reasoning models.

    Handles <think>...</think> (DeepSeek R1, QwQ, Nemotron), <reasoning> tags,
    and markdown-style reasoning fences. Unclosed tags are treated as running
    to end-of-text. Never returns empty: falls back to a safe refusal line.
    """
    if not text:
        return text
    for pattern in (
        re.compile(r"<think>.*?(</think>|$)", re.IGNORECASE | re.DOTALL),
        re.compile(r"<reasoning>.*?(</reasoning>|$)", re.IGNORECASE | re.DOTALL),
        re.compile(r"```(?:think|reasoning)\b.*?```", re.IGNORECASE | re.DOTALL),
    ):
        text = pattern.sub("", text)
    # Conversational thinking-out-loud has no tags ("Here's my thinking
    # process: ..."). Strip it with plain string matching (no regex).
    text = text.strip()
    _openers = (
        "here's a thinking process",
        "here is a thinking process",
        "here's my thinking",
        "here is my thinking",
        "here's the thinking",
        "let me think",
        "my reasoning is as follows",
        "the reasoning is as follows",
        "thinking out loud",
        "i'll think",
        "i'll reason",
    )
    lowered = text.lower()
    for opener in _openers:
        if lowered.startswith(opener):
            # Drop the opener line itself.
            newline = text.find("\n")
            text = "" if newline == -1 else text[newline + 1 :].strip()
            break
    # Structured chain-of-thought sections ("Analyze User Input: ...", "Check
    # Context:", ...). A header is a label line ending in a colon whose first
    # word is reasoning vocabulary, or a known two-word reasoning label
    # ("analyze user", "check context") followed by a colon — Nemotron puts
    # the section body on the same line. Single generic verbs ("Check your
    # inbox: ...") are NOT headers. Drop leading header blocks (header +
    # continuation lines) until the first real paragraph.
    _header_first_words = {
        "analyze",
        "analysis",
        "check",
        "checking",
        "consider",
        "consideration",
        "constraint",
        "constraints",
        "context",
        "plan",
        "planning",
        "reasoning",
        "think",
        "thinking",
        "deliberation",
        "evaluation",
        "assessment",
        "review",
        "verdict",
    }
    _inline_labels = {
        ("analyze", "user"),
        ("check", "context"),
        ("user", "intent"),
        ("thought", "process"),
    }
    _single_nouns = {
        "reasoning",
        "analysis",
        "deliberation",
        "evaluation",
        "assessment",
        "verdict",
        "constraints",
        "plan",
        "planning",
    }

    def _is_header(line: str) -> bool:
        stripped = line.strip().lstrip("#*>-•").strip()
        i = 0
        while i < len(stripped) and stripped[i] in "0123456789.)":
            i += 1
        stripped = stripped[i:].strip().lstrip("*").strip()
        if ":" not in stripped:
            return False
        head, _, rest = stripped.partition(":")
        words = head.split()
        if not words:
            return False
        first = words[0].lower()
        if not rest.strip():
            # Pure label line ("Check Context:").
            return first in _header_first_words
        if len(words) >= 2:
            two = (first, words[1].lower())
            if two in _inline_labels:
                return True
            if first == "step":
                return words[1].rstrip(".,").isdigit()
            return False
        return first in _single_nouns

    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        if not lines[i].strip():
            i += 1
            continue
        if _is_header(lines[i]):
            i += 1
            while i < n and lines[i].strip() and not _is_header(lines[i]):
                i += 1
            continue
        break
    text = "\n".join(lines[i:]).strip()
    return text or "Thanks for your message — our team will follow up shortly."


def to_plaintext(text: str) -> str:
    """Strip markdown/HTML to SMS-safe plain text."""
    text = _SCRIPT_TAG_RE.sub("", text)
    text = _HTML_TAG_RE.sub("", text)
    # markdown links/images -> label text
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    # bold/italic/strikethrough/inline code
    text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text)
    text = re.sub(r"(\*|_|~~|`)(.*?)\1", r"\2", text)
    # headings, quotes, bullets, numbered lists, hr
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s{0,3}>\s?", "", text)
    text = re.sub(r"(?m)^\s*([-*+]|\d+[.)])\s+", "", text)
    text = re.sub(r"(?m)^\s*(-{3,}|_{3,}|\*{3,})\s*$", "", text)
    return re.sub(r"[ \t]+\n", "\n", text).strip()
