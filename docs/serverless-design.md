# Haumea Serverless Architecture & Future Design Space

> **Status:** Living design document (future-facing proposals, not yet implemented).
> Per project convention, `lat.md/` documents only the current implemented state; this file captures the design space as we iterate on Haumea's serverless-first roadmap.

---

## Guiding Principles

Haumea's core differentiator is **one isolated Python function per `route.py`**, a minimal ASGI + `msgspec` core, and build-time compilation to **Vercel Build Output API v3**.

Every new feature should pass this litmus test:
1. **Can `haumea build` precompute this into `config.json` / `static/`, or scope it to only the routes that import it?**
2. **Does it avoid double cold starts and cross-route dependency bloat?**
3. **Does `haumea dev` preserve behavioral parity with `haumea build`?**

---

## 1. Proxy & Middleware Architecture

Following Next.js's evolution from `middleware.ts` to `proxy.ts`, Haumea should separate **network-boundary routing** from **in-process request middleware** and **typed handler dependencies** rather than copying either FastAPI's global ASGI middleware or Next.js's old global edge middleware.

On Vercel, Python runs as serverless functions (not V8 edge isolates), so a global Python proxy in front of every route would cause two Python invocations (and potentially two cold starts) per cache miss.

### Tier 1: Declarative Edge Rules (0 ms Python Boot)
- **What belongs here:** Redirects, rewrites, CORS headers, security headers, and static `has`/`missing` cookie/header/query routing.
- **How it works:** Declared in project/route config and compiled directly into `.vercel/output/config.json` (`routes` with `continue: true`, `headers`, `status`, `dest`).
- **Dev parity:** `Haumea` evaluates the same rule list before route dispatch in `haumea dev`.

### Tier 2: Directory-Scoped In-Process Middleware (`middleware.py`)
- **What belongs here:** Request IDs, structured logging, timing, response header shaping, rate-limit hooks.
- **How it works:** Placed along the `app/` tree (`app/middleware.py`, `app/api/admin/middleware.py`). Both `create_app` (dev) and `create_route_app` (prod) compose only the `middleware.py` ancestors along a route's path.
- **Cold-start win:** Public/health routes never import heavy auth or telemetry dependencies used by `/admin/middleware.py`, and everything executes in-process with zero extra network hop.
- **API shape:** Simple `async def middleware(request: Request, call_next) -> Response` operating on `Request`/`Response` (inside exception mapping so route errors become `Response`s before middleware inspects status codes).

### Tier 3: Optional `proxy.py` (True Pre-Routing Function)
- **What belongs here:** Dynamic multi-tenant subdomain routing, signed-cookie gating in front of `public/` static files, or dynamic cross-route rewrites.
- **Guardrails:** Opt-in only, with a mandatory `matcher` config so unrelated routes never pay a double Python cold start.

### Typed Dependencies (`Depends`) for Auth & Context
- Auth, user resolution, and DB sessions should live in `Depends(...)` rather than `middleware.py` so that:
  - `openapi.json` accurately reflects security schemes and `401`/`403` responses.
  - Only routes that declare a dependency import and execute it.

---

## 2. Serverless Runtime Primitives

Features where server-ful frameworks break on Vercel and how Haumea should adapt them:

- **Post-response work (`after()` / `waitUntil`):**
  - FastAPI's `BackgroundTasks` gets cut off when Vercel freezes the container after sending the response.
  - Provide `after(fn, *args)` backed by `vercel-runtime`'s `waitUntil` in production and post-response execution in `haumea dev`.
- **Lifecycle without `lifespan`:**
  - Serverless functions freeze/thaw and rarely guarantee clean shutdown. Favor lazy per-route singleton helpers (`lazy(...)`) over eager global startup, and document/support small-pool or HTTP-first DB/client patterns.
- **Build-time static routes & CDN caching (`static` / `revalidate`):**
  - `static = True` on a `GET` `route.py`: evaluated once during `haumea build` and emitted to `.vercel/output/static/` (zero runtime function).
  - `revalidate = 60`: compiles to `Cache-Control: s-maxage=60, stale-while-revalidate` or Build Output API `.prerender-config.json` for ISR-style caching.
- **Per-route dependency tree-shaking:**
  - Currently `build.py` hard-links one shared `_vendor/` into every `.func`, which means every deployed function carries the full dependency size.
  - Add static AST import tracing (or optional per-route dependency groups) so heavy libraries (`pandas`, `numpy`, ML SDKs) are only packaged into the `.func`s that import them, plus optional route coalescing for projects that prefer fewer cold starts over strict 1:1 isolation.
- **First-class Streaming & SSE (`StreamingResponse` / `SSEResponse`):**
  - Build on `supportsResponseStreaming: True` with clean async-generator streaming and `http.disconnect` cancellation handling for LLM/AI endpoints.
- **Segment-scoped error boundaries (`error.py`):**
  - Directory-scoped `error.py` alongside `middleware.py` so `/api/v1/` and `/webhooks/` can format exceptions differently without a global `app` object.
- **Typed platform metadata:**
  - Expose `request.ip`, `request.geo`, and `request.id` from Vercel edge headers (`x-forwarded-for`, `x-vercel-ip-*`, `x-vercel-id`) with local-dev fallbacks.

---

