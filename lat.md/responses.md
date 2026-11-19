# Responses

Response classes are ASGI callables that [[src/haumea/app.py#Haumea#__call__]] invokes with `receive`, `send` and `include_body` (false for `HEAD`).

[[src/haumea/responses.py#Response#__call__]] takes `receive` as well as `send`, so streaming responses can watch for a client disconnect. Buffered responses ([[src/haumea/responses.py#Response]], [[src/haumea/responses.py#JSONResponse]], [[src/haumea/responses.py#HTMLResponse]], [[src/haumea/responses.py#PlainTextResponse]], [[src/haumea/responses.py#RedirectResponse]]) render their body up front and send a `content-length`.

## Streaming

[[src/haumea/responses.py#StreamingResponse]] streams an async or sync iterable of `bytes` or `str` chunks, with `more_body: True` on each chunk and no `content-length`, even a caller-supplied one.

A final empty body with `more_body: False` ends the stream. Sync iterables are wrapped in [[src/haumea/responses.py#_ThreadedIterator]], which advances them with `asyncio.to_thread` so blocking producers never stall the event loop. For `HEAD`, the response sends headers and an empty final body before obtaining an iterator, so `__iter__`/`__aiter__` are never called and no producer work runs.

While streaming, [[src/haumea/responses.py#StreamingResponse#__call__]] races the producer against [[src/haumea/responses.py#StreamingResponse#_listen_for_disconnect]], which reads `receive` until `http.disconnect`. On a disconnect the producer task is cancelled and the iterator is always closed (`aclose()` for async iterators, `close()` in a worker thread for sync ones). [[src/haumea/responses.py#StreamingResponse#_stream]] yields to the event loop after every chunk, so a producer and `send` that never suspend still let the listener observe a disconnect promptly.

A worker thread cannot be interrupted, so [[src/haumea/responses.py#_ThreadedIterator#aclose]] first waits for an in-flight `next()` to return before calling `close()`; a generator cannot be closed while it is executing. A sync producer blocked forever in `next()` therefore keeps the response from returning after a disconnect. Errors from the producer or from `receive()` propagate after both tasks finish and the iterator is closed, so a failing `receive()` never ends the response silently.

Cleanup is [[src/haumea/responses.py#_stop]]: cancel the stream and listener tasks, wait for both to stop, then close the iterator, because an async generator cannot be closed while a task is still running it (for example, a producer whose `finally` awaits). [[src/haumea/responses.py#_run_to_completion]] runs that cleanup as a shielded task. If the caller cancels the request meanwhile, the response still waits for cleanup to finish, then re-raises `CancelledError`.

## Server-Sent Events

[[src/haumea/responses.py#SSEResponse]] is a streaming response with `content-type: text/event-stream` and `cache-control: no-cache`, which always win over caller `headers`.

Each event is a [[src/haumea/responses.py#ServerSentEvent]] (`data`, `event`, `id`, `retry`), a `str` used as the data, or any other value, which becomes the data of an event JSON-encoded by `msgspec` (so `Struct`s, dicts and lists work, and a raw `None` is `data: null`). Only a `ServerSentEvent` whose `data` is left unset (`msgspec.UNSET`) has no `data:` line. [[src/haumea/responses.py#ServerSentEvent#encode]] writes `event`, `id` and `retry` lines, then one `data:` line per line of the data (splitting on `\r\n`, `\r` or `\n`), then a blank line. `event` and `id` containing a newline are rejected with `ValueError` at construction, since they would break framing.

With `ping`, [[src/haumea/responses.py#SSEResponse#_frames]] sends a `: ping` comment line every `ping` seconds on a fixed schedule, interleaved with events, to keep proxies from closing idle connections. `ping` must be a positive finite number, so a zero interval cannot become a tight loop; anything else raises `ValueError` at construction, and `None` disables pings. Closing the frames generator on a disconnect cancels any pending read and closes the events iterator.
