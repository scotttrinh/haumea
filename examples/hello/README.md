# hello

A minimal Haumea app. `pyproject.toml` depends on `haumea` at runtime and adds
`haumea[dev]` to `[dependency-groups].dev` so `haumea dev` has `uvicorn`
locally while `haumea build` omits it from built functions.

```sh
uv run haumea dev      # http://127.0.0.1:8000
uv run haumea build    # writes .vercel/output
vercel deploy --prebuilt
```
