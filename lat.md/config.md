# Project Configuration

Project-level settings live in `[tool.haumea]` in the project's `pyproject.toml` and are read without importing user code.

## Config Struct

[[src/haumea/config.py#load_config]] parses `pyproject.toml` with `tomllib` and converts `[tool.haumea]` into the frozen [[src/haumea/config.py#HaumeaConfig]] `msgspec.Struct`.

- `base_path: str = ""` — URL prefix for every route, doc and static file ([[config#Base Path]]).
- `tree_shake: bool = True` — bundle only the distributions each function traces plus always-included sets, or link the whole store when `False` ([[build#Dependency Tree-Shaking and Size Reporting#Import Tracing, Closure, and Emit]]).
- `include: tuple[str, ...] = ()` — distribution names bundled (with their closures) into every function alongside route-level `include` ([[build#Dependency Tree-Shaking and Size Reporting#Import Tracing, Closure, and Emit]]).

A missing table gives the defaults. A non-table `tool` or `tool.haumea`, unknown keys (`forbid_unknown_fields`), wrong types (strict: `tree_shake = 1`, `include = "anyio"` and `include = [1]` all fail) and invalid prefixes raise `ValueError` naming the file. Route-level settings stay in `route.py` ([[build#Per-Route Function Config]]).

## Base Path

`base_path` mounts the whole project under a URL prefix, so a backend can sit behind a service rewrite such as `/api/:path*` beside a frontend.

[[src/haumea/config.py#normalize_base_path]] runs in `HaumeaConfig.__post_init__` and in every consumer: the prefix always starts with `/` and never ends with one, so `"api"` and `"/api/"` become `"/api"`, while `""` and `"/"` mean no prefix.

After stripping outer slashes, every `/`-separated segment must be non-empty, not `.` or `..`, not end in `.func`, and match the [[src/haumea/config.py#BASE_PATH_SEGMENT]] allowlist: ASCII letters, digits and `-._~!+,;=@`. Everything else is rejected, including control characters, whitespace, non-ASCII, `%` (no percent-encoding), quotes, `\`, `<>`, `{}?#`, `$`, `[]` and `()`. The prefix is therefore a literal URL path that is inert in HTML, JS, regexes and Vercel `dest` substitutions, and a safe `static/` subdirectory. The `.func` rule exists because Vercel's Build Output reader treats `functions/<prefix>.func` as a leaf function directory (even without `.vc-config.json`) and stops descending, hiding every function beneath it.

[[src/haumea/openapi.py#swagger_html]] still escapes defensively: the page title goes through `html.escape`, and the `openapi.json` URL through `json.dumps` with `<` written as `\u003c`, so `</script>` or `<!--` cannot end the inline `<script>` early.

The `app/` tree never contains the prefix; moving a project between standalone and service deployments changes only config. With `base_path = "/api"`, `app/route.py` serves `/api`, `app/users/route.py` serves `/api/users`, `public/logo.svg` is `/api/logo.svg`, and the docs are `/api/docs` and `/api/openapi.json` ([[routing#File-System Discovery]], [[build#Static Docs and Routing Config]]).

[[src/haumea/routing.py#create_app]] and [[src/haumea/routing.py#create_route_app]] take `base_path=None` to mean "read it with [[src/haumea/config.py#project_base_path]] from the `pyproject.toml` beside `app_dir`", so `haumea dev` ([[src/haumea/cli.py#dev_app]]) serves the same prefixed URLs as the build. "Beside" uses the absolute but unresolved `app_dir`, so a symlinked `app -> src/app` reads the project root's `pyproject.toml`, as [[src/haumea/build.py#plan_build]] does. Built functions get the planned prefix passed explicitly and never parse TOML at runtime. When `watchfiles` is installed, [[src/haumea/cli.py#main]] also has `haumea dev` reload on `pyproject.toml` changes; uvicorn's fallback reloader only watches `*.py`.

## Full Request Path

[[src/haumea/requests.py#Request#path]] returns the full public path, which is what routes, docs and static files match against.

Haumea reads the scope the way `vercel-runtime`'s prefix strip writes it: `scope["path"]` is relative to `scope["root_path"]` (trailing `/` stripped), and the full path is `root_path + path` with no `startswith` check, because a stripped path can itself begin with the prefix (`/api` + `/api/users` is `/api/api/users`). At the prefix root, when `root_path` is non-empty and `path` is `/`, the full path is `root_path` alone, because the strip maps both `/api` and `/api/` there.

Under Vercel Services today nothing strips the prefix, so `root_path` is empty and `path` is already the full path. The `uvicorn --root-path` convention, where `path` already includes `root_path`, is not supported.

Route patterns match exactly and never end in `/`, so without a strip `/api/` does not match `app/route.py`.

## Unmatched Paths

In dev, any unmatched path inside or outside the prefix gets Haumea's JSON 404 (`{"detail": "Not Found"}`).

Deployed, an unmatched path inside the prefix gets Vercel's `NOT_FOUND` page because no function runs, and paths outside it never reach the service. Only the body differs; clients should rely on the status.
