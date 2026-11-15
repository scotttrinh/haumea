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
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
ROUTE_FILE = "route.py"


def _segments(app_dir: Path, route_file: Path) -> list[tuple[int, str]]:
    """Turn a route file's directory into ``(specificity, segment)`` pairs."""
    segments: list[tuple[int, str]] = []
    for part in route_file.parent.relative_to(app_dir).parts:
        if part.startswith("(") and part.endswith(")"):
            continue  # route group: organizational only
        if part.startswith("[...") and part.endswith("]"):
            segments.append((2, f"{{{part[4:-1]}:path}}"))
        elif part.startswith("[") and part.endswith("]"):
            segments.append((1, f"{{{part[1:-1]}}}"))
        else:
            segments.append((0, part))
    return segments


def _load_module(route_file: Path, url_path: str):
    name = "haumea_routes." + (re.sub(r"\W+", "_", url_path).strip("_") or "index")
    spec = importlib.util.spec_from_file_location(name, route_file)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def discover_routes(app_dir: Path) -> list[tuple[str, Path]]:
    """Return ``(url_path, route_file)`` pairs, most specific first.

    Static segments sort ahead of dynamic ones, and catch-alls go last, so
    ``/items/new`` wins over ``/items/{item_id}``.
    """
    found = []
    for route_file in app_dir.rglob(ROUTE_FILE):
        segments = _segments(app_dir, route_file)
        url_path = "/" + "/".join(segment for _, segment in segments)
        found.append(([rank for rank, _ in segments], url_path, route_file))
    found.sort(key=lambda item: (item[0], item[1]))
    return [(url_path, route_file) for _, url_path, route_file in found]


def create_app(
    app_dir: str | Path = "app",
    *,
    public_dir: str | Path | None = None,
    **fastapi_kwargs: Any,
) -> FastAPI:
    """Build a FastAPI app from a directory of ``route.py`` files.

    ``public_dir`` is only for local development: in a deployment, static
    files are served by the platform before the function is ever invoked.
    """
    app_dir = Path(app_dir).resolve()
    if not app_dir.is_dir():
        raise FileNotFoundError(f"app directory not found: {app_dir}")

    # Let route modules import sibling packages from the project root.
    project_root = str(app_dir.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    app = FastAPI(**fastapi_kwargs)
    for url_path, route_file in discover_routes(app_dir):
        module = _load_module(route_file, url_path)
        for method in HTTP_METHODS:
            handler = getattr(module, method, None)
            if callable(handler):
                app.add_api_route(url_path, handler, methods=[method])

    if public_dir is not None and Path(public_dir).is_dir():
        app.mount("/", StaticFiles(directory=public_dir), name="public")
    return app
