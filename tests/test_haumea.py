import asyncio
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from haumea import create_app
from haumea.build import build

EXAMPLE = Path(__file__).parent.parent / "examples" / "hello"
AUTH = {"Authorization": "Bearer secret"}


@pytest.fixture(scope="module")
def client():
    app = create_app(EXAMPLE / "app", public_dir=EXAMPLE / "public")

    def request(method: str, url: str, **kwargs) -> httpx.Response:
        async def send():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as http:
                return await http.request(method, url, **kwargs)

        return asyncio.run(send())

    return request


def test_routes_and_parameters(client):
    assert client("GET", "/").json() == {"message": "Hello from Haumea"}
    assert client("GET", "/api/hello?name=jj").json() == {"hello": "jj"}
    assert client("GET", "/api/items/7?verbose=true").json() == {
        "item_id": 7,
        "verbose": True,
    }
    assert client("HEAD", "/api/hello").content == b""

    response = client("POST", "/api/hello")
    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert client("GET", "/nope").json() == {"detail": "Not Found"}


def test_validation_errors(client):
    response = client("GET", "/api/items/nope")
    assert response.status_code == 422
    assert response.json() == {
        "detail": [{"loc": ["path", "item_id"], "msg": "Expected `int`, got `str`"}]
    }

    response = client("PUT", "/api/items/7", json={"name": "x"}, headers=AUTH)
    assert response.status_code == 422
    assert response.json()["detail"] == [
        {"loc": ["body"], "msg": "Object missing required field `price`"}
    ]

    # Constraints declared with msgspec.Meta are enforced, with a path.
    item = {"name": "x", "price": -1}
    response = client("PUT", "/api/items/7", json=item, headers=AUTH)
    assert response.json()["detail"] == [
        {"loc": ["body", "price"], "msg": "Expected `float` > 0.0"}
    ]

    response = client("PUT", "/api/items/7", content=b"{nope", headers=AUTH)
    assert response.status_code == 422
    assert "malformed" in response.json()["detail"][0]["msg"]

    response = client("PUT", "/api/items/7", headers=AUTH)
    assert response.json()["detail"] == [{"loc": ["body"], "msg": "Field required"}]


def test_body_and_dependencies(client):
    item = {"name": "x", "price": 1.5}
    assert client("PUT", "/api/items/7", json=item, headers=AUTH).json() == item

    response = client("PUT", "/api/items/7", json=item)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["header", "authorization"]

    response = client("PUT", "/api/items/7", json=item, headers={"Authorization": "x"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}


def test_dev_docs_and_static_files(client):
    assert "static file" in client("GET", "/hello.txt").text
    assert client("GET", "/../pyproject.toml").status_code == 404
    assert "swagger" in client("GET", "/docs").text.lower()

    schema = client("GET", "/openapi.json").json()
    put = schema["paths"]["/api/items/{item_id}"]["put"]
    assert put["summary"] == "Replace an item. Requires `Authorization: Bearer secret`."
    assert {(p["name"], p["in"]) for p in put["parameters"]} == {
        ("item_id", "path"),
        ("authorization", "header"),
    }
    ref = {"$ref": "#/components/schemas/Item"}
    assert put["requestBody"]["content"]["application/json"]["schema"] == ref
    assert put["responses"]["200"]["content"]["application/json"]["schema"] == ref
    properties = schema["components"]["schemas"]["Item"]["properties"]
    assert properties["price"] == {"type": "number", "exclusiveMinimum": 0}
    verbose = schema["paths"]["/api/items/{item_id}"]["get"]["parameters"][1]
    assert verbose == {
        "name": "verbose",
        "in": "query",
        "required": False,
        "schema": {"type": "boolean"},
    }


@pytest.fixture(scope="module")
def output():
    # Vendor for this machine so the function can be invoked locally.
    return build(EXAMPLE, platform=None)


def test_build_output_layout(output):
    config = json.loads((output / "config.json").read_text())
    assert config["version"] == 3
    assert config["routes"] == [
        {"handle": "filesystem"},
        {"src": "^/docs$", "dest": "/docs.html"},
        {"src": "^/api/items/([^/]+)$", "dest": "/api/items/[item_id]"},
    ]

    static = output / "static"
    assert (static / "hello.txt").is_file()
    assert "swagger" in (static / "docs.html").read_text().lower()
    schema = json.loads((static / "openapi.json").read_text())
    assert set(schema["paths"]) == {"/", "/api/hello", "/api/items/{item_id}"}

    functions = output / "functions"
    assert {p.relative_to(functions).as_posix() for p in functions.rglob("*.func")} == {
        "index.func",
        "api/hello.func",
        "api/items/[item_id].func",
    }
    func = functions / "api" / "items" / "[item_id].func"
    vc_config = json.loads((func / ".vc-config.json").read_text())
    assert vc_config["runtime"] == "python3.12"
    assert vc_config["handler"] == "vc__handler__python.vc_handler"
    assert vc_config["maxDuration"] == 10
    assert (func / "_vendor" / "haumea").is_dir()
    assert (func / "_vendor" / "msgspec").is_dir()
    assert not (func / "_vendor" / "pydantic").exists()
    assert not (func / "public").exists()

    hello_config = (functions / "api" / "hello.func" / ".vc-config.json").read_text()
    assert "maxDuration" not in json.loads(hello_config)


INVOKE = """
import json, sys
from vc__handler__python import vc_handler
event = {"body": json.dumps({"method": "GET", "path": sys.argv[1], "headers": {}})}
print(json.dumps(vc_handler(event, None)))
"""


def invoke(output, function, path):
    """Invoke a built function the way Vercel does, using only vendored deps."""
    result = subprocess.run(
        [sys.executable, "-S", "-c", INVOKE, path],
        cwd=output / "functions" / f"{function}.func",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_built_functions_handle_their_own_route(output):
    response = invoke(output, "api/items/[item_id]", "/api/items/42?q=1")
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"item_id": 42, "verbose": False}

    response = invoke(output, "index", "/")
    assert json.loads(response["body"]) == {"message": "Hello from Haumea"}

    # Each function only knows its own route.
    assert invoke(output, "api/hello", "/api/items/42")["statusCode"] == 404
    assert invoke(output, "api/hello", "/docs")["statusCode"] == 404
