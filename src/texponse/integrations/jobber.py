from __future__ import annotations

import asyncio as _pacing_asyncio
import time as _pacing_time
from typing import Any

import httpx


class JobberAPIError(Exception):
    def __init__(self, message: str, response: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.response: dict[str, Any] = response or {}


# ── Client-side Jobber pacing ──────────────────────────────────────────
# Jobber throttles aggressively (~tens of req/min). Serialize GraphQL calls
# per-process with a minimum gap so concurrent worker threads + the hourly
# sync don't burst past the limit. Also caches phone lookups (5 min TTL)
# since the same caller often texts several times in a row.
_JOBBER_LOCK = _pacing_asyncio.Lock()
_JOBBER_LAST_CALL: float = 0.0
_JOBBER_MIN_INTERVAL = 2.0  # seconds between GraphQL calls per process
_PHONE_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}
_PHONE_CACHE_TTL = 300.0


class JobberClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        graphql_version: str,
        base_url: str = "https://api.getjobber.com/api/graphql",
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._graphql_version = graphql_version
        self._base_url = base_url
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(30.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def exchange_code(self, code: str) -> dict[str, Any]:
        token_url = "https://api.getjobber.com/api/oauth/token"
        resp = await self._client.post(
            token_url,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": self._redirect_uri,
                "code": code,
                "grant_type": "authorization_code",
            },
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        token_url = "https://api.getjobber.com/api/oauth/token"
        resp = await self._client.post(
            token_url,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        access_token: str = "",
        max_retries: int = 5,
    ) -> dict[str, Any]:
        import asyncio
        import logging
        import random

        logger = logging.getLogger(__name__)
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "X-Jobber-GraphQL-Version": self._graphql_version,
        }
        global _JOBBER_LAST_CALL
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                async with _JOBBER_LOCK:
                    gap = _pacing_time.monotonic() - _JOBBER_LAST_CALL
                    if gap < _JOBBER_MIN_INTERVAL:
                        await _pacing_asyncio.sleep(_JOBBER_MIN_INTERVAL - gap)
                    resp = await self._client.post(
                        "",
                        json={"query": query, "variables": variables or {}},
                        headers=headers,
                    )
                    _JOBBER_LAST_CALL = _pacing_time.monotonic()
                if resp.status_code == 429:
                    raise JobberAPIError("Throttled", {"status": 429})
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
            except (httpx.HTTPStatusError, httpx.TransportError, JobberAPIError) as exc:
                last_exc = exc
                msg = str(exc)
                throttled = "429" in msg or "Throttled" in msg or "throttl" in msg.lower()
                if not throttled or attempt >= max_retries:
                    raise
                delay = min(120.0, (5.0 * 2.0**attempt)) + random.uniform(0, 2)
                logger.warning(
                    "Jobber throttled (attempt %d/%d), retrying in %.1fs",
                    attempt + 1,
                    max_retries,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            if "errors" in data:
                error_msg = data["errors"][0].get("message", "Unknown error")
                if "throttl" in str(error_msg).lower() and attempt < max_retries:
                    delay = min(120.0, (5.0 * 2.0**attempt)) + random.uniform(0, 2)
                    logger.warning(
                        "Jobber GraphQL throttled (attempt %d/%d), retrying in %.1fs",
                        attempt + 1,
                        max_retries,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise JobberAPIError(str(error_msg), data)

            return data
        raise JobberAPIError(f"Jobber request failed after retries: {last_exc}")

    async def find_client_by_phone(
        self, phone_number: str, access_token: str, max_retries: int = 1
    ) -> dict[str, Any] | None:
        import time as _t

        # In-memory TTL cache: repeat texts from the same caller skip Jobber.
        _digits_key = "".join(c for c in phone_number if c.isdigit())[-10:]
        cached = _PHONE_CACHE.get((_digits_key, access_token[-8:]))
        if cached and _t.monotonic() - cached[0] < _PHONE_CACHE_TTL:
            return cached[1]
        # Lightweight query: no nested visits, fewer jobs. The old version
        # pulled jobs+visits for 100 clients per SMS — the main throttle cause.
        query = """
        query ListClients($first: Int) {
          clients(first: $first) {
            nodes {
              id
              name
              companyName
              phones { number primary }
              email
              balance
              isLead
              jobs(first: 3) {
                nodes { id title jobStatus total }
              }
            }
            totalCount
          }
        }
        """
        # Enrichment is best-effort: a throttled Jobber must not hold the SMS
        # hostage. 1 retry, then give up and answer from retrieval alone.
        try:
            result = await self.graphql(
                query, {"first": 25}, access_token, max_retries=max_retries
            )
        except Exception:
            import logging

            logging.getLogger(__name__).warning(
                "Jobber client lookup gave up after %d retries; continuing without "
                "business context",
                max_retries,
            )
            return None
        data = result.get("data", {})
        nodes: list[dict[str, Any]] = (
            data.get("clients", {}).get("nodes", []) or [] if isinstance(data, dict) else []
        )

        def _digits(s: str) -> str:
            return "".join(c for c in s if c.isdigit())

        want = _digits(phone_number)
        # Try exact (last-10-digit) match first, then fall back to first client.
        found: dict[str, Any] | None = None
        for c in nodes:
            phones = c.get("phones", []) or []
            for p in phones:
                num = str(p.get("number", ""))
                if num and _digits(num)[-10:] == want[-10:] and len(want) >= 10:
                    found = c
                    break
            if found:
                break
        _PHONE_CACHE[(_digits_key, access_token[-8:])] = (_t.monotonic(), found)
        return found

    async def list_all_clients(self, access_token: str, limit: int = 1_000) -> list[dict[str, Any]]:
        query = """
        query ListClients($first: Int, $after: String) {
          clients(first: $first, after: $after) {
            nodes {
              id
              name
              firstName
              lastName
              companyName
              phones { number primary }
              email
              balance
              isLead
              jobs(first: 5) {
                nodes {
                  id title jobStatus total startAt endAt
                  visits(first: 5) {
                    nodes { id title visitStatus startAt endAt }
                  }
                }
              }
            }
            pageInfo { hasNextPage endCursor }
            totalCount
          }
        }
        """
        out: list[dict[str, Any]] = []
        after: str | None = None
        while len(out) < limit:
            batch = min(25, limit - len(out))
            result = await self.graphql(query, {"first": batch, "after": after}, access_token)
            conn = result.get("data", {}).get("clients", {})
            out.extend(conn.get("nodes", []) or [])
            page = conn.get("pageInfo", {})
            if not page.get("hasNextPage"):
                break
            new_after = page.get("endCursor")
            if not new_after or new_after == after:
                break
            after = new_after
            import asyncio as _asyncio
            import random as _random

            # Stay under Jobber's rate limit: 25/page + ~2s pacing.
            # Smaller pages cost more requests but each is cheaper server-side
            # and far less likely to trip the throttle window.
            await _asyncio.sleep(2.0 + _random.uniform(0, 1))
        return out

    async def _paginate_root(
        self, root: str, query: str, access_token: str, limit: int
    ) -> list[dict[str, Any]]:
        """Generic cursor paginator for jobs/quotes/invoices roots (50/page)."""
        import asyncio as _asyncio
        import random as _random

        out: list[dict[str, Any]] = []
        after: str | None = None
        while len(out) < limit:
            batch = min(50, limit - len(out))
            result = await self.graphql(
                query, {"first": batch, "after": after}, access_token
            )
            conn = result.get("data", {}).get(root, {}) or {}
            out.extend(conn.get("nodes", []) or [])
            page = conn.get("pageInfo", {}) or {}
            if not page.get("hasNextPage"):
                break
            new_after = page.get("endCursor")
            if not new_after or new_after == after:
                break
            after = new_after
            await _asyncio.sleep(2.0 + _random.uniform(0, 1))
        return out

    async def list_all_jobs_flat(
        self, access_token: str, limit: int = 5_000
    ) -> list[dict[str, Any]]:
        query = """
        query KpiJobsFlat($first: Int, $after: String) {
          jobs(first: $first, after: $after) {
            nodes {
              id title jobStatus jobType total invoicedTotal
              completedAt createdAt startAt endAt source
              client { id }
              jobCosting {
                expenseCost labourCost lineItemCost
                totalCost totalRevenue profitAmount profitPercentage
              }
            }
            pageInfo { hasNextPage endCursor }
            totalCount
          }
        }
        """
        return await self._paginate_root("jobs", query, access_token, limit)

    async def list_all_quotes(
        self, access_token: str, limit: int = 2_000
    ) -> list[dict[str, Any]]:
        query = """
        query KpiQuotes($first: Int, $after: String) {
          quotes(first: $first, after: $after) {
            nodes { id title quoteStatus createdAt amounts { total } }
            pageInfo { hasNextPage endCursor }
            totalCount
          }
        }
        """
        return await self._paginate_root("quotes", query, access_token, limit)

    async def list_all_invoices(
        self, access_token: str, limit: int = 3_000
    ) -> list[dict[str, Any]]:
        query = """
        query KpiInvoices($first: Int, $after: String) {
          invoices(first: $first, after: $after) {
            nodes { id invoiceStatus invoiceNet dueDate createdAt }
            pageInfo { hasNextPage endCursor }
            totalCount
          }
        }
        """
        return await self._paginate_root("invoices", query, access_token, limit)

    async def get_client_properties(
        self, client_id: str, access_token: str
    ) -> list[dict[str, Any]]:
        query = """
        query ClientProperties($id: ID!) {
          client(id: $id) {
            clientProperties {
              id
              name
              address {
                street
                city
                province
                postalCode
                country
              }
              jobs {
                id
                title
                jobStatus
                jobType
                total
                startAt
                endAt
              }
            }
          }
        }
        """
        result = await self.graphql(query, {"id": client_id}, access_token)
        data = result.get("data", {})
        client = data.get("client", {})
        return client.get("clientProperties", []) or []

    async def get_job_details(self, job_id: str, access_token: str) -> dict[str, Any] | None:
        query = """
        query JobDetails($id: ID!) {
          job(id: $id) {
            id
            title
            jobStatus
            jobType
            total
            instructions
            startAt
            endAt
            client {
              id
              name
              phone
              email
            }
            property {
              id
              name
              address {
                street
                city
                province
                postalCode
              }
            }
            lineItems(first: 10) {
              nodes {
                name
                description
                quantity
                unitCost
                total
              }
            }
            visits {
              id
              title
              visitStatus
              startAt
              endAt
              instructions
            }
          }
        }
        """
        result = await self.graphql(query, {"id": job_id}, access_token)
        data = result.get("data", {})
        job: dict[str, Any] | None = data.get("job")
        return job
