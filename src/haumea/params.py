"""Work out a handler's inputs from its signature.

- a parameter named like a path segment (``[item_id]``) comes from the path
- a model (msgspec ``Struct``, dataclass or attrs class) comes from the JSON body
- a ``Request`` annotation receives the request
- ``Depends(fn)`` is the result of calling ``fn``, resolved the same way
- anything else is a query parameter

``Query()``, ``Header()`` and ``Body()`` override the inferred source.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Annotated, Any, get_args, get_origin

import msgspec

from haumea.exceptions import RequestValidationError
from haumea.requests import Request

_MISSING: Any = object()


class DependsInfo:
    def __init__(self, dependency: Callable[..., Any]) -> None:
        self.dependency = dependency


class ParamInfo:
    def __init__(self, source: str, default: Any, alias: str | None) -> None:
        self.source = source
        self.default = default
        self.alias = alias


# These return Any so they type-check as defaults: `q: str = Query("x")`.


def Depends(dependency: Callable[..., Any]) -> Any:
    return DependsInfo(dependency)


def Query(default: Any = ..., *, alias: str | None = None) -> Any:
    return ParamInfo("query", default, alias)


def Header(default: Any = ..., *, alias: str | None = None) -> Any:
    return ParamInfo("header", default, alias)


def Body(default: Any = ..., *, alias: str | None = None) -> Any:
    return ParamInfo("body", default, alias)


@dataclass
class Field:
    name: str
    source: str  # path | query | header | body | request | depends
    alias: str
    annotation: Any  # as written, including any Annotated[..., Meta(...)]
    default: Any = _MISSING
    dependency: Endpoint | None = None

    @property
    def required(self) -> bool:
        return self.default is _MISSING

    @property
    def is_list(self) -> bool:
        annotation = self.annotation
        if get_origin(annotation) is Annotated:
            annotation = get_args(annotation)[0]
        return get_origin(annotation) in (list, set, tuple)


class Endpoint:
    """A handler (or dependency) plus the plan for building its arguments."""

    def __init__(self, func: Callable[..., Any], path_params: set[str]) -> None:
        self.func = func
        self.fields = [
            _field(parameter, path_params)
            for parameter in inspect.signature(func, eval_str=True).parameters.values()
        ]

    def request_fields(self) -> Iterator[Field]:
        """Every field read from the request, including via dependencies."""
        for field in self.fields:
            if field.dependency:
                yield from field.dependency.request_fields()
            elif field.source != "request":
                yield field

    async def call(self, request: Request) -> Any:
        errors: list[dict[str, Any]] = []
        result = await self._call(request, {}, errors)
        if errors:
            raise RequestValidationError(errors)
        return result

    async def _call(
        self, request: Request, cache: dict[Any, Any], errors: list[dict[str, Any]]
    ) -> Any:
        kwargs = await self._resolve(request, cache, errors)
        if errors:
            return None
        if inspect.iscoroutinefunction(self.func):
            return await self.func(**kwargs)
        return await asyncio.to_thread(self.func, **kwargs)

    async def _resolve(
        self, request: Request, cache: dict[Any, Any], errors: list[dict[str, Any]]
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        body_fields = [field for field in self.fields if field.source == "body"]
        body = _MISSING
        if len(body_fields) > 1:  # each field is a key of a JSON object
            body = _decode_body(await request.body(), dict[str, Any], errors)

        for field in self.fields:
            if field.source == "request":
                kwargs[field.name] = request
            elif field.dependency:
                func = field.dependency.func
                if func not in cache:  # each dependency runs once per request
                    cache[func] = await field.dependency._call(request, cache, errors)
                kwargs[field.name] = cache[func]
            elif field.source == "body" and len(body_fields) == 1:
                # Decode and validate straight from the bytes, in one pass.
                value = _decode_body(await request.body(), field.annotation, errors)
                kwargs[field.name] = _or_default(field, value, ["body"], errors)
            elif field.source == "body":
                raw = (
                    body.get(field.alias, _MISSING) if isinstance(body, dict) else body
                )
                kwargs[field.name] = _convert(field, raw, strict=True, errors=errors)
            else:
                # Path, query and header values are strings: convert leniently.
                if field.source == "path":
                    raw = request.path_params.get(field.alias, _MISSING)
                elif field.source == "header":
                    raw = request.headers.get(field.alias.lower(), _MISSING)
                else:
                    values = request.query_params.get(field.alias)
                    raw = values or _MISSING
                    if values and not field.is_list:
                        raw = values[-1]
                kwargs[field.name] = _convert(field, raw, strict=False, errors=errors)
        return kwargs


_ERROR_PATH = re.compile(r" - at `\$(.*)`$")
_PATH_PART = re.compile(r"\.([^.\[]+)|\[(\d+|\.\.\.)\]")


def _error(loc: list[Any], exc: msgspec.MsgspecError) -> dict[str, Any]:
    """Turn a msgspec error into ``{"loc": [...], "msg": "..."}``.

    msgspec reports the first problem it finds, with a path suffix like
    ``- at `$.items[0].price```; fold that path into ``loc``.
    """
    msg = str(exc)
    if match := _ERROR_PATH.search(msg):
        msg = msg[: match.start()]
        for key, index in _PATH_PART.findall(match[1]):
            loc = [*loc, key or (int(index) if index.isdigit() else index)]
    return {"loc": loc, "msg": msg}


def _decode_body(raw: bytes, annotation: Any, errors: list[dict[str, Any]]) -> Any:
    if not raw:
        return _MISSING
    try:
        return msgspec.json.decode(raw, type=annotation)
    except msgspec.MsgspecError as exc:
        errors.append(_error(["body"], exc))
        return None


def _or_default(
    field: Field, value: Any, loc: list[Any], errors: list[dict[str, Any]]
) -> Any:
    if value is not _MISSING:
        return value
    if field.required:
        errors.append({"loc": loc, "msg": "Field required"})
    return field.default


def _convert(
    field: Field, raw: Any, *, strict: bool, errors: list[dict[str, Any]]
) -> Any:
    loc = [field.source, field.alias]
    if raw is _MISSING:
        return _or_default(field, raw, loc, errors)
    try:
        return msgspec.convert(raw, field.annotation, strict=strict)
    except msgspec.MsgspecError as exc:
        errors.append(_error(loc, exc))
        return None


def _is_model(annotation: Any) -> bool:
    return isinstance(annotation, type) and (
        issubclass(annotation, msgspec.Struct)
        or dataclasses.is_dataclass(annotation)
        or hasattr(annotation, "__attrs_attrs__")
    )


def _field(parameter: inspect.Parameter, path_params: set[str]) -> Field:
    name = parameter.name
    annotation = parameter.annotation
    default = parameter.default
    if annotation is inspect.Parameter.empty:
        annotation = Any
    if default is inspect.Parameter.empty:
        default = _MISSING

    # A marker may be the default (`q: str = Query()`) or in Annotated metadata.
    marker = None
    declared = annotation
    if get_origin(annotation) is Annotated:
        annotation, *metadata = get_args(annotation)
        marker = next(
            (m for m in metadata if isinstance(m, DependsInfo | ParamInfo)), None
        )
    if isinstance(default, DependsInfo | ParamInfo):
        marker = default
        default = _MISSING
    if (
        isinstance(marker, ParamInfo)
        and default is _MISSING
        and marker.default is not ...
    ):
        default = marker.default

    if isinstance(marker, DependsInfo):
        dependency = Endpoint(marker.dependency, path_params)
        return Field(name, "depends", name, declared, dependency=dependency)
    if annotation is Request:
        return Field(name, "request", name, declared)

    if isinstance(marker, ParamInfo):
        source = marker.source
    elif name in path_params:
        source = "path"
    elif _is_model(annotation):
        source = "body"
    else:
        source = "query"

    alias = marker.alias if isinstance(marker, ParamInfo) and marker.alias else name
    if source == "header" and alias == name:
        alias = name.replace("_", "-")
    return Field(name, source, alias, declared, default)
