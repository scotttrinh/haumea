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

Verify JSON body decoding alongside `Depends` and `Header` resolution, missing header `422` errors, and `HTTPException` `401` propagation.

## Dev docs and static files

Verify `public/` static file serving, raw-scope path traversal and trailing-slash rejection (`404`), `/docs` Swagger UI, and `/openapi.json` schema generation including summaries, parameters, `$ref` schemas, and `Meta` constraints.

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
