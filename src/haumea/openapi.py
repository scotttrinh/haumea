"""Generate an OpenAPI 3.1 document, and a Swagger UI page to browse it."""

from __future__ import annotations

import html
import inspect
import json
import re
from typing import TYPE_CHECKING, Any, Literal

import msgspec

from haumea._pydantic import json_schema as pydantic_json_schema
from haumea._pydantic import pydantic_adapter
from haumea.params import Endpoint, RequestInput
from haumea.responses import Response

if TYPE_CHECKING:
    from haumea.app import Haumea

_REF = "#/components/schemas/{name}"
_PYDANTIC_REF = "#/components/schemas/{model}"

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


def _rewrite_refs(node: Any, renames: dict[str, str]) -> Any:
    if isinstance(node, dict):
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value in renames:
                result[key] = renames[value]
            elif key == "discriminator" and isinstance(value, dict):
                disc = _rewrite_refs(value, renames)
                if isinstance(mapping := disc.get("mapping"), dict):
                    disc["mapping"] = {
                        k: renames.get(v, v) if isinstance(v, str) else v
                        for k, v in mapping.items()
                    }
                result[key] = disc
            else:
                result[key] = _rewrite_refs(value, renames)
        return result
    if isinstance(node, list):
        return [_rewrite_refs(item, renames) for item in node]
    return node


def _merge_components(
    schema: dict[str, Any],
    definitions: dict[str, Any],
    components: dict[str, Any],
) -> dict[str, Any]:
    """Merge ``definitions`` into ``components`` without overwriting distinct
    same-named schemas, rewriting ``$ref`` targets when a suffix is allocated."""
    if not definitions:
        return schema

    name_map = {name: name for name in definitions}
    reserved = set(components) | set(definitions)
    while True:
        renames = {
            f"#/components/schemas/{old}": f"#/components/schemas/{new}"
            for old, new in name_map.items()
            if old != new
        }
        rewritten = {
            name_map[old]: _rewrite_refs(def_schema, renames)
            for old, def_schema in definitions.items()
        }
        changed = False
        for old, current in list(name_map.items()):
            if current in components and components[current] != rewritten[current]:
                n = 2
                while f"{old}_{n}" in reserved:
                    n += 1
                fresh = f"{old}_{n}"
                reserved.add(fresh)
                name_map[old] = fresh
                changed = True
        if not changed:
            components.update(rewritten)
            return _rewrite_refs(schema, renames)


def _schema(
    annotation: Any,
    components: dict[str, Any],
    *,
    mode: Literal["validation", "serialization"] = "validation",
) -> dict[str, Any]:
    if (adapter := pydantic_adapter(annotation)) is not None:
        schema = pydantic_json_schema(adapter, ref_template=_PYDANTIC_REF, mode=mode)
        defs = schema.pop("$defs", None) or {}
        return _merge_components(schema, defs, components)
    (schema,), definitions = msgspec.json.schema_components(
        [annotation], ref_template=_REF
    )
    return _merge_components(schema, definitions, components)


def _input_schema(
    request_input: RequestInput, components: dict[str, Any]
) -> dict[str, Any]:
    """The schema every consumer of ``request_input`` accepts: its one
    distinct schema, or the ``allOf`` of them, since each one validates."""
    schemas: list[dict[str, Any]] = []
    for annotation in request_input.annotations:
        if (schema := _schema(annotation, components)) not in schemas:
            schemas.append(schema)
    return schemas[0] if len(schemas) == 1 else {"allOf": schemas}


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

    fields = endpoint.request_inputs()
    parameters = [
        {
            "name": field.alias,
            "in": field.source,
            "required": field.required,
            "schema": _input_schema(field, components),
        }
        for field in fields
        if field.source != "body"
    ]
    if parameters:
        operation["parameters"] = parameters

    body = [field for field in fields if field.source == "body"]
    # Each whole-body field decodes the entire body; embedded ones are keys
    # of one JSON object. A request must satisfy all of them at once.
    parts: list[dict[str, Any]] = []
    for field in body:
        if field.embed:
            continue
        if (part := _input_schema(field, components)) not in parts:
            parts.append(part)
    if embedded := [field for field in body if field.embed]:
        parts.append(
            {
                "type": "object",
                "properties": {f.alias: _input_schema(f, components) for f in embedded},
                "required": [f.alias for f in embedded if f.required],
            }
        )
    body_schema = parts[0] if len(parts) == 1 else {"allOf": parts}
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
            "application/json": {
                "schema": _schema(returns, components, mode="serialization")
            }
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
