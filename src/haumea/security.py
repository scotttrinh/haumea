"""Credential schemes: dependencies that read a credential and document it."""

from __future__ import annotations

import re
from typing import Any

from haumea.exceptions import HTTPException
from haumea.requests import Request

# OpenAPI 3.1 requires component names to match this.
_COMPONENT_NAME = re.compile(r"[a-zA-Z0-9._-]+")


class HTTPBearer:
    """A dependency returning the token from ``Authorization: Bearer <token>``.

    Use it as ``token: str = Depends(bearer)``. A missing or non-bearer
    credential gets a ``401`` with ``WWW-Authenticate: Bearer``. OpenAPI
    documents it once under ``components.securitySchemes[name]`` and lists it
    in the ``security`` of every operation that depends on it.
    """

    def __init__(self, name: str = "HTTPBearer") -> None:
        if not isinstance(name, str) or not _COMPONENT_NAME.fullmatch(name):
            raise ValueError(
                "HTTPBearer name must be an OpenAPI component name "
                f"(letters, digits, '.', '_' or '-'), got {name!r}"
            )
        self.name = name

    def openapi(self) -> dict[str, Any]:
        return {"type": "http", "scheme": "bearer"}

    def __call__(self, request: Request) -> str:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(
                401, "Not authenticated", {"www-authenticate": "Bearer"}
            )
        return token
