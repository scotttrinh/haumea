# Haumea

A Python framework for seamless serverless usage

## Design

A familiar FastAPI-like backend optimized for serverless deployments. Has native support for Vercel's [Build Outputs API](https://vercel.com/docs/build-output-api).

## Usage

Routes live in an `app/` directory, laid out like the Next.js App Router. Each
`route.py` exports functions named after HTTP methods, and each of those is a
plain FastAPI path operation function:

```
app/route.py                      ->  /
app/api/hello/route.py            ->  /api/hello
app/api/items/[item_id]/route.py  ->  /api/items/{item_id}
app/files/[...path]/route.py      ->  /files/{path:path}
app/(group)/about/route.py        ->  /about
public/                           ->  static files
```

```python
# app/api/items/[item_id]/route.py
def GET(item_id: int):
    return {"item_id": item_id}
```

```sh
haumea dev      # local server with reload
haumea build    # writes .vercel/output (Build Output API v3)
vercel deploy --prebuilt
```

`haumea build` produces a single Python function containing the app and its
vendored dependencies (resolved from the project's `pyproject.toml` with `uv`),
plus `public/` as static files. See [`examples/hello`](examples/hello).

## Development

Tooling is [uv](https://docs.astral.sh/uv/), [ruff](https://docs.astral.sh/ruff/) and [ty](https://docs.astral.sh/ty/):

```sh
uv sync                 # install dependencies
uv run pytest           # tests
uv run ruff format .    # format
uv run ruff check .     # lint
uv run ty check         # type check
```
