# Parameters and Validation

Haumea infers each handler parameter's source, coercion rules, and dependencies directly from its Python signature and type annotations.

## Source Inference

[[src/haumea/params.py#_field]] classifies each parameter of a handler or dependency into one of seven sources (`path`, `query`, `header`, `cookie`, `body`, `request`, `depends`).

1. [[src/haumea/params.py#Depends]] (as default or inside `Annotated`) creates a recursive [[src/haumea/params.py#Endpoint]] (`depends`).
2. A [[src/haumea/requests.py#Request]] annotation receives the raw request (`request`).
3. Explicit [[src/haumea/params.py#Query]], [[src/haumea/params.py#Header]], [[src/haumea/params.py#Cookie]], or [[src/haumea/params.py#Body]] markers override inference; `Header()` converts parameter underscores to hyphens unless `alias` is set.
4. A parameter name matching a route `{segment}` comes from `path`.
5. A model type ([[src/haumea/params.py#_is_model]]: `msgspec.Struct`, `dataclass`, or `attrs` class) comes from `body`.
6. Everything else defaults to `query`.

## Body Decoding and Conversion

[[src/haumea/params.py#Endpoint#_resolve]] applies distinct decoding strategies depending on parameter source and body field count.

- **Single body field**: Decodes and validates raw request bytes directly into the field's annotation via [[src/haumea/params.py#_decode_body]] in a single `msgspec.json.decode` pass.
- **Multiple body fields**: Decodes the request body once as `dict[str, Any]` and validates each field by alias using `strict=True` in [[src/haumea/params.py#_convert]].
- **Path, query, header, and cookie**: Values arrive as strings, so [[src/haumea/params.py#_convert]] runs `msgspec.convert(..., strict=False)` for lenient coercion (e.g. `"7"` -> `int`). Non-list query parameters take the last value when repeated. Cookie fields read [[src/haumea/requests.py#Request#cookies]] by `alias` (the parameter name unless set), and OpenAPI documents them as `in: cookie`.

## Dependencies and Execution

[[src/haumea/params.py#Endpoint#_call]] resolves dependencies recursively, caches results per request, and offloads sync functions to a worker thread.

Dependencies are cached in a per-request dictionary keyed by `field.dependency.func`, so a shared dependency runs at most once per request. Async handlers and dependencies are awaited directly; synchronous ones run via `asyncio.to_thread`.

## Validation Errors

All conversion and decoding failures are collected into a list of `{"loc": [...], "msg": ...}` dictionaries and raised as [[src/haumea/exceptions.py#RequestValidationError]] (`422`).

[[src/haumea/params.py#_error]] parses `msgspec`'s `- at $.path` error suffix and appends object keys and array indices to `loc`, giving field-level error locations even for nested `Struct` constraints.

## Request Cookies

[[src/haumea/requests.py#Request#cookies]] parses every `cookie` request header into a `dict[str, str]`, which `Cookie()` parameters read from.

Each `;`-separated pair splits on its first `=`, so values may contain `=`; names and values are stripped, and pairs without `=` or a name are skipped. [[src/haumea/requests.py#_unquote]] reverses `http.cookies` quoting (surrounding quotes, backslash and `\ooo` octal escapes), so values written by [[src/haumea/responses.py#Response#set_cookie]] round-trip. When a name repeats the first value wins, since browsers send the most specific path first.

## Platform Metadata

[[src/haumea/requests.py#Request]] exposes Vercel's per-request metadata as `id`, `ip` and `geo`, each with a local fallback so handlers run unchanged in dev and tests.

- [[src/haumea/requests.py#Request#id]] is `x-vercel-id`, else a UUID4 hex string generated when the `Request` is constructed, so repeated and concurrent reads from several threads agree.
- [[src/haumea/requests.py#Request#ip]] is `x-real-ip`, else the first trimmed `x-forwarded-for` entry, else the host of `scope["client"]`, or `None` without a client.

[[src/haumea/requests.py#Request#_first_header]] reads `x-vercel-id`, `x-real-ip` and `x-forwarded-for` in `scope["headers"]` order. `Request.headers` keeps the last of repeated field lines, which would pick a proxy-appended `x-forwarded-for` line over the client's.
- [[src/haumea/requests.py#Request#geo]] returns a frozen [[src/haumea/requests.py#Geo]] from the `x-vercel-ip-*` headers; fields are `None` when their header is absent.

`Geo.region` reads `x-vercel-ip-country-region`, Vercel's ISO 3166-2 region code (`countryRegion` in `@vercel/functions`); there is no `x-vercel-ip-region` header, and the edge region lives in `x-vercel-id`. Vercel percent-encodes non-ASCII city names (RFC 3986), so `city` is decoded with `urllib.parse.unquote`.

Vercel sets these headers at the edge, but outside Vercel they come straight from the client, so they must not be trusted for authorization.

## Lazy Values

[[src/haumea/params.py#lazy]] turns a zero-argument `factory` into a callable that creates a process-wide value (a client or pool) on first call, in place of startup hooks.

- **Sync factories**: creation runs under a `threading.Lock` with a double check against a private sentinel, so concurrent first calls run `factory` once and `None` or falsy values are cached too.
- **Async factories**: [[src/haumea/params.py#_is_async]] treats coroutine functions and instances with `async def __call__` as async. The callable is then a coroutine function, so [[src/haumea/params.py#Endpoint#_call]] awaits it.
- **Shared creation**: the first call starts a task on its own loop that calls and awaits `factory()`, and publishes the outcome to a `concurrent.futures.Future`. Every caller awaits that through `asyncio.wrap_future` and `asyncio.shield`, so callers on other threads' event loops share the creation, and cancelling one caller leaves it running for the others. Once settled, calls return the stored value directly.
- **Task lifetime**: the shared state holds the creation task alongside its future until it settles, since event loops only keep weak references to tasks and the initiating caller may be cancelled and garbage-collected.
- **Abandoned waiters**: a cancelled caller attaches [[src/haumea/params.py#_retrieve]] to its `wrap_future` waiter, because `shield` stops watching the inner future once the caller is cancelled. A later failure is then not logged as an unretrieved exception, and active waiters still get the error.
- **Failures**: if `factory` raises, even synchronously from a function marked with `inspect.markcoroutinefunction`, nothing is cached. Callers sharing that attempt get the error, and the next call tries again. If the creating task is cancelled (for example its `asyncio.run` loop ends first), waiters get `CancelledError` and the next call retries.
- **As a dependency**: [[src/haumea/params.py#_named]] copies the factory's name and docstring but not `__wrapped__`, so `inspect.signature` reports no parameters and `Depends(lazy(factory))` adds no request inputs. Its per-request cache key is the `lazy` callable itself.
