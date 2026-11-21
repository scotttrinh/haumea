from __future__ import annotations

import asyncio
import math
import re
from collections.abc import AsyncIterable, AsyncIterator, Coroutine, Iterable
from datetime import UTC, datetime
from email.utils import format_datetime
from http.cookies import CookieError, SimpleCookie
from typing import Any, Literal

import msgspec

from haumea._pydantic import enc_hook as _pydantic_enc_hook
from haumea.requests import Message, Receive, Send


def _header_dict(
    headers: dict[str, str] | None, media_type: str | None
) -> dict[str, str]:
    """Lower-case ``headers``, adding ``content-type`` from ``media_type``
    (with ``charset=utf-8`` for ``text/*``) unless one was given."""
    result = {key.lower(): value for key, value in (headers or {}).items()}
    if media_type and "content-type" not in result:
        if media_type.startswith("text/"):
            media_type += "; charset=utf-8"
        result["content-type"] = media_type
    return result


_SAMESITE = ("lax", "strict", "none")
_EPOCH = "Thu, 01 Jan 1970 00:00:00 GMT"
_COOKIE_ATTRIBUTE = re.compile(r"[\x20-\x3a\x3c-\x7e]*")


def _is_latin_1(text: str) -> bool:
    try:
        text.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


class Response:
    """A buffered response.

    ``headers`` holds one value per name. Headers that repeat, such as
    ``set-cookie``, go through ``_append_header`` and are sent after them.
    """

    media_type: str | None = None

    def __init__(
        self,
        content: Any = b"",
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        media_type: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = self.render(content)
        self._init_headers(headers, media_type)
        self.headers["content-length"] = str(len(self.body))

    def _init_headers(
        self, headers: dict[str, str] | None, media_type: str | None
    ) -> None:
        self.headers = _header_dict(headers, media_type or self.media_type)
        self._appended_headers: list[tuple[str, str]] = []

    def _append_header(self, key: str, value: str) -> None:
        """Send another ``key`` header, keeping any already set."""
        self._appended_headers.append((key.lower(), value))

    def set_cookie(
        self,
        key: str,
        value: str = "",
        *,
        max_age: int | None = None,
        expires: datetime | int | str | None = None,
        path: str | None = "/",
        domain: str | None = None,
        secure: bool = True,
        httponly: bool = True,
        samesite: Literal["lax", "strict", "none"] | None = "lax",
    ) -> None:
        """Add a ``set-cookie`` header; each call adds another.

        ``value`` may be any Latin-1 text and is quoted as needed; encode
        other text yourself (for example with ``urllib.parse.quote``).
        ``path``, ``domain`` and a string ``expires`` must be printable ASCII
        without ``;``. ``expires`` is a datetime (naive means UTC), seconds
        from now, or an HTTP date string. Anything invalid raises
        ``ValueError``.
        """
        if not _is_latin_1(value):
            raise ValueError(f"cookie value must be Latin-1 text, not {value!r}")
        cookie: SimpleCookie = SimpleCookie()
        try:
            cookie[key] = value
        except CookieError as exc:
            raise ValueError(f"invalid cookie name {key!r}") from exc
        morsel = cookie[key]
        if max_age is not None:
            morsel["max-age"] = max_age
        if isinstance(expires, datetime):
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            expires = format_datetime(expires.astimezone(UTC), usegmt=True)
        for attribute, setting in (
            ("expires", expires),
            ("path", path),
            ("domain", domain),
        ):
            if setting is None:
                continue
            if isinstance(setting, str) and not _COOKIE_ATTRIBUTE.fullmatch(setting):
                raise ValueError(
                    f"cookie {attribute} must be printable ASCII without ';', "
                    f"not {setting!r}"
                )
            morsel[attribute] = setting
        morsel["secure"] = secure
        morsel["httponly"] = httponly
        if samesite is not None:
            if samesite.lower() not in _SAMESITE:
                raise ValueError(
                    f"samesite must be one of {_SAMESITE} or None, not {samesite!r}"
                )
            morsel["samesite"] = samesite.capitalize()
        self._append_header("set-cookie", morsel.OutputString())

    def delete_cookie(
        self, key: str, *, path: str | None = "/", domain: str | None = None
    ) -> None:
        """Expire cookie ``key``; ``path`` and ``domain`` must match the ones
        it was set with. It keeps ``set_cookie``'s default flags, since a
        browser may ignore a non-``Secure`` overwrite of a ``Secure`` cookie."""
        self.set_cookie(key, max_age=0, expires=_EPOCH, path=path, domain=domain)

    def render(self, content: Any) -> bytes:
        if content is None:
            return b""
        return content if isinstance(content, bytes) else str(content).encode()

    def _start_message(self) -> Message:
        return {
            "type": "http.response.start",
            "status": self.status_code,
            "headers": [
                (key.encode("latin-1"), value.encode("latin-1"))
                for key, value in [*self.headers.items(), *self._appended_headers]
            ],
        }

    async def __call__(
        self, receive: Receive, send: Send, *, include_body: bool = True
    ) -> None:
        await send(self._start_message())
        body = self.body if include_body else b""
        await send({"type": "http.response.body", "body": body})


class JSONResponse(Response):
    """Serializes Structs, dataclasses, attrs, BaseModels, datetimes, etc. as JSON."""

    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return msgspec.json.encode(content, enc_hook=_pydantic_enc_hook)


class HTMLResponse(Response):
    media_type = "text/html"


class PlainTextResponse(Response):
    media_type = "text/plain"


class RedirectResponse(Response):
    def __init__(self, url: str, status_code: int = 307) -> None:
        super().__init__(status_code=status_code, headers={"location": url})


_DONE: Any = object()


class _ThreadedIterator:
    """Advance a sync iterator in a worker thread, as an async iterator.

    Cancelling ``__anext__`` leaves the in-flight ``next()`` running (threads
    cannot be interrupted); ``aclose`` waits for it before closing, since a
    generator cannot be closed while it is executing.
    """

    def __init__(self, iterable: Iterable[Any]) -> None:
        self._iterator = iter(iterable)
        self._pending: asyncio.Future[Any] | None = None

    def __aiter__(self) -> _ThreadedIterator:
        return self

    async def __anext__(self) -> Any:
        self._pending = asyncio.ensure_future(
            asyncio.to_thread(next, self._iterator, _DONE)
        )
        item = await asyncio.shield(self._pending)
        self._pending = None
        if item is _DONE:
            raise StopAsyncIteration
        return item

    async def aclose(self) -> None:
        if self._pending is not None:
            await asyncio.wait([self._pending])
        if close := getattr(self._iterator, "close", None):
            await asyncio.to_thread(close)


def _async_iterator(content: AsyncIterable[Any] | Iterable[Any]) -> AsyncIterator[Any]:
    if isinstance(content, AsyncIterable):
        return aiter(content)
    return _ThreadedIterator(content)


async def _aclose(iterator: AsyncIterator[Any]) -> None:
    if aclose := getattr(iterator, "aclose", None):
        await aclose()


async def _stop(iterator: AsyncIterator[Any], *tasks: asyncio.Future[Any]) -> None:
    """Cancel ``tasks`` and wait for them to stop before closing ``iterator``:
    an async generator cannot be closed while a task is still running it."""
    for task in tasks:
        task.cancel()
    await asyncio.wait(tasks)
    await _aclose(iterator)


async def _run_to_completion(cleanup: Coroutine[Any, Any, None]) -> None:
    """Await ``cleanup`` even if the caller is cancelled meanwhile, then
    re-raise that ``CancelledError``."""
    task = asyncio.ensure_future(cleanup)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    task.result()


class StreamingResponse(Response):
    """Stream ``content``, an async or sync iterable of ``bytes`` or ``str``.

    No ``content-length`` is sent. A client disconnect cancels the producer
    and closes the iterator.
    """

    def __init__(
        self,
        content: AsyncIterable[bytes | str] | Iterable[bytes | str],
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        media_type: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.content = content
        self._init_headers(headers, media_type)
        self.headers.pop("content-length", None)

    async def __call__(
        self, receive: Receive, send: Send, *, include_body: bool = True
    ) -> None:
        await send(self._start_message())
        if not include_body:
            # Never touch the content, so a HEAD costs no producer work.
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        iterator = _async_iterator(self.content)
        stream = asyncio.ensure_future(self._stream(iterator, send))
        listen = asyncio.ensure_future(self._listen_for_disconnect(receive))
        try:
            await asyncio.wait([stream, listen], return_when="FIRST_COMPLETED")
        finally:
            await _run_to_completion(_stop(iterator, stream, listen))
        for task in (stream, listen):
            if not task.cancelled() and (error := task.exception()):
                raise error

    @staticmethod
    async def _stream(iterator: AsyncIterator[bytes | str], send: Send) -> None:
        async for chunk in iterator:
            body = chunk.encode() if isinstance(chunk, str) else bytes(chunk)
            await send({"type": "http.response.body", "body": body, "more_body": True})
            # Yield even if the producer and `send` never suspend, so the
            # disconnect listener gets to run.
            await asyncio.sleep(0)
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    @staticmethod
    async def _listen_for_disconnect(receive: Receive) -> None:
        while (await receive())["type"] != "http.disconnect":
            pass


class ServerSentEvent(msgspec.Struct):
    """One SSE event. ``data`` that is not a ``str`` is JSON-encoded, so
    ``None`` is ``null``; leave ``data`` unset for an event without data."""

    data: Any = msgspec.UNSET
    event: str | None = None
    id: str | None = None
    retry: int | None = None

    def __post_init__(self) -> None:
        for field in ("event", "id"):
            value = getattr(self, field)
            if value is not None and ("\n" in value or "\r" in value):
                raise ValueError(f"SSE {field} must not contain a newline")

    def encode(self) -> bytes:
        """The event's wire framing, ending in a blank line."""
        lines = [
            f"{field}: {value}"
            for field in ("event", "id", "retry")
            if (value := getattr(self, field)) is not None
        ]
        if self.data is not msgspec.UNSET:
            data = self.data
            if not isinstance(data, str):
                data = msgspec.json.encode(data, enc_hook=_pydantic_enc_hook).decode()
            lines.extend(f"data: {line}" for line in re.split(r"\r\n|\r|\n", data))
        return ("\n".join(lines) + "\n\n").encode()


_PING = b": ping\n\n"


class SSEResponse(StreamingResponse):
    """Server-sent events from an async or sync iterable.

    Each event is a ``ServerSentEvent``, a ``str`` (the data) or any value
    ``msgspec`` can JSON-encode. With ``ping``, a comment line is sent every
    ``ping`` seconds to keep the connection alive.
    """

    def __init__(
        self,
        events: AsyncIterable[Any] | Iterable[Any],
        ping: float | None = None,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        if ping is not None and not (math.isfinite(ping) and ping > 0):
            raise ValueError(
                f"SSE ping must be a positive number of seconds, not {ping}"
            )
        headers = {key.lower(): value for key, value in (headers or {}).items()}
        headers["content-type"] = "text/event-stream"
        headers["cache-control"] = "no-cache"
        super().__init__(self._frames(events, ping), status_code, headers)

    @staticmethod
    async def _frames(
        events: AsyncIterable[Any] | Iterable[Any], ping: float | None
    ) -> AsyncIterator[bytes]:
        iterator = _async_iterator(events)
        loop = asyncio.get_running_loop()
        next_ping = None if ping is None else loop.time() + ping
        pending: asyncio.Future[Any] | None = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(anext(iterator))
                timeout = None if next_ping is None else max(next_ping - loop.time(), 0)
                done, _ = await asyncio.wait([pending], timeout=timeout)
                if not done:
                    assert ping is not None and next_ping is not None
                    next_ping += ping
                    yield _PING
                    continue
                try:
                    event = pending.result()
                except StopAsyncIteration:
                    return
                finally:
                    pending = None
                if not isinstance(event, ServerSentEvent):
                    event = ServerSentEvent(data=event)
                yield event.encode()
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.wait([pending])
            await _aclose(iterator)
