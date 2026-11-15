from __future__ import annotations

import mimetypes
import re
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from haumea.exceptions import HTTPException, RequestValidationError
from haumea.openapi import openapi_schema, swagger_html
from haumea.params import Endpoint
from haumea.requests import Receive, Request, Scope, Send
from haumea.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

_PATH_PARAM = re.compile(r"\{(\w+)(:path)?\}")


def _compile_path(path: str) -> re.Pattern[str]:
    """``/items/{item_id}`` -> regex; ``{name:path}`` also matches slashes."""
    regex, position = "", 0
    for match in _PATH_PARAM.finditer(path):
        regex += re.escape(path[position : match.start()])
        regex += f"(?P<{match[1]}>{'.+' if match[2] else '[^/]+'})"
        position = match.end()
    return re.compile(regex + re.escape(path[position:]))


class Haumea:
    """A small ASGI application: routes, request validation and OpenAPI.

    ``docs`` and ``public_dir`` exist for local development. In a deployment
    the docs are pre-rendered and static files are served by the platform.
    """

    def __init__(
        self,
        *,
        title: str = "Haumea",
        version: str = "0.1.0",
        docs: bool = True,
        public_dir: str | Path | None = None,
    ) -> None:
        self.title = title
        self.version = version
        self.docs = docs
        self.public_dir = Path(public_dir).resolve() if public_dir else None
        self.routes: list[tuple[str, re.Pattern[str], dict[str, Endpoint]]] = []

    def add_route(
        self, path: str, handler: Callable[..., Any], methods: list[str]
    ) -> None:
        """Register ``handler`` for ``path``. Routes match in the order added."""
        endpoint = Endpoint(handler, {m[1] for m in _PATH_PARAM.finditer(path)})
        endpoints = next((e for p, _, e in self.routes if p == path), None)
        if endpoints is None:
            endpoints = {}
            self.routes.append((path, _compile_path(path), endpoints))
        endpoints.update({method.upper(): endpoint for method in methods})

    def openapi(self) -> dict[str, Any]:
        return openapi_schema(self)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                await send({"type": f"{message['type']}.complete"})
                if message["type"] == "lifespan.shutdown":
                    return
        if scope["type"] != "http":
            return

        request = Request(scope, receive)
        try:
            response = await self._handle(request)
        except HTTPException as exc:
            response = JSONResponse(
                {"detail": exc.detail}, exc.status_code, exc.headers
            )
        except RequestValidationError as exc:
            response = JSONResponse({"detail": exc.errors}, 422)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            response = PlainTextResponse("Internal Server Error", 500)
        await response(send, include_body=request.method != "HEAD")

    async def _handle(self, request: Request) -> Response:
        method = "GET" if request.method == "HEAD" else request.method
        allowed: set[str] = set()
        for _, pattern, endpoints in self.routes:
            match = pattern.fullmatch(request.path)
            if not match:
                continue
            endpoint = endpoints.get(request.method) or endpoints.get(method)
            if endpoint is None:
                allowed.update(endpoints)
                continue
            request.path_params = match.groupdict()
            result = await endpoint.call(request)
            return result if isinstance(result, Response) else JSONResponse(result)

        if allowed:
            headers = {"allow": ", ".join(sorted(allowed))}
            raise HTTPException(405, "Method Not Allowed", headers)
        if method == "GET":
            if self.docs and request.path == "/openapi.json":
                return JSONResponse(self.openapi())
            if self.docs and request.path == "/docs":
                return HTMLResponse(swagger_html(self.title))
            if response := self._static_file(request.path):
                return response
        raise HTTPException(404, "Not Found")

    def _static_file(self, path: str) -> Response | None:
        if self.public_dir is None:
            return None
        file = (self.public_dir / path.lstrip("/")).resolve()
        if not file.is_relative_to(self.public_dir) or not file.is_file():
            return None
        media_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        return Response(file.read_bytes(), media_type=media_type)
