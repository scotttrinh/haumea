from __future__ import annotations

from typing import Any

from pydantic_core import to_json

from haumea.requests import Send


class Response:
    media_type: str | None = None

    def __init__(
        self,
        content: Any = b"",
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        media_type: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = self.render(content)
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}
        media_type = media_type or self.media_type
        if media_type and "content-type" not in self.headers:
            if media_type.startswith("text/"):
                media_type += "; charset=utf-8"
            self.headers["content-type"] = media_type
        self.headers["content-length"] = str(len(self.body))

    def render(self, content: Any) -> bytes:
        if content is None:
            return b""
        return content if isinstance(content, bytes) else str(content).encode()

    async def __call__(self, send: Send, *, include_body: bool = True) -> None:
        headers = [
            (key.encode("latin-1"), value.encode("latin-1"))
            for key, value in self.headers.items()
        ]
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": headers,
            }
        )
        body = self.body if include_body else b""
        await send({"type": "http.response.body", "body": body})


class JSONResponse(Response):
    """Serializes Pydantic models, dataclasses, datetimes, etc. as JSON."""

    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return to_json(content)


class HTMLResponse(Response):
    media_type = "text/html"


class PlainTextResponse(Response):
    media_type = "text/plain"


class RedirectResponse(Response):
    def __init__(self, url: str, status_code: int = 307) -> None:
        super().__init__(status_code=status_code, headers={"location": url})
