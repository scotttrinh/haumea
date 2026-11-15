from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from urllib.parse import parse_qs

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


class Request:
    """An incoming HTTP request, wrapping the ASGI scope."""

    def __init__(self, scope: Scope, receive: Receive) -> None:
        self.scope = scope
        self._receive = receive
        self._body: bytes | None = None
        self.path_params: dict[str, str] = {}

    @property
    def method(self) -> str:
        return self.scope["method"]

    @property
    def path(self) -> str:
        return self.scope["path"]

    @property
    def headers(self) -> dict[str, str]:
        """Request headers, keyed by lower-case name."""
        return {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in self.scope.get("headers", [])
        }

    @property
    def query_params(self) -> dict[str, list[str]]:
        """Query parameters; each name maps to every value given for it."""
        query = self.scope.get("query_string", b"").decode("latin-1")
        return parse_qs(query, keep_blank_values=True)

    async def body(self) -> bytes:
        if self._body is None:
            chunks = []
            while True:
                message = await self._receive()
                chunks.append(message.get("body", b""))
                if not message.get("more_body"):
                    break
            self._body = b"".join(chunks)
        return self._body

    async def json(self) -> Any:
        return json.loads(await self.body())
