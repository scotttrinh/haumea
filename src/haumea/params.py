"""Work out a handler's inputs from its signature.

- a parameter named like a path segment (``[item_id]``) comes from the path
- a model (msgspec ``Struct``, dataclass or attrs class) comes from the JSON body
- a ``Request`` annotation receives the request
- ``Depends(fn)`` is the result of calling ``fn``, resolved the same way
- anything else is a query parameter

``Query()``, ``Header()``, ``Cookie()`` and ``Body()`` override the inferred source.
``lazy(factory)`` makes a process-wide value usable as ``Depends(...)``.
``@raises(status, description)`` documents an error response in OpenAPI.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import functools
import inspect
import re
import threading
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass
from typing import Annotated, Any, get_args, get_origin, overload

import msgspec

from haumea.exceptions import RequestValidationError
from haumea.requests import Request
from haumea.security import HTTPBearer

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


_RAISES = "__haumea_raises__"


def raises[F: Callable[..., Any]](
    status_code: int, description: str
) -> Callable[[F], F]:
    """Document that a handler or dependency may respond with an error.

    ``@raises(401, "Not authenticated")`` adds that response to every
    operation using the decorated function. Stack it for several statuses.
    It only documents: raise ``HTTPException`` to send the response. When
    stacked decorators repeat a status, the outermost one wins.
    """
    if not isinstance(status_code, int) or isinstance(status_code, bool):
        raise TypeError(f"raises() takes an int status code, got {status_code!r}")
    if not 400 <= status_code <= 599:
        raise ValueError(f"raises() takes an error status code, got {status_code}")

    def decorate(func: F) -> F:
        # Decorators apply inner first, so the outer one overwrites.
        declared = getattr(func, _RAISES, {})
        setattr(func, _RAISES, {**declared, status_code: description})
        return func

    return decorate


def Query(default: Any = ..., *, alias: str | None = None) -> Any:
    return ParamInfo("query", default, alias)


def Header(default: Any = ..., *, alias: str | None = None) -> Any:
    return ParamInfo("header", default, alias)


def Cookie(default: Any = ..., *, alias: str | None = None) -> Any:
    return ParamInfo("cookie", default, alias)


def Body(default: Any = ..., *, alias: str | None = None) -> Any:
    return ParamInfo("body", default, alias)


@overload
def lazy[T](
    factory: Callable[[], Coroutine[Any, Any, T]],
) -> Callable[[], Coroutine[Any, Any, T]]: ...
@overload
def lazy[T](factory: Callable[[], T]) -> Callable[[], T]: ...
def lazy(factory: Callable[[], Any]) -> Callable[[], Any]:
    """A zero-argument callable that creates its value with ``factory()`` on
    first call and returns that value for the life of the process.

    Creation runs under a lock, so concurrent first calls run ``factory``
    once. For an async ``factory`` (a coroutine function or an instance with
    ``async def __call__``) the first call's task is shared: callers await
    it, even from other threads' event loops, and cancelling one caller does
    not cancel the creation. If ``factory`` raises, nothing is cached and
    the next call tries again.

    The callable takes no parameters, so ``Depends(lazy(factory))`` adds no
    request inputs even when ``factory`` has defaulted parameters.
    """
    lock = threading.Lock()
    value: Any = _MISSING

    if not _is_async(factory):

        def get():
            nonlocal value
            if value is _MISSING:
                with lock:
                    if value is _MISSING:
                        value = factory()
            return value

        return _named(get, factory)

    # A concurrent.futures.Future, unlike an asyncio one, can be awaited from
    # any thread's event loop, so overlapping loops share one creation. The
    # task is held here too: the event loop keeps only weak references to
    # tasks, and the caller that started it may be cancelled and dropped.
    pending: tuple[concurrent.futures.Future[Any], asyncio.Task[Any]] | None = None

    async def create() -> Any:
        # Calling factory() in the task body makes a synchronous raise fail
        # this attempt like any other error.
        return await factory()

    def settle(future: concurrent.futures.Future[Any], task: asyncio.Task[Any]):
        nonlocal pending, value
        with lock:
            pending = None
            if not task.cancelled() and task.exception() is None:
                value = task.result()
        if task.cancelled():
            future.cancel()
        elif (error := task.exception()) is not None:
            future.set_exception(error)
        else:
            future.set_result(task.result())

    async def aget():
        nonlocal pending
        if value is not _MISSING:
            return value
        with lock:
            if value is not _MISSING:
                return value
            if pending is None:
                task = asyncio.ensure_future(create())
                future = concurrent.futures.Future()
                task.add_done_callback(functools.partial(settle, future))
                pending = (future, task)
            future, _ = pending
        waiter = asyncio.wrap_future(future)
        try:
            # shield: cancelling this caller must not cancel the shared future.
            return await asyncio.shield(waiter)
        except asyncio.CancelledError:
            # Nobody awaits the abandoned waiter now, so retrieve its outcome
            # to keep asyncio from reporting an unretrieved exception.
            waiter.add_done_callback(_retrieve)
            raise

    return _named(aget, factory)


def _retrieve(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()


def _is_async(factory: Callable[..., Any]) -> bool:
    """A coroutine function, or an instance whose ``__call__`` is one."""
    return inspect.iscoroutinefunction(factory) or inspect.iscoroutinefunction(
        type(factory).__call__
    )


def _named(func: Callable[[], Any], factory: Callable[..., Any]) -> Callable[[], Any]:
    """Copy ``factory``'s name and docs without ``__wrapped__``, which would
    make ``inspect.signature`` report the factory's parameters."""
    for attr in ("__module__", "__name__", "__qualname__", "__doc__"):
        if hasattr(factory, attr):
            setattr(func, attr, getattr(factory, attr))
    return func


@dataclass
class Field:
    name: str
    source: str  # path | query | header | cookie | body | request | depends
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

    def security_schemes(self) -> Iterator[HTTPBearer]:
        """Every credential scheme this endpoint or its dependencies use."""
        if isinstance(self.func, HTTPBearer):
            yield self.func
        for field in self.fields:
            if field.dependency:
                yield from field.dependency.security_schemes()

    def declared_responses(self) -> dict[int, str]:
        """Error responses declared with ``raises``, here and in dependencies.

        The first declaration of a status wins, visiting level by level: this
        function, then its direct dependencies in parameter order, then
        theirs. A nearer declaration so beats a deeper one in any branch.
        """
        declared: dict[int, str] = {}
        level: list[Endpoint] = [self]
        while level:
            for endpoint in level:
                for status, description in getattr(endpoint.func, _RAISES, {}).items():
                    declared.setdefault(status, description)
            level = [f.dependency for e in level for f in e.fields if f.dependency]
        return declared

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
                # Path, query, header and cookie values are strings: convert
                # leniently.
                if field.source == "path":
                    raw = request.path_params.get(field.alias, _MISSING)
                elif field.source == "header":
                    raw = request.headers.get(field.alias.lower(), _MISSING)
                elif field.source == "cookie":
                    raw = request.cookies.get(field.alias, _MISSING)
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
