from __future__ import annotations

import asyncio
import inspect
import mimetypes
import re
import sys
import traceback
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from haumea.config import normalize_base_path
from haumea.exceptions import HTTPException, RequestValidationError
from haumea.openapi import openapi_schema, swagger_html
from haumea.params import Endpoint
from haumea.requests import Receive, Request, Scope, Send
from haumea.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

_PATH_PARAM = re.compile(r"\{(\w+)(:path)?\}")

CallNext = Callable[[Request], Awaitable[Response]]
Middleware = Callable[[Request, CallNext], Awaitable[Response]]
ErrorHandler = Callable[[Request, Exception], Response | Awaitable[Response]]


def error_response(exc: Exception) -> Response:
    """The default mapping: ``HTTPException`` -> JSON, ``RequestValidationError``
    -> JSON 422, anything else is logged and becomes a plain-text 500."""
    if isinstance(exc, HTTPException):
        return JSONResponse({"detail": exc.detail}, exc.status_code, exc.headers)
    if isinstance(exc, RequestValidationError):
        return JSONResponse({"detail": exc.errors}, 422)
    traceback.print_exception(exc, file=sys.stderr)
    return PlainTextResponse("Internal Server Error", 500)


def _call_sync_handler(handler: ErrorHandler, request: Request, exc: Exception) -> Any:
    """Run a sync ``handle_error`` with ``exc`` active, so a bare ``raise``
    in a worker thread re-raises it, and its traceback left as it was."""
    tb = exc.__traceback__
    try:
        raise exc
    except Exception:
        exc.__traceback__ = tb
        return handler(request, exc)


def _is_async(handler: ErrorHandler) -> bool:
    """An ``async def`` function or an instance with ``async def __call__``."""
    return inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(
        type(handler).__call__
    )


async def _error_boundary(
    handlers: Sequence[ErrorHandler], request: Request, exc: Exception
) -> Response:
    """Hand ``exc`` to the nearest of ``handlers`` (root to leaf). Whatever
    it raises, including a non-``Response`` result as ``TypeError``, goes
    to the next ancestor; past the root, ``error_response`` maps it.

    Async handlers run on the event loop; sync ones in a worker thread. An
    awaitable result, such as a sync wrapper's coroutine, is awaited before
    the ``Response`` check. Each ancestor runs inside the ``except`` of the
    one below it, so a bare ``raise`` there re-raises the latest exception."""
    if not handlers:
        return error_response(exc)
    *ancestors, handler = handlers
    try:
        if _is_async(handler):
            response = handler(request, exc)
        else:
            response = await asyncio.to_thread(
                _call_sync_handler, handler, request, exc
            )
        if inspect.isawaitable(response):  # still inside the caller's except
            response = await response
        if not isinstance(response, Response):
            name = getattr(handler, "__qualname__", repr(handler))
            raise TypeError(
                f"handle_error {handler.__module__}.{name} "
                f"returned {response!r}, not a Response"
            )
    except Exception as raised:
        return await _error_boundary(ancestors, request, raised)
    return response


def _wrap(middleware: Middleware, call_next: CallNext) -> CallNext:
    async def call(request: Request) -> Response:
        response = await middleware(request, call_next)
        if not isinstance(response, Response):
            raise TypeError(
                f"middleware {middleware.__module__}.{middleware.__qualname__} "
                f"returned {response!r}, not a Response"
            )
        return response

    return call


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
        self.middleware: dict[str, tuple[Middleware, ...]] = {}
        """Each path's middleware chain, outermost first."""
        self.error_handlers: dict[str, tuple[ErrorHandler, ...]] = {}
        """Each path's error handlers, root to leaf; the leaf runs first."""

    def add_route(
        self,
        path: str,
        handler: Callable[..., Any],
        methods: list[str],
        *,
        include_in_schema: bool = True,
        middleware: Sequence[Middleware] = (),
        error_handlers: Sequence[ErrorHandler] = (),
        dependencies: Sequence[Sequence[Callable[..., Any]]] = (),
    ) -> None:
        """Register ``handler`` for ``path``; ``include_in_schema=False``
        serves it without documenting it in OpenAPI.

        ``middleware`` wraps every method of ``path``, outermost first.
        ``error_handlers`` (root to leaf) handle its endpoint's errors,
        nearest first. Each call for one path must pass the same chains.
        ``dependencies`` holds one group of checks per segment, root to leaf;
        they run in that order before ``handler``'s arguments, sharing their
        per-request ``Depends`` cache."""
        path_params = {m[1] for m in _PATH_PARAM.finditer(path)}
        endpoint = Endpoint(handler, path_params, dependencies)
        endpoints = next((e for p, _, e in self.routes if p == path), None)
        chain, handlers = tuple(middleware), tuple(error_handlers)
        if endpoints is None:
            endpoints = {}
            self.routes.append((path, _compile_path(path), endpoints))
            self.middleware[path] = chain
            self.error_handlers[path] = handlers
        elif self.middleware[path] != chain:
            raise ValueError(f"conflicting middleware for route {path!r}")
        elif self.error_handlers[path] != handlers:
            raise ValueError(f"conflicting error handlers for route {path!r}")
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
        except Exception as exc:  # unmatched paths and middleware errors
            response = error_response(exc)
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
        """Serve the request with the first of ``routes`` whose regex matches
        the path, as the platform sends it to that route's function, or
        return None if none matches.

        The route's middleware wraps its endpoint, or the ``405`` when no
        endpoint supports the method. Inside the innermost middleware, the
        route's error boundary has already turned endpoint errors into a
        ``Response``."""
        for path, pattern, endpoints in routes:
            if match := pattern.fullmatch(request.path):
                request.path_params = match.groupdict()
                call = self._endpoint_call(endpoints, self.error_handlers.get(path, ()))
                for middleware in reversed(self.middleware.get(path, ())):
                    call = _wrap(middleware, call)
                return await call(request)
        return None

    @staticmethod
    def _endpoint_call(
        endpoints: dict[str, Endpoint], error_handlers: Sequence[ErrorHandler]
    ) -> CallNext:
        async def call(request: Request) -> Response:
            method = "GET" if request.method == "HEAD" else request.method
            endpoint = endpoints.get(request.method) or endpoints.get(method)
            try:
                if endpoint is None:
                    headers = {"allow": ", ".join(sorted(endpoints))}
                    raise HTTPException(405, "Method Not Allowed", headers)
                result = await endpoint.call(request)
                if isinstance(result, Response):
                    return result
                return JSONResponse(result)
            except Exception as exc:
                return await _error_boundary(error_handlers, request, exc)

        return call

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
