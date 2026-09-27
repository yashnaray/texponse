from __future__ import annotations

from typing import Any

import httpx


class JobberAPIError(Exception):
    def __init__(self, message: str, response: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.response: dict[str, Any] = response or {}


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
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "X-Jobber-GraphQL-Version": self._graphql_version,
        }
        resp = await self._client.post(
            "",
            json={"query": query, "variables": variables or {}},
            headers=headers,
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()

        if "errors" in data:
            error_msg = data["errors"][0].get("message", "Unknown error")
            raise JobberAPIError(str(error_msg), data)

        return data

    async def find_client_by_phone(
        self, phone_number: str, access_token: str
    ) -> dict[str, Any] | None:
        query = """
        query ListClients($first: Int) {
          clients(first: $first) {
            nodes {
              id
              firstName
              lastName
              name
              companyName
              phones { number primary }
              email
              balance
              isLead
            }
            totalCount
          }
        }
        """
        result = await self.graphql(query, {"first": 50}, access_token)
        data = result.get("data", {})
        nodes: list[dict[str, Any]] = (
            data.get("clients", {}).get("nodes", []) or [] if isinstance(data, dict) else []
        )

        def _digits(s: str) -> str:
            return "".join(c for c in s if c.isdigit())

        want = _digits(phone_number)
        # Try exact (last-10-digit) match first, then fall back to first client.
        for c in nodes:
            phones = c.get("phones", []) or []
            for p in phones:
                num = str(p.get("number", ""))
                if num and _digits(num)[-10:] == want[-10:] and len(want) >= 10:
                    return c
        return None

    async def list_all_clients(self, access_token: str, limit: int = 200) -> list[dict[str, Any]]:
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
                nodes { id title jobStatus total }
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
            batch = min(50, limit - len(out))
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
        return out

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
