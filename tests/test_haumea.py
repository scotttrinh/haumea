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
    assert config["routes"][0] == {"handle": "filesystem"}

    assert (output / "static" / "hello.txt").is_file()

    func = output / "functions" / "index.func"
    vc_config = json.loads((func / ".vc-config.json").read_text())
    assert vc_config["runtime"] == "python3.12"
    assert vc_config["handler"] == "vc__handler__python.vc_handler"
    assert (func / "app" / "api" / "hello" / "route.py").is_file()
    assert (func / "_vendor" / "fastapi").is_dir()
    assert (func / "_vendor" / "haumea").is_dir()
    assert not (func / "public").exists()


INVOKE = """
import json, sys
from vc__handler__python import vc_handler
event = {"body": json.dumps({"method": "GET", "path": sys.argv[1], "headers": {}})}
print(json.dumps(vc_handler(event, None)))
"""


def test_built_function_handles_requests(output):
    """Invoke the function the way Vercel does, using only vendored deps."""
    func = output / "functions" / "index.func"
    result = subprocess.run(
        [sys.executable, "-S", "-c", INVOKE, "/api/items/42?q=1"],
        cwd=func,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout.strip().splitlines()[-1])
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"item_id": 42}
