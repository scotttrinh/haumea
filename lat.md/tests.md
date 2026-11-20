---
lat:
  require-code-mention: true
---
# Tests

Test specifications for Haumea, anchored to [[tests/test_haumea.py]] against the `examples/hello` project.

## Routes and parameters

Verify basic routing, query/path parameter binding, `HEAD` handling, `405` `Allow` headers, and `404` responses.

## Validation errors

Verify `422` `RequestValidationError` responses and `loc` paths for invalid path types, missing body fields, `msgspec.Meta` constraints, malformed JSON, and missing required bodies.

## Body and dependencies

Verify JSON body decoding alongside `Depends` and `HTTPBearer` resolution, a missing credential's `401` with `WWW-Authenticate: Bearer`, and a wrong token's `HTTPException` `401`.

## Dev docs and static files

Verify `public/` static file serving, raw-scope path traversal and trailing-slash rejection (`404`), `/docs` Swagger UI, and `/openapi.json` schema generation including summaries, parameters, `$ref` schemas, and `Meta` constraints.

It also checks the hello `PUT`'s bearer `security` and declared `401`, with no `authorization` header parameter and no `security` on the public `GET`.

## Project config

Verify [[src/haumea/config.py#load_config]] defaults, parsing of all three keys, `base_path` normalization, and `ValueError` for unknown keys, strict type errors, non-table `tool` values and invalid prefixes.

Invalid prefixes include control characters, `%`, quotes, `\`, `<>`, `$`, non-ASCII and segments ending in `.func`. A separate test checks [[src/haumea/openapi.py#swagger_html]] escapes its title and URL, with no `<` left inside the inline script.

## Prefixed dev routing

Verify a `base_path = "/api/"` project served by `create_app` picks up the config: prefixed routes, static files, docs and `openapi.json` paths, and JSON 404s outside the prefix.

Uses `isolated_imports`. Also checks raw-scope traversal rejection (httpx would normalize `..`), that `/api/` misses the root route, and that an explicit `base_path=""` overrides the config.

## Root path reconstruction

Verify [[src/haumea/requests.py#Request#path]] joins `root_path + path` with the prefix-root exception and no `startswith` check, and that routing matches the reconstructed path.

Covers the five spec cases: `/api` + `/` hits `app/route.py`, unstripped `/api/` misses it, `/api` + `/users` matches, `/apiary` becomes `/api/apiary`, and `/api` + `/api/users` becomes `/api/api/users` and misses. A `create_route_app` function also serves its stripped path.

## Dev routing priority

Verify a prefixed catch-all route does not shadow `/api/docs.html`, `/api/openapi.json` or `/api/hello.txt` in dev, while literal routes still win: a `POST`-only `app/docs/route.py` makes `GET /api/docs` a `405`.

Raw scopes check that trailing slashes, symlink loops, `..` and NUL bytes fall through to the catch-all instead of serving a file or returning `500`.

## Dev docs precedence

Verify dev follows the build's file precedence: `public/docs` beats the Swagger UI on `/api/docs`, while generated `docs.html` and `openapi.json` beat their `public/` copies.

Non-`GET`/`HEAD` requests to those files get `405` with `Allow: GET, HEAD` before generating the OpenAPI schema or reading static file bytes, instead of reaching a `POST` catch-all, and without `public/docs` the docs path serves Swagger.

## Route load cache

Verify [[src/haumea/routing.py#Route#load]] returns one module for a file discovered under two prefixes, and that a failing import leaves `sys.modules` unchanged and raises again on retry.

## Route segments

Verify `Route.segments` lists the directories from `app/` to the route's own directory, root to leaf, for the root route, nested routes, `(group)` directories, and `[id]` and `[...path]` segments, with and without `base_path`.

## Segment module loading

Verify [[src/haumea/routing.py#load_module]] runs a segment file once when two sibling routes load it, shares the cache with [[src/haumea/routing.py#Route#load]], and leaves `sys.modules` clean after a failing import that raises again on retry.

## Ancestor segment loading

Verify [[src/haumea/routing.py#Route#load_segments]] loads only the route's own ancestor files, root to leaf, skipping a sibling directory's file and missing files, and that routes sharing an ancestor (including through a route group) reuse its module.

## Route module isolation

Verify `app/route.py` in two projects loads two modules, and that `base_path = "/v1+"` keeps `app/route.py` and `app/!/route.py` apart: modules are keyed by file, not by the function path.

## Symlinked app directory

Verify a project whose `app` symlinks to `src/app` reads `base_path` from the root `pyproject.toml` in `create_app`, `create_route_app` and `plan_build`.

## Build plan

Verify [[src/haumea/build.py#plan_build]] on a temporary project without writing output: paths, functions, entrypoints, `.vc-config.json` overrides, generated docs, and `config.json` routes.

The `isolated_imports` fixture restores `sys.path` and `sys.modules` after loading route modules and disables bytecode writes.

## Build emit

Verify [[src/haumea/build.py#emit_build]] (with vendoring stubbed) preserves `copytree` semantics for `public/` (symlinks, modes, late files/dirs) and clones route config safely.

Checks mutable `regions` isolation, mixed dict keys, and values whose `__deepcopy__` raises.

## Build output layout

Verify `.vercel/output` `config.json` rewrites, static `docs.html`/`openapi.json`/`public/` files, per-route `.func` directories, `.vc-config.json` `maxDuration` overrides, and `_vendor` contents (including omitting `pydantic` and `uvicorn`).

## Built function invocation

Verify built `.func` directories execute standalone via `vc_handler` with `python -S` using only vendored dependencies, handle their own route, and return `404` for other routes and `/docs`.

## Prefixed build output

Verify a real build of a `base_path = "/api/"` project: prefixed `config.json` rewrites, `static/api/` files and docs, `functions/api.func`-style paths, and standalone invocation under the prefix.

Dependencies are vendored from `examples/hello`'s lock, since the temporary project has none. The module-scoped `prefixed_output` fixture restores `sys.path`, `sys.modules` and bytecode writes after building.

## Prefixed build without public

Verify a prefixed build with no `public/` writes only `static/api/` docs, and that a `public/` created while route modules import is still copied under `static/api/`.

## Custom build output directory

Verify [[src/haumea/build.py#plan_build]], [[src/haumea/build.py#build]], and [[src/haumea/cli.py#main]] (`--output`) write to a custom directory without creating `.vercel/output`.

Checks output paths both outside and inside `project_dir` (ensuring neither the output directory nor `.haumea-*` staging dirs leak into `.func` payloads across repeated builds) and `ValueError` rejection for paths containing the project or overlapping `app/` or `public/`.

## Locked lockfile

Verify [[src/haumea/build.py#build]] fails with `RuntimeError` when `uv.lock` is stale relative to `pyproject.toml` or missing, without modifying `uv.lock`.

## Deterministic build output

Verify repeated builds of the same project into separate directories produce byte-for-byte identical trees and that `config.json`, `openapi.json`, and `.vc-config.json` serialize with sorted keys.

## Workspace discovery

Verify [[src/haumea/build.py#plan_build]] finds the nearest `[tool.uv.workspace]` with `uv.lock`, stops at standalone `uv.lock` boundaries, sets `package` from `[project].name` in workspaces (`None` outside), and rejects unnamed workspace projects.

Covers ancestors missing `uv.lock` or `[tool.uv.workspace]`, standalone projects or ancestors with their own `uv.lock` under a workspace root, nested workspaces where the nearest match wins, a non-virtual workspace root acting as a service, and `ValueError` rejection for unnamed workspace members or virtual roots.

## Workspace build

Verify [[src/haumea/build.py#build]] on a uv workspace with `apps/api`, `packages/core`, and an unrelated `apps/worker`/`packages/unused` sibling resolves `workspace_root`, vendors `core` non-editably, omits `api` and `unused`, and serves `core` via `vc_handler`.

## Full-stack example build

Verify [[examples/fullstack]] declares the Vercel Services config and Python 3.12 pin, builds [[examples/fullstack/backend]] as a uv workspace member under `/api`, emits prefixed docs/static/functions without `uvicorn` in `_vendor/`, and invokes each built function standalone.

## Dev requires uvicorn

Verify [[src/haumea/cli.py#main]] exits with a `SystemExit` message pointing to `haumea[dev]` when `haumea dev` cannot import `uvicorn`.

## Store index

Verify [[src/haumea/build.py#normalize_dist_name]] and [[src/haumea/build.py#index_store]] record PEP 503 names, in-store `RECORD` paths, top-level import names, `Requires-Dist` names, and byte sizes.

Covers a synthetic store fixture (including `top_level.txt`, namespace packages, quoted CSV paths, escaped/absolute `RECORD` entries, excluded `*.dist-info`/`*.data`/`__pycache__`/`bin/`, and missing stores) and the real `_vendor/` from `examples/hello`.

## Build size report and limits

Verify [[src/haumea/build.py#SIZE_WARN_BYTES]], [[src/haumea/build.py#SIZE_LIMIT_BYTES]], per-function size and distribution count reporting in [[src/haumea/build.py#build]] and [[src/haumea/cli.py#main]], warnings above the warn threshold, and `RuntimeError` failure above the limit.

Lowers thresholds via `monkeypatch` to confirm warning messages name the largest distributions in descending size order while letting the build succeed, and that exceeding the fail limit raises `RuntimeError` naming the oversized function and largest distributions.

## Per-function tree-shaking

Verify [[src/haumea/build.py#build]] on a multi-package uv workspace bundles only each route's traced dependencies plus `haumea`, `vercel-runtime`, and their closures, invokes every built function standalone via `vc_handler`, and honors `include` and `tree_shake = false`.

Covers recursive project helpers with relative imports and cycles, transitive `Requires-Dist`, lazy imports inside handlers, skipped `TYPE_CHECKING` blocks, `importlib.import_module` and `__import__` string literals, route and project `include`, hard-linking across functions, per-function reported distribution counts, and `tree_shake = false` restoring the full store.

## Import tracer and closure

Verify [[src/haumea/build.py#_trace_function_chains]], [[src/haumea/build.py#_resolve_closure_with_reasons]], [[src/haumea/build.py#ALWAYS_INCLUDED]], and [[src/haumea/build.py#_read_route_include]] handle tracer, closure, size-report, and validation edge cases against a synthetic store.

Covers `vercel-functions` inclusion with its closure, cyclic `Requires-Dist`, namespace packages in the store and project helpers, package `__init__.py` tracing, `if TYPE_CHECKING:` `else` branches and `if not TYPE_CHECKING:`, aliased/relative `importlib.import_module`, unknown/stdlib ignores, per-function size warnings, and `ValueError` on invalid route `include`.

## Build-time import check

Verify [[src/haumea/build.py#build]] with `platform=None` fails with a `RuntimeError` naming the function, missing module, and `include` when a route imports a workspace package by computed name, and succeeds once added to route or project `include`.

Also verifies `PYTHONPATH` is ignored by `-I` and no `__pycache__` directories are written into built functions.

## Import check platform matching

Verify [[src/haumea/build.py#_check_function_imports]] skips with a printed message (without invoking `uv python find`) when `plan.platform` does not match the host OS/architecture, and runs when it matches.

## Import check target interpreter

Verify [[src/haumea/build.py#_check_function_imports]] executes the interpreter returned by `uv python find <python>` with `-I` instead of `sys.executable`, and skips with a printed message when `uv python find` finds none.

## Build explain

Verify `haumea build --explain <function>` (via [[src/haumea/cli.py#main]] and [[src/haumea/build.py#build]]) prints reason chains for included distributions and lists excluded ones.

Covers direct and helper-file traced imports, multi-hop transitive `Requires-Dist` chains, always-included packages and their dependencies, route and project `include`, excluded distributions omitted from `_vendor/`, `.func` suffix normalization, `tree_shake = false`, and `ValueError` on unknown function names without modifying an existing output directory.

## Streaming responses

Verify [[src/haumea/responses.py#StreamingResponse]] sends async-generator, sync-generator and list chunks in order (`str` encoded as UTF-8), with `more_body: True` on each and a final empty body.

No `content-length` is sent even when the caller passes `Content-Length`. `HEAD` sends headers and an empty final body without calling `__iter__`/`__aiter__`, for `SSEResponse` too.

## Streaming disconnect

Verify an `http.disconnect` from `receive` during an endless stream cancels the producer and closes both async and sync generators (their `finally` runs), with chunks still sent in order and no final `more_body: False` message.

## SSE framing

Verify [[src/haumea/responses.py#SSEResponse]] headers and framing for `ServerSentEvent` fields, multi-line data, and `str`, dict and `msgspec.Struct` events.

Also covers raw `None` as `data: null` versus data-less `ServerSentEvent`s, `: ping` lines while the source is idle, sync iterables, and `ValueError` for newlines in `event`/`id`.

## Streaming fast producer disconnect

Verify a disconnect that becomes ready after the first chunk stops a 10,000-chunk producer early even when neither the producer nor `send` suspends, and the generator is closed.

## Streaming receive errors

Verify an exception raised by `receive()` while streaming closes the producer and propagates out of the response instead of returning normally.

## Streaming cancellation during cleanup

Verify that cancelling the request while a disconnect is closing the producer waits for the producer's `finally` to finish, then raises `CancelledError` rather than `RuntimeError` from `aclose()`.

## SSE headers and ping validation

Verify [[src/haumea/responses.py#SSEResponse]] always sends `content-type: text/event-stream` and `cache-control: no-cache` over caller headers, and rejects zero, negative and non-finite `ping` with `ValueError`.

## Request cookies

Verify [[src/haumea/requests.py#Request#cookies]] for a missing or empty header, several cookies, stray whitespace and `;`, `=` in values, quoted and escaped values, name-less pairs, several `cookie` headers, and first-wins repeats.

## Cookie parameters

Verify `Cookie()` binding in handlers and dependencies with defaults, `alias`, `int` coercion, `422` `loc: ["cookie", name]` errors, the `haumea` export, and OpenAPI `in: cookie` parameters.

## Set-cookie headers

Verify through the ASGI boundary that [[src/haumea/responses.py#Response#set_cookie]] and `delete_cookie` send secure defaults and every attribute, each `set-cookie` as its own raw header tuple after the single-valued headers.

Also covers `ValueError` for a bad name or `samesite`, and `TypeError` for `delete_cookie` keywords beyond the spec's `path` and `domain`.

## Set-cookie on every response type

Verify `JSONResponse`, `StreamingResponse` and `SSEResponse` each send two separate `set-cookie` headers through the ASGI boundary.

## Set-cookie encoding

Verify `set_cookie` rejects non-Latin-1 values, non-ASCII `path`/`domain`/`expires`, and `;` or newlines in attributes with `ValueError`, and that an app doing so sends a complete `500` rather than failing while sending headers.

## Cookie round trip

Verify cookies set through an app, including empty values and ones with spaces, `=`, quotes, backslashes, `;`, `,` and Latin-1 text, come back intact through `Cookie()` and `request.cookies` when sent back.

## Platform metadata from Vercel headers

Verify `request.id`, `request.ip` and `request.geo` read `x-vercel-id`, `x-real-ip` and every `x-vercel-ip-*` field, percent-decoding `city` (including non-ASCII), and that `Geo` is exported from `haumea` and `haumea.requests`.

## Platform client IP sources

Verify [[src/haumea/requests.py#Request#ip]] prefers `x-real-ip`, then the first trimmed `x-forwarded-for` entry over the ASGI client, then the client host, and is `None` when `client` is missing or empty.

## Platform metadata dev fallbacks

Verify that without Vercel headers `request.id` is a UUID4 hex string, stable on one request and distinct across requests, `ip` is the client host, and `geo` is an all-`None` `Geo`.

## Platform metadata through create_app

Verify a `route.py` handler taking `Request` sees Vercel header metadata through `create_app`, and without the headers gets distinct generated ids, the ASGI client host and an all-`None` geo.

## Platform client IP from repeated forwarded headers

Verify that with two raw `x-forwarded-for` header tuples [[src/haumea/requests.py#Request#ip]] returns the first entry of the first line, while `request.headers` still keeps the last value.

## Platform request id under concurrent reads

Verify that threads released together by a barrier all read the same generated `request.id` from one `Request`, repeated over many requests to expose races.

## Hidden routes leave the schema

Verify `include_in_schema = False` drops a route's operations and its exclusive component schemas from dev `/openapi.json` and the build's static `openapi.json`, keeps shared schemas, and still serves the route in dev and as a deployed function.

## Schema with only hidden routes

Verify that when every route is hidden, `openapi.json` has empty `paths` and no `components`, while the hidden route still answers requests.

## Invalid include_in_schema

Verify a non-`bool` `include_in_schema` (a string, `0` or `None`) fails app creation with a `TypeError` naming the setting, instead of being read as truthy or falsy.

## Explicit security and responses

Verify a protected operation references one `HTTPBearer` scheme in `security` with no credential header parameter, a public sibling has no `security`, and only `raises`-declared `401`/`403` join `200`/`422`.

The same project checks that a dependency raising if called still documents its scheme, that a hidden protected route leaks no scheme into `components.securitySchemes`, that the build's static `openapi.json` matches dev, and that the protected route enforces its token.

## HTTP bearer credentials

Verify `HTTPBearer` returns the stripped token for a case-insensitive `Bearer` credential and answers missing, empty, malformed or non-bearer credentials with `401` and `WWW-Authenticate: Bearer`.

## HTTP bearer scheme names

Verify `HTTPBearer` names outside the OpenAPI component-name pattern (empty, spaces, `/`, `:`, non-ASCII) raise `ValueError` at declaration, while valid names become `components.securitySchemes` keys.

## Declared response precedence

Verify through the generated schema that the outermost stacked `raises` wins, a handler beats its dependencies, and a direct dependency beats a nested one in an earlier parameter.

## Declared responses must be errors

Verify `raises` rejects success, redirect-boundary and out-of-range statuses with `ValueError`, so only error responses can be declared.

## Declared response statuses must be integers

Verify `raises` rejects non-`int` statuses (`401.5`, `"401"`, `True`, `None`) with `TypeError`, so a response key such as `"401.5"` never reaches the schema.

## Lazy values

Tests for [[src/haumea/params.py#lazy]]: one creation per process for sync and async factories, retry after failure, and use as a dependency.

### Sync lazy creates once

Verify 16 threads released by a barrier get the same value from one `factory` call, including `None` and falsy values, and later calls reuse it.

### Async lazy creates once

Verify `asyncio.gather` callers share one creation, the callable is a coroutine function, and the value is reused from later event loops without calling `factory` again.

### Failed creation is not cached

Verify a sync `factory` that raises is retried on the next call, and that async callers sharing a failed attempt all get its error while the next call starts a fresh attempt.

### Cancelled waiter keeps creation

Verify cancelling one caller awaiting an async creation does not cancel it: another caller still gets the value and `factory` runs once.

### Lazy as a dependency

Verify sync and async `lazy` callables work with `Depends` (as a default and in `Annotated`) in sync and async handlers through `create_app`, creating once across requests and adding no OpenAPI parameters from the factory's own defaults.

### Lazy exports

Verify `lazy` is exported from `haumea` and `haumea.params`, and that a sync `lazy` callable has an empty signature and is not a coroutine function.

### Async lazy across event loops

Verify two threads each running `asyncio.run` share one in-flight async creation started on the other thread's loop, get the same value, and call `factory` once.

The second loop sets an event via `call_soon` just before awaiting, so the creation is only released once that caller is suspended on it.

### Async callable instance factory

Verify `Depends(lazy(Pool()))` where `Pool` has `async def __call__` awaits the factory through `create_app`, returning its value rather than a coroutine, created once across requests.

### Async factory raising synchronously

Verify a `markcoroutinefunction` factory that raises before returning an awaitable fails that call, leaves no unresolved attempt behind, and lets the next call create the value.

### Creation outlives its initiator

Verify that after the initiating caller is cancelled and dropped and `gc.collect()` runs, the creation task survives (its awaited future is only weakly referenced by the test), another caller gets the value, and asyncio logs no errors.

### Abandoned waiter after failure

Verify that when a cancelled caller's creation later fails, an active caller still gets the error and asyncio logs no unretrieved-exception error after `gc.collect()`.
