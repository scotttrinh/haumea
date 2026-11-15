"""Generate an OpenAPI 3.1 document, and a Swagger UI page to browse it."""

from __future__ import annotations

import inspect
import re
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, TypeAdapter

from haumea.params import Endpoint
from haumea.responses import Response

if TYPE_CHECKING:
    from haumea.app import Haumea

_REF = "#/components/schemas/{model}"

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
    schema = TypeAdapter(annotation).json_schema(ref_template=_REF)
    components.update(schema.pop("$defs", {}))
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        components[annotation.__name__] = schema
        return {"$ref": _REF.format(model=annotation.__name__)}
    return schema


def _operation(
    method: str, path: str, endpoint: Endpoint, components: dict[str, Any]
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
    operation["responses"] = {"200": success}
    if fields:
        operation["responses"]["422"] = _VALIDATION_ERROR
    return operation


def openapi_schema(app: Haumea) -> dict[str, Any]:
    components: dict[str, Any] = {}
    paths: dict[str, Any] = {}
    for path, _, endpoints in app.routes:
        paths[path.replace(":path}", "}")] = {
            method.lower(): _operation(method, path, endpoint, components)
            for method, endpoint in endpoints.items()
        }
    schema: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": app.title, "version": app.version},
        "paths": paths,
    }
    if components:
        schema["components"] = {"schemas": components}
    return schema


def swagger_html(title: str, openapi_url: str = "/openapi.json") -> str:
    cdn = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5"
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="{cdn}/swagger-ui.css">
</head>
<body>
<div id="swagger-ui"></div>
<script src="{cdn}/swagger-ui-bundle.js"></script>
<script>
SwaggerUIBundle({{ url: "{openapi_url}", dom_id: "#swagger-ui" }});
</script>
</body>
</html>
"""
