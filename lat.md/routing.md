# Routing

Haumea maps an `app/` directory tree to HTTP endpoints using Next.js App Router conventions and serves them via a lightweight ASGI router.

## File-System Discovery

Routes are discovered from `route.py` files under `app/` and sorted from most specific to least specific.

[[src/haumea/routing.py#discover_routes]] walks `app_dir` for [[src/haumea/routing.py#ROUTE_FILE]] (`route.py`) and builds [[src/haumea/routing.py#Route]] records using [[src/haumea/routing.py#_route]]:
- Static directory segments map literally and receive rank `0`.
- `[param]` segments map to `{param}` (`[^/]+`) and receive rank `1`.
- `[...param]` catch-all segments map to `{param:path}` (`(.+)`) and receive rank `2`.
- `(group)` parenthesized directories are stripped from both the URL path and the deployed function name for organization only.

With a normalized `base_path` ([[config#Base Path]]), `url_path`, `function` and `pattern` all carry the prefix: under `/api`, `app/route.py` is `/api` (function `api`) and `app/users/[id]/route.py` is `/api/users/{id}` (function `api/users/[id]`, pattern `^/api/users/([^/]+)$`). Only an unprefixed root route is named `index`.

Routes are sorted by `(route.rank, route.url_path)` so static paths (e.g. `/items/new`) match before dynamic ones (`/items/{item_id}`), and catch-alls match last. When discovering routes, [[src/haumea/routing.py#discover_routes]] also prepends `app_dir.parent` to `sys.path` so route modules can import sibling project packages.

## Handler Registration

Each `route.py` exports top-level functions named after standard HTTP methods.

[[src/haumea/routing.py#_add_route]] loads the module via [[src/haumea/routing.py#Route#load]] and registers any callable matching [[src/haumea/routing.py#HTTP_METHODS]].

[[src/haumea/routing.py#Route#load]] caches the module in `sys.modules` as `haumea_routes._<hash>`, keyed only by 12 hex digits of the SHA-1 of the resolved `route.py` path. Same-named routes in different projects never share a module, and one file discovered under two `base_path` values reuses one. If the import raises, the entry is removed before re-raising, so no partial module is cached.

- [[src/haumea/routing.py#create_app]] registers all discovered routes and enables `/docs`, `/openapi.json`, and optional `public_dir` static serving.
- [[src/haumea/routing.py#create_route_app]] registers only the single [[src/haumea/routing.py#Route]] matching `function` and sets `docs=False`.

Both take `base_path`; `None` reads it from `[tool.haumea]` beside `app_dir`, and the value is passed to both [[src/haumea/app.py#Haumea]] and [[src/haumea/routing.py#discover_routes]].

## ASGI Request Handling

[[src/haumea/app.py#Haumea#__call__]] dispatches HTTP requests through [[src/haumea/app.py#Haumea#_handle]], handles lifespan events as no-ops, and maps exceptions to HTTP responses.

Matching always uses the full public path from [[src/haumea/requests.py#Request#path]] ([[config#Full Request Path]]).

- **Method matching & HEAD**: `HEAD` falls back to `GET` handlers if no explicit `HEAD` handler exists, and [[src/haumea/app.py#Haumea#__call__]] suppresses the response body for `HEAD` requests. It calls the response with `receive` and `send` ([[responses]]).
- **405 Method Not Allowed**: If a literal route matches the path without supporting the method, [[src/haumea/app.py#Haumea#_handle]] raises [[src/haumea/exceptions.py#HTTPException]] `405` with a sorted `Allow` header without falling through to docs, static files, or dynamic routes. A docs or `public/` file hit answers methods other than `GET`/`HEAD` with `405` and `Allow: GET, HEAD`. If dynamic routes match the path regex without supporting the method, `_handle` raises `405` with their combined sorted `Allow` header.
- **Error mapping**: [[src/haumea/exceptions.py#HTTPException]] renders JSON `{"detail": ...}` with its status/headers; [[src/haumea/exceptions.py#RequestValidationError]] renders `422`; unhandled exceptions log a traceback to `stderr` and return `500` plain text.
- **Match order**: [[src/haumea/app.py#Haumea#_handle]] mirrors Build Output priority: literal routes (no path parameters) match first, like static-path functions in the `filesystem` check; then [[src/haumea/app.py#Haumea#_dev_file]] serves docs and `public_dir` files; dynamic and catch-all routes match last, like the rewrites after the docs rewrite, so a catch-all never shadows `/docs`, `/openapi.json`, or a `public/` file.
- **Dev docs & static files**: [[src/haumea/app.py#Haumea#_dev_file]] follows the build's precedence and rejects non-`GET`/`HEAD` requests with `405` before generating OpenAPI or reading file bytes. If `docs=True`, the generated `{base_path}/openapi.json` and `{base_path}/docs.html` win over `public/` copies, as [[src/haumea/build.py#_write_static]] writes them over `public/`. `{base_path}/docs` is the Swagger UI (loading the prefixed `openapi.json`) only when `public/docs` does not exist, because the `filesystem` check runs before the docs rewrite. [[src/haumea/app.py#Haumea#_static_file]] serves files from `public_dir`, guarding against path traversal with `Path.is_relative_to`. Under a prefix it only serves paths strictly under `{base_path}/`, so `/hello.txt` and `/apiary/hello.txt` get the JSON 404. Paths ending in `/`, symlink loops and NUL bytes are not files.

*Note on non-obvious behavior*: [[src/haumea/app.py#Haumea#__call__]] ignores non-`http`/non-`lifespan` ASGI scopes (e.g. `websocket`) by returning immediately.
