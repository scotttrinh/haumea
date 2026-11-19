from __future__ import annotations

import mimetypes
import re
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from haumea.config import normalize_base_path
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
    ``base_path`` prefixes the docs and static URLs; route paths passed to
    ``add_route`` already include it.
    """

    def __init__(
        self,
        *,
        title: str = "Haumea",
        version: str = "0.1.0",
        docs: bool = True,
        public_dir: str | Path | None = None,
        base_path: str = "",
    ) -> None:
        self.title = title
        self.base_path = normalize_base_path(base_path)
        self.version = version
        self.docs = docs
        self.public_dir = Path(public_dir).resolve() if public_dir else None
        self.routes: list[tuple[str, re.Pattern[str], dict[str, Endpoint]]] = []
        self.hidden_operations: set[tuple[str, str]] = set()
        """``(path, method)`` pairs served but left out of the OpenAPI schema."""

    def add_route(
        self,
        path: str,
        handler: Callable[..., Any],
        methods: list[str],
        *,
        include_in_schema: bool = True,
    ) -> None:
        """Register ``handler`` for ``path``; ``include_in_schema=False``
        serves it without documenting it in OpenAPI."""
        endpoint = Endpoint(handler, {m[1] for m in _PATH_PARAM.finditer(path)})
        endpoints = next((e for p, _, e in self.routes if p == path), None)
        if endpoints is None:
            endpoints = {}
            self.routes.append((path, _compile_path(path), endpoints))
        for method in map(str.upper, methods):
            endpoints[method] = endpoint
            if include_in_schema:
                self.hidden_operations.discard((path, method))
            else:
                self.hidden_operations.add((path, method))

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
        await response(receive, send, include_body=request.method != "HEAD")

    async def _handle(self, request: Request) -> Response:
        """Match in Build Output order: literal routes, then docs and
        ``public_dir``, then dynamic routes."""
        literal = [route for route in self.routes if not route[1].groups]
        if response := await self._dispatch(request, literal):
            return response
        if response := self._dev_file(request):
            return response
        dynamic = [route for route in self.routes if route[1].groups]
        if response := await self._dispatch(request, dynamic):
            return response
        raise HTTPException(404, "Not Found")

    async def _dispatch(
        self,
        request: Request,
        routes: list[tuple[str, re.Pattern[str], dict[str, Endpoint]]],
    ) -> Response | None:
        """Call the first of ``routes`` matching the path and method; raise
        ``405`` if one or more matched the path regex but none supported the
        request method."""
        method = "GET" if request.method == "HEAD" else request.method
        allowed: set[str] = set()
        for _, pattern, endpoints in routes:
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
        return None

    def _dev_file(self, request: Request) -> Response | None:
        """The docs, ``openapi.json`` or a ``public_dir`` file at the path,
        with Build Output precedence; ``405`` for methods other than
        ``GET``/``HEAD``.

        The build writes ``openapi.json`` and ``docs.html`` over ``public/``,
        but ``public/docs`` wins over the ``docs`` -> ``docs.html`` rewrite.
        """
        path, openapi_url = request.path, f"{self.base_path}/openapi.json"
        docs_html = f"{self.base_path}/docs.html"
        if self.docs and path == openapi_url:
            self._require_get(request)
            return JSONResponse(self.openapi())
        if self.docs and path == docs_html:
            self._require_get(request)
            return HTMLResponse(swagger_html(self.title, openapi_url))
        if response := self._static_file(path, request.method):
            return response
        if self.docs and path == f"{self.base_path}/docs":
            self._require_get(request)
            return HTMLResponse(swagger_html(self.title, openapi_url))
        return None

    @staticmethod
    def _require_get(request: Request) -> None:
        if request.method not in ("GET", "HEAD"):
            raise HTTPException(405, "Method Not Allowed", {"allow": "GET, HEAD"})

    def _static_file(self, path: str, method: str = "GET") -> Response | None:
        if self.public_dir is None or path.endswith("/"):
            return None
        if self.base_path:
            # Only paths strictly under the prefix: not /apiary or /api itself.
            if not path.startswith(f"{self.base_path}/"):
                return None
            path = path[len(self.base_path) :]
        try:
            file = (self.public_dir / path.lstrip("/")).resolve()
            if not file.is_relative_to(self.public_dir) or not file.is_file():
                return None
        except (OSError, RuntimeError, ValueError):  # symlink loops, NUL bytes
            return None
        if method not in ("GET", "HEAD"):
            raise HTTPException(405, "Method Not Allowed", {"allow": "GET, HEAD"})
        media_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        return Response(file.read_bytes(), media_type=media_type)
