# Haumea Serverless Spec

> **Status:** Agreed design, not yet implemented. The build order is in [serverless-plan.md](serverless-plan.md).
> The wider design space and longer-term ideas are in [serverless-design.md](serverless-design.md).
> As each part ships, its current-state description moves into `lat.md/`.

This spec covers four areas, in priority order:

1. Deploying a Haumea backend beside a frontend (Vercel Services, `base_path`, uv workspaces).
2. Per-function dependency tree-shaking.
3. Request and response additions (streaming, cookies, platform metadata, `after()`, `lazy()`).
4. Directory-scoped middleware, error handling, and typed dependencies.

Every feature still has to pass the litmus test in [serverless-design.md](serverless-design.md#guiding-principles). It should compile into Build Output or be scoped to the routes that use it, it should not add cold starts, and `haumea dev` should behave like the deployed build.

## Public routing contract

The filesystem declares each HTTP endpoint. For example, `app/items/[id]/route.py` owns `/items/{id}` and exports the methods it serves. Its `GET` and `PUT` share one deployed function. Each handler's typed signature declares its inputs and dependencies. Dev, build, and OpenAPI derive their operations from those same files, subject to schema visibility (§6.4).

App construction and route registration belong to the framework. The target public API excludes manual registration, router mounting, and registration-order overrides. Today the package exports `Haumea` and exposes `add_route`; bringing those exports into line with this contract is an implementation follow-up. A test can exercise discovery and dispatch using a fixture containing ordinary route files.

Common uses of a [FastAPI router](https://fastapi.tiangolo.com/tutorial/bigger-applications/) have the following counterparts:

- **Prefixes and organization.** Nested directories compose URL segments, `(group)` directories organize related routes without adding a segment, and `base_path` mounts the whole service (§2).
- **Reuse at several URLs.** Two route files can export the same imported handler or call a shared Python function. For example, keeping both a v1 and a v2 URL leaves two discoverable endpoints, each with its own function configuration.
- **Shared request behavior.** A handler can import a typed dependency such as `current_user` and request its value with `Depends`. The planned segment files (§7) wrap requests or handle exceptions across a subtree, including a URL-transparent group. A segment can also require a typed dependency for all its handlers (§7.5). Explicit OpenAPI security and response metadata have a separate contract (§6.7).

Each endpoint must work on its first invocation in a fresh instance. A warm instance can reuse a client (§6.6). Correctness must survive instance replacement and concurrent requests on different instances. Route discovery and schema generation must work without starting clients, opening connections, or running application startup hooks.

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

These were checked by the 1.1 spike (Vercel CLI 65.12.1, `@vercel/static-build` 27.0.0, `vercel-runtime` 0.26.0). A Haumea backend was deployed as a service beside a Next.js app, built with `vercel build`, run with `vercel dev --local`, and deployed with `vercel deploy`.

#### Spike fixture

The fixture is not the proposed layout in "Service configuration" below; it predates `base_path`.

- **Vercel project.** Deployed to `haumea-spike` (`scotttrinhs-projects`), retained intentionally as a reusable test project for the rest of this implementation effort.
- **Rewrite.** `{"source": "/api/(.*)", "destination": {"service": "api"}}`, then `/(.*)` to `web`. Neither rewrite sets `destination.path`, and the service declares no `routes` of its own, so no `request.path` transform applies.
- **Backend.** `examples/hello` with its routes moved under `app/api/` by hand (`app/api/hello`, `app/api/items/[item_id]`, `app/api/debug`), plus unprefixed `app/route.py`, `app/debug/route.py` and `public/hello.txt`. The debug routes echo `scope["path"]`, `scope["raw_path"]`, `scope["root_path"]` and every `VERCEL_SERVICE_*` variable.
- **Haumea source.** Resolved from a local copy, `haumea = { path = "vendor/haumea" }`. There is no `.python-version`.
- **Service.** `framework: "vite"`, `buildCommand: "uv run --frozen haumea build"`, `devCommand: "uv run --frozen haumea dev --port $PORT"`.

Observed requests (same status in `vercel dev` and in the deployment unless noted):

| Request | Reached | Result |
| --- | --- | --- |
| `/api/debug` | `api`, `app/api/debug` | 200; `path` and `raw_path` are `/api/debug`, `root_path` is `""`, no `VERCEL_SERVICE_*` variables |
| `/api/hello?name=x`, `/api/items/42` | `api` | 200 |
| `/api` | `web` | 404 from Next.js; `(.*)` requires the slash |
| `/debug` | `web` | 404 from Next.js; the unprefixed `app/debug` is unreachable |
| `/hello.txt` | `web` | 404 from Next.js |
| `/api/docs`, `/api/hello.txt` | `api` | 404; the unprefixed `static/docs.html` and `static/hello.txt` don't match |
| `/api/nope` | `api` | 404: Haumea's JSON body in dev, Vercel's `NOT_FOUND` page when deployed |

#### Conclusions

The path conclusions below hold for a top-level rewrite to a service **without `destination.path` and without a `request.path` transform** in the service's `routes`. [Services routing](https://vercel.com/docs/services/routing) documents that `destination.path` changes which service route is looked up, and that a service `request.path` transform changes the path the code sees. Haumea's service setup uses neither.

- **Services keep the original path.** A request to `/api/users` reaches the service as `/api/users`, with an empty `scope["root_path"]`. Route lookup inside the service also uses the original path.
- **The prefix is never stripped under `services`.** `vercel-runtime` (`vercel_runtime/routing.py`) only strips `VERCEL_SERVICE_ROUTE_PREFIX` when `VERCEL_SERVICE_ROUTE_PREFIX_STRIP` is set. Under `services` that variable is never set:
  - The CLI only injects it for the legacy `experimentalServices` schema with a `routePrefix`.
  - `services` entries reject both `routePrefix` and `mount`.
  - Neither variable appeared in `vercel dev` or the deployed function.

  Haumea must therefore match the full prefixed path itself. §2.2 still defines `root_path` handling in case a future platform version strips the prefix.
- **Static files and docs must be prefixed too.** The service's own route lookup sees `/api/docs`, so unprefixed static files never match.
- **The rewrite must also match the bare prefix.** `"/api/(.*)"` compiles to `^/api(?:/(.*))$`, so `/api` itself falls through to the next rewrite (the frontend). Use `"/api/:path*"`, which compiles to `^/api(?:/…)?$`, when `app/route.py` should serve `/api`. This is read from the compiled route, not deployed.
- **Unmatched paths differ between dev and deployment.** Routing into a service is final. When deployed, a path with no function and no rewrite in the service's `config.json` gets Vercel's `NOT_FOUND` page. In `vercel dev` the proxy forwards it to `haumea dev`, which returns Haumea's JSON 404. Both are 404s; only the body differs.

#### Service configuration

With `base_path` (§2.2), a Haumea service is declared like this. It differs from the spike fixture in two ways: the rewrite is `/api/:path*` so that bare `/api` reaches the backend, and routes live at `app/` with `base_path = "/api"` instead of under `app/api/`.

```json
{
  "services": {
    "web": { "root": "frontend/", "framework": "nextjs" },
    "api": {
      "root": "backend/",
      "framework": "vite",
      "buildCommand": "uv run --frozen haumea build",
      "devCommand": "uv run --frozen haumea dev --port $PORT"
    }
  },
  "rewrites": [
    { "source": "/api/:path*", "destination": { "service": "api" } },
    { "source": "/(.*)", "destination": { "service": "web" } }
  ]
}
```

- **The service must build with `@vercel/static-build`.** That is the only builder that runs `buildCommand` and then consumes a Build Output v3 tree.
  - Without `framework`, a `pyproject.toml` is auto-detected as `python`, and the build fails because no `entrypoint` is set.
  - `framework: "python"` uses `@vercel/python`. That builder runs `buildCommand` only as a pre-step and still requires and builds its own entrypoint.
  - `framework` must be a non-empty known slug, so `null`, `""` and `"other"` are rejected.
  - Any frontend slug whose framework has no `useRuntime`, such as `"vite"`, maps to `@vercel/static-build`. The slug is otherwise unused because `buildCommand` and `devCommand` override its defaults. `vercel dev` labels the service "Vite".
- **Build output location.** After `buildCommand`, `@vercel/static-build` looks for `<service root>/.vercel/output/config.json`. If it is there, the CLI moves that tree into `.vercel/output/services/<name>/`. `.vercelignore` is applied to `static/`.
  - Haumea's `config.json` routes are kept as written, and the CLI adds an empty `crons`.
  - Function `.vc-config.json` files are not modified; in particular, no service environment variables are injected.
  - The top-level `config.json` gets the service rewrites as `{"destination": {"service": "api", "type": "service"}}` routes.
  - The lookup path is fixed, and the service's `outputDirectory` setting does not change it.
- **Dev.** `vercel dev` runs `devCommand` through a shell in the service root, with `$PORT` set to a free port, and proxies matching requests to it with the original path. `devCommand` is required: `@vercel/static-build` refuses to serve a Build Output v3 tree in dev, and the framework's own default dev command (`vite`) would run otherwise.
- **Deployed build.** Vercel's build image provides `uv`, and `uv run --frozen haumea build` ran unchanged. With no `.python-version` and `requires-python = ">=3.12"`, `uv` chose CPython 3.14 for the build process. Dependencies were still vendored for 3.12 (`uv pip install --python-version`), and the functions ran on the `python3.12` runtime declared in `.vc-config.json`. Build-time route imports therefore ran on a different minor version than the functions; §5.7 covers how the import check gets a matching interpreter.

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
- **Runtime matching.** `Haumea` matches routes against the full public path, and `request.path` returns that path in every mode.
- **Scope convention.** Haumea reads the scope the way `vercel-runtime`'s prefix strip writes it (`apply_service_route_prefix_to_asgi_scope`): `scope["path"]` is relative to `scope["root_path"]`, as `PATH_INFO` is to `SCRIPT_NAME` in WSGI. The full path is `root_path + path`, with no `startswith` check, because a stripped path can itself begin with the prefix. The one exception is the prefix root: when `root_path` is non-empty and `path` is `/`, the full path is `root_path` alone:

  | `root_path` | `path` | Full path |
  | --- | --- | --- |
  | `""` | `/` | `/` |
  | `""` | `/api/users` | `/api/users` (services today) |
  | `/api` | `/` | `/api` |
  | `/api` | `/users` | `/api/users` |
  | `/api` | `/apiary` | `/api/apiary` |
  | `/api` | `/api/users` | `/api/api/users` |

  The other convention, where `path` already includes `root_path` (as `uvicorn --root-path` produces), is not supported. `haumea dev` never sets `root_path`, and no Vercel path sets both.
- **Trailing slashes.** Haumea route patterns never end in `/` and match exactly, so `/api/users/` does not match a literal route like `app/users/route.py` (while a catch-all such as `app/[...path]/route.py` matches it and captures `users/`). The prefix root follows the same literal rule: `/api` matches `app/route.py` and `/api/` does not. The exception above exists because `vercel-runtime` 0.26.0's `strip_service_route_prefix` maps both `/api` and `/api/` to `root_path="/api"`, `path="/"`. Plain concatenation would turn both into `/api/` and miss the root route. With the exception, both become `/api`. So under a prefix strip, `/api/` also reaches `app/route.py`, while today (no strip, §2.1) it gets a 404. The strip loses the difference between the two, so Haumea can't tell them apart; deeper paths keep their slash (`/api/users/` strips to `/users/`) and behave identically in both modes (unmatched by a literal route like `app/users/route.py`, while a catch-all like `app/[...path]/route.py` still matches and captures `users/`).
- **OpenAPI.** Paths in `openapi.json` include the prefix, and Swagger UI loads `<base_path>/openapi.json`.
- **Dev parity.** `haumea dev` serves the same prefixed URLs. Unmatched paths are a known difference in the response body, not the status:
  - In dev, any unmatched path, inside or outside the prefix, gets Haumea's JSON 404 (`{"detail": "Not Found"}`). Under `vercel dev`, paths outside the prefix go to the next rewrite and never reach Haumea.
  - When deployed, an unmatched path inside the prefix gets Vercel's `NOT_FOUND` page (§2.1), because no function runs. Clients should rely on the status, not the 404 body.

### 2.3 Build output

- **Location.** In services mode, `haumea build` still writes a complete, self-contained tree to `.vercel/output` in the service root, and the Vercel CLI moves it into `.vercel/output/services/<name>/` (§2.1). Haumea never writes into `services/` itself.
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

- **`--output <dir>`.** Defaults to `.vercel/output` in the project dir. Service builds must keep the default, because `@vercel/static-build` only looks there (§2.1). A custom directory is for tests and for standalone workflows that build once and deploy later, such as CI building into an artifact directory.
  - `vercel deploy --prebuilt` only reads `.vercel/output` in the linked project directory and takes no output-path option. A standalone workflow must stage the custom tree there first: link the project (`vercel link` or `vercel pull`, which writes `.vercel/project.json`), replace `.vercel/output` with the custom directory (`rm -rf .vercel/output && cp -R <dir> .vercel/output`), then run `vercel deploy --prebuilt`. `<dir>` must lie outside `.vercel/output` (for example `dist/vercel-output`), or the `rm -rf` deletes the source before `cp` reads it.
  - `cp -R` breaks the hard links between functions, so the staged copy uses more disk. The deployed result is the same.
  - Haumea doesn't stage or deploy; the workflow owns those steps.
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
- its ancestor segment files (§7), including the declarations of inherited dependencies;
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

- **Interpreter.** The check runs under the target interpreter, not the one running `haumea build`. The build resolves it with `uv python find <--python>`. `sys.executable` is used only if it already has the target minor version. The spike shows why: `uv run` chose CPython 3.14 for the build while the functions target 3.12 (§2.1).
- **When it runs.** When the host OS and architecture match `--platform` (or `--platform native` is used) and a target-minor interpreter is found. Otherwise the build prints that the check was skipped and why.
- **On Vercel.** Build machines are Linux x86_64, which matches the default platform. Whether a 3.12 interpreter is already in the build image is unverified, so projects pin it: a `.python-version` with the target minor (`3.12`) in the service root makes `uv run` install that interpreter and run the build on it. `examples/fullstack` (plan step 1.6) ships this pin. Without the pin, the check runs only if `uv python find` locates a 3.12 interpreter, and otherwise it is skipped with the message above.
- **How it runs.** `<interpreter> -I` in a fresh subprocess, with only the function directory and its `_vendor/` on `sys.path`.
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
- **Backends.** The framework selects the backend when it constructs the app. Only the generated entrypoint opts into the platform backend, so tests and dev always run the work locally.

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

### 6.7 Explicit OpenAPI security and responses

The current generator walks the declared request fields of a handler and its dependencies. For example, the hello project's `PUT` operation currently emits an ordinary `authorization` header and documents `200` and `422`, even though its dependency can raise `401`.

Security documentation needs an explicit declaration of the credential scheme. For a declared HTTP bearer scheme, emit one entry in `components.securitySchemes` and reference it in each affected operation's `security` list. Represent its credential there only. OpenAPI reserves the `Authorization` header for this purpose. Document `401` and `403` when explicitly declared. A bearer scheme describes how to present credentials. Application code still decides whether the caller may perform the operation.

This feature applies to ordinary handler dependencies independently of inheritance. Schema generation reads declarations without executing checks or inspecting function bodies for exceptions. The Python spelling of these declarations remains open (§9).

---

## 7. Segment files

Segment files apply to the routes in their directory and below. Each route composes only the files on its own ancestor path.

### 7.1 Discovery

- **Segments.** `Route` gains `segments`: the directories from `app/` down to the route's own directory, inclusive. Route groups count as segments.
- **Loading.** Each deployed function loads only its ancestor segment files, once per instance. Dev can reuse the same module for several routes in one process. Segment code must work when those routes run in separate instances.

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
3. inherited dependencies, then handler arguments and the handler (§7.5).

- **Middleware sees a response.** Handler and dependency errors have already become a `Response` by the time middleware inspects it.
- **Middleware errors.** An exception raised by middleware itself skips `error.py` and gets the default mapping.
- **Unmatched paths.** The layers run only after a path matches. A path with no route never reaches a deployed function, so in dev it returns Haumea's JSON 404 without running any segment file. The deployed response is Vercel's `NOT_FOUND` page instead (§2.2, dev parity).
- **405s.** A 405 runs through the matched route's chain, as it does in production.
- **Parity.** `create_app` and `create_route_app` build the same per-route chain.

### 7.5 Inherited typed dependencies

A segment declares an ordered collection of dependency callables. For example, an admin directory can require `require_admin` for every exported method beneath it. A new route inherits the check by placement, including through URL-transparent groups. The declaration syntax is an implementation-plan decision (§9).

Both app factories attach ancestor checks to each matching method's `Endpoint` during registration. On dispatch, resolve them from root to leaf using the same request cache as `Depends`. A failed check prevents the handler from running. A handler that needs the result can request the same callable with `Depends` and receive its cached value. Checks run inside the selected function. A 404 or method mismatch has no selected handler and runs no typed check.

The existing build-time app construction can inspect these same dependency signatures. Include their declared request inputs once per operation, even if a handler also requests the dependency. Reading a raw `Request` exposes no fields to introspection. Neither discovery nor schema generation invokes the checks. Formal security requirements and error responses follow the explicit metadata rules in §6.7. Inheritance adds no build stage.

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

1. **Service build handoff.** *Resolved by the 1.1 spike; details are in §2.1.*
   - **Build.** The service sets a static-build `framework` such as `"vite"` and `buildCommand: "uv run --frozen haumea build"`, and writes Build Output v3 to `<service root>/.vercel/output`. The CLI moves that tree into `.vercel/output/services/<name>/`. Haumea's `config.json` routes and `.vc-config.json` files are preserved; the CLI applies `.vercelignore` to `static/` and adds an empty `crons` field.
   - **Dev.** `vercel dev` runs `devCommand` in the service root with `$PORT` set and proxies the original, unstripped path.
   - **Prefix.** `VERCEL_SERVICE_ROUTE_PREFIX_STRIP` is never set under `services`, so Haumea must route the full prefixed path itself. This holds for a rewrite without `destination.path` or a service `request.path` transform.
   - **Evidence.** Checked with `vercel build`, `vercel dev --local` and a real `vercel deploy`, and backed by reading the CLI 65.12.1, `@vercel/static-build` 27.0.0 and `@vercel/python` 31.0.0 sources.
   - **Read from source only.** That `outputDirectory` does not change the lookup path, and that `@vercel/python` cannot consume Build Output.
   - **Follow-up.** The `"vite"` framework slug is a workaround. Ask Vercel whether `services` will get a builder-neutral way to declare a prebuilt Build Output service.
2. **`uv export --package` in a workspace.** *Resolved in 1.5 (`uv` 0.11.23).* `--package <name> --no-emit-project` drops only `<name>` (even when `<name>` has a `[build-system]`), keeps workspace sibling dependencies such as `./packages/core`, and omits unrelated workspace members and their dependencies (`test_workspace_build`). Verified manually with `uv` 0.11.23 that a non-virtual workspace root package is also kept when depended on by `<name>` and dropped when exported as `<name>`.
3. **`vercel-functions` import path.** Does the split package keep the `vercel.functions` import path? Check when it is published.
4. **Segment dependency declaration.** Choose the segment filename and exported collection for §7.5 before implementing plan step 4.5. Keep route authorship in `route.py`.
5. **Operation metadata.** Choose how a dependency declares a credential scheme and how an operation or dependency declares documented responses (§6.7). Broader defaults such as subtree tags and method-specific success responses remain open.
