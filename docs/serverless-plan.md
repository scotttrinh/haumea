# Haumea Serverless Plan

> **Status:** Implementation plan for [serverless-spec.md](serverless-spec.md). Section numbers like §2 refer to the spec.

Each step should land as its own reviewable change with its tests. When a step changes behavior, move its spec section into `lat.md/` as current state, add `lat.md/tests` entries for the new tests, and run `lat check`.

---

## Phase 1: Deploy beside a frontend

**Goal:** a Haumea project in a uv workspace deploys as a Vercel service under `/api`, next to a Next.js frontend.

### 1.1 Spike: Haumea as a Vercel service

- **What:** deploy `examples/hello` as a service beside a minimal Next.js app, with a top-level rewrite of `/api/(.*)` and `haumea build` as the service's `buildCommand`.
- **Deliverable:** answers to spec §9 question 1, written back into the spec. No merged framework code.
- **Exit:** we know where the service build must write its output, how `vercel dev` runs it, and whether the runtime strips the prefix.
- **Result:** done; spec §2.1 records the fixture, the observed requests and the conclusions. The build writes to `<service root>/.vercel/output`, `vercel dev` proxies the original path to `devCommand`, and nothing strips the prefix. The tested rewrite `/api/(.*)` does not match bare `/api`; 1.6 uses `/api/:path*`. Keep the Vercel project `haumea-spike` (`scotttrinhs-projects`) as a reusable test project for this implementation effort.

### 1.2 Build plan refactor (§3.1)

- **What:** split `build()` into discover, plan and emit, with no change in output.
- **Tests:**
  - The existing build test passes unchanged.
  - A new test checks the `BuildPlan` produced for a fixture project.

### 1.3 `[tool.haumea]` config and `base_path` (§1, §2)

- **What:** a config struct, prefixed routing, `root_path` reconstruction, prefixed docs and OpenAPI paths, and the prefixed build layout and rewrites. Output stays at `<service root>/.vercel/output`, the location 1.1 confirmed (spec §2.3).
- **Tests:**
  - Dev routing works under the prefix, and paths outside it return Haumea's JSON 404.
  - `root_path + path` is the full path, except at the prefix root (spec §2.2, scope convention and trailing slashes):
    - `root_path="/api"`, `path="/"` (what `vercel-runtime` produces for both `/api` and `/api/`) becomes `/api` and matches `app/route.py`;
    - without a strip, `root_path=""`, `path="/api/"` does not match `app/route.py`;
    - `root_path="/api"`, `path="/users"` matches `/api/users`;
    - `root_path="/api"`, `path="/apiary"` becomes `/api/apiary`, not `/apiary`;
    - repeated prefix: `root_path="/api"`, `path="/api/users"` becomes `/api/api/users` and does not match `/api/users`.
  - Built function paths, static paths and rewrites are prefixed.
  - Unknown config keys fail.
- **Depends on:** 1.2.

### 1.4 `--output` and `--locked` (§3.2)

- **Tests:** output goes to a custom directory, and a stale lock fails the build.

### 1.5 uv workspace support (§4)

- **What:** workspace root discovery, scoped `uv export --package`, and installing from the workspace root.
- **Tests:** a fixture workspace with `apps/api` (Haumea) and `packages/core`. The build runs from `apps/api`, and the function can import `core`. This test also answers spec §9 question 2.

### 1.6 Full-stack example

- **What:** add `examples/fullstack` (Next.js plus a Haumea service in a uv workspace), based on the spike, using the spec §2.1 service configuration.
  - The top-level rewrite is `/api/:path*`, not the spike's `/api/(.*)`, so that bare `/api` reaches `app/route.py`.
  - The backend uses `base_path = "/api"` with routes at `app/`, and pins `.python-version` to `3.12` so that `uv run haumea build` on Vercel uses the same minor version as the `python3.12` function runtime instead of the 3.14 that `uv` picked in the spike (spec §2.1). The build-time import check that relies on this pin arrives in 2.4 (spec §5.7).
- **Exit:** the example deploys and `vercel dev` runs both services. `/api` and `/api/docs` reach the backend in both. (Can deploy to the retained `haumea-spike` Vercel test project.)

