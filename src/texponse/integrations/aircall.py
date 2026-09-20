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
            headers={"Content-Type": "application/json"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def send_message(
        self,
        from_number: str,
        to_number: str,
        content: str,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        resp = await self._client.post(
            "/messages",
            json={"from": from_number, "to": to_number, "content": content},
            timeout=timeout,
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
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
