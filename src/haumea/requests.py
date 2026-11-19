from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from urllib.parse import parse_qs

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

_COOKIE_ESCAPE = re.compile(r"\\(?:([0-3][0-7]{2})|(.))")


def _unquote(value: str) -> str:
    """Undo the quoting ``http.cookies`` applies to values: surrounding
    double quotes, backslash escapes and ``\\ooo`` octal escapes."""
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        return value
    return _COOKIE_ESCAPE.sub(
        lambda m: chr(int(m[1], 8)) if m[1] else m[2], value[1:-1]
    )


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
        """The full public path, ``root_path + path``.

        ``scope["path"]`` is relative to ``scope["root_path"]`` (as
        ``vercel-runtime``'s prefix strip writes it), so there is no
        ``startswith`` check. At the prefix root, ``path == "/"``, the full
        path is ``root_path`` alone.
        """
        root_path = self.scope.get("root_path", "").rstrip("/")
        path = self.scope["path"]
        if root_path and path == "/":
            return root_path
        return f"{root_path}{path}"

    @property
    def headers(self) -> dict[str, str]:
        """Request headers, keyed by lower-case name."""
        return {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in self.scope.get("headers", [])
        }

    @property
    def cookies(self) -> dict[str, str]:
        """Cookies from every ``cookie`` header, keyed by name.

        Quoted values are unquoted. When a name repeats, the first value wins:
        browsers send the cookie with the most specific path first.
        """
        cookies: dict[str, str] = {}
        for key, value in self.scope.get("headers", []):
            if key.lower() != b"cookie":
                continue
            for pair in value.decode("latin-1").split(";"):
                name, sep, raw = pair.partition("=")
                name = name.strip()
                if sep and name and name not in cookies:
                    cookies[name] = _unquote(raw.strip())
        return cookies

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
