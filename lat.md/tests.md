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

Verify `public/` static file serving, path traversal rejection (`404`), `/docs` Swagger UI, and `/openapi.json` schema generation including summaries, parameters, `$ref` schemas, and `Meta` constraints.

## Build output layout

Verify `.vercel/output` `config.json` rewrites, static `docs.html`/`openapi.json`/`public/` files, per-route `.func` directories, `.vc-config.json` `maxDuration` overrides, and `_vendor` contents.

## Built function invocation

Verify built `.func` directories execute standalone via `vc_handler` with `python -S` using only vendored dependencies, handle their own route, and return `404` for other routes and `/docs`.
