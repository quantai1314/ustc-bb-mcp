"""Offline HTTP fixtures; no request is sent to a real Blackboard instance."""

from contextlib import asynccontextmanager

import httpx
import pytest

from ustc_bb_mcp.errors import BBError


class FixtureSession:
    identity = "fixture-session-identity"

    def __init__(self, handler):
        self.requests = []

        def record(request):
            self.requests.append(request)
            return handler(request)

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(record))

    async def get(self, url, **kwargs):
        response = await self.client.get(url)
        if response.status_code >= 400:
            raise BBError("http_error", f"HTTP {response.status_code}")
        return response

    @asynccontextmanager
    async def stream(self, url):
        async with self.client.stream("GET", url) as response:
            yield response


@pytest.fixture
async def session_factory():
    sessions = []

    def create(handler):
        session = FixtureSession(handler)
        sessions.append(session)
        return session

    yield create
    for session in sessions:
        await session.client.aclose()
