from __future__ import annotations

from typing import Any


class HTTPException(Exception):
    """Raise from a handler or dependency to return an error response."""

    def __init__(
        self,
        status_code: int,
        detail: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.detail = detail
        self.headers = headers
        super().__init__(f"{status_code}: {detail}")


class RequestValidationError(Exception):
    """The request's parameters or body did not match the handler signature."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        super().__init__(errors)
