# Overview

Haumea is a serverless-first Python web framework with FastAPI-style typed handler signatures, built around one-function-per-route deployments for Vercel.

## Serverless-First Model

Haumea adapts web framework features to isolated serverless functions rather than assuming a single long-lived process.

In production builds ([[src/haumea/build.py#build]]), each `route.py` becomes an independent function running [[src/haumea/routing.py#create_route_app]] with docs disabled, while static assets and pre-rendered API docs are served from the platform CDN. In local development, [[src/haumea/routing.py#create_app]] combines all routes, interactive docs, and `public/` static files into one [[src/haumea/app.py#Haumea]] ASGI application.

## Key Design Decisions

Haumea intentionally replaces general-purpose ASGI/validation stacks with a minimal core tailored to cold-start size and build-time generation.

- **Minimal ASGI core**: [[src/haumea/app.py#Haumea]] implements ASGI HTTP and no-op lifespan handling directly instead of depending on FastAPI or Starlette, keeping vendored function payloads small.
- **msgspec over Pydantic**: Request body decoding, parameter conversion, JSON serialization ([[src/haumea/responses.py#JSONResponse]]), and OpenAPI 3.1 schema generation ([[src/haumea/openapi.py#openapi_schema]]) all use `msgspec` for single-pass validation without Pydantic.
- **Config without imports**: `[tool.haumea]` is parsed with `tomllib` before any route loads; its `base_path` prefixes every URL in dev and in the build ([[config]]).
- **Static API docs**: [[src/haumea/build.py#_plan_static]] renders `/openapi.json` and `/docs` (`docs.html`) at build time so deployed functions carry zero docs overhead.

*Note on inferred intent*: The commit history confirms the framework migrated from FastAPI/Pydantic to a custom ASGI + `msgspec` core, but specific cold-start/size benchmarks or plans for non-Vercel deployment targets are not documented in the repository.