---

## Phase 2: Dependency tree-shaking

**Goal:** each function bundles only the dependencies it imports, and the build reports and checks the result.

### 2.1 Move `uvicorn` to `haumea[dev]` (§5.9)

- **What:** update `examples/hello` and the docs to match.
- **Exit:** `uvicorn` no longer appears in built functions.

### 2.2 Store index and size report (§5.1, §5.8)

- **What:** index the store, print per-function sizes, warn above 250 MB and fail above 500 MB. Every function still links the whole store, which gives a baseline before shaking.
- **Tests:** with the thresholds lowered in the test, a fixture triggers both the warning and the failure.

### 2.3 Tracer, closure and `include` (§5.2–§5.6)

- **What:** static tracing, the closure step, the always-included set, project and route `include`, and the `tree_shake` switch.
- **Tests:** a fixture where two routes import different small pure-Python dependencies. Each function contains only its own dependency.
  - Lazy imports inside function bodies are bundled.
  - Imports under `TYPE_CHECKING` are not.
  - `include` adds a distribution.
  - `tree_shake = false` restores the full store.

### 2.4 Build-time import check (§5.7)

- **Tests:**
  - With `--platform native`, a route that imports a dependency by computed name fails the build with a message pointing at `include`.
  - The check is skipped when the platform doesn't match the host.
  - The check runs under the interpreter `uv python find` resolves for `--python`, not `sys.executable`, and is skipped with a message when none is found.

### 2.5 `--explain` (§5.8)

- **Tests:** the output gives the reason chain for an included distribution and lists the excluded ones.

---

## Phase 3: Request and response surface

**Goal:** the runtime pieces AI/API endpoints need. Steps 3.2 to 3.5 are independent of each other.

### 3.1 `Response` takes `receive`; `StreamingResponse` and `SSEResponse` (§6.1)

- **Tests:**
  - Chunks are streamed in order.
  - A client disconnect closes the generator.
  - A sync iterator works.
  - SSE framing is correct, including the ping lines.

### 3.2 Cookies (§6.2)

- **What:** `request.cookies`, the `Cookie(...)` parameter, `set_cookie` and `delete_cookie`, and multi-value headers.

### 3.3 Platform metadata (§6.3)

- **Tests:** a request with and without the Vercel headers.

### 3.4 `include_in_schema` and `lazy()` (§6.4, §6.6)

### 3.5 `after()` (§6.5)

- **What:** the `local` and `wait_until` backends, the `after_backend` switch, and the build installing and bundling `vercel-functions`.
- **Tests:**
  - The work runs after the response body is sent.
  - Exceptions are logged.
  - Calling `after()` outside a request raises.
  - The generated entrypoint selects `wait_until`.

---

## Phase 4: Segment files

**Goal:** `middleware.py` and `error.py` apply to routes by directory, the same way in dev and in deployed functions.

### 4.1 `Route.segments` and segment loading (§7.1)

### 4.2 `middleware.py` (§7.2, §7.4)

- **Tests:**
  - Middleware runs outer to inner.
  - Short-circuiting works.
  - Middleware sees the error response.
  - Middleware doesn't run on unmatched paths, but does run on a 405.
  - Dev and `create_route_app` behave the same.

### 4.3 `error.py` (§7.3)

- **Tests:**
  - The nearest `error.py` wins.
  - Re-raising passes to the next ancestor.
  - Without `error.py` the default mapping applies.
  - An exception raised by middleware skips `error.py`.

### 4.4 Segment files as trace roots (§5.2)

- **Tests:** a dependency imported only by `app/admin/middleware.py` is bundled only into functions under `app/admin/`.

---

## Gates and dependencies

- **Spike first.** Done. 1.1 confirmed that the service build writes to `<service root>/.vercel/output` (spec §2.1), so 1.3 keeps that location.
- **Workspace fixture before tree-shaking.** 2.3 builds on the 1.2 refactor and should run against the 1.5 fixture, because shared workspace packages are where the size savings show up.
- **`after()` needs `vercel-functions` published.** It is expected well before 3.5 starts. Confirm its import path then.
- **Segment tracing needs the tracer.** 4.4 requires 2.3.
