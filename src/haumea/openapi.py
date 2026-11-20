"""Generate an OpenAPI 3.1 document, and a Swagger UI page to browse it."""

from __future__ import annotations

import html
import inspect
import json
import re
from typing import TYPE_CHECKING, Any

import msgspec

from haumea.params import Endpoint
from haumea.responses import Response

if TYPE_CHECKING:
    from haumea.app import Haumea

_REF = "#/components/schemas/{name}"

_VALIDATION_ERROR = {
    "description": "Validation Error",
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "properties": {
                    "detail": {"type": "array", "items": {"type": "object"}}
                },
            }
        }
    },
}


def _schema(annotation: Any, components: dict[str, Any]) -> dict[str, Any]:
    (schema,), definitions = msgspec.json.schema_components(
        [annotation], ref_template=_REF
    )
    components.update(definitions)
    return schema


def _operation(
    method: str,
    path: str,
    endpoint: Endpoint,
    components: dict[str, Any],
    security_schemes: dict[str, Any],
) -> dict[str, Any]:
    doc = inspect.getdoc(endpoint.func) or ""
    operation: dict[str, Any] = {
        "summary": doc.split("\n")[0] or f"{method} {path}",
        "operationId": re.sub(r"\W+", "_", f"{method}_{path}".lower()).strip("_"),
    }
    if "\n" in doc:
        operation["description"] = doc

    fields = list(endpoint.request_fields())
    parameters = [
        {
            "name": field.alias,
            "in": field.source,
            "required": field.required,
            "schema": _schema(field.annotation, components),
        }
        for field in fields
        if field.source != "body"
    ]
    if parameters:
        operation["parameters"] = parameters

    body = [field for field in fields if field.source == "body"]
    if len(body) == 1:
        body_schema = _schema(body[0].annotation, components)
    else:
        body_schema = {
            "type": "object",
            "properties": {f.alias: _schema(f.annotation, components) for f in body},
            "required": [f.alias for f in body if f.required],
        }
    if body:
        operation["requestBody"] = {
            "required": any(field.required for field in body),
            "content": {"application/json": {"schema": body_schema}},
        }

    success: dict[str, Any] = {"description": "Successful Response"}
    returns = inspect.signature(endpoint.func, eval_str=True).return_annotation
    is_response = isinstance(returns, type) and issubclass(returns, Response)
    if returns not in (inspect.Signature.empty, None) and not is_response:
        success["content"] = {
            "application/json": {"schema": _schema(returns, components)}
        }
    responses: dict[int, Any] = {200: success}
    if fields:
        responses[422] = _VALIDATION_ERROR
    for status, description in endpoint.declared_responses().items():
        responses.setdefault(status, {"description": description})
    operation["responses"] = {str(k): v for k, v in sorted(responses.items())}

    # All dependencies run, so every scheme is required together: one
    # requirement object naming each of them.
    schemes = {scheme.name: scheme for scheme in endpoint.security_schemes()}
    if schemes:
        operation["security"] = [{name: [] for name in schemes}]
        for name, scheme in schemes.items():
            security_schemes[name] = scheme.openapi()
    return operation


def openapi_schema(app: Haumea) -> dict[str, Any]:
    components: dict[str, Any] = {}
    security_schemes: dict[str, Any] = {}
    paths: dict[str, Any] = {}
    for path, _, endpoints in app.routes:
        # Components are collected only from documented operations, so a
        # schema or scheme used solely by hidden ones is left out too.
        operations = {
            method.lower(): _operation(
                method, path, endpoint, components, security_schemes
            )
            for method, endpoint in endpoints.items()
            if (path, method) not in app.hidden_operations
        }
        if operations:
            paths[path.replace(":path}", "}")] = operations
    schema: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": app.title, "version": app.version},
        "paths": paths,
    }
    if components:
        schema["components"] = {"schemas": components}
    if security_schemes:
        schema.setdefault("components", {})["securitySchemes"] = security_schemes
    return schema


def swagger_html(title: str, openapi_url: str = "/openapi.json") -> str:
    cdn = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5"
    # A JS string literal that cannot close the inline <script> early.
    url = json.dumps(openapi_url).replace("<", "\\u003c")
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="{cdn}/swagger-ui.css">
</head>
<body>
<div id="swagger-ui"></div>
<script src="{cdn}/swagger-ui-bundle.js"></script>
<script>
SwaggerUIBundle({{ url: {url}, dom_id: "#swagger-ui" }});
</script>
</body>
</html>
"""
