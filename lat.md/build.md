# Build and OpenAPI

`haumea build` compiles a project into a Vercel Build Output API v3 directory with one isolated function per route and pre-rendered static API docs.

## Build Output Layout

[[src/haumea/build.py#build]] writes `.vercel/output/` containing `config.json`, `static/`, and `functions/<route>.func/`.

- **Shared payload & hard-linking**: Project files (excluding [[src/haumea/build.py#IGNORE]]) and vendored dependencies are assembled once in a temporary directory and hard-linked (`os.link`) into every `.func` directory to save disk space and build time.
- **Dependency vendoring**: [[src/haumea/build.py#_install_dependencies]] exports locked non-dev dependencies with `uv export` (`--no-emit-project`, `--no-editable`) and installs them alongside `vercel-runtime` into `_vendor/` for [[src/haumea/build.py#DEFAULT_PLATFORM]] (`x86_64-manylinux_2_28`) and [[src/haumea/build.py#DEFAULT_PYTHON]] (`3.12`), or the host platform when `platform=None`.
- **Function trampoline**: Each `.func` directory receives `vc__handler__python.py` ([[src/haumea/build.py#TRAMPOLINE_SOURCE]]), `index.py` ([[src/haumea/build.py#ENTRYPOINT_SOURCE]] calling [[src/haumea/routing.py#create_route_app]]), and `.vc-config.json`.

## Per-Route Function Config

Route modules can configure their own Vercel function settings using module-level variables.

[[src/haumea/build.py#_write_function]] reads [[src/haumea/build.py#ROUTE_CONFIG]] (`max_duration` -> `maxDuration`, `memory` -> `memory`, `regions` -> `regions`) from the loaded route module and writes them into `.vc-config.json` alongside `supportsResponseStreaming: true` and `PYTHONDONTWRITEBYTECODE=1`.

## Static Docs and Routing Config

API documentation and URL rewrites are generated at build time so runtime functions stay minimal.

- [[src/haumea/build.py#_write_docs]] instantiates the full app via [[src/haumea/routing.py#create_app]] and writes `openapi.json` and `docs.html` into `.vercel/output/static/`.
- `config.json` places `{"handle": "filesystem"}` first (matching `static/` files and static-path `.func` directories), then rewrites `^/docs$` to `/docs.html` and each dynamic `route.pattern` to `/{route.function}`.
- [[src/haumea/openapi.py#openapi_schema]] generates OpenAPI 3.1.0 from `Endpoint.request_fields()`, using `msgspec.json.schema_components`, the first line of the handler docstring as `summary`, and adding a `422` response whenever the endpoint has request fields. Return annotations that subclass [[src/haumea/responses.py#Response]] omit a JSON response schema.
