# Haumea

A Python framework for seamless serverless usage

## Design

A serverless-first Python web framework. Its handler API follows the shape FastAPI made familiar (typed signatures, typed models, `Depends`, generated OpenAPI) but is built from the ground up around one-function-per-route deployments. Has native support for Vercel's [Build Outputs API](https://vercel.com/docs/build-output-api).

## Usage

Routes live in an `app/` directory, laid out like the Next.js App Router. Each
`route.py` exports functions named after HTTP methods:

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
from typing import Annotated

from haumea import Depends, Header, HTTPException
from msgspec import Meta, Struct


class Item(Struct):
    name: str
    price: Annotated[float, Meta(gt=0)]


def current_user(authorization: str = Header()) -> str:
    if authorization != "Bearer secret":
        raise HTTPException(401, "Not authenticated")
    return "scott"


def GET(item_id: int, verbose: bool = False):
    return {"item_id": item_id, "verbose": verbose}


async def PUT(item_id: int, item: Item, user: str = Depends(current_user)) -> Item:
    return item
```

A handler's signature declares its inputs, which are validated with
[msgspec](https://jcristharif.com/msgspec/) (invalid requests get a `422`):

- a parameter named after a `[segment]` comes from the path
- a model (a msgspec `Struct`, dataclass or attrs class) comes from the JSON
  body, decoded and validated in a single pass
- `Depends(fn)` is the result of calling `fn`, whose own signature is resolved
  the same way
- a `Request` annotation receives the raw request
- anything else is a query parameter; `Query()`, `Header()` and `Body()`
  override the default

Handlers can be sync or async, and return JSON-serializable data or a
`Response`.

```sh
haumea dev      # local server with reload
haumea build    # writes .vercel/output (Build Output API v3)
vercel deploy --prebuilt
```

`haumea build` deploys each `route.py` as its own function, so routes scale,
cold-start and are configured independently. A route can set per-function
options as module-level values, like Next.js route segment config:

```python
max_duration = 30  # seconds
memory = 1024  # MB
regions = ["iad1"]
```

Framework features that assume one long-lived app are adapted to this model
rather than run inside a function. The API docs are the first example: the
OpenAPI schema and Swagger UI are rendered at build time and served as static
files at `/openapi.json` and `/docs`, straight from the CDN.

Dependencies are resolved from the project's `pyproject.toml` with `uv` and
vendored into every function; `public/` is served as static files. See
[`examples/hello`](examples/hello).

## Development

Tooling is [uv](https://docs.astral.sh/uv/), [ruff](https://docs.astral.sh/ruff/) and [ty](https://docs.astral.sh/ty/):

```sh
uv sync                 # install dependencies
uv run pytest           # tests
uv run ruff format .    # format
uv run ruff check .     # lint
uv run ty check         # type check
```
