"""Work out a handler's inputs from its signature.

- a parameter named like a path segment (``[item_id]``) comes from the path
- a model (msgspec ``Struct``, dataclass, attrs class, or pydantic
  ``BaseModel``) comes from the JSON body
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
import types
from collections.abc import Callable, Coroutine, Iterator, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, get_args, get_origin, overload

import msgspec

from haumea._pydantic import is_pydantic_model
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


def is_generator_callable(func: Callable[..., Any]) -> bool:
    """A sync or async generator function, or an instance whose ``__call__``
    is one. Calling it only creates a generator, so as a check it would
    never run. Wrappers can hide one, so ``Endpoint.call`` also refuses a
    check whose result is a generator."""
    return any(
        inspect.isgeneratorfunction(target) or inspect.isasyncgenfunction(target)
        for target in (func, type(func).__call__)
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
    embed: bool = False
    """A body field that is one key of a JSON object, because its function
    has several body fields; otherwise it decodes the whole body."""

    @property
    def required(self) -> bool:
        return self.default is _MISSING

    @property
    def is_list(self) -> bool:
        annotation = self.annotation
        if get_origin(annotation) is Annotated:
            annotation = get_args(annotation)[0]
        return get_origin(annotation) in (list, set, tuple)


def _key(func: Callable[..., Any]) -> Any:
    """``func``'s identity for the per-request cache and schema traversal.

    Distinct callables never share a key, even when they compare equal. A
    bound method is keyed by its instance and function, since each
    ``obj.method`` access makes a new method object. The ``Endpoint`` tree
    holds every callable, so the ids stay valid."""
    if isinstance(func, types.MethodType):
        return (id(func.__self__), id(func.__func__))
    return id(func)


@dataclass
class RequestInput:
    """One documented request input and every field that reads it."""

    fields: list[Field]
    """Its consumers, first use first; all share ``source`` and ``alias``."""

    @property
    def source(self) -> str:
        return self.fields[0].source

    @property
    def alias(self) -> str:
        return self.fields[0].alias

    @property
    def embed(self) -> bool:
        return self.fields[0].embed

    @property
    def required(self) -> bool:
        """Required if any consumer requires it, since each one validates."""
        return any(field.required for field in self.fields)

    @property
    def annotations(self) -> list[Any]:
        return [field.annotation for field in self.fields]


class Endpoint:
    """A handler (or dependency) plus the plan for building its arguments.

    ``dependencies`` are inherited checks, one group per segment, root to
    leaf: each runs before the handler's arguments are resolved, sharing
    their per-request cache, and its result is discarded unless a
    ``Depends`` requests the same callable.
    """

    def __init__(
        self,
        func: Callable[..., Any],
        path_params: set[str],
        dependencies: Sequence[Sequence[Callable[..., Any]]] = (),
    ) -> None:
        self.func = func
        fields = [
            _field(parameter, path_params)
            for parameter in inspect.signature(func, eval_str=True).parameters.values()
        ]
        if sum(field.source == "body" for field in fields) > 1:
            for field in fields:
                field.embed = field.source == "body"
        self.fields = fields
        for check in (check for group in dependencies for check in group):
            if is_generator_callable(check):
                raise TypeError(
                    f"inherited check {check!r} is a generator function: it "
                    "would never run; use a plain or async function"
                )
        self.check_groups = [
            [Endpoint(check, path_params) for check in group] for group in dependencies
        ]
        self.checks = [check for group in self.check_groups for check in group]
        """The inherited checks in run order, root to leaf."""

    def _walk(self) -> Iterator[Endpoint | Field]:
        """Inherited checks root to leaf, then this endpoint: each callable
        once, as it runs once per request, followed by its own fields in
        parameter order with each ``Depends`` walked in place. Never calls
        anything."""
        seen: set[Any] = set()

        def visit(endpoint: Endpoint) -> Iterator[Endpoint | Field]:
            seen.add(_key(endpoint.func))
            yield endpoint
            for field in endpoint.fields:
                if not field.dependency:
                    yield field
                elif _key(field.dependency.func) not in seen:
                    yield from visit(field.dependency)

        for check in self.checks:
            if _key(check.func) not in seen:
                yield from visit(check)
        yield from visit(self)

    def request_inputs(self) -> list[RequestInput]:
        """Every input read from the request, including via dependencies and
        inherited checks, in first-use order.

        A path, query, header, cookie or embedded body input repeated across
        callables (same source and alias, header names case-insensitively)
        is one input holding every consumer. Each whole-body field is its own
        input, since each decodes the entire body. A query input read as a
        list by one consumer and as a single value by another has no schema
        both accept, so it raises ``ValueError``."""
        inputs: dict[Any, RequestInput] = {}
        for field in self._walk():
            if not isinstance(field, Field) or field.source == "request":
                continue
            whole = field.source == "body" and not field.embed
            alias = field.alias.lower() if field.source == "header" else field.alias
            key = object() if whole else (field.source, alias)
            request_input = inputs.setdefault(key, RequestInput([]))
            # Only query strings switch between one value and repeated values;
            # other sources decode as their annotation says.
            first = request_input.fields[0] if request_input.fields else None
            if field.source == "query" and first and first.is_list != field.is_list:
                raise ValueError(
                    f"{field.source} input {field.alias!r} is read as a list by "
                    "one dependency and as a single value by another"
                )
            request_input.fields.append(field)
        return list(inputs.values())

    def security_schemes(self) -> Iterator[HTTPBearer]:
        """Every credential scheme used here, by a dependency or a check."""
        for endpoint in self._walk():
            if isinstance(endpoint, Endpoint) and isinstance(endpoint.func, HTTPBearer):
                yield endpoint.func

    def declared_responses(self) -> dict[int, str]:
        """Error responses declared with ``raises``, here, in dependencies
        and in inherited checks.

        The first declaration of a status wins, visiting level by level: this
        function; then its direct dependencies in parameter order, followed
        by its inherited checks nearest segment first and in declared order
        within a segment; then their dependencies, and so on. A nearer
        declaration so beats a deeper one in any branch.
        """
        declared: dict[int, str] = {}
        level: list[Endpoint] = [self]
        checks = [check for group in reversed(self.check_groups) for check in group]
        while level:
            for endpoint in level:
                for status, description in getattr(endpoint.func, _RAISES, {}).items():
                    declared.setdefault(status, description)
            level = [f.dependency for e in level for f in e.fields if f.dependency]
            level, checks = [*level, *checks], []
        return declared

    async def call(self, request: Request) -> Any:
        errors: list[dict[str, Any]] = []
        cache: dict[Any, Any] = {}
        for check in self.checks:  # root to leaf, before any argument
            result = await _depend(check, request, cache, errors)
            if errors:
                raise RequestValidationError(errors)
            if inspect.isgenerator(result) or inspect.isasyncgen(result):
                # A wrapper hid a generator from is_generator_callable: its
                # body never ran, so the check never passed.
                raise TypeError(
                    f"inherited check {check.func!r} returned a generator "
                    "instead of running"
                )
        result = await self._call(request, cache, errors)
        if errors:
            raise RequestValidationError(errors)
        return result

    async def _call(
        self, request: Request, cache: dict[Any, Any], errors: list[dict[str, Any]]
    ) -> Any:
        """Call ``func`` with resolved arguments: async callables (including
        instances with ``async def __call__``) on the loop, others in a
        worker thread. An awaitable result, such as a sync wrapper's
        coroutine, is awaited, so a check can never be skipped unawaited."""
        kwargs = await self._resolve(request, cache, errors)
        if errors:
            return None
        if _is_async(self.func):
            result = self.func(**kwargs)
        else:
            result = await asyncio.to_thread(self.func, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def _resolve(
        self, request: Request, cache: dict[Any, Any], errors: list[dict[str, Any]]
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        body = _MISSING
        if any(field.embed for field in self.fields):  # keys of a JSON object
            body = _decode_body(await request.body(), dict[str, Any], errors)

        for field in self.fields:
            if field.source == "request":
                kwargs[field.name] = request
            elif field.dependency:
                kwargs[field.name] = await _depend(
                    field.dependency, request, cache, errors
                )
            elif field.source == "body" and not field.embed:
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


async def _depend(
    dependency: Endpoint,
    request: Request,
    cache: dict[Any, Any],
    errors: list[dict[str, Any]],
) -> Any:
    """``dependency``'s result, running it only on its first use in this
    request: inherited checks and ``Depends`` share ``cache``."""
    key = _key(dependency.func)
    if key not in cache:
        cache[key] = await dependency._call(request, cache, errors)
    return cache[key]


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
        or is_pydantic_model(annotation)
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
