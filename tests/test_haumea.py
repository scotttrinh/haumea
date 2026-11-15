import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from haumea import create_app
from haumea.build import build

EXAMPLE = Path(__file__).parent.parent / "examples" / "hello"


def test_file_system_routes():
    client = TestClient(create_app(EXAMPLE / "app", public_dir=EXAMPLE / "public"))

    assert client.get("/").json() == {"message": "Hello from Haumea"}
    assert client.get("/api/hello?name=jj").json() == {"hello": "jj"}
    assert client.get("/api/items/7").json() == {"item_id": 7}
    assert client.get("/api/items/nope").status_code == 422
    assert client.put("/api/items/7", json={"name": "x", "price": 1.5}).json() == {
        "item_id": 7,
        "name": "x",
        "price": 1.5,
    }
    assert client.post("/api/hello").status_code == 405
    assert "static file" in client.get("/hello.txt").text


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
    assert (func / "_vendor" / "fastapi").is_dir()
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
    assert json.loads(response["body"]) == {"item_id": 42}

    response = invoke(output, "index", "/")
    assert json.loads(response["body"]) == {"message": "Hello from Haumea"}

    # Each function only knows its own route.
    assert invoke(output, "api/hello", "/api/items/42")["statusCode"] == 404
    assert invoke(output, "api/hello", "/docs")["statusCode"] == 404
