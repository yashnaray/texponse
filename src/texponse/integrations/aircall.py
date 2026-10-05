from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx

from texponse.config import Settings


class AircallClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.aircall_base_url,
            auth=(settings.aircall_api_id, settings.aircall_api_token),
            timeout=httpx.Timeout(30.0),
            headers={"Accept": "application/json"},
        )
        self._json_headers = {"Content-Type": "application/json"}

    async def close(self) -> None:
        await self._client.aclose()

    async def send_message(
        self,
        line_id: int,
        to_number: str,
        content: str,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Send an SMS via the Public API Messaging endpoint.

        The Aircall number (``line_id``) must first be configured for Public
        API messaging, otherwise Aircall returns 404. See
        ``configure_number_for_messaging``.
        """
        resp = await self._client.post(
            f"/numbers/{line_id}/messages/send",
            json={"to": to_number, "body": content},
            headers=self._json_headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def list_numbers(self, timeout: float = 30.0) -> list[dict[str, Any]]:
        """List Aircall numbers (used to find the ``line_id`` for sending)."""
        resp = await self._client.get("/numbers", timeout=timeout)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        result: list[dict[str, Any]] = data.get("numbers", [])
        return result

    async def configure_number_for_messaging(
        self,
        line_id: int,
        callback_url: str,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Configure a number for Public API messaging.

        Warning: a number configured this way can no longer send SMS from
        the Aircall app.
        """
        resp = await self._client.post(
            f"/numbers/{line_id}/messages/configuration",
            json={"callbackUrl": callback_url},
            headers=self._json_headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def get_number_messaging_config(
        self,
        line_id: int,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        resp = await self._client.get(
            f"/numbers/{line_id}/messages/configuration",
            headers=self._json_headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def delete_number_messaging_config(
        self,
        line_id: int,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        resp = await self._client.delete(
            f"/numbers/{line_id}/messages/configuration",
            headers=self._json_headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        if not resp.content:
            return {}
        try:
            data: dict[str, Any] = resp.json()
        except ValueError:
            return {}
        return data

    async def get_call(self, call_id: str, timeout: float = 30.0) -> dict[str, Any]:
        resp = await self._client.get(f"/calls/{call_id}", timeout=timeout)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def get_contact(self, contact_id: str, timeout: float = 30.0) -> dict[str, Any]:
        resp = await self._client.get(f"/contacts/{contact_id}", timeout=timeout)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def list_webhooks(self) -> list[dict[str, Any]]:
        resp = await self._client.get("/webhooks")
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        result: list[dict[str, Any]] = data.get("webhooks", [])
        return result

    async def create_webhook(self, event: str, url: str) -> dict[str, Any]:
        resp = await self._client.post(
            "/webhooks",
            json={"events": [event], "url": url},
            headers=self._json_headers,
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    @staticmethod
    def verify_webhook_signature(
        payload: bytes,
        signature: str,
        secret: str,
    ) -> bool:
        expected = hmac.new(
            secret.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    @staticmethod
    def normalize_callback_event(raw: dict[str, Any]) -> dict[str, Any]:
        """Normalize a proxy-mode messaging callback payload.

        Proxy callbacks use ``{"event_name": ..., "data": {...}}`` instead of
        the webhook ``{"event": ..., "resource": {...}}`` shape. Field names
        inside ``data`` are handled defensively since Aircall does not
        guarantee every field on every event.
        """
        event_type = str(raw.get("event_name", "") or "")
        data = raw.get("data", {})
        if not isinstance(data, dict):
            data = {}

        def _phone(value: Any) -> str | None:
            if isinstance(value, str):
                cleaned = value.strip().replace(" ", "").replace("-", "")
                if cleaned and all(ch.isdigit() or ch == "+" for ch in cleaned):
                    return cleaned if cleaned.startswith("+") else f"+{cleaned}"
                return value or None
            if isinstance(value, dict):
                for key in ("phone_number", "phone", "number", "digits", "raw_digits"):
                    nested = value.get(key)
                    if isinstance(nested, str):
                        resolved = _phone(nested)
                        if resolved:
                            return resolved
            return None

        from_val = _phone(data.get("from", data.get("from_number", data.get("raw_digits"))))
        to_val = _phone(data.get("to", data.get("to_number", data.get("number"))))
        content = data.get("body", data.get("content", data.get("text")))
        direction = data.get("direction", "inbound")

        return {
            "event_type": event_type,
            "aircall_message_id": data.get("id", data.get("message_id")),
            "from_number": from_val,
            "to_number": to_val,
            "direction": direction,
            "content": content if isinstance(content, str) else None,
            "raw_payload": raw,
        }

    @staticmethod
    def normalize_event(raw: dict[str, Any]) -> dict[str, Any]:
        event_type = raw.get("event", "")
        resource = raw.get("resource", {})

        normalized: dict[str, Any] = {
            "event_type": event_type,
            "raw_payload": raw,
        }

        if event_type.startswith("call."):
            from_val = resource.get("from")
            to_val = resource.get("to")
            normalized.update(
                {
                    "aircall_call_id": resource.get("id"),
                    "from_number": from_val
                    if isinstance(from_val, str)
                    else from_val.get("phone_number")
                    if isinstance(from_val, dict)
                    else None,
                    "to_number": to_val
                    if isinstance(to_val, str)
                    else to_val.get("phone_number")
                    if isinstance(to_val, dict)
                    else None,
                    "direction": "inbound"
                    if resource.get("direction") == "Incoming"
                    else "outbound",
                    "content": None,
                }
            )
        elif event_type.startswith("message."):
            from_val = resource.get("from")
            to_val = resource.get("to")
            normalized.update(
                {
                    "aircall_message_id": resource.get("id"),
                    "from_number": from_val
                    if isinstance(from_val, str)
                    else from_val.get("phone_number")
                    if isinstance(from_val, dict)
                    else None,
                    "to_number": to_val
                    if isinstance(to_val, str)
                    else to_val.get("phone_number")
                    if isinstance(to_val, dict)
                    else None,
                    "direction": resource.get("direction"),
                    "content": resource.get("content"),
                }
            )

        return normalized