## 3. Vercel Services & Python SDK Integration

Rather than reinventing brokers, schedulers, or workflow engines, Haumea should act as a thin, fast-booting host and Build Output compiler for **Vercel Services** and Vercel's official Python SDKs:

- **Vercel `services` (`vercel.json`):**
  - Support a `base_path` / `route_prefix` option (e.g. `/api`) so Haumea can mount cleanly alongside a Next.js frontend service without `/docs`, `/openapi.json`, or `config.json` rewrites colliding with `/`.
  - Keep build-time `openapi.json` easy to consume from sibling frontend services (e.g. `openapi-typescript`).
- **Vercel Queues (`vercel-queue`):**
  - Support file-system consumer discovery (e.g. `queue.py` / `@subscribe(topic=...)`), packaging each consumer as its own isolated `.func` with the right Build Output API subscription config.
  - Keep `msgspec` first-class (optional codec/helper without forcing Pydantic into core `haumea`).
  - **Dev parity:** `haumea dev` bridges `vercel.queue.send(...)` to local `@subscribe` handlers automatically.
- **Vercel Workflows (`vercel-workflow`):**
  - Support `workflow.py` discovery so `haumea build` wires internal step/webhook callback `.func`s and rewrites automatically—without polluting public `/openapi.json` or `/docs`—and respects per-module `max_duration`/`memory`.
- **Crons & Schedules:**
  - Allow `cron = "0 * * * *"` (or `schedules`) alongside `max_duration` / `memory` in `route.py`, compiling into Vercel's `crons` config.
  - Automatically enforce `CRON_SECRET` (`Authorization: Bearer ...`) in production and exclude/tag cron routes in OpenAPI.
- **Daemons:**
  - Keep `app/` shared business packages easy to import from both serverless `route.py` functions and separate always-on `worker/` daemon entrypoints.

---

## 4. Monorepo Support

Current `build.py` assumes `pyproject.toml`, `uv.lock`, and `app/` all live in `project_dir` and uses `uv export --no-emit-project --no-editable`. To support monorepos (`uv` workspaces + Turborepo + Vercel `services`):

1. **Workspace root discovery:** Walk up to locate `uv.lock` / `[tool.uv.workspace]` separately from the app's `project_dir`.
2. **Scoped dependency export:** Run `uv export --package <name>` from the workspace root so only this service's transitive dependencies are exported.
3. **Local workspace siblings:** Bundle editable workspace packages (e.g. `packages/core`, `packages/db`) into `_vendor/` or `shared/` instead of dropping them via `--no-editable`.
4. **Configurable output & cacheable builds:** Support `--output` for `.vercel/output` placement and deterministic inputs/outputs for Turborepo / Vercel remote caching.
5. **Per-route tree-shaking:** Especially important in monorepos where shared packages grow large and would otherwise bloat every `.func`.

---

## 5. Full-Stack HTMX Support

Pairing Haumea with HTMX gives a zero-JS-framework, CDN-first stack (`htmx.min.js` + CSS in `public/`, fast Python `.func`s for HTML fragments):

- **Form body decoding:** Support `Form` extraction (`application/x-www-form-urlencoded` and `multipart/form-data`) decoded into `msgspec.Struct`s with the same validation ergonomics as JSON bodies.
- **HTMX request/response ergonomics:**
  - `request.hx` typed properties (`request`, `target`, `trigger`, `boosted`, `current_url`).
  - Response helpers for `HX-Redirect`, `HX-Refresh`, `HX-Trigger`, `HX-Retarget`, and `HX-Reswap`.
- **Segment layouts (`layout.py`) + partial negotiation:**
  - Directory-scoped `layout.py` wraps `HTMLResponse` fragments automatically on full-page browser navigation (`not request.hx.request` or `request.hx.boosted`) and skips the outer shell on partial `HX-Request` swaps.
- **HTML validation errors & OpenAPI separation:**
  - Render `RequestValidationError` as an HTML error fragment (via `error.py` or `HX-Request` negotiation) instead of JSON `422`.
  - Allow HTML/UI routes to opt out of `/openapi.json` (`include_in_schema = False` or automatic for UI routes).
- **Cookies & stateless CSRF:**
  - First-class `request.cookies` / `Response.set_cookie(...)`.
  - Lightweight stateless CSRF middleware checking `Sec-Fetch-Site` / `Origin` headers (zero session-store lookup).
- **SSE HTML streaming:**
  - Combine `SSEResponse` with HTMX `hx-ext="sse"` to stream live HTML fragments (LLM responses, `vercel-workflow` progress) directly into the DOM.

---

## Open Questions for Next Iterations

- [ ] **Middleware composition order:** Should `middleware.py` run outside or inside `HTTPException`/`RequestValidationError` mapping (or support both)?
- [ ] **Dependency tree-shaking strategy:** Automatic AST import tracing vs. explicit per-route `dependencies` / `uv` groups—or a hybrid?
- [ ] **`vercel-queue` / `vercel-workflow` discovery:** Dedicated file names (`queue.py`, `workflow.py`) vs. decorators scanned across `app/`?
- [ ] **HTMX templating stance:** Purely template-agnostic (`HTMLResponse` + `layout.py` callables) vs. first-party helpers for Jinja2 / `htpy`?
