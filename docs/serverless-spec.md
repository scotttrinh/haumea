# Haumea Serverless Spec

> **Status:** Agreed design, not yet implemented. The build order is in [serverless-plan.md](serverless-plan.md).
> The wider design space and longer-term ideas are in [serverless-design.md](serverless-design.md).
> As each part ships, its current-state description moves into `lat.md/`.

This spec covers four areas, in priority order:

1. Deploying a Haumea backend beside a frontend (Vercel Services, `base_path`, uv workspaces).
2. Per-function dependency tree-shaking.
3. Request and response additions (streaming, cookies, platform metadata, `after()`, `lazy()`).
4. Directory-scoped `middleware.py` and `error.py`.

Every feature still has to pass the litmus test in [serverless-design.md](serverless-design.md#guiding-principles). It should compile into Build Output or be scoped to the routes that use it, it should not add cold starts, and `haumea dev` should behave like the deployed build.

---

## 1. Project configuration

Project-level settings live in `[tool.haumea]` in the project's `pyproject.toml`.

- **Read without importing user code.** The build parses the file with `tomllib` and decodes it into a `msgspec.Struct`. Unknown keys are an error.
- **Route-level settings stay in `route.py`.** They remain module-level variables, as `max_duration`, `memory` and `regions` are today.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `base_path` | `str` | `""` | URL prefix for every route, doc and static file (§2). |
| `tree_shake` | `bool` | `true` | Bundle only the distributions each function imports (§5). |
| `include` | `list[str]` | `[]` | Distribution names bundled into every function (§5.5). |

The new route-level variables are `include: list[str]` (§5.5) and `include_in_schema: bool` (§6.4).

---

## 2. Base path

`base_path` mounts the whole project under a URL prefix. A backend can then sit behind a service rewrite such as `/api/(.*)` beside a frontend at `/`.

### 2.1 Platform behavior this relies on

- **Services keep the original path.** A request to `/api/users` reaches the service as `/api/users`. This is documented under [Services routing](https://vercel.com/docs/services/routing).
- **Each service is a complete Build Output tree.** It lives at `.vercel/output/services/<name>/` with its own `config.json`, `functions/` and `static/`.
- **The runtime can strip the prefix.** When `VERCEL_SERVICE_ROUTE_PREFIX_STRIP` is set, `vercel-runtime` (`vercel_runtime/routing.py`) strips `VERCEL_SERVICE_ROUTE_PREFIX` from `scope["path"]` and moves it into `scope["root_path"]`.

### 2.2 Semantics

- **Normalization.** The prefix always starts with `/` and never ends with one. `""` and `"/"` both mean no prefix.
- **Every public URL is prefixed.** With `base_path = "/api"`:

  | Source | URL |
  | --- | --- |
  | `app/route.py` | `/api` |
  | `app/users/route.py` | `/api/users` |
  | `public/logo.svg` | `/api/logo.svg` |
  | Docs | `/api/docs` and `/api/openapi.json` |

  The `app/` tree doesn't contain the prefix, so moving a project between standalone and service deployments changes only config.
- **Runtime matching.** `Haumea` matches routes against the full public path. If `scope["root_path"]` is set and `scope["path"]` does not start with it, the full path is `root_path + path`. `request.path` returns the full path in every mode.
- **OpenAPI.** Paths in `openapi.json` include the prefix, and Swagger UI loads `<base_path>/openapi.json`.
- **Dev parity.** `haumea dev` serves the same prefixed URLs. Requests outside the prefix return 404.

### 2.3 Build output

- **Function paths.** They are prefixed: `app/users/route.py` becomes `functions/api/users.func`, and `app/route.py` becomes `functions/api.func`. Only an unprefixed root route is named `index`.
- **Static files.** `public/` is copied to `static/<base_path>/`. Docs are written to `static/<base_path>/docs.html` and `static/<base_path>/openapi.json`.
- **Rewrites.** Each dynamic route pattern and the `<base_path>/docs` rewrite include the prefix. The `{"handle": "filesystem"}` entry stays first.

---

## 3. Build pipeline

`build()` is split into three stages:

1. **Discover:** find routes and read config.
2. **Plan:** produce a `BuildPlan`.
3. **Emit:** write the output tree from the plan.

The split keeps tree-shaking, monorepo support and future function kinds (static routes, queue consumers) from piling into `_write_function`.

### 3.1 Plan types

- **`BuildPlan`** holds project paths (project dir, workspace root, app dir, public dir, output dir), the parsed config, the function specs, static file mappings and the `config.json` routes.
- **`FunctionSpec`** holds one function's path, its route, its entrypoint source, its `.vc-config.json` content, its trace roots (§5.2) and its distributions.

### 3.2 CLI changes

- **`--output <dir>`.** Defaults to `.vercel/output` in the project dir.
- **Locked lockfile.** The build runs `uv export` with `--locked`, so a stale `uv.lock` fails the build instead of being re-resolved.
- **Deterministic output.** Build output contains no timestamps, and files and JSON keys are written in stable order. This lets Turborepo and remote caches key on it.

---

## 4. uv workspaces (monorepos)

Vercel builds each service from its `root`, so `haumea build` runs inside the service directory. The uv workspace root may be above it.

- **Workspace discovery.** The workspace root is the nearest directory, starting at the project dir and walking up, whose `pyproject.toml` declares `[tool.uv.workspace]` and which contains `uv.lock`. If none is found, it is the project dir.
- **Scoped export.** The build runs `uv export --package <project name> --locked --no-dev --no-emit-project --no-editable --no-hashes` from the workspace root. This exports only this service's transitive dependencies.
- **Install.** The build runs `uv pip install --target <store>` from the workspace root, so relative requirement paths resolve.
- **Workspace siblings.** With `--no-editable`, a sibling package such as `packages/core` is built and installed as a normal distribution. Tree-shaking (§5) then treats it like any other dependency.
- **App source.** The service's own project files are still copied as source, as today.

---

## 5. Dependency tree-shaking

Each function bundles only the distributions it can import. First-party project source is still copied whole into every function, because it is usually small and the tracer reads it anyway.

### 5.1 Store and index

- **Store.** All dependencies are installed once into a store directory, which is today's shared `_vendor`.
- **Index.** The build reads every `*.dist-info` and records:
  - the normalized name;
  - the files listed in `RECORD`;
  - its top-level import names, from `RECORD` paths and `top_level.txt`;
  - the names in its `Requires-Dist`.

### 5.2 Trace roots

Each function has these trace roots:

- its `route.py`;
- its ancestor `middleware.py` and `error.py` files (§7);
- the generated entrypoint, which imports `haumea`.

### 5.3 Static import tracing

The tracer parses each root with `ast` and resolves imports as follows.

| What it sees | What happens |
| --- | --- |
| `import` / `from … import` anywhere in the module, including function bodies | Resolved and followed |
| Anything inside an `if TYPE_CHECKING:` block | Skipped |
| `importlib.import_module("…")` / `__import__("…")` with a string literal | Resolved and followed |
| Relative import | Resolved against the importing module |
| Module that resolves to a project file | Traced recursively |
| Name in `sys.stdlib_module_names` | Ignored |
| Any other top-level name | Mapped to a distribution through the index |
| Name that isn't in the index | Ignored |

Imports inside third-party distributions are not traced; §5.4 covers them.

### 5.4 Closure

The closure starts from the traced distributions and adds every `Requires-Dist` target that is installed in the store, ignoring environment markers and extras.

The store is already the resolved set for the target Python and platform. Ignoring markers can therefore over-include something that is installed for another reason, but it can never drop something that is needed.

### 5.5 Always included

These distributions and their closures are always bundled:

- `haumea`;
- `vercel-runtime`;
- `vercel-functions` (§6.5);
- project `include`;
- route `include`.

`include` is the escape hatch for anything loaded by computed name, such as string-configured database drivers or entry-point plugins.

### 5.6 Emit

- **Linking.** For each function, the build hard-links into `_vendor/` every file in its distributions' `RECORD` that lies inside the store, plus each distribution's `.dist-info` directory.
- **Layout.** The `_vendor/` layout and the trampoline don't change.
- **Opting out.** `tree_shake = false` links the whole store, which is today's behavior.

### 5.7 Build-time import check

The build checks that each function's entrypoint actually imports.

- **When it runs.** Only when the vendored target matches the host: same OS and architecture, and the same Python minor version as `--python`. Vercel's build machines are Linux x86_64, so deployed builds always run it.
- **How it runs.** `python -I` in a fresh subprocess, with only the function directory and its `_vendor/` on `sys.path`.
- **Failure.** An `ImportError` fails the build. The error names the function and the missing module, and suggests `include`.
- **Otherwise.** The build prints that the check was skipped.

The build already imports route modules (for `.vc-config.json` and the docs), so this adds no new kind of side effect.

### 5.8 Reporting

- **Default output.** `haumea build` prints each function's uncompressed bundle size and distribution count.
- **Size limits.** Vercel's maximum uncompressed function size is 500 MB.
  - Above 250 MB, the build warns and names the largest distributions.
  - Above 500 MB, the build fails, because the deploy would be rejected anyway.
  - Both values are constants in `build.py`.
- **`haumea build --explain <function>`.** Lists each included distribution with the reason it was included (traced from a file, required by another distribution, or listed in `include`). It also lists the excluded distributions.

### 5.9 Haumea's own dependencies

`uvicorn` moves from Haumea's dependencies to a `haumea[dev]` extra, because only `haumea dev` uses it. `vercel-runtime` serves ASGI itself and has no dependencies. Apps add `haumea[dev]` to their dev dependency group.

---

## 6. Request and response additions

### 6.1 Streaming and SSE

- **`Response` signature.** `Response.__call__` takes `receive` as well as `send`.
- **`StreamingResponse(content, status_code=200, headers=None, media_type=None)`.**
  - `content` is an async iterable or a sync iterable of `bytes` or `str`; sync iterables are advanced in a worker thread.
  - It sends no `content-length` and sets `more_body: True` on each chunk.
  - It listens for `http.disconnect` while streaming. On a disconnect it cancels the producer and closes the iterator (`aclose()` / `close()`).
- **`SSEResponse(events, ping=None)`.**
  - Sets `content-type: text/event-stream` and `cache-control: no-cache`.
  - Each event may be a `ServerSentEvent` (`data`, `event`, `id`, `retry`), a `str`, or a JSON-encodable value. `msgspec` encodes `Struct`s and other values.
  - `ping` sends a comment line at that interval of seconds.

### 6.2 Cookies

- **Reading.** `request.cookies` returns a `dict[str, str]` parsed from the `cookie` header. Handlers can also declare `Cookie(...)` parameters, alongside `Query` and `Header`.
- **Writing.** `Response.set_cookie(key, value, *, max_age=None, expires=None, path="/", domain=None, secure=True, httponly=True, samesite="lax")` and `Response.delete_cookie(key, *, path="/", domain=None)`.
  - Defaults are secure; browsers accept `Secure` cookies on `localhost`, so dev still works.
- **Header storage.** Response headers gain a multi-value path so that several `set-cookie` headers can be sent.

### 6.3 Platform metadata

| Property | Source | Dev fallback |
| --- | --- | --- |
| `request.id` | `x-vercel-id` | A generated UUID4 hex string per request |
| `request.ip` | `x-real-ip`, else the first `x-forwarded-for` entry | `scope["client"]` host |
| `request.geo` | `Geo` struct (`country`, `region`, `city` URL-decoded, `latitude`, `longitude`, `postal_code`) from `x-vercel-ip-*` | All fields `None` |

On Vercel these headers are set at the edge. In dev they come from the client, so they must not be trusted for authorization.

### 6.4 `include_in_schema`

A module-level `include_in_schema = False` in `route.py` leaves the route's operations out of `openapi.json`. Component schemas that only that route uses are left out too.

### 6.5 `after()`

`after(fn, *args, **kwargs)` schedules work to run after the response is sent.

- **Arguments.** `fn` may be sync or async. Sync callables are wrapped with `asyncio.to_thread`.
- **When it can be called.** Only while a request is being handled; otherwise it raises `RuntimeError`.
- **Errors.** Exceptions in the work are logged and never reach the client.
- **Backends.** `Haumea(after_backend=…)` chooses how the work runs. Only the generated entrypoint opts into the platform backend, so tests and dev always run the work locally.

  | Backend | Used by | Behavior |
  | --- | --- | --- |
  | `"local"` | `create_app`, `create_route_app` (dev and tests) | Queues work in a per-request context variable and runs it after the final `send` |
  | `"wait_until"` | The build-generated entrypoint | Passes each awaitable to `vercel.functions.wait_until` |

- **Lifetime.** With `wait_until`, the work is bounded by the function's `max_duration` and is not retried; `after()` is not a durable queue.
- **Dependency.** `vercel.functions` comes from the `vercel-functions` package being split out of the `vercel` SDK. That package is expected before Haumea's first internal testing release, so `after()` assumes it exists. The build installs it alongside `vercel-runtime` and always bundles it (§5.5).

### 6.6 `lazy()`

`lazy(factory)` returns a zero-argument callable that creates its value on first call and keeps it for the life of the process. It is meant for clients and connection pools in place of startup hooks.

- **Thread safety.** Creation runs under a lock.
- **Async factories.** The first call's task is cached and awaited by later callers.
- **Use as a dependency.** The callable works directly with `Depends(...)`.

---

## 7. Segment files: `middleware.py` and `error.py`

Both files apply to the routes in their directory and below. Each route composes only the files on its own ancestor path.

### 7.1 Discovery

- **Segments.** `Route` gains `segments`: the directories from `app/` down to the route's own directory, inclusive. Route groups count as segments.
- **Loading.** Segment files load once per process and are shared by every route under them.

### 7.2 `middleware.py`

The file exports `async def middleware(request: Request, call_next) -> Response`.

- **Order.** The root segment's middleware runs outermost and the route directory's innermost.
- **Short-circuiting.** Middleware may return a response without calling `call_next`.

### 7.3 `error.py`

The file exports `handle_error(request, exc) -> Response`, which may be sync or async.

- **Nearest wins.** The closest `error.py` handles the exception. Re-raising passes it to the next ancestor.
- **Default.** Past the last ancestor, today's mapping applies: `HTTPException` becomes JSON, `RequestValidationError` becomes a JSON 422, and anything else is logged and becomes a plain-text 500.

### 7.4 Composition order

Each request runs through these layers, outermost first:

1. middleware chain (root to leaf);
2. error boundary;
3. dependencies and handler.

- **Middleware sees a response.** Handler and dependency errors have already become a `Response` by the time middleware inspects it.
- **Middleware errors.** An exception raised by middleware itself skips `error.py` and gets the default mapping.
- **Unmatched paths.** The layers run only after a path matches. A path with no route never reaches a deployed function, so in dev it returns 404 without running any segment file.
- **405s.** A 405 runs through the matched route's chain, as it does in production.
- **Parity.** `create_app` and `create_route_app` build the same per-route chain.

---

## 8. Out of scope

These items stay in [serverless-design.md](serverless-design.md):

- **Edge rules (Tier 1).** In services mode these compile into the service's own `config.json` `routes`.
- **Crons.** `vercel-runtime` ships `crons.py` and `schedules.py`; read them before designing this.
- **`static` / `revalidate` routes.**
- **HTMX support** (form decoding, `request.hx`, `layout.py`).
- **Queues and Workflows.**
- **`proxy.py` (Tier 3).** Dropped: services don't support middleware, and it conflicts with one function per route.
- **Route coalescing.** Dropped for the same reason.

---

## 9. Open questions

1. **Service build handoff.** How does a service with a custom `buildCommand` hand Vercel a prebuilt Build Output tree, and what does `vercel dev` expect from its `devCommand`? Is `VERCEL_SERVICE_ROUTE_PREFIX_STRIP` ever set under the `services` model? The spike in Phase 1 answers these.
2. **`uv export --package` in a workspace.** Does `--no-emit-project` combined with `--package` drop exactly the selected package? A workspace fixture in Phase 1 checks this.
3. **`vercel-functions` import path.** Does the split package keep the `vercel.functions` import path? Check when it is published.
