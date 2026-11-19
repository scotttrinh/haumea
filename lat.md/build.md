# Build and OpenAPI

`haumea build` compiles a project into a Vercel Build Output API v3 directory with one isolated function per route and pre-rendered static API docs.

## Build Pipeline

[[src/haumea/build.py#build]] runs discover, plan and emit in sequence, so every output decision is made and inspectable before anything is written.

- **Discover**: [[src/haumea/build.py#plan_build]] resolves the project, requires `pyproject.toml`, reads `[tool.haumea]` with [[src/haumea/config.py#load_config]] before importing any route, and finds routes under its `base_path` with [[src/haumea/routing.py#discover_routes]].
- **Plan**: [[src/haumea/build.py#plan_build]] returns a [[src/haumea/build.py#BuildPlan]] and leaves the output directory untouched. It imports every route module, so route code runs during planning rather than emission, and renders the API docs, but runs no `uv` commands. Routes are assumed not to modify project source files at import time.
- **Emit**: [[src/haumea/build.py#emit_build]] replaces the output directory and writes it from the plan, copying the project and `public/` trees with `shutil.copytree` as they are at emit time; it vendors dependencies but never imports route modules.

### Plan Types

The plan is a tree of frozen dataclasses describing the full output tree.

- [[src/haumea/build.py#BuildPlan]] holds the project paths (`project_dir`, `workspace_root`, `app_dir`, `public_dir` or `None`, `output_dir`), the parsed [[src/haumea/config.py#HaumeaConfig]] (`config`), target `python`/`platform`, the function specs, generated static files, and the `config.json` `routes`. `workspace_root` is always `project_dir` for now.
- [[src/haumea/build.py#FunctionSpec]] holds one function's `name` (its path under `functions/`), its [[src/haumea/routing.py#Route]], its `index.py` `entrypoint` source, and its `.vc-config.json` content (`vc_config`).
- [[src/haumea/build.py#StaticFile]] maps a path under `static/` to generated text written over the copied `public/` files.

## Build Output Layout

[[src/haumea/build.py#emit_build]] writes `.vercel/output/` containing `config.json`, `static/`, and `functions/<route>.func/`.

The tree is always written to `<project_dir>/.vercel/output`, also in Vercel Services mode, where the CLI moves it into `services/<name>/`. Function paths are the routes' prefixed `function` names, e.g. `functions/api.func` and `functions/api/users/[id].func` under `base_path = "/api"`.

- **Shared payload & hard-linking**: Project files (excluding [[src/haumea/build.py#IGNORE]]) and vendored dependencies are assembled once in a temporary directory and hard-linked (`os.link`) into every `.func` directory to save disk space and build time.
- **Dependency vendoring**: [[src/haumea/build.py#_install_dependencies]] exports locked non-dev dependencies with `uv export` (`--no-emit-project`, `--no-editable`) and installs them alongside `vercel-runtime` into `_vendor/` for [[src/haumea/build.py#DEFAULT_PLATFORM]] (`x86_64-manylinux_2_28`) and [[src/haumea/build.py#DEFAULT_PYTHON]] (`3.12`), or the host platform when `platform=None`.
- **Function trampoline**: Each `.func` directory receives `vc__handler__python.py` ([[src/haumea/build.py#TRAMPOLINE_SOURCE]]), `index.py` ([[src/haumea/build.py#ENTRYPOINT_SOURCE]] calling [[src/haumea/routing.py#create_route_app]] with the planned `base_path`), and `.vc-config.json`.

## Per-Route Function Config

Route modules can configure their own Vercel function settings using module-level variables.

[[src/haumea/build.py#_plan_function]] reads [[src/haumea/build.py#ROUTE_CONFIG]] (`max_duration` -> `maxDuration`, `memory` -> `memory`, `regions` -> `regions`) from the loaded route module into the function's `vc_config`, cloning nested containers with [[src/haumea/build.py#_clone_config]] so later route imports cannot mutate it without invoking `__deepcopy__` or collapsing keys, alongside `supportsResponseStreaming: true` and `PYTHONDONTWRITEBYTECODE=1`.

## Static Docs and Routing Config

API documentation and URL rewrites are generated at build time so runtime functions stay minimal.

- [[src/haumea/build.py#_write_static]] copies `public/` into `static/<base_path>/` with `shutil.copytree` (checking whether `public/` exists at emit time, and creating the directory otherwise); [[src/haumea/build.py#_plan_static]] instantiates the full app via [[src/haumea/routing.py#create_app]] to render `<base_path>/openapi.json` and `<base_path>/docs.html` (Swagger UI loading the prefixed `openapi.json`), which win over same-named `public/` files.
- [[src/haumea/build.py#_plan_routes]] makes `config.json` place `{"handle": "filesystem"}` first (matching `static/` files and static-path `.func` directories), then rewrites `^<base_path>/docs$` to `<base_path>/docs.html` and each dynamic, already-prefixed `route.pattern` to `/{route.function}`.
- [[src/haumea/openapi.py#openapi_schema]] generates OpenAPI 3.1.0 from `Endpoint.request_fields()`, using `msgspec.json.schema_components`, the first line of the handler docstring as `summary`, and adding a `422` response whenever the endpoint has request fields. Return annotations that subclass [[src/haumea/responses.py#Response]] omit a JSON response schema.
