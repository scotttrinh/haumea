"""Work out a handler's inputs from its signature.

- a parameter named like a path segment (``[item_id]``) comes from the path
- a Pydantic model comes from the JSON body
- a ``Request`` annotation receives the request
- ``Depends(fn)`` is the result of calling ``fn``, resolved the same way
- anything else is a query parameter

``Query()``, ``Header()`` and ``Body()`` override the inferred source.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Annotated, Any, get_args, get_origin

from pydantic import BaseModel, TypeAdapter, ValidationError

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
    annotation: Any
    default: Any = _MISSING
    adapter: TypeAdapter[Any] | None = None
    dependency: Endpoint | None = None

    @property
    def required(self) -> bool:
        return self.default is _MISSING

    @property
    def is_list(self) -> bool:
        return get_origin(self.annotation) in (list, set, tuple)


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
        body = await _read_body(request, errors) if body_fields else _MISSING

        for field in self.fields:
            if field.source == "request":
                kwargs[field.name] = request
            elif field.dependency:
                func = field.dependency.func
                if func not in cache:  # each dependency runs once per request
                    cache[func] = await field.dependency._call(request, cache, errors)
                kwargs[field.name] = cache[func]
            else:
                loc = [field.source, field.alias]
                if field.source == "path":
                    raw = request.path_params.get(field.alias, _MISSING)
                elif field.source == "header":
                    raw = request.headers.get(field.alias.lower(), _MISSING)
                elif field.source == "query":
                    values = request.query_params.get(field.alias)
                    raw = values or _MISSING
                    if values and not field.is_list:
                        raw = values[-1]
                elif len(body_fields) == 1:
                    raw, loc = body, ["body"]
                else:  # several body fields: each is a key of a JSON object
                    raw = body.get(field.alias, _MISSING) if _is_dict(body) else body
                kwargs[field.name] = _validate(field, raw, loc, errors)
        return kwargs


def _is_dict(value: Any) -> bool:
    return isinstance(value, dict)


async def _read_body(request: Request, errors: list[dict[str, Any]]) -> Any:
    raw = await request.body()
    if not raw:
        return _MISSING
    try:
        return json.loads(raw)
    except ValueError:
        errors.append(
            {
                "type": "json_invalid",
                "loc": ["body"],
                "msg": "Invalid JSON",
                "input": None,
            }
        )
        return None


def _validate(
    field: Field, raw: Any, loc: list[Any], errors: list[dict[str, Any]]
) -> Any:
    if raw is _MISSING:
        if field.required:
            errors.append(
                {"type": "missing", "loc": loc, "msg": "Field required", "input": None}
            )
        return field.default
    assert field.adapter
    try:
        return field.adapter.validate_python(raw)
    except ValidationError as exc:
        for error in exc.errors(include_url=False, include_context=False):
            errors.append({**error, "loc": [*loc, *error["loc"]]})
        return None


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
        return Field(name, "depends", name, annotation, dependency=dependency)
    if annotation is Request:
        return Field(name, "request", name, annotation)

    if isinstance(marker, ParamInfo):
        source = marker.source
    elif name in path_params:
        source = "path"
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
        source = "body"
    else:
        source = "query"

    alias = marker.alias if isinstance(marker, ParamInfo) and marker.alias else name
    if source == "header" and alias == name:
        alias = name.replace("_", "-")
    return Field(name, source, alias, annotation, default, TypeAdapter(annotation))
