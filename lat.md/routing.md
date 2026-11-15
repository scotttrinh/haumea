# Routing

Haumea maps an `app/` directory tree to HTTP endpoints using Next.js App Router conventions and serves them via a lightweight ASGI router.

## File-System Discovery

Routes are discovered from `route.py` files under `app/` and sorted from most specific to least specific.

[[src/haumea/routing.py#discover_routes]] walks `app_dir` for [[src/haumea/routing.py#ROUTE_FILE]] (`route.py`) and builds [[src/haumea/routing.py#Route]] records using [[src/haumea/routing.py#_route]]:
- Static directory segments map literally and receive rank `0`.
- `[param]` segments map to `{param}` (`[^/]+`) and receive rank `1`.
- `[...param]` catch-all segments map to `{param:path}` (`(.+)`) and receive rank `2`.
- `(group)` parenthesized directories are stripped from both the URL path and the deployed function name for organization only.

Routes are sorted by `(route.rank, route.url_path)` so static paths (e.g. `/items/new`) match before dynamic ones (`/items/{item_id}`), and catch-alls match last. When discovering routes, [[src/haumea/routing.py#discover_routes]] also prepends `app_dir.parent` to `sys.path` so route modules can import sibling project packages.

## Handler Registration

Each `route.py` exports top-level functions named after standard HTTP methods.

[[src/haumea/routing.py#_add_route]] loads the module via [[src/haumea/routing.py#Route#load]] (caching under `haumea_routes.*` in `sys.modules`) and registers any callable matching [[src/haumea/routing.py#HTTP_METHODS]].

- [[src/haumea/routing.py#create_app]] registers all discovered routes and enables `/docs`, `/openapi.json`, and optional `public_dir` static serving.
- [[src/haumea/routing.py#create_route_app]] registers only the single [[src/haumea/routing.py#Route]] matching `function` and sets `docs=False`.

## ASGI Request Handling

[[src/haumea/app.py#Haumea#__call__]] dispatches HTTP requests through [[src/haumea/app.py#Haumea#_handle]], handles lifespan events as no-ops, and maps exceptions to HTTP responses.

- **Method matching & HEAD**: `HEAD` falls back to `GET` handlers if no explicit `HEAD` handler exists, and [[src/haumea/app.py#Haumea#__call__]] suppresses the response body for `HEAD` requests.
- **405 Method Not Allowed**: If one or more routes match the path regex but none support the request method, [[src/haumea/app.py#Haumea#_handle]] raises [[src/haumea/exceptions.py#HTTPException]] `405` with a sorted `Allow` header.
- **Error mapping**: [[src/haumea/exceptions.py#HTTPException]] renders JSON `{"detail": ...}` with its status/headers; [[src/haumea/exceptions.py#RequestValidationError]] renders `422`; unhandled exceptions log a traceback to `stderr` and return `500` plain text.
- **Dev docs & static files**: On `GET`/`HEAD` misses, if `docs=True`, `/openapi.json` and `/docs` are served; otherwise [[src/haumea/app.py#Haumea#_static_file]] serves files from `public_dir`, guarding against path traversal with `Path.is_relative_to`.

*Note on non-obvious behavior*: [[src/haumea/app.py#Haumea#__call__]] ignores non-`http`/non-`lifespan` ASGI scopes (e.g. `websocket`) by returning immediately.
