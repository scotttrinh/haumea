"""File-system routing: ``app/**/route.py`` files become FastAPI routes.

Conventions borrowed from the Next.js App Router:

- ``app/route.py``                      -> ``/``
- ``app/api/hello/route.py``            -> ``/api/hello``
- ``app/api/items/[item_id]/route.py``  -> ``/api/items/{item_id}``
- ``app/files/[...path]/route.py``      -> ``/files/{path:path}``
- ``app/(group)/about/route.py``        -> ``/about``

A route module exports functions named after HTTP methods (``GET``, ``POST``,
...). Each one is an ordinary FastAPI path operation function, so parameters,
Pydantic models and ``Depends`` all work as usual.

Locally, every route is served by one FastAPI app (``create_app``). When
deployed, each route is its own function running a single-route app
(``create_route_app``).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
ROUTE_FILE = "route.py"


@dataclass(frozen=True)
class Route:
    url_path: str
    """FastAPI path, e.g. ``/api/items/{item_id}``."""
    file: Path
    function: str
    """Function path in the build output, e.g. ``api/items/[item_id]``."""
    pattern: str | None
    """Regex matching this route's URLs, or None if the path is static."""
    rank: tuple[int, ...]

    def load(self) -> ModuleType:
        name = "haumea_routes." + re.sub(r"\W+", "_", self.function)
        if name in sys.modules:
            return sys.modules[name]
        spec = importlib.util.spec_from_file_location(name, self.file)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module


def _route(app_dir: Path, route_file: Path) -> Route:
    url, function, regex, rank = [], [], [], []
    for part in route_file.parent.relative_to(app_dir).parts:
        if part.startswith("(") and part.endswith(")"):
            continue  # route group: organizational only
        function.append(part)
        if part.startswith("[...") and part.endswith("]"):
            url.append(f"{{{part[4:-1]}:path}}")
            regex.append("(.+)")
            rank.append(2)
        elif part.startswith("[") and part.endswith("]"):
            url.append(f"{{{part[1:-1]}}}")
            regex.append("([^/]+)")
            rank.append(1)
        else:
            url.append(part)
            regex.append(re.escape(part))
            rank.append(0)
    return Route(
        url_path="/" + "/".join(url),
        file=route_file,
        function="/".join(function) or "index",
        pattern=f"^/{'/'.join(regex)}$" if any(rank) else None,
        rank=tuple(rank),
    )


def discover_routes(app_dir: str | Path) -> list[Route]:
    """Find all routes, most specific first.

    Static segments sort ahead of dynamic ones, and catch-alls go last, so
    ``/items/new`` wins over ``/items/{item_id}``.
    """
    app_dir = Path(app_dir).resolve()
    if not app_dir.is_dir():
        raise FileNotFoundError(f"app directory not found: {app_dir}")

    # Let route modules import sibling packages from the project root.
    project_root = str(app_dir.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    routes = [_route(app_dir, file) for file in app_dir.rglob(ROUTE_FILE)]
    return sorted(routes, key=lambda route: (route.rank, route.url_path))


def _add_route(app: FastAPI, route: Route) -> None:
    module = route.load()
    for method in HTTP_METHODS:
        handler = getattr(module, method, None)
        if callable(handler):
            app.add_api_route(route.url_path, handler, methods=[method])


def create_app(
    app_dir: str | Path = "app",
    *,
    public_dir: str | Path | None = None,
    **fastapi_kwargs: Any,
) -> FastAPI:
    """Build one FastAPI app serving every route. Used for local development
    and for generating the OpenAPI schema at build time.

    ``public_dir`` is only for local development: in a deployment, static
    files are served by the platform.
    """
    app = FastAPI(**fastapi_kwargs)
    for route in discover_routes(app_dir):
        _add_route(app, route)
    if public_dir is not None and Path(public_dir).is_dir():
        app.mount("/", StaticFiles(directory=public_dir), name="public")
    return app


def create_route_app(app_dir: str | Path, function: str) -> FastAPI:
    """Build the single-route app that runs inside one deployed function.

    The docs endpoints are off: the build emits them as static files instead.
    """
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    for route in discover_routes(app_dir):
        if route.function == function:
            _add_route(app, route)
            return app
    raise LookupError(f"no route for function {function!r} in {app_dir}")
