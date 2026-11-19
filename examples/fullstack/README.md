# fullstack

A Next.js frontend and a Haumea backend deployed together as Vercel Services in
a uv workspace.

## Layout

- `vercel.json` — declares `web` (`frontend/`, Next.js) and `api` (`backend/`,
  Haumea), routing `/api/:path*` to `api` and everything else to `web`.
- `pyproject.toml`, `uv.lock`, `.python-version` — uv workspace root pinned to
  Python 3.12 (matching the `python3.12` Vercel function runtime; `uv run` in a
  workspace reads `.python-version` here).
- `backend/` — Haumea service with `base_path = "/api"`, `haumea[dev]` in
  `[dependency-groups].dev` (so `haumea dev` has `uvicorn` without vendoring
  it into built functions), a backup `backend/.python-version` (`3.12`) for
  standalone commands inside `backend/`, and routes under `app/` (`/api`,
  `/api/hello`, `/api/items/{item_id}`, `/api/docs`, `/api/openapi.json`,
  `/api/hello.txt`).
- `frontend/` — minimal Next.js App Router app serving `/`.

## Local development

Install frontend dependencies once, then run both services together with the
Vercel CLI:

```sh
npm --prefix frontend install
vercel dev --local
```

Visit `http://localhost:3000`:

- `/` is served by Next.js
- `/api`, `/api/hello?name=world`, `/api/items/42`, `/api/docs`,
  `/api/openapi.json`, and `/api/hello.txt` are served by Haumea

You can also run or build the backend directly:

```sh
cd backend
uv run --frozen haumea dev      # http://127.0.0.1:8000/api
uv run --frozen haumea build    # writes backend/.vercel/output
```

## Build and deploy

Because `pyproject.toml` points `haumea` at the repository root
(`haumea = { path = "../..", editable = true }`), deploying from
`examples/fullstack/` alone will not upload `haumea`. Deploy from the
repository root against a Vercel project whose Root Directory is set to
`examples/fullstack` with source files outside the root directory included (for
example, the `haumea-spike` project in `scotttrinhs-projects`):

```sh
# From the repository root:
vercel project update haumea-spike --scope scotttrinhs-projects \
  --root-directory examples/fullstack --source-files-outside-root-directory
vercel deploy -p haumea-spike --scope scotttrinhs-projects
```

To verify a preview deployment with Deployment Protection enabled:

```sh
vercel curl /api --deployment <deployment-url> --scope scotttrinhs-projects
```

To deploy `examples/fullstack` as a standalone repository outside this repo,
either vendor Haumea inside the project (for example
`haumea = { path = "vendor/haumea" }`) or remove `[tool.uv.sources]` once
`haumea` is installed from PyPI, then regenerate `uv.lock` with `uv lock`.
