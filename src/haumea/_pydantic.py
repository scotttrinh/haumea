"""Lazy Pydantic detection and validation without importing ``pydantic``.

Checks ``sys.modules.get("pydantic")`` so ``pydantic`` is never imported unless
user code has already loaded it.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from types import ModuleType
from typing import Annotated, Any, Literal, TypeAliasType, get_args, get_origin


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


def _iter_pydantic_models(annotation: Any) -> Iterator[type]:
    base_model = _base_model_type()
    if base_model is None:
        return

    seen: set[int] = set()

    def walk(ann: Any) -> Iterator[type]:
        key = id(ann)
        if key in seen:
            return
        seen.add(key)

        if isinstance(ann, type):
            if issubclass(ann, base_model):
                yield ann
            return
        if isinstance(ann, TypeAliasType):
            yield from walk(ann.__value__)
            return

        origin = get_origin(ann)
        if origin is None:
            return
        if origin is Annotated:
            args = get_args(ann)
            if args:
                yield from walk(args[0])
            return
        yield from walk(origin)
        for arg in get_args(ann):
            yield from walk(arg)

    yield from walk(annotation)


def contains_pydantic_model(annotation: Any) -> bool:
    """Return whether ``annotation`` is or wraps a ``pydantic.BaseModel`` subclass."""
    return any(_iter_pydantic_models(annotation))


def _serialization_schema_generator() -> type | None:
    pydantic = pydantic_module()
    if pydantic is None:
        return None
    json_schema_mod = getattr(pydantic, "json_schema", None)
    base: Any = getattr(json_schema_mod, "GenerateJsonSchema", None)
    if not isinstance(base, type):
        return None

    class _SerializationJsonSchema(base):
        def model_schema(self, schema: Any) -> Any:
            prev = self.by_alias
            config = getattr(schema["cls"], "model_config", None)
            self.by_alias = isinstance(config, dict) and bool(
                config.get("serialize_by_alias")
            )
            try:
                return super().model_schema(schema)
            finally:
                self.by_alias = prev

    return _SerializationJsonSchema


def json_schema(
    adapter: Any,
    *,
    ref_template: str,
    mode: Literal["validation", "serialization"],
) -> dict[str, Any]:
    """Generate a JSON Schema for a ``pydantic.TypeAdapter`` matching runtime mode."""
    if mode == "validation":
        return adapter.json_schema(ref_template=ref_template, mode="validation")
    kwargs: dict[str, Any] = {
        "ref_template": ref_template,
        "mode": "serialization",
        "by_alias": False,
    }
    if (generator := _serialization_schema_generator()) is not None:
        kwargs["schema_generator"] = generator
    return adapter.json_schema(**kwargs)


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


def enc_hook(obj: Any) -> Any:
    """``msgspec`` encoding hook for ``pydantic.BaseModel`` instances."""
    base_model = _base_model_type()
    if base_model is not None and isinstance(obj, base_model):
        return obj.model_dump(mode="json")
    raise TypeError(f"Encoding objects of type {type(obj).__name__} is unsupported")
