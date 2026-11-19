# Build and OpenAPI

`haumea build` compiles a project into a Vercel Build Output API v3 directory with one isolated function per route and pre-rendered static API docs.

## Build Pipeline

[[src/haumea/build.py#build]] runs discover, plan and emit in sequence, so every output decision is made and inspectable before anything is written.

- **Discover**: [[src/haumea/build.py#plan_build]] resolves the project, requires `pyproject.toml`, reads `[tool.haumea]` with [[src/haumea/config.py#load_config]] before importing any route, discovers any workspace root via [[src/haumea/build.py#_find_workspace_root]] (reading `[project].name` via [[src/haumea/build.py#_project_name]] when in a workspace), and finds routes under its `base_path` with [[src/haumea/routing.py#discover_routes]].
- **Plan**: [[src/haumea/build.py#plan_build]] returns a [[src/haumea/build.py#BuildPlan]] and leaves the output directory untouched. It imports every route module, so route code runs during planning rather than emission, and renders the API docs, but runs no `uv` commands. Routes are assumed not to modify project source files at import time.
- **Emit**: [[src/haumea/build.py#emit_build]] replaces the output directory and writes it from the plan, copying the project and `public/` trees with `shutil.copytree` as they are at emit time; it vendors dependencies but never imports route modules.

### Plan Types

The plan is a tree of frozen dataclasses describing the full output tree.

- [[src/haumea/build.py#BuildPlan]] holds the project paths (`project_dir`, `workspace_root`, `app_dir`, `public_dir` or `None`, `output_dir`), the workspace `package` name (`str | None`, passed to `uv export --package` in workspaces and `None` outside them), the parsed [[src/haumea/config.py#HaumeaConfig]] (`config`), target `python`/`platform`, the function specs, generated static files, and the `config.json` `routes`.
- [[src/haumea/build.py#FunctionSpec]] holds one function's `name` (its path under `functions/`), its [[src/haumea/routing.py#Route]], its `index.py` `entrypoint` source, its `.vc-config.json` content (`vc_config`), its `trace_roots` (project source files to trace, starting with `route.file`), and its normalized route-level `include` distribution names (read via [[src/haumea/build.py#_read_route_include]]).
- [[src/haumea/build.py#StaticFile]] maps a path under `static/` to generated text written over the copied `public/` files.

## Build Output Layout

[[src/haumea/build.py#emit_build]] writes a Build Output v3 tree containing `config.json`, `static/`, and `functions/<route>.func/` to `.vercel/output` by default or to a custom `--output` directory.

The default output location is `<project_dir>/.vercel/output` ([[src/haumea/build.py#OUTPUT_DIR]]), which Vercel Services mode requires so `@vercel/static-build` can move it into `services/<name>/`. [[src/haumea/cli.py#main]] exposes `haumea build --output <dir>` (threaded through [[src/haumea/build.py#build]] and [[src/haumea/build.py#plan_build]]) for tests and standalone CI workflows that stage the directory into `.vercel/output` before `vercel deploy --prebuilt`. [[src/haumea/build.py#_validate_output_dir]] rejects output paths that contain the project or overlap `app/` or `public/` before deleting anything. Function paths are the routes' prefixed `function` names, e.g. `functions/api.func` and `functions/api/users/[id].func` under `base_path = "/api"`.

- **Shared payload & hard-linking**: Project files (excluding [[src/haumea/build.py#IGNORE]] and the output directory via [[src/haumea/build.py#_ignore_path]]) plus the trampoline and the shared dependency store are staged once inside a `.haumea-` temporary directory in the output directory, then hard-linked (`os.link`) into each `.func` directory to save disk space and build time.
- **Dependency vendoring**: [[src/haumea/build.py#_install_dependencies]] runs `uv export --locked` (`--no-dev`, `--no-emit-project`, `--no-editable`, `--no-hashes`) and `uv pip install` from `workspace_root` so a stale or missing [[src/haumea/build.py#UV_LOCK]] fails the build instead of re-resolving and relative workspace paths resolve cleanly, installing dependencies alongside `vercel-runtime` into the staging store for [[src/haumea/build.py#DEFAULT_PLATFORM]] (`x86_64-manylinux_2_28`) and [[src/haumea/build.py#DEFAULT_PYTHON]] (`3.12`), or the host platform when `platform=None`. Because `uvicorn` lives in the `haumea[dev]` extra ([[pyproject.toml]]) and apps list `haumea[dev]` in `[dependency-groups].dev`, `--no-dev` keeps `uvicorn` out of `_vendor/`.
- **uv workspaces**: [[src/haumea/build.py#_find_workspace_root]] walks upward from `project_dir` until it hits a directory with `uv.lock`, returning that directory if it matches [[src/haumea/build.py#_is_uv_workspace]] (declares `[tool.uv.workspace]` in `pyproject.toml`, parsed with [[src/haumea/config.py#read_pyproject]]) or `None` if it does not (stopping the walk at any standalone `uv.lock` boundary). When found, [[src/haumea/build.py#plan_build]] stores it on `workspace_root` and reads `[project].name` via [[src/haumea/build.py#_project_name]] into `package` (falling back to `workspace_root = project_dir` and `package = None` otherwise). [[src/haumea/build.py#emit_build]] passes `plan.workspace_root` and `plan.package` to [[src/haumea/build.py#_install_dependencies]], exporting with `--package <name>` so only the service package is dropped while workspace siblings install non-editably into `_vendor/` and only `project_dir` is copied as app source.
- **Deterministic output**: Generated files contain no build-time timestamps, [[src/haumea/routing.py#discover_routes]] orders routes stably by `(rank, url_path, file)`, and [[src/haumea/build.py#_dump_json]] writes `config.json`, `openapi.json`, and `.vc-config.json` with sorted keys (using [[src/haumea/build.py#_sort_keys]] when a dict mixes key types).
- **Function trampoline**: Each `.func` directory receives `vc__handler__python.py` ([[src/haumea/build.py#TRAMPOLINE_SOURCE]]), `index.py` ([[src/haumea/build.py#ENTRYPOINT_SOURCE]] calling [[src/haumea/routing.py#create_route_app]] with the planned `base_path`), and `.vc-config.json`.

## Dependency Tree-Shaking and Size Reporting

[[src/haumea/build.py#emit_build]] indexes the shared dependency store, traces each function's imports to bundle only the distributions it needs, and reports uncompressed bundle sizes against Vercel's limits.

### Store Index

[[src/haumea/build.py#index_store]] reads every `*.dist-info` directory in the staging store into a [[src/haumea/build.py#StoreIndex]] of [[src/haumea/build.py#Distribution]] records.

- **Normalization**: [[src/haumea/build.py#normalize_dist_name]] normalizes distribution names per PEP 503 (`[-_.]+` collapsed to `-`, lowercased), falling back to the `*.dist-info` directory prefix if `METADATA` omits `Name`.
- **Files and sizes**: [[src/haumea/build.py#_read_record]] parses `RECORD` CSV rows into sorted in-store relative POSIX paths (ignoring absolute or escaping paths), and [[src/haumea/build.py#_distribution_size]] sums uncompressed `lstat` byte sizes across each distribution's recorded files and `*.dist-info` directory.
- **Imports and dependencies**: [[src/haumea/build.py#_top_level_imports]] derives top-level import names from `RECORD` (top-level `.py`/`.so`/`.pyd` module stems and package directories with Python modules, excluding `*.dist-info`, `*.data`, `__pycache__`, and non-package directories like `bin/`) plus `top_level.txt` when present, mapping each import name to sorted distribution names in `by_import` to support namespace packages. [[src/haumea/build.py#_parse_requires_dist]] extracts and normalizes `Requires-Dist` names from `METADATA`, stripping version specifiers, extras, URLs, and environment markers.

### Import Tracing, Closure, and Emit

[[src/haumea/build.py#resolve_distributions]] traces each function's imports, adds always-included and explicit `include` distributions, and expands their transitive `Requires-Dist` closure.

- **Static tracing**: [[src/haumea/build.py#trace_function]] parses each function's generated `entrypoint` and file `trace_roots` with [[src/haumea/build.py#_extract_imports]] and [[src/haumea/build.py#_ImportCollector]]. It follows `import` and `from ... import` anywhere in the module (including function bodies) plus string-literal `importlib.import_module(...)` ([[src/haumea/build.py#_parse_import_module_call]]) and `__import__(...)` ([[src/haumea/build.py#_parse_dunder_import_call]]), skips `if TYPE_CHECKING:` guards ([[src/haumea/build.py#_is_type_checking_guard]]), resolves relative imports and project modules recursively via [[src/haumea/build.py#_resolve_project_modules]] with cycle detection, ignores `sys.stdlib_module_names`, and maps remaining top-level names through `StoreIndex.by_import`.
- **Always-included and closure**: [[src/haumea/build.py#resolve_closure]] seeds from traced distributions, [[src/haumea/build.py#ALWAYS_INCLUDED]] (`haumea`, `vercel-runtime`, `vercel-functions`), project `config.include`, and route `include`, then expands `Requires-Dist` transitively for distributions present in the store (ignoring markers and extras).
- **Selective linking**: When `config.tree_shake` is `True` (default), [[src/haumea/build.py#_link_distributions]] hard-links into `_vendor/` each bundled distribution's in-store `RECORD` files and `*.dist-info` directory without duplicating colliding paths. When `config.tree_shake` is `False`, [[src/haumea/build.py#emit_build]] hard-links the entire store into `_vendor/`.

### Size Report and Limits

[[src/haumea/build.py#_report_functions]] prints each emitted function's uncompressed bundle size ([[src/haumea/build.py#_dir_size]]) and actual bundled distribution count, warns above [[src/haumea/build.py#SIZE_WARN_BYTES]], and fails above [[src/haumea/build.py#SIZE_LIMIT_BYTES]].

Each [[src/haumea/build.py#FunctionReport]] records the function's measured size and bundled distributions (tree-shaken when `tree_shake` is enabled, or the full store when disabled). Above [[src/haumea/build.py#SIZE_WARN_BYTES]] (up to [[src/haumea/build.py#SIZE_LIMIT_BYTES]]), the build emits a `UserWarning` listing the largest bundled distributions via [[src/haumea/build.py#_format_largest_distributions]]; above [[src/haumea/build.py#SIZE_LIMIT_BYTES]], it raises `RuntimeError` with the oversized functions and their largest distributions.

### Build-Time Import Check

[[src/haumea/build.py#_check_function_imports]] verifies each built function's `index.py` imports cleanly in an isolated target-Python subprocess when the target platform matches the host.

- **Platform gate**: [[src/haumea/build.py#_platform_matches_host]] passes when `plan.platform` is `None` (`--platform native`) or [[src/haumea/build.py#_parse_target_platform]] matches [[src/haumea/build.py#_host_os_arch]]. Otherwise the build prints why the check was skipped and continues.
- **Target interpreter**: [[src/haumea/build.py#_find_target_python]] runs `uv python find <plan.python>` from `plan.workspace_root` rather than reusing `sys.executable`; if none is found, the build prints a skip message and continues.
- **Isolated import**: [[src/haumea/build.py#_check_one_function]] runs `<interpreter> -I -S -B` with [[src/haumea/build.py#_IMPORT_CHECK_SCRIPT]] (sharing [[src/haumea/build.py#_VENDOR_SETUP_SOURCE]] with [[src/haumea/build.py#TRAMPOLINE_SOURCE]]) so only stdlib, the function directory, and its `_vendor/` are on `sys.path` and no `__pycache__` files are written. Any `ImportError` (extracted via [[src/haumea/build.py#_parse_import_check_error]]) raises `RuntimeError` naming the function and missing module and suggesting `include`.

## Per-Route Function Config

Route modules can configure their own Vercel function settings and explicit distribution includes using module-level variables.

[[src/haumea/build.py#_plan_function]] reads [[src/haumea/build.py#ROUTE_CONFIG]] (`max_duration` -> `maxDuration`, `memory` -> `memory`, `regions` -> `regions`) from the loaded route module into the function's `vc_config`, cloning nested containers with [[src/haumea/build.py#_clone_config]] so later route imports cannot mutate it without invoking `__deepcopy__` or collapsing keys, alongside `supportsResponseStreaming: true` and `PYTHONDONTWRITEBYTECODE=1`. [[src/haumea/build.py#_read_route_include]] reads module-level `include` (a non-string sequence of non-empty distribution names, normalized via [[src/haumea/build.py#normalize_dist_name]]) onto `FunctionSpec.include`, raising `ValueError` on invalid values.

## Static Docs and Routing Config

API documentation and URL rewrites are generated at build time so runtime functions stay minimal.

- [[src/haumea/build.py#_write_static]] copies `public/` into `static/<base_path>/` with `shutil.copytree` (checking whether `public/` exists at emit time, and creating the directory otherwise); [[src/haumea/build.py#_plan_static]] instantiates the full app via [[src/haumea/routing.py#create_app]] to render `<base_path>/openapi.json` and `<base_path>/docs.html` (Swagger UI loading the prefixed `openapi.json`), which win over same-named `public/` files.
- [[src/haumea/build.py#_plan_routes]] makes `config.json` place `{"handle": "filesystem"}` first (matching `static/` files and static-path `.func` directories), then rewrites `^<base_path>/docs$` to `<base_path>/docs.html` and each dynamic, already-prefixed `route.pattern` to `/{route.function}`.
- [[src/haumea/openapi.py#openapi_schema]] generates OpenAPI 3.1.0 from `Endpoint.request_fields()`, using `msgspec.json.schema_components`, the first line of the handler docstring as `summary`, and adding a `422` response whenever the endpoint has request fields. Return annotations that subclass [[src/haumea/responses.py#Response]] omit a JSON response schema.
