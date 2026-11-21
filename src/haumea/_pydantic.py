"""Lazy Pydantic detection and validation without importing ``pydantic``.

Checks ``sys.modules.get("pydantic")`` so ``pydantic`` is never imported unless
user code has already loaded it.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Annotated, Any, TypeAliasType, get_args, get_origin


def pydantic_module() -> ModuleType | None:
    """Return the loaded ``pydantic`` module, or ``None`` if not imported."""
    module = sys.modules.get("pydantic")
    return module if isinstance(module, ModuleType) else None


def _base_model_type(pydantic: ModuleType | None = None) -> type | None:
    module = pydantic_module() if pydantic is None else pydantic
    if module is None:
        return None
    base_model = getattr(module, "BaseModel", None)
    return base_model if isinstance(base_model, type) else None


def is_pydantic_model(annotation: Any) -> bool:
    """Return whether ``annotation`` is a ``pydantic.BaseModel`` subclass."""
    if not isinstance(annotation, type):
        return False
    base_model = _base_model_type()
    return base_model is not None and issubclass(annotation, base_model)


def contains_pydantic_model(annotation: Any) -> bool:
    """Return whether ``annotation`` is or wraps a ``pydantic.BaseModel`` subclass."""
    base_model = _base_model_type()
    if base_model is None:
        return False

    seen: set[int] = set()

    def walk(ann: Any) -> bool:
        key = id(ann)
        if key in seen:
            return False
        seen.add(key)

        if isinstance(ann, type):
            return issubclass(ann, base_model)
        if isinstance(ann, TypeAliasType):
            return walk(ann.__value__)

        origin = get_origin(ann)
        if origin is None:
            return False
        if origin is Annotated:
            args = get_args(ann)
            return bool(args) and walk(args[0])
        return walk(origin) or any(walk(arg) for arg in get_args(ann))

    return walk(annotation)


def pydantic_adapter(annotation: Any) -> Any:
    """Return a cached-ready ``pydantic.TypeAdapter`` for Pydantic annotations."""
    if not contains_pydantic_model(annotation):
        return None
    pydantic = pydantic_module()
    if pydantic is None:
        return None
    adapter_cls = getattr(pydantic, "TypeAdapter", None)
    if adapter_cls is None:
        return None
    return adapter_cls(annotation)


def _validation_error_type() -> tuple[type[BaseException], ...]:
    pydantic = pydantic_module()
    if pydantic is None:
        return ()
    exc_type = getattr(pydantic, "ValidationError", None)
    if isinstance(exc_type, type) and issubclass(exc_type, BaseException):
        return (exc_type,)
    return ()


def _format_errors(loc: list[Any], exc: Any) -> list[dict[str, Any]]:
    return [{"loc": [*loc, *err["loc"]], "msg": err["msg"]} for err in exc.errors()]


def validate_json(
    adapter: Any, raw: bytes, loc: list[Any], errors: list[dict[str, Any]]
) -> Any:
    """Validate raw JSON bytes with a cached ``pydantic.TypeAdapter``."""
    try:
        return adapter.validate_json(raw)
    except _validation_error_type() as exc:
        errors.extend(_format_errors(loc, exc))
        return None


def validate_python(
    adapter: Any,
    raw: Any,
    loc: list[Any],
    *,
    strict: bool,
    errors: list[dict[str, Any]],
) -> Any:
    """Validate a Python value with a cached ``pydantic.TypeAdapter``."""
    try:
        return adapter.validate_python(raw, strict=strict)
    except _validation_error_type() as exc:
        errors.extend(_format_errors(loc, exc))
        return None
