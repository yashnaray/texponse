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
        query SearchClients($search: String!) {
          searchClients(search: $search) {
            clients {
              id
              firstName
              lastName
              name
              companyName
              phone
              email
              balance
              isLead
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
              }
              jobs {
                id
                title
                jobStatus
                jobType
                total
                startAt
                endAt
                instructions
              }
              quotes {
                id
                title
                quoteStatus
                total
              }
              invoices {
                id
                invoiceNumber
                invoiceStatus
                total
              }
            }
          }
        }
        """
        result = await self.graphql(query, {"search": phone_number}, access_token)
        data = result.get("data", {})
        search = data.get("searchClients", {})
        clients = search.get("clients", []) if isinstance(search, dict) else []
        return clients[0] if clients else None

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
