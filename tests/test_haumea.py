import asyncio
import gc
import inspect
import json
import re
import subprocess
import sys
import threading
import time
import uuid
import warnings
import weakref
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import httpx
import msgspec
import pytest

from haumea import (
    Cookie,
    Depends,
    Geo,
    Haumea,
    HaumeaConfig,
    HTTPBearer,
    JSONResponse,
    Request,
    Response,
    ServerSentEvent,
    SSEResponse,
    StreamingResponse,
    create_app,
    create_route_app,
    lazy,
    load_config,
    raises,
)
from haumea import build as build_module
from haumea import params as params_module
from haumea import requests as requests_module
from haumea.build import build, emit_build, plan_build
from haumea.cli import main as cli_main
from haumea.config import normalize_base_path
from haumea.openapi import swagger_html
from haumea.routing import discover_routes, load_module

EXAMPLE = Path(__file__).parent.parent / "examples" / "hello"
FULLSTACK = Path(__file__).parent.parent / "examples" / "fullstack"
AUTH = {"Authorization": "Bearer secret"}


def make_client(app, root_path: str = ""):
    def request(method: str, url: str, **kwargs) -> httpx.Response:
        async def send():
            transport = httpx.ASGITransport(app=app, root_path=root_path)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as http:
                return await http.request(method, url, **kwargs)

        return asyncio.run(send())

    return request


def raw_get(app, path: str, root_path: str = "") -> tuple[int, bytes]:
    """Call ``app`` with an unnormalized ``path``, which httpx would clean up."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "root_path": root_path,
        "headers": [],
        "query_string": b"",
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    return sent[0]["status"], body


@pytest.fixture(scope="module")
def example_app():
    return create_app(EXAMPLE / "app", public_dir=EXAMPLE / "public")


@pytest.fixture(scope="module")
def client(example_app):
    return make_client(example_app)


# @lat: [[tests#Routes and parameters]]
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


# @lat: [[tests#Validation errors]]
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


# @lat: [[tests#Body and dependencies]]
def test_body_and_dependencies(client):
    item = {"name": "x", "price": 1.5}
    assert client("PUT", "/api/items/7", json=item, headers=AUTH).json() == item

    response = client("PUT", "/api/items/7", json=item)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"

    headers = {"Authorization": "Bearer wrong"}
    response = client("PUT", "/api/items/7", json=item, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}


# @lat: [[tests#Dev docs and static files]]
def test_dev_docs_and_static_files(client, example_app):
    assert "static file" in client("GET", "/hello.txt").text
    assert raw_get(example_app, "/../pyproject.toml")[0] == 404
    assert raw_get(example_app, "/hello.txt/")[0] == 404
    assert "swagger" in client("GET", "/docs").text.lower()

    schema = client("GET", "/openapi.json").json()
    put = schema["paths"]["/api/items/{item_id}"]["put"]
    assert put["summary"] == "Replace an item. Requires `Authorization: Bearer secret`."
    # The bearer credential is a security scheme, not a header parameter.
    assert [(p["name"], p["in"]) for p in put["parameters"]] == [("item_id", "path")]
    assert put["security"] == [{"HTTPBearer": []}]
    assert schema["components"]["securitySchemes"] == {
        "HTTPBearer": {"type": "http", "scheme": "bearer"}
    }
    assert list(put["responses"]) == ["200", "401", "422"]
    assert put["responses"]["401"] == {"description": "Not authenticated"}
    assert "security" not in schema["paths"]["/api/items/{item_id}"]["get"]
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


@pytest.fixture
def isolated_imports(monkeypatch):
    """Undo the sys.path and sys.modules changes made by loading route modules."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    modules = set(sys.modules)
    yield
    for name in set(sys.modules) - modules:
        del sys.modules[name]


def write_project(project: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_text(content)


# @lat: [[tests#Build plan]]
def test_build_plan(tmp_path, isolated_imports):
    project = tmp_path / "shop"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "shop"\nversion = "0"\n',
            "app/(admin)/orders/route.py": "def GET():\n    return []\n",
            "app/orders/[order_id]/route.py": (
                "max_duration = 30\nmemory = 1024\nregions = ['iad1']\n\n"
                "def GET(order_id: int):\n    return {'id': order_id}\n"
            ),
            "public/css/site.css": "body {}\n",
        },
    )

    plan = plan_build(project, python="3.13")

    assert plan.project_dir == plan.workspace_root == project
    assert plan.package is None
    assert plan.app_dir == project / "app"
    assert plan.public_dir == project / "public"
    assert plan.output_dir == project / ".vercel" / "output"
    assert not plan.output_dir.exists()

    assert [f.name for f in plan.functions] == ["orders", "orders/[order_id]"]
    assert [f.route.url_path for f in plan.functions] == [
        "/orders",
        "/orders/{order_id}",
    ]
    orders, order = plan.functions
    assert plan.config == HaumeaConfig()
    assert "create_route_app(" in order.entrypoint
    assert "'orders/[order_id]',\n    base_path='',\n)" in order.entrypoint
    assert order.vc_config == {
        "runtime": "python3.13",
        "handler": "vc__handler__python.vc_handler",
        "supportsResponseStreaming": True,
        "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
        "maxDuration": 30,
        "memory": 1024,
        "regions": ["iad1"],
    }
    assert set(orders.vc_config) == {
        "runtime",
        "handler",
        "supportsResponseStreaming",
        "environment",
    }
    assert orders.trace_roots == (orders.route.file,)
    assert orders.include == ()
    assert order.trace_roots == (order.route.file,)
    assert order.include == ()

    static = {f.path: f.content for f in plan.static_files}
    assert set(static) == {"openapi.json", "docs.html"}
    assert set(json.loads(static["openapi.json"])["paths"]) == {
        "/orders",
        "/orders/{order_id}",
    }
    assert "swagger" in static["docs.html"].lower()

    assert plan.routes == (
        {"handle": "filesystem"},
        {"src": "^/docs$", "dest": "/docs.html"},
        {"src": "^/orders/([^/]+)$", "dest": "/orders/[order_id]"},
    )


# @lat: [[tests#Build emit]]
def test_emit_build_preserves_public_and_config_isolation(
    tmp_path, isolated_imports, monkeypatch
):
    project = tmp_path / "snap"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "snap"\nversion = "0"\n',
            "snap_config.py": (
                "REGIONS = ['iad1']\n\n"
                "class Regions(list):\n"
                "    def __deepcopy__(self, memo):\n"
                "        raise TypeError('no deepcopy')\n"
            ),
            "app/a/route.py": "from snap_config import REGIONS as regions\n",
            "app/b/route.py": (
                "from pathlib import Path\nfrom snap_config import REGIONS\n"
                "REGIONS.append('sfo1')\n"
                "(Path(__file__).parents[2] / 'public' / 'late.txt').write_text('x')\n"
            ),
            "app/c/route.py": (
                "from snap_config import Regions\nregions = Regions(['cdg1'])\n"
            ),
            "app/d/route.py": "regions = {1: 'first', '1': 'second'}\n",
            "assets/logo.svg": "<svg/>",
        },
    )
    (project / "public" / "empty").mkdir(parents=True)
    (project / "public" / "empty").chmod(0o750)
    (project / "public" / "linked").symlink_to(project / "assets")

    plan = plan_build(project)
    assert plan.functions[0].vc_config["regions"] == ["iad1"]
    assert plan.functions[2].vc_config["regions"] == ["cdg1"]

    monkeypatch.setattr(build_module, "_install_dependencies", lambda *args: None)
    monkeypatch.setattr(build_module, "_check_function_imports", lambda *args: None)
    output = emit_build(plan)

    static = output / "static"
    assert (static / "linked" / "logo.svg").read_text() == "<svg/>"
    assert (static / "empty").stat().st_mode & 0o777 == 0o750
    # Like the original builder, public/ is copied after route imports.
    assert (static / "late.txt").read_text() == "x"
    functions = output / "functions"
    assert (functions / "b.func" / "app" / "b" / "route.py").is_file()
    a_config = json.loads((functions / "a.func" / ".vc-config.json").read_text())
    assert a_config["regions"] == ["iad1"]
    c_config = json.loads((functions / "c.func" / ".vc-config.json").read_text())
    assert c_config["regions"] == ["cdg1"]
    d_raw = (functions / "d.func" / ".vc-config.json").read_text()
    assert d_raw.count('"1":') == 2

    # public/ created during route imports is also discovered at emit time.
    late_public = tmp_path / "late_public"
    write_project(
        late_public,
        {
            "pyproject.toml": '[project]\nname = "late"\nversion = "0"\n',
            "app/late/route.py": (
                "from pathlib import Path\n"
                "pub = Path(__file__).parents[2] / 'public'\n"
                "pub.mkdir(exist_ok=True)\n"
                "(pub / 'late.txt').write_text('created')\n"
            ),
        },
    )
    late_output = emit_build(plan_build(late_public))
    assert (late_output / "static" / "late.txt").read_text() == "created"


@pytest.fixture(scope="module")
def output():
    # Vendor for this machine so the function can be invoked locally.
    return build(EXAMPLE, platform=None)


# @lat: [[tests#Build output layout]]
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
    assert not any(p.name.startswith("uvicorn") for p in (func / "_vendor").iterdir())
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


# @lat: [[tests#Built function invocation]]
def test_built_functions_handle_their_own_route(output):
    response = invoke(output, "api/items/[item_id]", "/api/items/42?q=1")
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"item_id": 42, "verbose": False}

    response = invoke(output, "index", "/")
    assert json.loads(response["body"]) == {"message": "Hello from Haumea"}

    # Each function only knows its own route.
    assert invoke(output, "api/hello", "/api/items/42")["statusCode"] == 404
    assert invoke(output, "api/hello", "/docs")["statusCode"] == 404


# @lat: [[tests#Project config]]
def test_project_config(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\n')
    assert load_config(pyproject) == HaumeaConfig(
        base_path="", tree_shake=True, include=()
    )

    pyproject.write_text(
        '[tool.haumea]\nbase_path = "api/"\ntree_shake = false\ninclude = ["anyio"]\n'
    )
    assert load_config(pyproject) == HaumeaConfig(
        base_path="/api", tree_shake=False, include=("anyio",)
    )

    for raw, normalized in [
        ("", ""),
        ("/", ""),
        ("/api/", "/api"),
        ("/api", "/api"),
        ("/v1/api", "/v1/api"),
        ("/v1+/a-b_c.d~e!,;=@", "/v1+/a-b_c.d~e!,;=@"),
    ]:
        assert HaumeaConfig(base_path=raw).base_path == normalized

    for table, error in [
        ("base_pth = '/api'", "unknown field `base_pth`"),
        ("base_path = 1", "Expected `str`, got `int`"),
        ("base_path = '/a//b'", "invalid base_path"),
        ("base_path = '/../up'", "invalid base_path"),
        ("base_path = '/{x}'", "invalid base_path"),
        ("base_path = '/a/./b'", "invalid base_path"),
        ("base_path = '/a\\u0000b'", "invalid base_path"),
        ("base_path = '/a\\nb'", "invalid base_path"),
        ("base_path = '/a\\u007fb'", "invalid base_path"),
        ("base_path = '/a b'", "invalid base_path"),
        ("base_path = '/a%2fb'", "invalid base_path"),
        ("base_path = '/a\"b'", "invalid base_path"),
        ('base_path = "/a\'b"', "invalid base_path"),
        ("base_path = '/a\\\\b'", "invalid base_path"),
        ("base_path = '/<script>'", "invalid base_path"),
        ("base_path = '/a?b'", "invalid base_path"),
        ("base_path = '/a#b'", "invalid base_path"),
        ("base_path = '/$1'", "invalid base_path"),
        ("base_path = '/caf\u00e9'", "invalid base_path"),
        ("base_path = '/svc.func'", "invalid base_path"),
        ("base_path = '/v1/svc.func/'", "invalid base_path"),
        ("tree_shake = 1", "Expected `bool`, got `int`"),
        ("include = 'anyio'", "Expected `array`, got `str`"),
        ("include = [1]", "Expected `str`, got `int`"),
    ]:
        pyproject.write_text(f"[tool.haumea]\n{table}\n")
        with pytest.raises(ValueError, match=error):
            load_config(pyproject)

    for toml, error in [
        ("tool = true", "`tool` must be a table"),
        ("[tool]\nhaumea = true", "`tool.haumea` must be a table"),
    ]:
        pyproject.write_text(f"{toml}\n")
        with pytest.raises(ValueError, match=f"invalid \\[tool.haumea\\] .*{error}"):
            load_config(pyproject)

    with pytest.raises(ValueError, match="invalid base_path"):
        HaumeaConfig(base_path="/a\x00b")
    # functions/svc.func would be read as a function, not a directory.
    with pytest.raises(ValueError, match="invalid base_path"):
        normalize_base_path("/svc.func")
    assert normalize_base_path("/svc.funcs/x.func.d") == "/svc.funcs/x.func.d"


def test_swagger_html_escapes_title_and_url():
    page = swagger_html("<b>&", '/x"y</script><!--')
    assert "<title>&lt;b&gt;&amp;</title>" in page
    script = page.split("<script>\n", 1)[1].split("\n</script>", 1)[0]
    assert "<" not in script
    assert 'url: "/x\\"y\\u003c/script>\\u003c!--"' in script
    url = script.split("url: ", 1)[1].split(", dom_id", 1)[0]
    assert json.loads(url) == '/x"y</script><!--'


PREFIXED_FILES = {
    "pyproject.toml": (
        '[project]\nname = "prefixed"\nversion = "0"\n\n'
        '[tool.haumea]\nbase_path = "/api/"\n'
    ),
    "app/route.py": "def GET():\n    return {'route': 'root'}\n",
    "app/users/route.py": "def GET():\n    return {'route': 'users'}\n",
    "app/users/[id]/route.py": (
        "from haumea import Request\n\n"
        "def GET(id: int, request: Request):\n"
        "    return {'id': id, 'path': request.path}\n"
    ),
    "public/hello.txt": "prefixed static\n",
}


@pytest.fixture(scope="module")
def prefixed_project(tmp_path_factory):
    project = tmp_path_factory.mktemp("prefixed")
    write_project(project, PREFIXED_FILES)
    return project


# @lat: [[tests#Prefixed dev routing]]
def test_prefixed_dev_routing(prefixed_project, isolated_imports):
    # base_path comes from [tool.haumea] beside app/, as in `haumea dev`.
    app = create_app(prefixed_project / "app", public_dir=prefixed_project / "public")
    assert app.base_path == "/api"
    client = make_client(app)

    assert client("GET", "/api").json() == {"route": "root"}
    assert client("GET", "/api/users").json() == {"route": "users"}
    assert client("GET", "/api/users/7").json() == {"id": 7, "path": "/api/users/7"}
    assert client("GET", "/api/hello.txt").text == "prefixed static\n"
    assert raw_get(app, "/api/../pyproject.toml")[0] == 404
    assert raw_get(app, "/api/../../pyproject.toml")[0] == 404

    docs = client("GET", "/api/docs").text
    assert docs == swagger_html("Haumea", "/api/openapi.json")
    schema = client("GET", "/api/openapi.json").json()
    assert set(schema["paths"]) == {"/api", "/api/users", "/api/users/{id}"}

    # Outside the prefix (or on its trailing-slash root): Haumea's JSON 404.
    for path in ["/", "/api/", "/users", "/hello.txt", "/apiary/hello.txt", "/docs"]:
        response = client("GET", path)
        assert response.status_code == 404, path
        assert response.json() == {"detail": "Not Found"}

    # An explicit base_path overrides the config.
    plain = make_client(create_app(prefixed_project / "app", base_path=""))
    assert plain("GET", "/users").json() == {"route": "users"}


# @lat: [[tests#Root path reconstruction]]
def test_root_path_reconstruction(prefixed_project, isolated_imports):
    async def receive():
        return {"type": "http.request"}

    for root_path, path, full in [
        ("", "/", "/"),
        ("", "/api/users", "/api/users"),
        ("/api", "/", "/api"),
        ("/api/", "/", "/api"),
        ("/api", "/users", "/api/users"),
        ("/api", "/apiary", "/api/apiary"),
        ("/api", "/api/users", "/api/api/users"),
    ]:
        scope = {"path": path, "root_path": root_path}
        assert Request(scope, receive).path == full
    assert Request({"path": "/x"}, receive).path == "/x"

    app = create_app(prefixed_project / "app")

    # What vercel-runtime's prefix strip produces for both /api and /api/.
    assert make_client(app, "/api")("GET", "/").json() == {"route": "root"}
    # Without a strip, /api/ is a literal miss.
    assert make_client(app, "")("GET", "/api/").status_code == 404
    assert make_client(app, "/api")("GET", "/users").json() == {"route": "users"}
    assert make_client(app, "/api/")("GET", "/users/3").json() == {
        "id": 3,
        "path": "/api/users/3",
    }
    # No startswith check: a stripped path may itself begin with the prefix.
    assert make_client(app, "/api")("GET", "/apiary").status_code == 404
    response = make_client(app, "/api")("GET", "/api/users")
    assert response.status_code == 404

    route_app = create_route_app(prefixed_project / "app", "api/users/[id]")
    route_client = make_client(route_app, "/api")
    assert route_client("GET", "/users/3").json() == {"id": 3, "path": "/api/users/3"}
    assert route_client("GET", "/").status_code == 404
    response = route_client("GET", "/api/users/3")
    assert response.status_code == 404
    response = route_client("GET", "/apiary/x")
    assert response.status_code == 404


@pytest.fixture(scope="module")
def prefixed_output(prefixed_project):
    install = build_module._install_dependencies

    def install_like_example(
        workspace_root, vendor_dir, python, platform, package=None
    ):
        # Same dependencies as examples/hello, which has a lock to export.
        install(EXAMPLE, vendor_dir, python, platform)

    path, modules = list(sys.path), set(sys.modules)
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(build_module, "_install_dependencies", install_like_example)
            patch.setattr(sys, "dont_write_bytecode", True)
            return build(prefixed_project, platform=None)
    finally:
        sys.path[:] = path
        for name in set(sys.modules) - modules:
            del sys.modules[name]


# @lat: [[tests#Prefixed build output]]
def test_prefixed_build_output(prefixed_project, prefixed_output):
    output = prefixed_output
    assert output == prefixed_project / ".vercel" / "output"

    config = json.loads((output / "config.json").read_text())
    assert config["routes"] == [
        {"handle": "filesystem"},
        {"src": "^/api/docs$", "dest": "/api/docs.html"},
        {"src": "^/api/users/([^/]+)$", "dest": "/api/users/[id]"},
    ]

    static = output / "static"
    assert {p.relative_to(static).as_posix() for p in static.rglob("*")} == {
        "api",
        "api/hello.txt",
        "api/docs.html",
        "api/openapi.json",
    }
    docs = (static / "api" / "docs.html").read_text()
    assert docs == swagger_html(prefixed_project.name, "/api/openapi.json")
    schema = json.loads((static / "api" / "openapi.json").read_text())
    assert set(schema["paths"]) == {"/api", "/api/users", "/api/users/{id}"}

    functions = output / "functions"
    assert {p.relative_to(functions).as_posix() for p in functions.rglob("*.func")} == {
        "api.func",
        "api/users.func",
        "api/users/[id].func",
    }

    response = invoke(output, "api", "/api")
    assert json.loads(response["body"]) == {"route": "root"}
    response = invoke(output, "api/users/[id]", "/api/users/5")
    assert json.loads(response["body"]) == {"id": 5, "path": "/api/users/5"}
    assert invoke(output, "api/users", "/users")["statusCode"] == 404


# @lat: [[tests#Dev routing priority]]
def test_dev_routing_priority(tmp_path, isolated_imports):
    project = tmp_path / "catchall"
    write_project(
        project,
        {
            "pyproject.toml": '[tool.haumea]\nbase_path = "/api"\n',
            "app/[...item]/route.py": (
                "def GET(item: str):\n    return {'item': item}\n"
            ),
            "app/docs/route.py": "def POST():\n    return {'route': 'docs'}\n",
            "app/users/route.py": "def GET():\n    return {'route': 'users'}\n",
            "public/hello.txt": "static\n",
            "public/users": "shadowed\n",
        },
    )
    (project / "public" / "loop").symlink_to(project / "public" / "loop")
    app = create_app(project / "app", public_dir=project / "public")
    client = make_client(app)

    # The filesystem check and docs rewrite run before dynamic routes.
    assert client("GET", "/api/hello.txt").text == "static\n"
    assert client("HEAD", "/api/hello.txt").content == b""
    assert client("GET", "/api/openapi.json").json()["openapi"] == "3.1.0"
    assert "swagger" in client("GET", "/api/docs.html").text.lower()
    # Literal routes still run, ahead of files and docs, like a static-path
    # function in the filesystem check: GET /api/docs is that function's 405.
    assert client("GET", "/api/users").json() == {"route": "users"}
    assert client("POST", "/api/docs").json() == {"route": "docs"}
    response = client("GET", "/api/docs")
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"
    # Anything else falls through to the catch-all.
    assert client("GET", "/api/a/b").json() == {"item": "a/b"}
    assert client("POST", "/api/a").headers["allow"] == "GET"

    # Directory-style, looping and traversing paths are not files.
    for path in ["/api/hello.txt/", "/api/loop", "/api/../pyproject.toml"]:
        status, body = raw_get(app, path)
        assert status == 200, path
        assert json.loads(body) == {"item": path.removeprefix("/api/")}

    plain = create_app(project / "app", public_dir=project / "public", base_path="")
    for path in ["/hello.txt/", "/loop", "/../pyproject.toml", "/a\x00b"]:
        assert raw_get(plain, path)[0] == 200, path
    assert raw_get(plain, "/hello.txt")[1] == b"static\n"


# @lat: [[tests#Dev docs precedence]]
def test_dev_docs_precedence(tmp_path, isolated_imports):
    project = tmp_path / "docsfiles"
    write_project(
        project,
        {
            "pyproject.toml": '[tool.haumea]\nbase_path = "/api"\n',
            "app/[...item]/route.py": (
                "def GET(item: str):\n    return {'get': item}\n\n"
                "def POST(item: str):\n    return {'post': item}\n"
            ),
            "public/docs": "static docs\n",
            "public/docs.html": "stale docs\n",
            "public/openapi.json": "{}\n",
        },
    )
    app = create_app(project / "app", public_dir=project / "public")
    client = make_client(app)
    swagger = swagger_html("Haumea", "/api/openapi.json")

    # public/docs is found by the filesystem check, before the docs rewrite;
    # the generated docs.html and openapi.json overwrite their public/ copies.
    assert client("GET", "/api/docs").text == "static docs\n"
    assert client("GET", "/api/docs.html").text == swagger
    assert client("GET", "/api/openapi.json").json()["openapi"] == "3.1.0"
    assert client("HEAD", "/api/docs.html").content == b""

    # Method checks run before generating the schema or reading file bytes.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(app, "openapi", lambda: pytest.fail("openapi() called on POST"))
        patch.setattr(
            Path, "read_bytes", lambda self: pytest.fail("read_bytes() called on POST")
        )
        # Files and docs answer other methods with 405, not the catch-all.
        for path in ["/api/docs", "/api/docs.html", "/api/openapi.json"]:
            response = client("POST", path)
            assert response.status_code == 405, path
            assert response.headers["allow"] == "GET, HEAD"
    assert client("POST", "/api/other").json() == {"post": "other"}

    # Without public/docs, /api/docs is the generated Swagger UI.
    bare = make_client(create_app(project / "app"))
    assert bare("GET", "/api/docs").text == swagger
    assert bare("POST", "/api/docs").headers["allow"] == "GET, HEAD"
    assert bare("POST", "/api/a/b").json() == {"post": "a/b"}


# @lat: [[tests#Route load cache]]
def test_route_load_cache(tmp_path, isolated_imports):
    write_project(
        tmp_path,
        {
            "app/route.py": "def GET():\n    return {}\n",
            "app/bad/route.py": "raise RuntimeError('boom')\n",
        },
    )
    plain, prefixed = (
        {r.url_path: r for r in discover_routes(tmp_path / "app", base)}
        for base in ["", "/v1"]
    )
    # One file under two prefixes is one module.
    assert plain["/"].function != prefixed["/v1"].function
    assert plain["/"].load() is prefixed["/v1"].load()

    before = set(sys.modules)
    for _ in range(2):  # retried, not served from a partial cache entry
        with pytest.raises(RuntimeError, match="boom"):
            prefixed["/v1/bad"].load()
        assert set(sys.modules) == before


# @lat: [[tests#Route segments]]
def test_route_segments(tmp_path, isolated_imports):
    write_project(
        tmp_path,
        {
            name: "def GET():\n    return {}\n"
            for name in [
                "app/route.py",
                "app/a/b/route.py",
                "app/(admin)/users/route.py",
                "app/items/[id]/route.py",
                "app/files/[...path]/route.py",
            ]
        },
    )
    app = (tmp_path / "app").resolve()
    for base in ["", "/v1"]:
        segments = {
            r.file.relative_to(app).as_posix(): r.segments
            for r in discover_routes(tmp_path / "app", base)
        }
        assert segments == {
            "route.py": (app,),
            "a/b/route.py": (app, app / "a", app / "a" / "b"),
            "(admin)/users/route.py": (app, app / "(admin)", app / "(admin)" / "users"),
            "items/[id]/route.py": (app, app / "items", app / "items" / "[id]"),
            "files/[...path]/route.py": (
                app,
                app / "files",
                app / "files" / "[...path]",
            ),
        }


def logging_segment(log: Path, name: str) -> str:
    """Source for a segment file that appends ``name`` to ``log`` when run."""
    return (
        f"NAME = {name!r}\n"
        f"with open({str(log)!r}, 'a') as f:\n"
        "    f.write(NAME + '\\n')\n"
    )


# @lat: [[tests#Ancestor segment loading]]
def test_route_load_segments(tmp_path, isolated_imports):
    log = tmp_path / "runs.log"
    write_project(
        tmp_path,
        {
            "app/segment.py": logging_segment(log, "root"),
            "app/admin/segment.py": logging_segment(log, "admin"),
            "app/admin/users/route.py": "def GET():\n    return {}\n",
            "app/admin/(group)/settings/route.py": "def GET():\n    return {}\n",
            "app/public/segment.py": logging_segment(log, "public"),
            "app/public/route.py": "def GET():\n    return {}\n",
        },
    )
    routes = {r.url_path: r for r in discover_routes(tmp_path / "app")}

    users = routes["/admin/users"].load_segments("segment.py")
    assert [m.NAME for m in users] == ["root", "admin"]  # root to leaf
    assert log.read_text().split() == ["root", "admin"]  # sibling not loaded

    # A route sharing those ancestors reuses their modules.
    settings = routes["/admin/settings"].load_segments("segment.py")
    assert all(a is b for a, b in zip(settings, users, strict=True))
    assert log.read_text().split() == ["root", "admin"]

    public = routes["/public"].load_segments("segment.py")
    assert [m.NAME for m in public] == ["root", "public"]
    assert public[0] is users[0]
    assert log.read_text().split() == ["root", "admin", "public"]

    assert routes["/admin/users"].load_segments("middleware.py") == []


# @lat: [[tests#Segment module loading]]
def test_segment_module_loading(tmp_path, isolated_imports):
    write_project(
        tmp_path,
        {
            "app/segment.py": logging_segment(tmp_path / "runs.log", "root"),
            "app/bad.py": "raise RuntimeError('boom')\n",
            "app/one/route.py": "def GET():\n    return {}\n",
            "app/two/route.py": "def GET():\n    return {}\n",
        },
    )
    routes = discover_routes(tmp_path / "app")
    assert len(routes) == 2
    # Sibling routes share their root segment, so its file loads once.
    modules = {load_module(r.segments[0] / "segment.py") for r in routes}
    assert len(modules) == 1
    assert load_module(tmp_path / "app" / "segment.py") in modules
    assert (tmp_path / "runs.log").read_text().split() == ["root"]

    # A route file and its module share the cache.
    assert load_module(routes[0].file) is routes[0].load()

    before = set(sys.modules)
    for _ in range(2):  # retried, not served from a partial cache entry
        with pytest.raises(RuntimeError, match="boom"):
            load_module(routes[0].segments[0] / "bad.py")
        assert set(sys.modules) == before


def middleware_source(name: str) -> str:
    """Source for a ``middleware.py`` that records ``name>`` on the way in and
    ``<name:status`` on the way out in ``mwlog.EVENTS``. ``x-stop: name``
    short-circuits it with a 418; ``x-raise: name`` makes it raise
    ``x-raise-kind`` (``http``, ``validation`` or anything else)."""
    return (
        "import mwlog\n"
        "from haumea import HTTPException, JSONResponse, RequestValidationError\n\n"
        "async def middleware(request, call_next):\n"
        f"    mwlog.EVENTS.append({name + '>'!r})\n"
        f"    if request.headers.get('x-stop') == {name!r}:\n"
        f"        return JSONResponse({{'stopped': {name!r}}}, 418)\n"
        f"    if request.headers.get('x-raise') == {name!r}:\n"
        "        kind = request.headers.get('x-raise-kind')\n"
        "        if kind == 'http':\n"
        f"            raise HTTPException(403, {name!r}, {{'x-why': 'mw'}})\n"
        "        if kind == 'validation':\n"
        f"            raise RequestValidationError([{{'msg': {name!r}}}])\n"
        f"        raise RuntimeError({name!r})\n"
        "    response = await call_next(request)\n"
        f"    mwlog.EVENTS.append(f'<{name}:{{response.status_code}}')\n"
        f"    response.headers['x-seen-' + {name!r}] = str(response.status_code)\n"
        "    return response\n"
    )


MIDDLEWARE_ROUTE = (
    "import mwlog\n"
    "from haumea import HTTPException\n\n"
    "def GET(n: int = 0, fail: str = ''):\n"
    "    mwlog.EVENTS.append('handler')\n"
    "    if fail == '401':\n"
    "        raise HTTPException(401, 'nope', {'www-authenticate': 'Bearer'})\n"
    "    if fail == 'boom':\n"
    "        raise RuntimeError('boom')\n"
    "    return {'n': n}\n"
)


@pytest.fixture
def middleware_project(tmp_path, isolated_imports):
    """A project with root, ``(group)`` and leaf middleware, a sibling with
    only the root's, and two dynamic routes that overlap on ``/items/x``.

    Yields ``(serve, events)``. ``serve(factory, method, path)`` sends one
    request to ``create_app`` (``factory="dev"``) or to the
    ``create_route_app`` for ``function`` (``factory="route"``)."""
    write_project(
        tmp_path,
        {
            "mwlog.py": "EVENTS = []\n",
            "app/middleware.py": middleware_source("root"),
            "app/(group)/middleware.py": middleware_source("group"),
            "app/(group)/admin/middleware.py": middleware_source("admin"),
            "app/(group)/admin/route.py": MIDDLEWARE_ROUTE,
            "app/public/route.py": MIDDLEWARE_ROUTE,
            "app/items/[id]/middleware.py": middleware_source("id"),
            "app/items/[id]/route.py": MIDDLEWARE_ROUTE,
            "app/items/[...rest]/middleware.py": middleware_source("rest"),
            "app/items/[...rest]/route.py": (
                "def POST(rest: str):\n    return {'rest': rest}\n"
            ),
            "public/hello.txt": "static\n",
        },
    )
    app_dir = tmp_path / "app"
    dev = create_app(app_dir, public_dir=tmp_path / "public")
    events: list[str] = sys.modules["mwlog"].EVENTS  # imported by middleware.py

    def serve(
        factory, method, path, function: str | None = None, **kwargs
    ) -> httpx.Response:
        if factory == "dev":
            app = dev
        else:
            assert function is not None
            app = create_route_app(app_dir, function)
        events.clear()
        return make_client(app)(method, path, **kwargs)

    return serve, events


FACTORIES = [
    pytest.param("dev", id="create_app"),
    pytest.param("route", id="create_route_app"),
]


# @lat: [[tests#Middleware#Runs outer to inner]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_middleware_order(middleware_project, factory):
    serve, events = middleware_project
    response = serve(factory, "GET", "/admin?n=3", function="admin")
    assert response.json() == {"n": 3}
    assert events == [
        "root>",
        "group>",
        "admin>",
        "handler",
        "<admin:200",
        "<group:200",
        "<root:200",
    ]
    assert response.headers["x-seen-root"] == "200"

    # A sibling outside the group runs only the root's middleware.
    assert serve(factory, "GET", "/public", function="public").json() == {"n": 0}
    assert events == ["root>", "handler", "<root:200"]


# @lat: [[tests#Middleware#Short-circuits]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_middleware_short_circuit(middleware_project, factory):
    serve, events = middleware_project
    headers = {"x-stop": "group"}
    response = serve(factory, "GET", "/admin", function="admin", headers=headers)
    assert response.status_code == 418
    assert response.json() == {"stopped": "group"}
    assert events == ["root>", "group>", "<root:418"]  # no admin, no handler


# @lat: [[tests#Middleware#Sees the error response]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_middleware_sees_error_response(middleware_project, factory):
    serve, events = middleware_project
    chain = ["root>", "group>", "admin>"]

    response = serve(factory, "GET", "/admin?fail=401", function="admin")
    assert response.status_code == 401
    assert response.json() == {"detail": "nope"}
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["x-seen-admin"] == "401"
    assert events == [*chain, "handler", "<admin:401", "<group:401", "<root:401"]

    response = serve(factory, "GET", "/admin?n=bad", function="admin")
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "n"]
    assert events == [*chain, "<admin:422", "<group:422", "<root:422"]

    response = serve(factory, "GET", "/admin?fail=boom", function="admin")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert response.headers["x-seen-root"] == "500"
    assert events == [*chain, "handler", "<admin:500", "<group:500", "<root:500"]


# @lat: [[tests#Middleware#Middleware errors get the default mapping]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_middleware_exception(middleware_project, factory):
    serve, events = middleware_project

    def raising(kind: str) -> httpx.Response:
        headers = {"x-raise": "group", "x-raise-kind": kind}
        return serve(factory, "GET", "/admin", function="admin", headers=headers)

    response = raising("http")
    assert response.status_code == 403
    assert response.json() == {"detail": "group"}
    assert response.headers["x-why"] == "mw"
    assert events == ["root>", "group>"]  # propagates past the outer layers

    response = raising("validation")
    assert response.status_code == 422
    assert response.json() == {"detail": [{"msg": "group"}]}

    response = raising("other")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert "x-seen-root" not in response.headers


# @lat: [[tests#Middleware#Skips unmatched paths but runs on 405]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_middleware_unmatched_and_405(middleware_project, factory):
    serve, events = middleware_project

    # A literal route's 405 runs through its own chain.
    response = serve(factory, "POST", "/admin", function="admin")
    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert response.headers["x-seen-admin"] == "405"
    assert events == [
        "root>",
        "group>",
        "admin>",
        "<admin:405",
        "<group:405",
        "<root:405",
    ]

    # /items/x matches [id] first, as the platform's first rewrite would, so
    # POST is that function's 405 rather than the catch-all's handler.
    response = serve(factory, "POST", "/items/x", function="items/[id]")
    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert events == ["root>", "id>", "<id:405", "<root:405"]
    response = serve(factory, "POST", "/items/x/y", function="items/[...rest]")
    assert response.json() == {"rest": "x/y"}
    assert events == ["root>", "rest>", "<rest:200", "<root:200"]

    # No route, no segment files.
    response = serve(factory, "GET", "/nope", function="admin")
    assert response.status_code == 404
    assert events == []


# @lat: [[tests#Middleware#Skips dev files]]
def test_middleware_skips_dev_files(middleware_project):
    serve, events = middleware_project
    assert serve("dev", "GET", "/hello.txt").text == "static\n"
    assert serve("dev", "GET", "/openapi.json").json()["openapi"] == "3.1.0"
    assert serve("dev", "POST", "/docs").status_code == 405
    assert events == []


# @lat: [[tests#Middleware#Invalid middleware]]
@pytest.mark.parametrize(
    "source",
    [
        "x = 1\n",
        "middleware = 1\n",
        "def middleware(request, call_next):\n    return call_next(request)\n",
    ],
)
def test_invalid_middleware(tmp_path, isolated_imports, source):
    write_project(
        tmp_path,
        {
            "app/(group)/middleware.py": source,
            "app/(group)/a/route.py": "def GET():\n    return {}\n",
        },
    )
    file = (tmp_path / "app" / "(group)" / "middleware.py").resolve()
    for make_app in [
        lambda: create_app(tmp_path / "app"),
        lambda: create_route_app(tmp_path / "app", "a"),
    ]:
        with pytest.raises(TypeError, match=re.escape(str(file))):
            make_app()


# @lat: [[tests#Middleware#Non-response result]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_middleware_non_response(tmp_path, isolated_imports, factory):
    write_project(
        tmp_path,
        {
            "app/middleware.py": (
                "async def middleware(request, call_next):\n"
                "    await call_next(request)\n"
                "    return {'not': 'a response'}\n"
            ),
            "app/route.py": "def GET():\n    return {}\n",
        },
    )
    app_dir = tmp_path / "app"
    app = (
        create_app(app_dir) if factory == "dev" else create_route_app(app_dir, "index")
    )
    response = make_client(app)("GET", "/")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"


# @lat: [[tests#Middleware#Conflicting chains for one path]]
def test_middleware_conflicting_chains(tmp_path, isolated_imports):
    middleware = "async def middleware(request, call_next):\n    return None\n"
    write_project(
        tmp_path,
        {
            "app/(a)/middleware.py": middleware,
            "app/(a)/x/route.py": "def GET():\n    return {}\n",
            "app/(b)/x/route.py": "def POST():\n    return {}\n",
        },
    )
    with pytest.raises(ValueError, match="conflicting middleware for route '/x'"):
        create_app(tmp_path / "app")


def error_source(name: str, *, sync: bool) -> str:
    """Source for an ``error.py`` that logs ``name!ExcType`` in ``mwlog.EVENTS``
    and, by default, returns a JSON 503 naming itself and the exception. The
    ``x-<name>`` header switches it to a bare ``raise`` (``reraise``), a new
    ``HTTPException(409, name)`` (``raise``), or returning ``None``."""
    return (
        "import mwlog\n"
        "from haumea import HTTPException, JSONResponse\n\n"
        f"{'' if sync else 'async '}def handle_error(request, exc):\n"
        f"    mwlog.EVENTS.append(f'{name}!{{type(exc).__name__}}')\n"
        f"    action = request.headers.get('x-{name}')\n"
        "    if action == 'reraise':\n"
        "        raise\n"
        "    if action == 'raise':\n"
        f"        raise HTTPException(409, {name!r})\n"
        "    if action == 'none':\n"
        "        return None\n"
        f"    return JSONResponse({{'by': {name!r}, 'error': str(exc)}}, 503)\n"
    )


@pytest.fixture
def error_project(tmp_path, isolated_imports):
    """Sync ``error.py`` at the root and in ``admin``, async in ``(group)``,
    plus root and ``(group)`` middleware. ``/admin`` has all three error
    files, ``/other`` the group's and root's, ``/plain`` only the root's.

    Yields ``serve`` like ``middleware_project`` and the shared event log."""
    write_project(
        tmp_path,
        {
            "mwlog.py": "EVENTS = []\n",
            "app/middleware.py": middleware_source("root"),
            "app/error.py": error_source("root", sync=True),
            "app/(group)/middleware.py": middleware_source("group"),
            "app/(group)/error.py": error_source("group", sync=False),
            "app/(group)/admin/error.py": error_source("admin", sync=True),
            "app/(group)/admin/route.py": MIDDLEWARE_ROUTE,
            "app/(group)/other/route.py": MIDDLEWARE_ROUTE,
            "app/plain/route.py": MIDDLEWARE_ROUTE,
        },
    )
    app_dir = tmp_path / "app"
    dev = create_app(app_dir)
    events: list[str] = sys.modules["mwlog"].EVENTS

    def serve(factory, method, path, function: str, **kwargs) -> httpx.Response:
        app = dev if factory == "dev" else create_route_app(app_dir, function)
        events.clear()
        return make_client(app)(method, path, **kwargs)

    return serve, events


# @lat: [[tests#Error boundaries#Nearest error.py wins]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_error_nearest_wins(error_project, factory):
    serve, events = error_project

    # Sync leaf handler; middleware sees its response from call_next.
    response = serve(factory, "GET", "/admin?fail=boom", "admin")
    assert response.status_code == 503
    assert response.json() == {"by": "admin", "error": "boom"}
    assert response.headers["x-seen-root"] == "503"
    assert events == [
        "root>",
        "group>",
        "handler",
        "admin!RuntimeError",
        "<group:503",
        "<root:503",
    ]

    # Validation errors and a 405 reach the boundary too.
    response = serve(factory, "GET", "/admin?n=bad", "admin")
    assert response.json()["by"] == "admin"
    assert events[2] == "admin!RequestValidationError"
    response = serve(factory, "POST", "/admin", "admin")
    assert response.json() == {"by": "admin", "error": "405: Method Not Allowed"}

    # Async nearest handler.
    response = serve(factory, "GET", "/other?fail=401", "other")
    assert response.json() == {"by": "group", "error": "401: nope"}
    assert events == [
        "root>",
        "group>",
        "handler",
        "group!HTTPException",
        "<group:503",
        "<root:503",
    ]

    response = serve(factory, "GET", "/plain?fail=boom", "plain")
    assert response.json() == {"by": "root", "error": "boom"}
    assert events == ["root>", "handler", "root!RuntimeError", "<root:503"]

    # Successful responses skip the boundary.
    assert serve(factory, "GET", "/admin?n=2", "admin").json() == {"n": 2}
    assert not [event for event in events if "!" in event]


# @lat: [[tests#Error boundaries#Re-raising passes to the next ancestor]]
@pytest.mark.parametrize("factory", FACTORIES)
@pytest.mark.parametrize(
    ("actions", "ran", "expected"),
    [
        # A bare raise passes the same exception on.
        (
            {"admin": "reraise"},
            ["admin!RuntimeError", "group!RuntimeError"],
            {"by": "group", "error": "boom"},
        ),
        # A new exception replaces it, even through a later bare raise
        # in the async group or the sync root.
        (
            {"admin": "raise"},
            ["admin!RuntimeError", "group!HTTPException"],
            {"by": "group", "error": "409: admin"},
        ),
        (
            {"admin": "raise", "group": "reraise"},
            ["admin!RuntimeError", "group!HTTPException", "root!HTTPException"],
            {"by": "root", "error": "409: admin"},
        ),
        # Past the root the default mapping applies to the last exception.
        (
            {"admin": "reraise", "group": "raise", "root": "reraise"},
            ["admin!RuntimeError", "group!RuntimeError", "root!HTTPException"],
            {"detail": "group"},
        ),
    ],
)
def test_error_reraise(error_project, factory, actions, ran, expected):
    serve, events = error_project
    headers = {f"x-{name}": action for name, action in actions.items()}
    response = serve(factory, "GET", "/admin?fail=boom", "admin", headers=headers)
    assert [event for event in events if "!" in event] == ran
    assert response.json() == expected
    assert response.headers["x-seen-root"] == str(response.status_code)


# @lat: [[tests#Error boundaries#Non-response result]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_error_non_response(error_project, factory):
    serve, events = error_project
    headers = {"x-admin": "none"}
    response = serve(factory, "GET", "/admin?fail=boom", "admin", headers=headers)
    assert [event for event in events if "!" in event] == [
        "admin!RuntimeError",
        "group!TypeError",
    ]
    body = response.json()
    assert body["by"] == "group"
    assert body["error"].endswith("returned None, not a Response")

    headers = {"x-admin": "none", "x-group": "none", "x-root": "none"}
    response = serve(factory, "GET", "/admin?fail=boom", "admin", headers=headers)
    assert (response.status_code, response.text) == (500, "Internal Server Error")


# @lat: [[tests#Error boundaries#Unhandled errors get the default mapping]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_error_unhandled_default(error_project, tmp_path, factory):
    serve, events = error_project
    headers = {"x-admin": "reraise", "x-group": "reraise", "x-root": "reraise"}
    response = serve(factory, "GET", "/admin?fail=boom", "admin", headers=headers)
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert response.headers["x-seen-root"] == "500"
    assert [event for event in events if "!" in event] == [
        "admin!RuntimeError",
        "group!RuntimeError",
        "root!RuntimeError",
    ]

    # A project without any error.py keeps today's mapping.
    project = tmp_path / "bare"
    write_project(project, {"app/x/route.py": MIDDLEWARE_ROUTE})
    app = (
        create_app(project / "app")
        if factory == "dev"
        else create_route_app(project / "app", "x")
    )
    client = make_client(app)
    response = client("GET", "/x?fail=401")
    assert (response.status_code, response.json()) == (401, {"detail": "nope"})
    assert response.headers["www-authenticate"] == "Bearer"
    assert client("GET", "/x?n=bad").status_code == 422
    response = client("GET", "/x?fail=boom")
    assert (response.status_code, response.text) == (500, "Internal Server Error")


# @lat: [[tests#Error boundaries#Middleware errors skip error.py]]
@pytest.mark.parametrize("factory", FACTORIES)
def test_error_skips_middleware_errors(error_project, factory):
    serve, events = error_project
    for kind, status in [("other", 500), ("http", 403), ("validation", 422)]:
        headers = {"x-raise": "group", "x-raise-kind": kind}
        response = serve(factory, "GET", "/admin", "admin", headers=headers)
        assert response.status_code == status
        assert events == ["root>", "group>"]

    # No route, no segment files.
    response = serve(factory, "GET", "/nope", "admin")
    assert response.status_code == 404
    assert events == []


ASYNC_RESULT = (
    "from haumea import JSONResponse\n\n"
    "async def respond(request, exc, by):\n"
    "    if request.headers.get('x-reraise'):\n"
    "        raise\n"
    "    return JSONResponse({'by': by, 'error': str(exc)}, 503)\n\n"
)


# @lat: [[tests#Error boundaries#Awaitable handlers]]
@pytest.mark.filterwarnings("error::RuntimeWarning")
@pytest.mark.parametrize("factory", FACTORIES)
def test_error_awaitable_handlers(tmp_path, isolated_imports, factory):
    write_project(
        tmp_path,
        {
            "app/error.py": (
                "from haumea import JSONResponse\n\n"
                "def handle_error(request, exc):\n"
                "    return JSONResponse({'by': 'root', 'error': str(exc)}, 500)\n"
            ),
            "app/obj/error.py": ASYNC_RESULT
            + (
                "class Handler:\n"
                "    async def __call__(self, request, exc):\n"
                "        return await respond(request, exc, 'obj')\n\n"
                "handle_error = Handler()\n"
            ),
            "app/obj/route.py": MIDDLEWARE_ROUTE,
            "app/wrapper/error.py": ASYNC_RESULT
            + (
                "def handle_error(request, exc):\n"
                "    return respond(request, exc, 'wrapper')\n"
            ),
            "app/wrapper/route.py": MIDDLEWARE_ROUTE,
            "mwlog.py": "EVENTS = []\n",
        },
    )
    app_dir = tmp_path / "app"
    for name in ["obj", "wrapper"]:
        app = (
            create_app(app_dir) if factory == "dev" else create_route_app(app_dir, name)
        )
        client = make_client(app)
        response = client("GET", f"/{name}?fail=boom")
        assert response.json() == {"by": name, "error": "boom"}

        # A bare raise in the awaited body re-raises the original exception.
        response = client("GET", f"/{name}?fail=boom", headers={"x-reraise": "1"})
        assert response.json() == {"by": "root", "error": "boom"}


# @lat: [[tests#Error boundaries#Invalid error.py]]
@pytest.mark.parametrize("source", ["x = 1\n", "handle_error = 1\n"])
def test_invalid_error_handler(tmp_path, isolated_imports, source):
    write_project(
        tmp_path,
        {
            "app/(group)/error.py": source,
            "app/(group)/a/route.py": "def GET():\n    return {}\n",
        },
    )
    file = (tmp_path / "app" / "(group)" / "error.py").resolve()
    for make_app in [
        lambda: create_app(tmp_path / "app"),
        lambda: create_route_app(tmp_path / "app", "a"),
    ]:
        with pytest.raises(TypeError, match=re.escape(str(file))):
            make_app()


# @lat: [[tests#Error boundaries#Conflicting error.py for one path]]
def test_error_conflicting_handlers(tmp_path, isolated_imports):
    write_project(
        tmp_path,
        {
            "app/(a)/error.py": "def handle_error(request, exc):\n    raise\n",
            "app/(a)/x/route.py": "def GET():\n    return {}\n",
            "app/(b)/x/route.py": "def POST():\n    return {}\n",
        },
    )
    with pytest.raises(ValueError, match="conflicting error handlers for route '/x'"):
        create_app(tmp_path / "app")


# @lat: [[tests#Route module isolation]]
def test_route_module_isolation(tmp_path, isolated_imports):
    for name in ["one", "two"]:
        write_project(
            tmp_path / name,
            {"app/route.py": f"def GET():\n    return {{'project': {name!r}}}\n"},
        )
    for name in ["one", "two"]:
        response = make_client(create_app(tmp_path / name / "app"))("GET", "/")
        assert response.json() == {"project": name}

    # "v1+" and "v1+/!" functions: distinct files, distinct modules.
    project = tmp_path / "punct"
    write_project(
        project,
        {
            "pyproject.toml": '[tool.haumea]\nbase_path = "/v1+"\n',
            "app/route.py": "def GET():\n    return {'route': 'root'}\n",
            "app/!/route.py": "def GET():\n    return {'route': 'bang'}\n",
        },
    )
    client = make_client(create_app(project / "app"))
    assert client("GET", "/v1+").json() == {"route": "root"}
    assert client("GET", "/v1+/!").json() == {"route": "bang"}


# @lat: [[tests#Symlinked app directory]]
def test_symlinked_app_directory(tmp_path, isolated_imports):
    project = tmp_path / "linked"
    write_project(
        project,
        {
            "pyproject.toml": (
                '[project]\nname = "linked"\nversion = "0"\n\n'
                '[tool.haumea]\nbase_path = "/api"\n'
            ),
            "src/app/users/route.py": "def GET():\n    return {'route': 'users'}\n",
        },
    )
    (project / "app").symlink_to(project / "src" / "app")

    app = create_app(project / "app")
    assert app.base_path == "/api"
    assert make_client(app)("GET", "/api/users").json() == {"route": "users"}
    route_app = create_route_app(project / "app", "api/users")
    assert route_app.base_path == "/api"

    plan = plan_build(project)
    assert plan.config.base_path == "/api"
    assert [f.name for f in plan.functions] == ["api/users"]


# @lat: [[tests#Prefixed build without public]]
def test_prefixed_build_without_public(tmp_path, isolated_imports, monkeypatch):
    monkeypatch.setattr(build_module, "_install_dependencies", lambda *args: None)
    monkeypatch.setattr(build_module, "_check_function_imports", lambda *args: None)
    files = {
        "pyproject.toml": (
            '[project]\nname = "nopub"\nversion = "0"\n\n'
            '[tool.haumea]\nbase_path = "/api"\n'
        ),
        "app/route.py": "def GET():\n    return {}\n",
    }
    project = tmp_path / "nopub"
    write_project(project, files)
    static = emit_build(plan_build(project)) / "static"
    assert {p.relative_to(static).as_posix() for p in static.rglob("*")} == {
        "api",
        "api/docs.html",
        "api/openapi.json",
    }

    late = tmp_path / "latepub"
    write_project(
        late,
        files
        | {
            "app/route.py": (
                "from pathlib import Path\n"
                "pub = Path(__file__).parents[1] / 'public'\n"
                "pub.mkdir(exist_ok=True)\n"
                "(pub / 'late.txt').write_text('created')\n"
            )
        },
    )
    plan = plan_build(late)
    assert plan.public_dir is None
    static = emit_build(plan) / "static"
    assert (static / "api" / "late.txt").read_text() == "created"
    assert (static / "api" / "docs.html").is_file()


# @lat: [[tests#Custom build output directory]]
def test_custom_build_output_directory(tmp_path, isolated_imports, monkeypatch, capsys):
    monkeypatch.setattr(build_module, "_install_dependencies", lambda *args: None)
    monkeypatch.setattr(build_module, "_check_function_imports", lambda *args: None)
    project = tmp_path / "custom"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "custom"\nversion = "0"\n',
            "app/route.py": "def GET():\n    return {'ok': True}\n",
            "public/hello.txt": "hi\n",
        },
    )

    # plan_build accepts a custom output path without creating it.
    external = tmp_path / "artifacts" / "out"
    plan = plan_build(project, output=external)
    assert plan.output_dir == external.resolve()
    assert not external.exists()

    # build() writes to the custom directory and leaves .vercel/output untouched.
    result = build(project, output=external)
    assert result == external.resolve()
    assert (external / "config.json").is_file()
    assert (external / "static" / "hello.txt").read_text() == "hi\n"
    assert (external / "functions" / "index.func" / "index.py").is_file()
    assert not (project / ".vercel").exists()

    # Output inside project_dir does not copy itself or temporary directories
    # into function payloads, even across repeated builds.
    nested = project / "dist" / "vercel-output"
    for _ in range(2):
        built = build(project, output=nested)
        assert built == nested.resolve()
        func = nested / "functions" / "index.func"
        assert (func / "app" / "route.py").is_file()
        assert not (func / "dist" / "vercel-output").exists()
        assert not list(func.rglob(".haumea-*"))

    direct = project / "out"
    built = build(project, output=direct)
    assert built == direct.resolve()
    assert not (direct / "functions" / "index.func" / "out").exists()

    # CLI `--output` builds into the requested directory (relative to cwd).
    monkeypatch.chdir(project)
    cli_main(["build", "--output", "cli-out", "--platform", "native"])
    cli_out = project / "cli-out"
    assert (cli_out / "config.json").is_file()
    assert (cli_out / "functions" / "index.func" / "index.py").is_file()
    assert not (cli_out / "functions" / "index.func" / "cli-out").exists()
    assert "cli-out" in capsys.readouterr().out
    assert not (project / ".vercel").exists()

    # Output directories overlapping project source trees are rejected before deletion.
    for bad in [
        project,
        project.parent,
        project / "app",
        project / "app" / "out",
        project / "public",
        project / "public" / "out",
    ]:
        with pytest.raises(ValueError, match="output directory"):
            plan_build(project, output=bad)
        assert (project / "app" / "route.py").is_file()
        assert (project / "public" / "hello.txt").is_file()


# @lat: [[tests#Locked lockfile]]
def test_stale_lockfile_fails_build(tmp_path, isolated_imports):
    project = tmp_path / "stale"
    pyproject = (
        '[project]\nname = "stale"\nversion = "0.1.0"\n'
        'requires-python = ">=3.12"\ndependencies = []\n'
    )
    write_project(
        project,
        {
            "pyproject.toml": pyproject,
            "app/route.py": "def GET():\n    return {'ok': True}\n",
        },
    )
    subprocess.run(["uv", "lock", "--quiet"], cwd=project, check=True)
    lock_before = (project / "uv.lock").read_text()

    # Drift pyproject.toml from uv.lock without re-locking.
    (project / "pyproject.toml").write_text(
        pyproject.replace("dependencies = []", 'dependencies = ["msgspec"]')
    )
    with pytest.raises(RuntimeError, match="uv.lock"):
        build(project, platform=None, output=tmp_path / "out")
    assert (project / "uv.lock").read_text() == lock_before

    # Missing uv.lock also fails instead of resolving on the fly.
    (project / "uv.lock").unlink()
    with pytest.raises(RuntimeError, match="uv.lock"):
        build(project, platform=None, output=tmp_path / "out2")


# @lat: [[tests#Deterministic build output]]
def test_deterministic_build_output(tmp_path, isolated_imports, monkeypatch):
    monkeypatch.setattr(build_module, "_install_dependencies", lambda *args: None)
    monkeypatch.setattr(build_module, "_check_function_imports", lambda *args: None)
    project = tmp_path / "det"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "det"\nversion = "0"\n',
            "app/z/route.py": "def GET():\n    return {'z': 1}\n",
            "app/a/route.py": "def GET():\n    return {'a': 1}\n",
            "app/items/[id]/route.py": (
                "import msgspec\n\n"
                "max_duration = 10\n"
                "memory = 512\n"
                "regions = ['iad1']\n\n"
                "class Item(msgspec.Struct):\n"
                "    name: str\n\n"
                "def PUT(id: int, item: Item) -> Item:\n"
                "    return item\n"
            ),
            "public/hello.txt": "hello\n",
        },
    )

    out1 = build(project, output=tmp_path / "out1")
    out2 = build(project, output=tmp_path / "out2")

    def snapshot(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    assert snapshot(out1) == snapshot(out2)

    config_text = (out1 / "config.json").read_text()
    assert config_text == json.dumps(json.loads(config_text), indent=2, sort_keys=True)

    openapi_text = (out1 / "static" / "openapi.json").read_text()
    assert openapi_text == json.dumps(json.loads(openapi_text), sort_keys=True)

    for vc_config in sorted((out1 / "functions").rglob(".vc-config.json")):
        text = vc_config.read_text()
        assert text == json.dumps(json.loads(text), indent=2, sort_keys=True)


# @lat: [[tests#Workspace discovery]]
def test_workspace_root_discovery(tmp_path, isolated_imports):
    # Non-workspace project falls back to project_dir, even under an ancestor
    # with pyproject.toml or [tool.uv.workspace] lacking uv.lock.
    outer = tmp_path / "outer"
    write_project(
        outer,
        {
            "pyproject.toml": '[tool.uv.workspace]\nmembers = ["apps/*"]\n',
            "apps/solo/pyproject.toml": '[project]\nname = "solo"\nversion = "0"\n',
            "apps/solo/app/route.py": "def GET():\n    return {}\n",
        },
    )
    solo = (outer / "apps" / "solo").resolve()
    solo_plan = plan_build(solo)
    assert solo_plan.workspace_root == solo
    assert solo_plan.package is None

    # Adding uv.lock at the ancestor makes it the workspace root.
    (outer / "uv.lock").write_text("version = 1\n")
    solo_ws_plan = plan_build(solo)
    assert solo_ws_plan.workspace_root == outer.resolve()
    assert solo_ws_plan.package == "solo"

    # Nearest ancestor with both [tool.uv.workspace] and uv.lock wins.
    inner = outer / "sub"
    write_project(
        inner,
        {
            "pyproject.toml": '[tool.uv.workspace]\nmembers = ["svc", "unnamed"]\n',
            "svc/pyproject.toml": '[project]\nname = "svc"\nversion = "0"\n',
            "svc/app/route.py": "def GET():\n    return {}\n",
        },
    )
    svc = (inner / "svc").resolve()
    # inner lacks uv.lock, so outer wins.
    svc_outer_plan = plan_build(svc)
    assert svc_outer_plan.workspace_root == outer.resolve()
    assert svc_outer_plan.package == "svc"
    # Once inner also has uv.lock, inner (nearest) wins.
    (inner / "uv.lock").write_text("version = 1\n")
    svc_inner_plan = plan_build(svc)
    assert svc_inner_plan.workspace_root == inner.resolve()
    assert svc_inner_plan.package == "svc"

    # A workspace member without [project].name cannot be exported by package name.
    unnamed = inner / "unnamed"
    write_project(
        unnamed,
        {
            "pyproject.toml": '[tool.haumea]\nbase_path = "/api"\n',
            "app/route.py": "def GET():\n    return {}\n",
        },
    )
    with pytest.raises(ValueError, match="name"):
        plan_build(unnamed)

    # A standalone project with its own uv.lock under a workspace root stops
    # walking up and resolves to itself.
    standalone = outer / "standalone"
    write_project(
        standalone,
        {
            "pyproject.toml": '[project]\nname = "standalone"\nversion = "0"\n',
            "uv.lock": "version = 1\n",
            "app/route.py": "def GET():\n    return {}\n",
        },
    )
    standalone_plan = plan_build(standalone)
    assert standalone_plan.workspace_root == standalone.resolve()
    assert standalone_plan.package is None

    # An ancestor with uv.lock but without [tool.uv.workspace] is not a
    # workspace root and stops walking up before reaching an outer workspace.
    plain_parent = outer / "plain_parent"
    write_project(
        plain_parent,
        {
            "pyproject.toml": '[project]\nname = "parent"\nversion = "0"\n',
            "uv.lock": "version = 1\n",
            "child/pyproject.toml": '[project]\nname = "child"\nversion = "0"\n',
            "child/app/route.py": "def GET():\n    return {}\n",
        },
    )
    child = (plain_parent / "child").resolve()
    child_plan = plan_build(child)
    assert child_plan.workspace_root == child
    assert child_plan.package is None

    # A workspace root that is itself a Haumea project resolves to itself,
    # while a virtual workspace root without [project].name is rejected.
    root_svc = tmp_path / "root_svc"
    write_project(
        root_svc,
        {
            "pyproject.toml": (
                '[project]\nname = "root-svc"\nversion = "0"\n\n'
                "[tool.uv.workspace]\nmembers = []\n"
            ),
            "uv.lock": "version = 1\n",
            "app/route.py": "def GET():\n    return {}\n",
        },
    )
    root_svc_plan = plan_build(root_svc)
    assert root_svc_plan.workspace_root == root_svc.resolve()
    assert root_svc_plan.package == "root-svc"

    virtual_root = tmp_path / "virtual_root"
    write_project(
        virtual_root,
        {
            "pyproject.toml": "[tool.uv.workspace]\nmembers = []\n",
            "uv.lock": "version = 1\n",
            "app/route.py": "def GET():\n    return {}\n",
        },
    )
    with pytest.raises(ValueError, match="name"):
        plan_build(virtual_root)


# @lat: [[tests#Workspace build]]
def test_workspace_build(tmp_path, isolated_imports, monkeypatch):
    repo_root = Path(__file__).parent.parent.resolve()
    workspace = tmp_path / "workspace"
    write_project(
        workspace,
        {
            "pyproject.toml": (
                '[tool.uv.workspace]\nmembers = ["apps/*", "packages/*"]\n'
            ),
            "packages/core/pyproject.toml": (
                '[project]\nname = "core"\nversion = "0.1.0"\n'
                'requires-python = ">=3.12"\ndependencies = []\n\n'
                '[build-system]\nrequires = ["hatchling"]\n'
                'build-backend = "hatchling.build"\n'
            ),
            "packages/core/src/core/__init__.py": (
                'def greet(who: str) -> str:\n    return f"hello {who} from core"\n'
            ),
            "packages/unused/pyproject.toml": (
                '[project]\nname = "unused"\nversion = "0.1.0"\n'
                'requires-python = ">=3.12"\ndependencies = []\n\n'
                '[build-system]\nrequires = ["hatchling"]\n'
                'build-backend = "hatchling.build"\n'
            ),
            "packages/unused/src/unused/__init__.py": "VALUE = 1\n",
            "apps/worker/pyproject.toml": (
                '[project]\nname = "worker"\nversion = "0.1.0"\n'
                'requires-python = ">=3.12"\ndependencies = ["unused"]\n\n'
                "[tool.uv.sources]\nunused = { workspace = true }\n"
            ),
            "apps/api/pyproject.toml": (
                '[project]\nname = "api"\nversion = "0.1.0"\n'
                'requires-python = ">=3.12"\n'
                'dependencies = ["haumea", "core"]\n\n'
                '[build-system]\nrequires = ["hatchling"]\n'
                'build-backend = "hatchling.build"\n\n'
                "[tool.hatch.build.targets.wheel]\n"
                'packages = ["src/api_pkg"]\n\n'
                "[tool.uv.sources]\n"
                f"haumea = {{ path = {json.dumps(repo_root.as_posix())},"
                " editable = true }\n"
                "core = { workspace = true }\n"
            ),
            "apps/api/src/api_pkg/__init__.py": 'MARKER = "api"\n',
            "apps/api/app/route.py": (
                "from core import greet\n\n"
                'def GET(name: str = "world"):\n'
                '    return {"greeting": greet(name)}\n'
            ),
        },
    )
    subprocess.run(["uv", "lock", "--quiet"], cwd=workspace, check=True)

    # Mirror the workspace environment during planning so route.py can import core;
    # invoke() runs in a fresh `python -S` subprocess with only _vendor/ on sys.path.
    monkeypatch.syspath_prepend(str(workspace / "packages" / "core" / "src"))

    api = workspace / "apps" / "api"
    plan = plan_build(api, platform=None)
    assert plan.project_dir == api.resolve()
    assert plan.workspace_root == workspace.resolve()
    assert plan.package == "api"

    output = build(api, platform=None)
    assert output == api.resolve() / ".vercel" / "output"

    vendor = output / "functions" / "index.func" / "_vendor"
    assert (vendor / "core" / "__init__.py").is_file()
    assert not (vendor / "unused").exists()
    assert not (vendor / "api_pkg").exists()

    response = invoke(output, "index", "/?name=haumea")
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"greeting": "hello haumea from core"}


# @lat: [[tests#Full-stack example build]]
def test_fullstack_example_build(tmp_path, isolated_imports):
    backend = FULLSTACK / "backend"
    vercel = json.loads((FULLSTACK / "vercel.json").read_text())
    assert vercel["services"]["web"] == {"root": "frontend/", "framework": "nextjs"}
    assert vercel["services"]["api"] == {
        "root": "backend/",
        "framework": "vite",
        "buildCommand": "uv run --frozen haumea build",
        "devCommand": "uv run --frozen haumea dev --port $PORT",
    }
    assert vercel["rewrites"] == [
        {"source": "/api/:path*", "destination": {"service": "api"}},
        {"source": "/(.*)", "destination": {"service": "web"}},
    ]
    assert (FULLSTACK / ".python-version").read_text().strip() == "3.12"
    assert (backend / ".python-version").read_text().strip() == "3.12"

    out = tmp_path / "output"
    plan = plan_build(backend, platform=None, output=out)
    assert plan.project_dir == backend.resolve()
    assert plan.workspace_root == FULLSTACK.resolve()
    assert plan.package == "fullstack-api"
    assert plan.config.base_path == "/api"

    output = build(backend, platform=None, output=out)
    assert output == out.resolve()

    config = json.loads((output / "config.json").read_text())
    assert config["routes"] == [
        {"handle": "filesystem"},
        {"src": "^/api/docs$", "dest": "/api/docs.html"},
        {"src": "^/api/items/([^/]+)$", "dest": "/api/items/[item_id]"},
    ]

    docs_html = output / "static" / "api" / "docs.html"
    assert docs_html.is_file()
    assert '"/api/openapi.json"' in docs_html.read_text()
    schema = json.loads((output / "static" / "api" / "openapi.json").read_text())
    assert set(schema["paths"]) == {"/api", "/api/hello", "/api/items/{item_id}"}
    assert (output / "static" / "api" / "hello.txt").read_text().strip() == (
        "A static file served from /api/hello.txt"
    )

    for func in ("api", "api/hello", "api/items/[item_id]"):
        func_dir = output / "functions" / f"{func}.func"
        assert (func_dir / ".vc-config.json").is_file()
        assert not any(
            p.name.startswith("uvicorn") for p in (func_dir / "_vendor").iterdir()
        )

    root_res = invoke(output, "api", "/api")
    assert root_res["statusCode"] == 200
    assert json.loads(root_res["body"]) == {
        "service": "haumea",
        "message": "Hello from Haumea",
    }

    hello_res = invoke(output, "api/hello", "/api/hello?name=vercel")
    assert hello_res["statusCode"] == 200
    assert json.loads(hello_res["body"]) == {"hello": "vercel"}

    item_res = invoke(output, "api/items/[item_id]", "/api/items/42")
    assert item_res["statusCode"] == 200
    assert json.loads(item_res["body"]) == {"item_id": 42}


# @lat: [[tests#Dev requires uvicorn]]
def test_dev_requires_uvicorn(monkeypatch):
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["dev"])
    message = str(exc_info.value)
    assert "uvicorn" in message
    assert "haumea[dev]" in message


# @lat: [[tests#Store index]]
def test_store_index(tmp_path, output):
    from haumea.build import index_store, normalize_dist_name

    assert normalize_dist_name("My_Cool.Pkg--Name") == "my-cool-pkg-name"
    assert normalize_dist_name("msgspec") == "msgspec"

    # Missing store directory returns an empty index.
    empty = index_store(tmp_path / "missing")
    assert empty.distributions == {}
    assert empty.by_import == {}

    # Synthetic store covering RECORD, top_level.txt, Requires-Dist, namespace
    # packages, single-file modules, excluded directories, and escaped paths.
    store = tmp_path / "_vendor"
    write_project(
        store,
        {
            "Alpha_Pkg-1.0.dist-info/METADATA": (
                "Metadata-Version: 2.1\n"
                "Name: Alpha_Pkg\n"
                "Version: 1.0\n"
                "Requires-Dist: Beta.Lib[extra1, extra2]>=2.0,<3; "
                "python_version >= '3.12'\n"
                "Requires-Dist: Gamma_Tool (==1.5)\n"
                "Requires-Dist: delta-dep @ https://example.com/delta.whl ; "
                "extra == 'dev'\n"
                "Requires-Dist: z\n"
            ),
            "Alpha_Pkg-1.0.dist-info/RECORD": (
                "alpha/__init__.py,sha256=abc,10\n"
                "alpha/sub.py,sha256=def,20\n"
                "alpha_helper.py,sha256=ghi,15\n"
                "ns_shared/alpha_part.py,sha256=jkl,5\n"
                "bin/alpha-cli,sha256=mno,30\n"
                "Alpha_Pkg-1.0.dist-info/METADATA,sha256=pqr,100\n"
                "Alpha_Pkg-1.0.dist-info/RECORD,,\n"
                "Alpha_Pkg-1.0.data/purelib/ignored.py,sha256=stu,10\n"
                "__pycache__/alpha_helper.cpython-312.pyc,sha256=vwx,10\n"
                "../../outside.py,sha256=yz,10\n"
                "/abs/outside.py,sha256=ab,10\n"
                '"alpha/data,file.txt",sha256=cd,4\n'
            ),
            "Alpha_Pkg-1.0.dist-info/top_level.txt": "\nalpha_extra\nns_top/sub\n",
            "alpha/__init__.py": "x = 1\n",
            "alpha/sub.py": "y = 2\n",
            "alpha/data,file.txt": "1234",
            "alpha_helper.py": "z = 3\n",
            "ns_shared/alpha_part.py": "a = 1\n",
            "bin/alpha-cli": "#!/usr/bin/env python\n",
            "Beta.Lib-2.0.dist-info/METADATA": (
                "Metadata-Version: 2.1\nName: Beta.Lib\nVersion: 2.0\n"
            ),
            "Beta.Lib-2.0.dist-info/RECORD": (
                "ns_shared/beta_part.py,sha256=aaa,8\n"
                "beta_ext.cpython-312-linux.so,sha256=bbb,12\n"
                "Beta.Lib-2.0.dist-info/METADATA,sha256=ccc,50\n"
                "Beta.Lib-2.0.dist-info/RECORD,,\n"
            ),
            "ns_shared/beta_part.py": "b = 2\n",
            "beta_ext.cpython-312-linux.so": "0123456789ab",
        },
    )

    index = index_store(store)
    assert set(index.distributions) == {"alpha-pkg", "beta-lib"}

    alpha = index.distributions["alpha-pkg"]
    assert alpha.name == "alpha-pkg"
    assert alpha.dist_info == "Alpha_Pkg-1.0.dist-info"
    assert alpha.requires == frozenset({"beta-lib", "gamma-tool", "delta-dep", "z"})
    assert alpha.top_level == frozenset(
        {"alpha", "alpha_helper", "ns_shared", "alpha_extra", "ns_top"}
    )
    assert "alpha/__init__.py" in alpha.files
    assert "alpha/data,file.txt" in alpha.files
    assert "bin/alpha-cli" in alpha.files
    assert "../../outside.py" not in alpha.files
    assert "/abs/outside.py" not in alpha.files
    expected_alpha_size = sum(
        (store / p).lstat().st_size
        for p in (
            "alpha/__init__.py",
            "alpha/sub.py",
            "alpha/data,file.txt",
            "alpha_helper.py",
            "ns_shared/alpha_part.py",
            "bin/alpha-cli",
            "Alpha_Pkg-1.0.dist-info/METADATA",
            "Alpha_Pkg-1.0.dist-info/RECORD",
            "Alpha_Pkg-1.0.dist-info/top_level.txt",
        )
    )
    assert alpha.size == expected_alpha_size

    beta = index.distributions["beta-lib"]
    assert beta.top_level == frozenset({"ns_shared", "beta_ext"})
    assert beta.requires == frozenset()

    assert index.by_import["alpha"] == ("alpha-pkg",)
    assert index.by_import["alpha_helper"] == ("alpha-pkg",)
    assert index.by_import["alpha_extra"] == ("alpha-pkg",)
    assert index.by_import["ns_shared"] == ("alpha-pkg", "beta-lib")
    assert index.by_import["beta_ext"] == ("beta-lib",)
    assert "bin" not in index.by_import

    # Real vendored store from examples/hello.
    real_index = index_store(output / "functions" / "index.func" / "_vendor")
    assert {"haumea", "msgspec", "vercel-runtime"} <= set(real_index.distributions)
    haumea_dist = real_index.distributions["haumea"]
    assert haumea_dist.top_level == frozenset({"haumea"})
    assert "msgspec" in haumea_dist.requires
    assert "haumea/__init__.py" in haumea_dist.files
    assert haumea_dist.size > 0
    assert real_index.by_import["msgspec"] == ("msgspec",)
    assert real_index.by_import["vercel_runtime"] == ("vercel-runtime",)


# @lat: [[tests#Build size report and limits]]
def test_build_size_report_and_limits(tmp_path, isolated_imports, monkeypatch, capsys):
    assert build_module.SIZE_WARN_BYTES == 250 * 1024 * 1024
    assert build_module.SIZE_LIMIT_BYTES == 500 * 1024 * 1024

    project = tmp_path / "sized"
    write_project(
        project,
        {
            "pyproject.toml": (
                '[project]\nname = "sized"\nversion = "0"\n\n'
                '[tool.haumea]\ninclude = ["big-pkg", "med-pkg", "small-pkg"]\n'
            ),
            "app/route.py": "def GET():\n    return {'ok': True}\n",
            "app/ping/route.py": "def GET():\n    return {'ping': True}\n",
        },
    )

    def fake_install(_workspace, vendor_dir, _python, _platform, _package=None):
        write_project(
            vendor_dir,
            {
                "big_pkg-1.0.dist-info/METADATA": (
                    "Metadata-Version: 2.1\nName: Big_Pkg\nVersion: 1.0\n"
                ),
                "big_pkg-1.0.dist-info/RECORD": (
                    "big_pkg/__init__.py,,\n"
                    "big_pkg/blob.bin,,\n"
                    "big_pkg-1.0.dist-info/METADATA,,\n"
                    "big_pkg-1.0.dist-info/RECORD,,\n"
                ),
                "big_pkg/__init__.py": "",
                "big_pkg/blob.bin": "x" * 8_192,
                "med_pkg-1.0.dist-info/METADATA": (
                    "Metadata-Version: 2.1\nName: med-pkg\nVersion: 1.0\n"
                ),
                "med_pkg-1.0.dist-info/RECORD": (
                    "med_pkg/__init__.py,,\n"
                    "med_pkg/blob.bin,,\n"
                    "med_pkg-1.0.dist-info/METADATA,,\n"
                    "med_pkg-1.0.dist-info/RECORD,,\n"
                ),
                "med_pkg/__init__.py": "",
                "med_pkg/blob.bin": "y" * 2_048,
                "small_pkg-1.0.dist-info/METADATA": (
                    "Metadata-Version: 2.1\nName: small-pkg\nVersion: 1.0\n"
                ),
                "small_pkg-1.0.dist-info/RECORD": (
                    "small_pkg/__init__.py,,\n"
                    "small_pkg-1.0.dist-info/METADATA,,\n"
                    "small_pkg-1.0.dist-info/RECORD,,\n"
                ),
                "small_pkg/__init__.py": "z = 1\n",
            },
        )

    monkeypatch.setattr(build_module, "_install_dependencies", fake_install)
    monkeypatch.setattr(build_module, "_check_function_imports", lambda *args: None)

    # Under thresholds: prints each function's size and distribution count, no warnings.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = build(project, platform=None, output=tmp_path / "out-ok")
    assert out.is_dir()
    assert caught == []
    report_out = capsys.readouterr().out
    assert "index" in report_out
    assert "ping" in report_out
    assert "KB" in report_out
    assert "3 distributions" in report_out

    # Above warn threshold (and below fail limit): emits warning naming largest
    # distributions in descending size order, and build still succeeds.
    monkeypatch.setattr(build_module, "SIZE_WARN_BYTES", 4_096)
    monkeypatch.setattr(build_module, "SIZE_LIMIT_BYTES", 1024 * 1024)
    with pytest.warns(UserWarning) as record:
        out_warn = build(project, platform=None, output=tmp_path / "out-warn")
    assert out_warn.is_dir()
    warn_messages = [str(item.message) for item in record]
    assert len(warn_messages) == 2
    for func_name in ("index", "ping"):
        msg = next(m for m in warn_messages if f"'{func_name}'" in m)
        assert "big-pkg" in msg
        assert "med-pkg" in msg
        assert msg.index("big-pkg") < msg.index("med-pkg")
    capsys.readouterr()

    # Above fail limit: build fails with RuntimeError naming function and largest dists.
    monkeypatch.setattr(build_module, "SIZE_LIMIT_BYTES", 4_096)
    with pytest.raises(RuntimeError) as exc_info:
        build(project, platform=None, output=tmp_path / "out-fail")
    error_message = str(exc_info.value)
    assert "index" in error_message
    assert "big-pkg" in error_message
    assert "med-pkg" in error_message
    capsys.readouterr()

    # CLI build prints the per-function size report alongside the build summary.
    monkeypatch.setattr(build_module, "SIZE_WARN_BYTES", 1024 * 1024)
    monkeypatch.setattr(build_module, "SIZE_LIMIT_BYTES", 2 * 1024 * 1024)
    monkeypatch.chdir(project)
    cli_main(["build", "--platform", "native", "--output", str(tmp_path / "cli-out")])
    cli_out = capsys.readouterr().out
    assert "index" in cli_out
    assert "ping" in cli_out
    assert "3 distributions" in cli_out
    assert "Build output written to" in cli_out


def _write_workspace_pkg(
    workspace: Path,
    dist_name: str,
    module_name: str,
    source: str,
    *,
    deps: list[str] | None = None,
) -> None:
    dep_list = deps or []
    sources_block = ""
    if dep_list:
        entries = "\n".join(f"{dep} = {{ workspace = true }}" for dep in dep_list)
        sources_block = f"\n[tool.uv.sources]\n{entries}\n"
    write_project(
        workspace / "packages" / dist_name,
        {
            "pyproject.toml": (
                f'[project]\nname = "{dist_name}"\nversion = "0.1.0"\n'
                f'requires-python = ">=3.12"\n'
                f"dependencies = {json.dumps(dep_list)}\n\n"
                '[build-system]\nrequires = ["hatchling"]\n'
                'build-backend = "hatchling.build"\n'
                f"{sources_block}"
            ),
            f"src/{module_name}/__init__.py": source,
        },
    )


# @lat: [[tests#Per-function tree-shaking]]
def test_tree_shaking_workspace_build(tmp_path, isolated_imports, monkeypatch, capsys):
    from haumea.build import index_store

    repo_root = Path(__file__).parent.parent.resolve()
    workspace = tmp_path / "shaken-ws"
    write_project(
        workspace,
        {
            "pyproject.toml": (
                '[tool.uv.workspace]\nmembers = ["apps/*", "packages/*"]\n'
            ),
        },
    )
    _write_workspace_pkg(workspace, "pkg-trans", "pkg_trans", 'VALUE = "from-trans"\n')
    _write_workspace_pkg(
        workspace,
        "pkg-a",
        "pkg_a",
        (
            "import pkg_trans\n\n"
            'def describe() -> str:\n    return f"pkg-a+{pkg_trans.VALUE}"\n'
        ),
        deps=["pkg-trans"],
    )
    _write_workspace_pkg(
        workspace,
        "pkg-b",
        "pkg_b",
        'def describe() -> str:\n    return "pkg-b"\n',
    )
    _write_workspace_pkg(
        workspace,
        "pkg-c",
        "pkg_c",
        'def describe() -> str:\n    return "pkg-c"\n',
    )
    _write_workspace_pkg(
        workspace,
        "pkg-d",
        "pkg_d",
        'def describe() -> str:\n    return "pkg-d"\n',
    )

    base_pyproject = (
        '[project]\nname = "api"\nversion = "0.1.0"\n'
        'requires-python = ">=3.12"\n'
        'dependencies = ["haumea", "pkg-a", "pkg-b", "pkg-c", "pkg-d"]\n\n'
        "[tool.uv.sources]\n"
        f"haumea = {{ path = {json.dumps(repo_root.as_posix())}, editable = true }}\n"
        "pkg-a = { workspace = true }\n"
        "pkg-b = { workspace = true }\n"
        "pkg-c = { workspace = true }\n"
        "pkg-d = { workspace = true }\n"
    )
    write_project(
        workspace / "apps" / "api",
        {
            "pyproject.toml": base_pyproject,
            "helpers/__init__.py": "",
            "helpers/chain.py": (
                "from . import leaf\n"
                "from .cycle import TAG\n\n"
                "def run() -> str:\n"
                '    return f"{leaf.run()}:{TAG}"\n'
            ),
            "helpers/cycle.py": (
                'TAG = "ok"\n\n'
                "def touch():\n"
                "    from . import chain\n"
                "    return chain\n"
            ),
            "helpers/leaf.py": (
                "import pkg_a\n\ndef run() -> str:\n    return pkg_a.describe()\n"
            ),
            "app/alpha/route.py": (
                "import typing\n"
                "from typing import TYPE_CHECKING\n"
                "from helpers.chain import run\n\n"
                "if TYPE_CHECKING:\n"
                "    import pkg_d\n"
                "if typing.TYPE_CHECKING:\n"
                "    import pkg_b\n\n"
                "def GET():\n"
                '    return {"result": run()}\n'
            ),
            "app/beta/route.py": (
                "def GET():\n"
                "    import pkg_b\n"
                '    return {"result": pkg_b.describe()}\n'
            ),
            "app/dynamic/route.py": (
                "import importlib\n"
                "from importlib import import_module\n\n"
                "def GET():\n"
                '    b = importlib.import_module("pkg_b")\n'
                '    c = import_module("pkg_c")\n'
                '    return {"b": b.describe(), "c": c.describe()}\n'
            ),
            "app/dunder/route.py": (
                "def GET():\n"
                '    c = __import__("pkg_c")\n'
                '    return {"c": c.describe()}\n'
            ),
            "app/included/route.py": (
                "import importlib\n\n"
                'include = ["Pkg_D"]\n\n'
                "def GET():\n"
                '    mod = importlib.import_module("".join(["pkg", "_d"]))\n'
                '    return {"d": mod.describe()}\n'
            ),
        },
    )
    subprocess.run(["uv", "lock", "--quiet"], cwd=workspace, check=True)

    for dist_name in ("pkg-trans", "pkg-a", "pkg-b", "pkg-c", "pkg-d"):
        monkeypatch.syspath_prepend(str(workspace / "packages" / dist_name / "src"))

    api = workspace / "apps" / "api"
    plan = plan_build(api, platform=None, output=tmp_path / "out-shaken")
    by_name = {spec.name: spec for spec in plan.functions}
    assert by_name["alpha"].trace_roots == (
        (api / "app" / "alpha" / "route.py").resolve(),
    )
    assert by_name["alpha"].include == ()
    assert by_name["included"].include == ("pkg-d",)

    shaken = build(api, platform=None, output=tmp_path / "out-shaken")
    report_out = capsys.readouterr().out

    def dists_for(out_dir: Path, func_name: str) -> set[str]:
        return set(
            index_store(
                out_dir / "functions" / f"{func_name}.func" / "_vendor"
            ).distributions
        )

    always = {"haumea", "msgspec", "vercel-runtime"}

    alpha_dists = dists_for(shaken, "alpha")
    beta_dists = dists_for(shaken, "beta")
    dynamic_dists = dists_for(shaken, "dynamic")
    dunder_dists = dists_for(shaken, "dunder")
    included_dists = dists_for(shaken, "included")

    assert always | {"pkg-a", "pkg-trans"} <= alpha_dists
    assert not ({"pkg-b", "pkg-c", "pkg-d"} & alpha_dists)

    assert always | {"pkg-b"} <= beta_dists
    assert not ({"pkg-a", "pkg-trans", "pkg-c", "pkg-d"} & beta_dists)

    assert always | {"pkg-b", "pkg-c"} <= dynamic_dists
    assert not ({"pkg-a", "pkg-trans", "pkg-d"} & dynamic_dists)

    assert always | {"pkg-c"} <= dunder_dists
    assert not ({"pkg-a", "pkg-trans", "pkg-b", "pkg-d"} & dunder_dists)

    assert always | {"pkg-d"} <= included_dists
    assert not ({"pkg-a", "pkg-trans", "pkg-b", "pkg-c"} & included_dists)

    # Size report reflects each function's actual bundled distribution count.
    for func_name, expected_dists in (
        ("alpha", alpha_dists),
        ("beta", beta_dists),
        ("dynamic", dynamic_dists),
        ("dunder", dunder_dists),
        ("included", included_dists),
    ):
        line = next(
            row for row in report_out.splitlines() if row.strip().startswith(func_name)
        )
        assert f"({len(expected_dists)} distributions)" in line

    # Shared files across functions are still hard-linked.
    alpha_haumea = (
        shaken / "functions" / "alpha.func" / "_vendor" / "haumea" / "__init__.py"
    )
    beta_haumea = (
        shaken / "functions" / "beta.func" / "_vendor" / "haumea" / "__init__.py"
    )
    assert alpha_haumea.stat().st_ino == beta_haumea.stat().st_ino

    # Every tree-shaken function executes standalone via vc_handler (python -S).
    assert json.loads(invoke(shaken, "alpha", "/alpha")["body"]) == {
        "result": "pkg-a+from-trans:ok"
    }
    assert json.loads(invoke(shaken, "beta", "/beta")["body"]) == {"result": "pkg-b"}
    assert json.loads(invoke(shaken, "dynamic", "/dynamic")["body"]) == {
        "b": "pkg-b",
        "c": "pkg-c",
    }
    assert json.loads(invoke(shaken, "dunder", "/dunder")["body"]) == {"c": "pkg-c"}
    assert json.loads(invoke(shaken, "included", "/included")["body"]) == {"d": "pkg-d"}

    # Project-level `include` adds its normalized distribution to every function.
    (api / "pyproject.toml").write_text(
        base_pyproject + '\n[tool.haumea]\ninclude = ["Pkg_D"]\n'
    )
    with_proj_inc = build(api, platform=None, output=tmp_path / "out-proj-inc")
    capsys.readouterr()
    beta_with_inc = dists_for(with_proj_inc, "beta")
    assert always | {"pkg-b", "pkg-d"} <= beta_with_inc
    assert not ({"pkg-a", "pkg-trans", "pkg-c"} & beta_with_inc)
    assert json.loads(invoke(with_proj_inc, "beta", "/beta")["body"]) == {
        "result": "pkg-b"
    }

    # `tree_shake = false` restores the full store into every function.
    (api / "pyproject.toml").write_text(
        base_pyproject + "\n[tool.haumea]\ntree_shake = false\n"
    )
    full = build(api, platform=None, output=tmp_path / "out-full")
    full_report = capsys.readouterr().out
    all_expected = always | {"pkg-a", "pkg-trans", "pkg-b", "pkg-c", "pkg-d"}
    for func_name in ("alpha", "beta", "dynamic", "dunder", "included"):
        func_dists = dists_for(full, func_name)
        assert all_expected <= func_dists
        line = next(
            row for row in full_report.splitlines() if row.strip().startswith(func_name)
        )
        assert f"({len(func_dists)} distributions)" in line
    assert json.loads(invoke(full, "alpha", "/alpha")["body"]) == {
        "result": "pkg-a+from-trans:ok"
    }
    assert json.loads(invoke(full, "beta", "/beta")["body"]) == {"result": "pkg-b"}


def make_dist(
    name: str,
    module: str,
    *,
    requires: list[str] | None = None,
    extra_files: dict[str, str] | None = None,
    top_level: str | None = None,
) -> dict[str, str]:
    """Files for a fake installed distribution: a package, `METADATA` and
    `RECORD`, ready for `write_project` into a store."""
    dist_info = f"{name.replace('-', '_')}-1.0.dist-info"
    meta_lines = ["Metadata-Version: 2.1", f"Name: {name}", "Version: 1.0"]
    for req in requires or ():
        meta_lines.append(f"Requires-Dist: {req}")
    files: dict[str, str] = {
        f"{module}/__init__.py": f"NAME = {name!r}\n",
        **(extra_files or {}),
    }
    record_lines = [f"{path},," for path in sorted(files)]
    record_lines.append(f"{dist_info}/METADATA,,")
    record_lines.append(f"{dist_info}/RECORD,,")
    files[f"{dist_info}/METADATA"] = "\n".join(meta_lines) + "\n"
    if top_level is not None:
        files[f"{dist_info}/top_level.txt"] = top_level
        record_lines.append(f"{dist_info}/top_level.txt,,")
    files[f"{dist_info}/RECORD"] = "\n".join(record_lines) + "\n"
    return files


# @lat: [[tests#Import tracer and closure]]
def test_trace_and_closure_edge_cases(tmp_path, isolated_imports, monkeypatch, capsys):
    from haumea.build import index_store

    store_files: dict[str, str] = {}
    for dist in (
        make_dist("haumea", "haumea", requires=["msgspec>=0.20"]),
        make_dist("msgspec", "msgspec"),
        make_dist("vercel-runtime", "vercel_runtime"),
        make_dist("vercel-functions", "vercel_functions", requires=["vf-dep"]),
        make_dist("vf-dep", "vf_dep"),
        make_dist("cyc-a", "cyc_a", requires=["cyc-b"]),
        make_dist("cyc-b", "cyc_b", requires=["cyc-a"]),
        make_dist("ns-one", "ns_one", top_level="ns_shared\n"),
        make_dist("ns-two", "ns_two", top_level="ns_shared\n"),
        make_dist("else-pkg", "else_pkg"),
        make_dist("not-tc-pkg", "not_tc_pkg"),
        make_dist("tc-only", "tc_only"),
        make_dist("rel-pkg", "rel_pkg"),
        make_dist("init-pkg", "init_pkg"),
        make_dist("alias-pkg", "alias_pkg"),
        make_dist(
            "huge-pkg",
            "huge_pkg",
            extra_files={"huge_pkg/weights.bin": "x" * 16_384},
        ),
    ):
        store_files.update(dist)

    def fake_install(_workspace, vendor_dir, _python, _platform, _package=None):
        write_project(vendor_dir, store_files)

    monkeypatch.setattr(build_module, "_install_dependencies", fake_install)
    monkeypatch.setattr(build_module, "_check_function_imports", lambda *args: None)

    project = tmp_path / "edge"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "edge"\nversion = "0"\n',
            # Namespace-package project helper directory (no __init__.py).
            "ns_helpers/sub.py": (
                "def use():\n    import rel_pkg\n    return rel_pkg\n"
            ),
            # Package whose __init__.py itself imports init_pkg.
            "pkg_helpers/__init__.py": (
                "def init_use():\n    import init_pkg\n    return init_pkg\n"
            ),
            "pkg_helpers/mod.py": (
                "def use():\n"
                "    import importlib as il\n"
                "    from importlib import import_module as im\n"
                '    il.import_module(name="alias_pkg")\n'
                '    im(".leaf", __package__)\n'
            ),
            "pkg_helpers/leaf.py": ("def use():\n    import cyc_a\n    return cyc_a\n"),
            "pkg_helpers/shared.py": (
                "def use():\n    import ns_shared\n    return ns_shared\n"
            ),
            "pkg_helpers/sub/__init__.py": "",
            "pkg_helpers/sub/deep.py": (
                "from ..shared import use as shared_use\n\n"
                "def run():\n    return shared_use()\n"
            ),
            "app/traced/route.py": (
                "import os\n"
                "from typing import TYPE_CHECKING\n"
                "from ns_helpers import sub\n"
                "from pkg_helpers.mod import use\n"
                "from pkg_helpers.sub.deep import run\n\n"
                "if TYPE_CHECKING:\n"
                "    import tc_only\n"
                "else:\n"
                "    def runtime_else():\n"
                "        import else_pkg\n"
                "        return else_pkg\n\n"
                "if not TYPE_CHECKING:\n"
                "    def runtime_not_tc():\n"
                "        import not_tc_pkg\n"
                "        return not_tc_pkg\n"
                "else:\n"
                "    import tc_only\n\n"
                "if TYPE_CHECKING and True:\n"
                "    import tc_only\n\n"
                "def GET():\n"
                "    if False:\n"
                "        import unknown_not_in_store\n"
                '    return {"ok": True}\n'
            ),
            "app/heavy/route.py": (
                'def GET():\n    import huge_pkg\n    return {"ok": True}\n'
            ),
            "app/tiny/route.py": ("def GET():\n    return {'ok': True}\n"),
        },
    )

    monkeypatch.setattr(build_module, "SIZE_WARN_BYTES", 8_192)
    monkeypatch.setattr(build_module, "SIZE_LIMIT_BYTES", 1024 * 1024)
    with pytest.warns(UserWarning) as record:
        out = build(project, platform=None, output=tmp_path / "out")
    capsys.readouterr()

    # Only `heavy` exceeds SIZE_WARN_BYTES; `tiny` and `traced` stay below it.
    warn_messages = [str(item.message) for item in record]
    assert len(warn_messages) == 1
    assert "'heavy'" in warn_messages[0]
    assert "huge-pkg" in warn_messages[0]

    always_with_vf = {
        "haumea",
        "msgspec",
        "vercel-runtime",
        "vercel-functions",
        "vf-dep",
    }

    traced_dists = set(
        index_store(out / "functions" / "traced.func" / "_vendor").distributions
    )
    assert traced_dists == always_with_vf | {
        "rel-pkg",
        "init-pkg",
        "alias-pkg",
        "cyc-a",
        "cyc-b",
        "ns-one",
        "ns-two",
        "else-pkg",
        "not-tc-pkg",
    }
    assert "tc-only" not in traced_dists
    assert "huge-pkg" not in traced_dists

    tiny_dists = set(
        index_store(out / "functions" / "tiny.func" / "_vendor").distributions
    )
    assert tiny_dists == always_with_vf

    heavy_dists = set(
        index_store(out / "functions" / "heavy.func" / "_vendor").distributions
    )
    assert heavy_dists == always_with_vf | {"huge-pkg"}

    # Invalid route-level `include` raises ValueError during plan_build.
    for i, bad_include in enumerate(
        ('include = "pkg-a"\n', "include = [123]\n", 'include = [""]\n')
    ):
        bad_proj = tmp_path / f"bad-{i}"
        write_project(
            bad_proj,
            {
                "pyproject.toml": '[project]\nname = "bad"\nversion = "0"\n',
                "app/route.py": bad_include + "def GET():\n    return {}\n",
            },
        )
        with pytest.raises(ValueError, match="include"):
            plan_build(bad_proj)


def lazy_middleware(*modules: str) -> str:
    """Source for a pass-through ``middleware.py`` importing ``modules`` lazily,
    so planning never needs them installed."""
    imports = "".join(f"    import {module}\n" for module in modules)
    return (
        "async def middleware(request, call_next):\n"
        f"{imports}"
        "    return await call_next(request)\n"
    )


# @lat: [[tests#Segment files as trace roots]]
def test_segment_files_are_trace_roots(tmp_path, isolated_imports, monkeypatch, capsys):
    store_files: dict[str, str] = {}
    for dist in (
        make_dist("haumea", "haumea"),
        make_dist("vercel-runtime", "vercel_runtime"),
        make_dist("root-mw-pkg", "root_mw_pkg"),
        make_dist("admin-mw-pkg", "admin_mw_pkg"),
        make_dist("admin-err-pkg", "admin_err_pkg"),
        make_dist("audit-pkg", "audit_pkg"),
        make_dist("public-mw-pkg", "public_mw_pkg"),
    ):
        store_files.update(dist)

    def fake_install(_workspace, vendor_dir, _python, _platform, _package=None):
        write_project(vendor_dir, store_files)

    monkeypatch.setattr(build_module, "_install_dependencies", fake_install)
    monkeypatch.setattr(build_module, "_check_function_imports", lambda *args: None)

    route = "def GET():\n    return {}\n"
    project = tmp_path / "segments"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "segments"\nversion = "0"\n',
            "helpers/__init__.py": "",
            "helpers/audit.py": "import audit_pkg\n",
            "app/middleware.py": lazy_middleware("root_mw_pkg"),
            "app/route.py": route,
            "app/admin/middleware.py": lazy_middleware("admin_mw_pkg", "helpers.audit"),
            "app/admin/error.py": (
                "def handle_error(request, exc):\n"
                "    import admin_err_pkg\n"
                "    raise exc\n"
            ),
            "app/admin/route.py": route,
            "app/admin/(team)/members/route.py": route,
            "app/admin/settings/[id]/route.py": route,
            "app/public/middleware.py": lazy_middleware("public_mw_pkg"),
            "app/public/route.py": route,
            "app/(site)/about/route.py": route,
        },
    )
    app = (project / "app").resolve()

    plan = plan_build(project, platform=None)
    by_name = {spec.name: spec for spec in plan.functions}
    admin_segments = (
        app / "middleware.py",
        app / "admin" / "middleware.py",
        app / "admin" / "error.py",
    )
    for name in ("admin", "admin/members", "admin/settings/[id]"):
        spec = by_name[name]
        assert spec.trace_roots == (spec.route.file, *admin_segments)
    assert by_name["public"].trace_roots == (
        app / "public" / "route.py",
        app / "middleware.py",
        app / "public" / "middleware.py",
    )
    for name in ("index", "about"):
        spec = by_name[name]
        assert spec.trace_roots == (spec.route.file, app / "middleware.py")

    out = build(
        project, platform=None, output=tmp_path / "out", explain="admin/members"
    )
    explain_out = capsys.readouterr().out

    def bundled(function: str) -> set[str]:
        vendor = out / "functions" / f"{function}.func" / "_vendor"
        return set(build_module.index_store(vendor).distributions)

    base = {"haumea", "vercel-runtime", "root-mw-pkg"}
    admin_only = {"admin-mw-pkg", "admin-err-pkg", "audit-pkg"}
    for name in ("admin", "admin/members", "admin/settings/[id]"):
        assert bundled(name) == base | admin_only
    assert bundled("public") == base | {"public-mw-pkg"}
    for name in ("index", "about"):
        assert bundled(name) == base

    reasons = {
        line.split()[0]: line for line in explain_out.splitlines() if "traced" in line
    }
    assert reasons.keys() == {"root-mw-pkg"} | admin_only
    assert reasons["root-mw-pkg"].endswith("traced from app/middleware.py")
    assert reasons["admin-mw-pkg"].endswith("traced from app/admin/middleware.py")
    assert reasons["admin-err-pkg"].endswith("traced from app/admin/error.py")
    assert reasons["audit-pkg"].endswith(
        "traced from app/admin/middleware.py -> helpers/audit.py"
    )
    excluded = explain_out.split("Excluded (", 1)[1].split()
    assert "public-mw-pkg" in excluded


# @lat: [[tests#Build-time import check]]
def test_build_import_check_catches_computed_import(
    tmp_path, isolated_imports, monkeypatch, capsys
):
    repo_root = Path(__file__).parent.parent.resolve()
    host_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    workspace = tmp_path / "check-ws"
    write_project(
        workspace,
        {
            "pyproject.toml": (
                '[tool.uv.workspace]\nmembers = ["apps/*", "packages/*"]\n'
            ),
        },
    )
    _write_workspace_pkg(
        workspace, "pkg-a", "pkg_a", 'def value() -> str:\n    return "from-pkg-a"\n'
    )

    base_pyproject = (
        '[project]\nname = "api"\nversion = "0.1.0"\n'
        'requires-python = ">=3.12"\n'
        'dependencies = ["haumea", "pkg-a"]\n\n'
        "[tool.uv.sources]\n"
        f"haumea = {{ path = {json.dumps(repo_root.as_posix())}, editable = true }}\n"
        "pkg-a = { workspace = true }\n"
    )
    computed_route = (
        "import importlib\n\n"
        'MOD = importlib.import_module("".join(["pkg", "_a"]))\n\n'
        "def GET():\n"
        '    return {"value": MOD.value()}\n'
    )
    write_project(
        workspace / "apps" / "api",
        {
            "pyproject.toml": base_pyproject,
            "app/ok/route.py": 'def GET():\n    return {"ok": True}\n',
            "app/computed/route.py": computed_route,
        },
    )
    subprocess.run(["uv", "lock", "--quiet"], cwd=workspace, check=True)
    monkeypatch.syspath_prepend(str(workspace / "packages" / "pkg-a" / "src"))
    monkeypatch.setenv("PYTHONPATH", str(workspace / "packages" / "pkg-a" / "src"))

    api = workspace / "apps" / "api"

    # Without `include`, the computed import is missed by static tracing and
    # caught by the build-time import check.
    with pytest.raises(RuntimeError) as exc_info:
        build(api, python=host_python, platform=None, output=tmp_path / "out-fail")
    message = str(exc_info.value)
    assert "computed" in message
    assert "pkg_a" in message
    assert "include" in message
    capsys.readouterr()

    # Adding route-level `include` bundles pkg-a and lets the check pass.
    (api / "app" / "computed" / "route.py").write_text(
        'include = ["pkg-a"]\n' + computed_route
    )
    for mod_name in [k for k in sys.modules if k.startswith("haumea_routes.")]:
        sys.modules.pop(mod_name, None)
    out_route = build(
        api, python=host_python, platform=None, output=tmp_path / "out-route-inc"
    )
    capsys.readouterr()
    assert not list((out_route / "functions").rglob("__pycache__"))
    assert json.loads(invoke(out_route, "computed", "/computed")["body"]) == {
        "value": "from-pkg-a"
    }

    # Project-level `[tool.haumea] include` also satisfies the check.
    (api / "app" / "computed" / "route.py").write_text(computed_route)
    for mod_name in [k for k in sys.modules if k.startswith("haumea_routes.")]:
        sys.modules.pop(mod_name, None)
    (api / "pyproject.toml").write_text(
        base_pyproject + '\n[tool.haumea]\ninclude = ["pkg-a"]\n'
    )
    out_proj = build(
        api, python=host_python, platform=None, output=tmp_path / "out-proj-inc"
    )
    capsys.readouterr()
    assert json.loads(invoke(out_proj, "computed", "/computed")["body"]) == {
        "value": "from-pkg-a"
    }


# @lat: [[tests#Import check platform matching]]
def test_build_import_check_skips_non_matching_platform(
    tmp_path, isolated_imports, monkeypatch, capsys
):
    project = tmp_path / "plat"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "plat"\nversion = "0"\n',
            "app/route.py": "def GET():\n    return {'ok': True}\n",
        },
    )
    monkeypatch.setattr(build_module, "_install_dependencies", lambda *args: None)

    uv_calls: list[list[str]] = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:3] == ["uv", "python", "find"]:
            uv_calls.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(build_module.subprocess, "run", spy_run)

    # On macOS arm64, DEFAULT_PLATFORM (x86_64-manylinux_2_28) skips the check.
    monkeypatch.setattr(build_module, "_host_os_arch", lambda: ("darwin", "aarch64"))
    out = build(project, output=tmp_path / "out-darwin")
    assert out.is_dir()
    skipped_out = capsys.readouterr().out
    assert "skip" in skipped_out.lower()
    assert build_module.DEFAULT_PLATFORM in skipped_out
    assert uv_calls == []

    # On Linux x86_64, a non-matching platform (e.g. aarch64-apple-darwin) also skips.
    monkeypatch.setattr(build_module, "_host_os_arch", lambda: ("linux", "x86_64"))
    out_linux_skip = build(
        project, platform="aarch64-apple-darwin", output=tmp_path / "out-linux-skip"
    )
    assert out_linux_skip.is_dir()
    linux_skip_out = capsys.readouterr().out
    assert "skip" in linux_skip_out.lower()
    assert "aarch64-apple-darwin" in linux_skip_out
    assert uv_calls == []

    # On Linux x86_64, DEFAULT_PLATFORM matches the host so the check runs
    # (and fails here because `_install_dependencies` was stubbed without haumea).
    host_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    with pytest.raises(RuntimeError) as exc_info:
        build(
            project,
            python=host_python,
            platform=build_module.DEFAULT_PLATFORM,
            output=tmp_path / "out-linux-match",
        )
    assert "index" in str(exc_info.value)
    assert "haumea" in str(exc_info.value)
    assert uv_calls == [["uv", "python", "find", host_python]]


# @lat: [[tests#Import check target interpreter]]
def test_build_import_check_uses_uv_python_find(
    tmp_path, isolated_imports, monkeypatch, capsys
):
    repo_root = Path(__file__).parent.parent.resolve()
    host_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    workspace = tmp_path / "interp-ws"
    write_project(
        workspace,
        {
            "pyproject.toml": '[tool.uv.workspace]\nmembers = ["apps/*"]\n',
        },
    )
    write_project(
        workspace / "apps" / "api",
        {
            "pyproject.toml": (
                '[project]\nname = "api"\nversion = "0.1.0"\n'
                'requires-python = ">=3.12"\n'
                'dependencies = ["haumea"]\n\n'
                "[tool.uv.sources]\n"
                f"haumea = {{ path = {json.dumps(repo_root.as_posix())}, "
                "editable = true }\n"
            ),
            "app/route.py": 'def GET():\n    return {"ok": True}\n',
        },
    )
    subprocess.run(["uv", "lock", "--quiet"], cwd=workspace, check=True)
    api = workspace / "apps" / "api"

    # Build a wrapper script at a distinct path so we can prove the import check
    # invokes the interpreter returned by `uv python find`, not `sys.executable`.
    marker = tmp_path / "invocations.log"
    wrapper = tmp_path / "resolved-python"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'printf "%s %s %s\\n" "$1" "$2" "$3" >> {json.dumps(str(marker))}\n'
        f'exec {json.dumps(sys.executable)} "$@"\n'
    )
    wrapper.chmod(0o755)

    real_run = subprocess.run
    find_calls: list[list[str]] = []
    find_returncode = 0
    find_stdout = f"{wrapper}\n"

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:3] == ["uv", "python", "find"]:
            find_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, find_returncode, find_stdout, "")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(build_module.subprocess, "run", fake_run)

    out = build(api, python=host_python, platform=None, output=tmp_path / "out-ok")
    assert out.is_dir()
    capsys.readouterr()
    assert find_calls == [["uv", "python", "find", host_python]]
    invoked = marker.read_text().splitlines()
    assert len(invoked) == 1
    assert "-I" in invoked[0].split()

    # Passing a different `--python` forwards that version to `uv python find`.
    find_calls.clear()
    marker.unlink()
    real_install = build_module._install_dependencies

    def install_for_host(ws_root, vendor_dir, _py, plat, pkg=None):
        real_install(ws_root, vendor_dir, host_python, plat, pkg)

    monkeypatch.setattr(build_module, "_install_dependencies", install_for_host)
    out_other = build(api, python="3.13", platform=None, output=tmp_path / "out-313")
    assert out_other.is_dir()
    capsys.readouterr()
    assert find_calls == [["uv", "python", "find", "3.13"]]
    assert marker.is_file()

    # When `uv python find` finds no interpreter, skip with a message instead of
    # falling back to `sys.executable`, even when `--python` matches host_python.
    find_calls.clear()
    marker.unlink()
    find_returncode = 2
    find_stdout = ""
    monkeypatch.setattr(build_module, "_install_dependencies", lambda *args: None)
    out_missing = build(
        api, python=host_python, platform=None, output=tmp_path / "out-no-interp"
    )
    assert out_missing.is_dir()
    skip_out = capsys.readouterr().out
    assert "skip" in skip_out.lower()
    assert host_python in skip_out
    assert find_calls == [["uv", "python", "find", host_python]]
    assert not marker.exists()


# @lat: [[tests#Build explain]]
def test_build_explain(tmp_path, isolated_imports, monkeypatch, capsys):
    repo_root = Path(__file__).parent.parent.resolve()
    workspace = tmp_path / "explain-ws"
    write_project(
        workspace,
        {
            "pyproject.toml": (
                '[tool.uv.workspace]\nmembers = ["apps/*", "packages/*"]\n'
            ),
        },
    )
    _write_workspace_pkg(workspace, "pkg-leaf", "pkg_leaf", 'VAL = "leaf"\n')
    _write_workspace_pkg(
        workspace,
        "pkg-mid",
        "pkg_mid",
        'import pkg_leaf\nVAL = f"mid+{pkg_leaf.VAL}"\n',
        deps=["pkg-leaf"],
    )
    _write_workspace_pkg(
        workspace,
        "pkg-a",
        "pkg_a",
        'import pkg_mid\ndef run() -> str:\n    return f"a+{pkg_mid.VAL}"\n',
        deps=["pkg-mid"],
    )
    _write_workspace_pkg(workspace, "pkg-direct", "pkg_direct", 'NAME = "direct"\n')
    _write_workspace_pkg(
        workspace, "pkg-route-inc", "pkg_route_inc", 'NAME = "route-inc"\n'
    )
    _write_workspace_pkg(
        workspace, "pkg-proj-inc", "pkg_proj_inc", 'NAME = "proj-inc"\n'
    )
    _write_workspace_pkg(workspace, "pkg-only-beta", "pkg_only_beta", 'NAME = "beta"\n')

    base_pyproject = (
        '[project]\nname = "api"\nversion = "0.1.0"\n'
        'requires-python = ">=3.12"\n'
        "dependencies = [\n"
        '    "haumea",\n'
        '    "pkg-a",\n'
        '    "pkg-direct",\n'
        '    "pkg-route-inc",\n'
        '    "pkg-proj-inc",\n'
        '    "pkg-only-beta",\n'
        "]\n\n"
        "[tool.uv.sources]\n"
        f"haumea = {{ path = {json.dumps(repo_root.as_posix())}, editable = true }}\n"
        "pkg-a = { workspace = true }\n"
        "pkg-direct = { workspace = true }\n"
        "pkg-route-inc = { workspace = true }\n"
        "pkg-proj-inc = { workspace = true }\n"
        "pkg-only-beta = { workspace = true }\n"
    )
    api = workspace / "apps" / "api"
    write_project(
        api,
        {
            "pyproject.toml": (
                base_pyproject + '\n[tool.haumea]\ninclude = ["pkg_proj_inc"]\n'
            ),
            "helpers/__init__.py": "",
            "helpers/a.py": (
                "import pkg_a\n\ndef run() -> str:\n    return pkg_a.run()\n"
            ),
            "app/alpha/route.py": (
                "import pkg_direct\n"
                "from helpers.a import run\n\n"
                'include = ["Pkg_Route_Inc"]\n\n'
                "def GET():\n"
                '    return {"direct": pkg_direct.NAME, "a": run()}\n'
            ),
            "app/beta/route.py": (
                "import pkg_only_beta\n\n"
                "def GET():\n"
                '    return {"beta": pkg_only_beta.NAME}\n'
            ),
        },
    )
    subprocess.run(["uv", "lock", "--quiet"], cwd=workspace, check=True)

    for dist_name in (
        "pkg-leaf",
        "pkg-mid",
        "pkg-a",
        "pkg-direct",
        "pkg-route-inc",
        "pkg-proj-inc",
        "pkg-only-beta",
    ):
        monkeypatch.syspath_prepend(str(workspace / "packages" / dist_name / "src"))

    def parse_explain_sections(output: str) -> tuple[dict[str, str], list[str]]:
        included: dict[str, str] = {}
        excluded: list[str] = []
        section: str | None = None
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("Included ("):
                section = "included"
                continue
            if stripped.startswith("Excluded ("):
                section = "excluded"
                continue
            if section == "included":
                if not line.startswith("    "):
                    section = None
                    continue
                if stripped == "(none)":
                    continue
                name, rest = stripped.split(None, 1)
                included[name] = rest
            elif section == "excluded":
                if not line.startswith("    "):
                    section = None
                    continue
                if stripped == "(none)":
                    continue
                excluded.append(stripped.split()[0])
        return included, excluded

    monkeypatch.chdir(api)
    out_alpha = tmp_path / "out-alpha"
    cli_main(
        [
            "build",
            "--platform",
            "native",
            "--output",
            str(out_alpha),
            "--explain",
            "alpha",
        ]
    )
    alpha_stdout = capsys.readouterr().out
    assert "Explain alpha:" in alpha_stdout
    inc, exc = parse_explain_sections(alpha_stdout)

    assert "always included" in inc["haumea"]
    assert "always included" in inc["vercel-runtime"]
    assert "required by haumea <- always included" in inc["msgspec"]
    assert "traced from app/alpha/route.py" in inc["pkg-direct"]
    assert "traced from app/alpha/route.py -> helpers/a.py" in inc["pkg-a"]
    assert (
        "required by pkg-a <- traced from app/alpha/route.py -> helpers/a.py"
        in inc["pkg-mid"]
    )
    assert (
        "required by pkg-mid <- pkg-a <- traced from app/alpha/route.py -> helpers/a.py"
        in inc["pkg-leaf"]
    )
    assert "listed in route include (app/alpha/route.py)" in inc["pkg-route-inc"]
    assert "listed in [tool.haumea] include" in inc["pkg-proj-inc"]

    assert "pkg-only-beta" in exc
    assert "pkg-only-beta" not in inc
    assert exc == sorted(exc)
    alpha_vendor = out_alpha / "functions" / "alpha.func" / "_vendor"
    assert not (alpha_vendor / "pkg_only_beta").exists()
    assert "pkg-only-beta" not in build_module.index_store(alpha_vendor).distributions

    # Trailing `.func` suffix is normalized to match the function name.
    out_beta = tmp_path / "out-beta"
    build(api, platform=None, output=out_beta, explain="beta.func")
    beta_stdout = capsys.readouterr().out
    assert "Explain beta:" in beta_stdout
    beta_inc, beta_exc = parse_explain_sections(beta_stdout)
    assert "traced from app/beta/route.py" in beta_inc["pkg-only-beta"]
    assert "listed in [tool.haumea] include" in beta_inc["pkg-proj-inc"]
    for excluded_name in (
        "pkg-a",
        "pkg-mid",
        "pkg-leaf",
        "pkg-direct",
        "pkg-route-inc",
    ):
        assert excluded_name in beta_exc
        assert excluded_name not in beta_inc

    # `tree_shake = false` explains every store distribution with none excluded.
    (api / "pyproject.toml").write_text(
        base_pyproject + "\n[tool.haumea]\ntree_shake = false\n"
    )
    out_full = tmp_path / "out-full"
    build(api, platform=None, output=out_full, explain="alpha")
    full_stdout = capsys.readouterr().out
    assert "Explain alpha:" in full_stdout
    full_inc, full_exc = parse_explain_sections(full_stdout)
    assert full_exc == []
    assert "(none)" in full_stdout
    for dist_name in (
        "haumea",
        "pkg-a",
        "pkg-mid",
        "pkg-leaf",
        "pkg-only-beta",
    ):
        assert "tree_shake = false" in full_inc[dist_name]

    # Unknown `--explain` function raises ValueError naming it and available functions,
    # before touching an existing output directory.
    sentinel = out_alpha / "keep.txt"
    sentinel.write_text("untouched")
    with pytest.raises(ValueError) as exc_info:
        cli_main(["build", "--output", str(out_alpha), "--explain", "missing"])
    msg = str(exc_info.value)
    assert "missing" in msg
    assert "alpha" in msg
    assert "beta" in msg
    assert sentinel.read_text() == "untouched"


def stream_scope(method: str = "GET") -> dict:
    return {
        "type": "http",
        "method": method,
        "path": "/s",
        "root_path": "",
        "headers": [],
        "query_string": b"",
    }


def call_asgi(app, receive, method="GET", on_send=None) -> list[dict]:
    """Run one ``/s`` request through ``app``; return the sent messages.

    ``on_send(sent)`` runs after each message, to react to the stream.
    """
    scope = stream_scope(method)
    sent: list[dict] = []

    async def send(message):
        sent.append(message)
        if on_send:
            on_send(sent)

    async def run():
        await asyncio.wait_for(app(scope, receive, send), timeout=5)

    asyncio.run(run())
    return sent


def stream_app(response_factory) -> Haumea:
    app = Haumea(docs=False)

    def handler():
        return response_factory()

    app.add_route("/s", handler, ["GET"])
    return app


async def wait_for_disconnect():
    """A ``receive`` that never disconnects until the response is done."""
    await asyncio.Event().wait()


def body_messages(sent: list[dict]) -> list[dict]:
    return [m for m in sent if m["type"] == "http.response.body"]


async def async_chunks():
    for chunk in (b"one ", "two ", b"three"):
        await asyncio.sleep(0)
        yield chunk


def sync_chunks():
    yield from (b"one ", "two ", b"three")


# @lat: [[tests#Streaming responses]]
@pytest.mark.parametrize(
    "make_content",
    [async_chunks, sync_chunks, lambda: [b"one ", "two ", b"three"]],
    ids=["async-generator", "sync-generator", "list"],
)
def test_streaming_response_streams_chunks_in_order(make_content):
    app = stream_app(
        lambda: StreamingResponse(
            make_content(),
            status_code=201,
            headers={"Content-Length": "99", "X-Trace": "t"},
            media_type="text/plain",
        )
    )
    sent = call_asgi(app, wait_for_disconnect)
    start = sent[0]
    assert start["status"] == 201
    headers = dict(start["headers"])
    assert b"content-length" not in headers
    assert headers[b"x-trace"] == b"t"
    assert headers[b"content-type"] == b"text/plain; charset=utf-8"
    bodies = body_messages(sent)
    assert [m["body"] for m in bodies] == [b"one ", b"two ", b"three", b""]
    assert [m["more_body"] for m in bodies] == [True, True, True, False]

    # HEAD sends headers and an empty final body without running the producer.
    consumed = []

    def tracked():
        consumed.append(True)
        yield b"never sent"

    head = call_asgi(
        stream_app(lambda: StreamingResponse(tracked())), wait_for_disconnect, "HEAD"
    )
    assert consumed == []
    assert body_messages(head) == [
        {"type": "http.response.body", "body": b"", "more_body": False}
    ]
    assert b"content-length" not in dict(head[0]["headers"])

    class NoIter:
        def __iter__(self):
            raise AssertionError("__iter__ called for HEAD")

    class NoAiter:
        def __aiter__(self):
            raise AssertionError("__aiter__ called for HEAD")

    for make in (
        lambda: StreamingResponse(NoIter()),
        lambda: StreamingResponse(NoAiter()),
        lambda: SSEResponse(NoIter()),
        lambda: SSEResponse(NoAiter()),
    ):
        head = call_asgi(stream_app(make), wait_for_disconnect, "HEAD")
        assert head[0]["type"] == "http.response.start"
        assert body_messages(head) == [
            {"type": "http.response.body", "body": b"", "more_body": False}
        ]


def endless_sync(closed: list):
    try:
        n = 0
        while True:
            time.sleep(0.001)
            yield f"{n},"
            n += 1
    finally:
        closed.append(True)


async def endless_async(closed: list):
    try:
        n = 0
        while True:
            await asyncio.sleep(0.001)
            yield f"{n},"
            n += 1
    finally:
        closed.append(True)


# @lat: [[tests#Streaming disconnect]]
@pytest.mark.parametrize(
    "producer", [endless_async, endless_sync], ids=["async", "sync"]
)
def test_streaming_response_disconnect_closes_generator(producer):
    closed: list = []
    disconnect: list[asyncio.Event] = []

    async def receive():
        # A request body message first, then wait until a few chunks went out.
        if not disconnect:
            disconnect.append(asyncio.Event())
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect[0].wait()
        return {"type": "http.disconnect"}

    def on_send(sent):
        if len(body_messages(sent)) == 3:
            disconnect[0].set()

    app = stream_app(lambda: StreamingResponse(producer(closed)))
    sent = call_asgi(app, receive, on_send=on_send)
    assert closed == [True]
    bodies = body_messages(sent)
    assert len(bodies) >= 3
    assert all(m["more_body"] for m in bodies)
    chunks = b"".join(m["body"] for m in bodies).decode().rstrip(",").split(",")
    assert chunks == [str(n) for n in range(len(chunks))]


class Point(msgspec.Struct):
    x: int
    y: int


# @lat: [[tests#SSE framing]]
def test_sse_response_framing_and_pings():
    state: dict = {}

    async def events():
        yield ServerSentEvent(
            data="multi\nline\r\nevent", event="update", id="7", retry=1500
        )
        yield "plain"
        yield {"a": [1, "two"]}
        yield Point(x=1, y=2)
        yield ServerSentEvent(event="empty")
        state["resume"] = asyncio.Event()
        await state["resume"].wait()
        yield "done"

    def on_send(sent):
        if sent[-1].get("body") == b": ping\n\n" and "resume" in state:
            state["resume"].set()

    app = stream_app(lambda: SSEResponse(events(), ping=0.01))
    sent = call_asgi(app, wait_for_disconnect, on_send=on_send)
    headers = dict(sent[0]["headers"])
    assert headers[b"content-type"] == b"text/event-stream"
    assert headers[b"cache-control"] == b"no-cache"
    assert b"content-length" not in headers
    body = b"".join(m["body"] for m in body_messages(sent)).decode()
    expected_head = (
        "event: update\nid: 7\nretry: 1500\n"
        "data: multi\ndata: line\ndata: event\n\n"
        "data: plain\n\n"
        'data: {"a":[1,"two"]}\n\n'
        'data: {"x":1,"y":2}\n\n'
        "event: empty\n\n"
    )
    assert body.startswith(expected_head)
    rest = body[len(expected_head) :]
    assert re.fullmatch(r"(: ping\n\n)+data: done\n\n", rest)

    # Sync iterables work too, without pings.
    sync_sent = call_asgi(
        stream_app(lambda: SSEResponse(["a", 1])), wait_for_disconnect
    )
    sync_body = b"".join(m["body"] for m in body_messages(sync_sent))
    assert sync_body == b"data: a\n\ndata: 1\n\n"

    # A raw `None` is JSON `null`; only a `ServerSentEvent` without data omits it.
    nulls = [None, ServerSentEvent(data=None), ServerSentEvent(event="tick")]
    null_sent = call_asgi(stream_app(lambda: SSEResponse(nulls)), wait_for_disconnect)
    null_body = b"".join(m["body"] for m in body_messages(null_sent))
    assert null_body == b"data: null\n\ndata: null\n\nevent: tick\n\n"
    assert ServerSentEvent().encode() == b"\n\n"

    # Newlines in single-line fields would break framing.
    with pytest.raises(ValueError):
        ServerSentEvent(data="x", event="a\nb")
    with pytest.raises(ValueError):
        ServerSentEvent(data="x", id="1\r2")


# @lat: [[tests#Streaming fast producer disconnect]]
def test_streaming_response_disconnect_interrupts_fast_producer():
    """Neither the producer nor ``send`` suspends, yet a disconnect wins."""
    closed: list = []

    async def fast():
        try:
            for n in range(10_000):
                yield f"{n},"
        finally:
            closed.append(True)

    ready: list = []

    async def receive():
        while not ready:
            await asyncio.sleep(0)
        return {"type": "http.disconnect"}

    def on_send(sent):
        ready.append(True)

    sent = call_asgi(
        stream_app(lambda: StreamingResponse(fast())), receive, on_send=on_send
    )
    bodies = body_messages(sent)
    assert len(bodies) < 100
    assert all(m["more_body"] for m in bodies)
    assert closed == [True]


# @lat: [[tests#Streaming receive errors]]
def test_streaming_response_propagates_receive_errors():
    closed: list = []

    async def receive():
        raise RuntimeError("receive failed")

    app = stream_app(lambda: StreamingResponse(endless_async(closed)))
    with pytest.raises(RuntimeError, match="receive failed"):
        call_asgi(app, receive)
    assert closed == [True]


# @lat: [[tests#Streaming cancellation during cleanup]]
def test_streaming_response_cancel_during_disconnect_cleanup():
    async def run():
        disconnect = asyncio.Event()
        cleanup_started = asyncio.Event()
        release = asyncio.Event()
        cleaned: list = []

        async def producer():
            try:
                yield "first"
                await asyncio.Event().wait()  # suspended inside the generator
            finally:
                cleanup_started.set()
                await release.wait()
                cleaned.append(True)

        async def receive():
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message.get("body") == b"first":
                disconnect.set()

        app = stream_app(lambda: StreamingResponse(producer()))
        request = asyncio.ensure_future(app(stream_scope(), receive, send))
        await cleanup_started.wait()
        request.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not request.done(), "returned before the producer finished cleanup"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert cleaned == [True]

    asyncio.run(asyncio.wait_for(run(), timeout=5))


# @lat: [[tests#SSE headers and ping validation]]
def test_sse_response_enforces_headers_and_validates_ping():
    response = SSEResponse(
        ["a"],
        headers={
            "Content-Type": "text/plain",
            "Cache-Control": "max-age=60",
            "X-Extra": "1",
        },
    )
    sent = call_asgi(stream_app(lambda: response), wait_for_disconnect)
    headers = dict(sent[0]["headers"])
    assert headers[b"content-type"] == b"text/event-stream"
    assert headers[b"cache-control"] == b"no-cache"
    assert headers[b"x-extra"] == b"1"

    for ping in (0, -1, 0.0, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="ping"):
            SSEResponse(["a"], ping=ping)
    SSEResponse(["a"], ping=None)
    SSEResponse(["a"], ping=0.5)


def cookie_request(*cookie_headers: str) -> Request:
    scope = stream_scope()
    scope["headers"] = [
        (b"cookie", value.encode("latin-1")) for value in cookie_headers
    ]
    return Request(scope, wait_for_disconnect)


# @lat: [[tests#Request cookies]]
@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ((), {}),
        (("",), {}),
        (("a=1",), {"a": "1"}),
        (("a=1; b=2",), {"a": "1", "b": "2"}),
        (("  a = 1 ;b=2;  ;",), {"a": "1", "b": "2"}),
        (("token=abc==; next=/x?y=1",), {"token": "abc==", "next": "/x?y=1"}),
        (('q="hello world"; e=""',), {"q": "hello world", "e": ""}),
        (('s="a\\"b\\\\c\\073d"',), {"s": 'a"b\\c;d'}),
        (("flag; a=1",), {"a": "1"}),
        (("a=1", "b=2; c=3"), {"a": "1", "b": "2", "c": "3"}),
        (("a=first; a=second",), {"a": "first"}),
    ],
)
def test_request_cookies(headers, expected):
    assert cookie_request(*headers).cookies == expected


# @lat: [[tests#Cookie parameters]]
def test_cookie_parameters():
    assert Cookie is params_module.Cookie
    app = Haumea()

    def current_user(session: str = Cookie()) -> str:
        return f"user:{session}"

    def handler(
        user: str = Depends(current_user),
        count: int = Cookie(0),
        theme: Annotated[str | None, Cookie(alias="ui-theme")] = None,
    ):
        return {"user": user, "count": count, "theme": theme}

    app.add_route("/c", handler, ["GET"])
    request = make_client(app)

    response = request("GET", "/c", headers={"cookie": "session=s1"})
    assert response.json() == {"user": "user:s1", "count": 0, "theme": None}

    cookie = "session=s1; count=7; ui-theme=dark; theme=ignored"
    response = request("GET", "/c", headers={"cookie": cookie})
    assert response.json() == {"user": "user:s1", "count": 7, "theme": "dark"}

    response = request("GET", "/c", headers={"cookie": "count=many"})
    assert response.status_code == 422
    locs = [error["loc"] for error in response.json()["detail"]]
    assert locs == [["cookie", "session"], ["cookie", "count"]]

    parameters = app.openapi()["paths"]["/c"]["get"]["parameters"]
    assert [(p["name"], p["in"], p["required"]) for p in parameters] == [
        ("session", "cookie", True),
        ("count", "cookie", False),
        ("ui-theme", "cookie", False),
    ]


def sent_headers(response_factory) -> list[tuple[bytes, bytes]]:
    """The raw header list an app returning ``response_factory()`` sends."""
    sent = call_asgi(stream_app(response_factory), wait_for_disconnect)
    return sent[0]["headers"]


def set_cookie_headers(response_factory) -> list[str]:
    return [
        value.decode("latin-1")
        for key, value in sent_headers(response_factory)
        if key == b"set-cookie"
    ]


# @lat: [[tests#Set-cookie headers]]
def test_set_cookie_and_delete_cookie():
    def factory():
        response = Response("ok", headers={"X-Extra": "1"})
        response.set_cookie("session", "abc")
        response.set_cookie(
            "prefs",
            "a b",
            max_age=3600,
            expires=datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC),
            path="/app",
            domain="example.com",
            secure=False,
            httponly=False,
            samesite="strict",
        )
        response.set_cookie("third", "x", samesite=None)
        response.delete_cookie("old", path="/app", domain="example.com")
        return response

    headers = sent_headers(factory)
    # Every set-cookie is its own tuple, after the single-valued headers.
    assert [key for key, _ in headers] == [
        b"x-extra",
        b"content-length",
        *[b"set-cookie"] * 4,
    ]
    assert headers[:2] == [(b"x-extra", b"1"), (b"content-length", b"2")]
    session, prefs, third, old = (value.decode() for _, value in headers[2:])
    assert session == "session=abc; HttpOnly; Path=/; SameSite=Lax; Secure"
    assert prefs.startswith('prefs="a b"; ')
    assert set(prefs.split("; ")[1:]) == {
        "Domain=example.com",
        "expires=Wed, 02 Jan 2030 03:04:05 GMT",
        "Max-Age=3600",
        "Path=/app",
        "SameSite=Strict",
    }
    assert third == "third=x; HttpOnly; Path=/; Secure"
    assert old.startswith('old=""; ')
    assert set(old.split("; ")[1:]) == {
        "Domain=example.com",
        "expires=Thu, 01 Jan 1970 00:00:00 GMT",
        "HttpOnly",
        "Max-Age=0",
        "Path=/app",
        "SameSite=Lax",
        "Secure",
    }

    response = Response()
    with pytest.raises(ValueError, match="samesite"):
        response.set_cookie("bad", samesite="sometimes")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        response.set_cookie("bad key", "x")
    # delete_cookie has exactly the spec's signature.
    with pytest.raises(TypeError):
        response.delete_cookie("old", secure=False)  # type: ignore[call-arg]


# @lat: [[tests#Set-cookie on every response type]]
@pytest.mark.parametrize(
    "make_response",
    [
        lambda: JSONResponse({}),
        lambda: StreamingResponse(iter([b"x"])),
        lambda: SSEResponse(iter(["x"])),
    ],
)
def test_set_cookie_on_every_response_type(make_response):
    def factory():
        response = make_response()
        response.set_cookie("a", "1")
        response.set_cookie("b", "2")
        return response

    a, b = set_cookie_headers(factory)
    assert a.startswith("a=1; ") and b.startswith("b=2; ")


# @lat: [[tests#Set-cookie encoding]]
@pytest.mark.parametrize(
    "kwargs",
    [
        {"value": "東京"},
        {"path": "/東京"},
        {"domain": "例え.jp"},
        {"expires": "Wed, 東京"},
        {"path": "/a; Domain=evil.com"},
        {"path": "/a\r\nX-Injected: 1"},
        {"domain": "example.com\n"},
    ],
)
def test_set_cookie_rejects_unencodable_values(kwargs):
    with pytest.raises(ValueError, match="cookie"):
        Response().set_cookie("city", **{"value": "x", **kwargs})

    def handler():
        response = Response("ok")
        response.set_cookie("city", **{"value": "x", **kwargs})
        return response

    # The error surfaces in the handler, so the client gets a whole 500
    # instead of a crash while sending headers.
    sent = call_asgi(stream_app(handler), wait_for_disconnect)
    assert sent[0]["status"] == 500
    assert body_messages(sent)[-1]["body"] == b"Internal Server Error"


# @lat: [[tests#Cookie round trip]]
@pytest.mark.parametrize(
    "value",
    ["plain", "", "a b", "x=y==", 'quo"te', "back\\slash", "semi;colon", "comma,é"],
)
def test_cookie_round_trip(value):
    app = Haumea(docs=False)

    def login():
        response = Response("set")
        response.set_cookie("a", value)
        response.set_cookie("b", "second")
        return response

    def echo(request: Request, a: str = Cookie(), b: str = Cookie()):
        return {"a": a, "b": b, "cookies": request.cookies}

    app.add_route("/login", login, ["GET"])
    app.add_route("/echo", echo, ["GET"])
    request = make_client(app)

    set_cookies = request("GET", "/login").headers.get_list("set-cookie")
    assert len(set_cookies) == 2
    # Send back what a browser would: the name=value pair of each cookie.
    cookie = "; ".join(header.split("; ")[0] for header in set_cookies)
    response = request("GET", "/echo", headers={"cookie": cookie.encode("latin-1")})
    expected = {"a": value, "b": "second"}
    assert response.json() == {**expected, "cookies": expected}


def platform_request(headers: dict[str, str], client=("10.0.0.9", 5000)) -> Request:
    scope = stream_scope()
    scope["headers"] = [(k.encode(), v.encode()) for k, v in headers.items()]
    if client is not None:
        scope["client"] = client
    return Request(scope, wait_for_disconnect)


VERCEL_HEADERS = {
    "x-vercel-id": "iad1::abc12-1700000000000-deadbeef",
    "x-real-ip": "203.0.113.7",
    "x-forwarded-for": "198.51.100.1, 10.0.0.1",
    "x-vercel-ip-country": "US",
    "x-vercel-ip-country-region": "CA",
    "x-vercel-ip-city": "San%20Francisco",
    "x-vercel-ip-latitude": "37.7749",
    "x-vercel-ip-longitude": "-122.4194",
    "x-vercel-ip-postal-code": "94103",
}


# @lat: [[tests#Platform metadata from Vercel headers]]
def test_platform_metadata_from_vercel_headers():
    assert Geo is requests_module.Geo
    request = platform_request(VERCEL_HEADERS)
    assert request.id == "iad1::abc12-1700000000000-deadbeef"
    assert request.ip == "203.0.113.7"
    assert request.geo == Geo(
        country="US",
        region="CA",
        city="San Francisco",
        latitude="37.7749",
        longitude="-122.4194",
        postal_code="94103",
    )
    # Non-ASCII city names arrive percent-encoded (RFC 3986).
    assert platform_request({"x-vercel-ip-city": "S%C3%A3o%20Paulo"}).geo == Geo(
        city="São Paulo"
    )


# @lat: [[tests#Platform client IP sources]]
@pytest.mark.parametrize(
    ("headers", "client", "expected"),
    [
        (
            {"x-real-ip": "203.0.113.7", "x-forwarded-for": "1.1.1.1"},
            None,
            "203.0.113.7",
        ),
        (
            {"x-forwarded-for": " 198.51.100.1 , 10.0.0.1, 10.0.0.2"},
            None,
            "198.51.100.1",
        ),
        ({"x-forwarded-for": "198.51.100.1"}, ("10.0.0.9", 1), "198.51.100.1"),
        ({}, ("10.0.0.9", 5000), "10.0.0.9"),
        ({}, None, None),
        ({}, (), None),
    ],
)
def test_platform_client_ip_sources(headers, client, expected):
    assert platform_request(headers, client=client).ip == expected


# @lat: [[tests#Platform metadata dev fallbacks]]
def test_platform_metadata_dev_fallbacks():
    request = platform_request({})
    request_id = request.id
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)
    assert uuid.UUID(hex=request_id).version == 4
    assert request.id == request_id
    assert platform_request({}).id != request_id
    assert request.ip == "10.0.0.9"
    assert request.geo == Geo()
    assert all(value is None for value in msgspec.structs.astuple(request.geo))


# @lat: [[tests#Platform metadata through create_app]]
def test_platform_metadata_through_create_app(tmp_path, isolated_imports):
    write_project(
        tmp_path,
        {
            "app/route.py": (
                "import msgspec\n"
                "from haumea import Request\n\n"
                "def GET(request: Request):\n"
                "    return {'id': request.id, 'ip': request.ip,\n"
                "            'geo': msgspec.structs.asdict(request.geo)}\n"
            )
        },
    )
    request = make_client(create_app(tmp_path / "app"))
    body = request("GET", "/", headers=VERCEL_HEADERS).json()
    assert body == {
        "id": "iad1::abc12-1700000000000-deadbeef",
        "ip": "203.0.113.7",
        "geo": {
            "country": "US",
            "region": "CA",
            "city": "San Francisco",
            "latitude": "37.7749",
            "longitude": "-122.4194",
            "postal_code": "94103",
        },
    }
    first, second = request("GET", "/").json(), request("GET", "/").json()
    assert re.fullmatch(r"[0-9a-f]{32}", first["id"])
    assert first["id"] != second["id"]
    assert first["ip"] == "127.0.0.1"
    assert first["geo"] == dict.fromkeys(body["geo"])


# @lat: [[tests#Platform client IP from repeated forwarded headers]]
def test_platform_ip_from_repeated_forwarded_headers():
    request = platform_request({}, client=None)
    request.scope["headers"] = [
        (b"X-Forwarded-For", b" 198.51.100.1, 10.0.0.2"),
        (b"x-forwarded-for", b"10.0.0.1"),
    ]
    assert request.ip == "198.51.100.1"
    # Request.headers keeps its last-value-wins behavior.
    assert request.headers["x-forwarded-for"] == "10.0.0.1"


# @lat: [[tests#Platform request id under concurrent reads]]
def test_platform_request_id_under_concurrent_reads():
    for _ in range(50):
        request = platform_request({})
        barrier = threading.Barrier(8)

        def read(request=request, barrier=barrier):
            barrier.wait()
            return request.id

        with ThreadPoolExecutor(8) as pool:
            ids = set(pool.map(lambda _: read(), range(8)))
        assert len(ids) == 1


SCHEMA_MODELS = """\
import msgspec


class Shared(msgspec.Struct):
    name: str


class Secret(msgspec.Struct):
    token: str


class SecretReply(msgspec.Struct):
    ok: bool
"""


# @lat: [[tests#Hidden routes leave the schema]]
def test_include_in_schema_false(tmp_path, isolated_imports):
    project = tmp_path / "hiding"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "hiding"\nversion = "0"\n',
            "schema_models.py": SCHEMA_MODELS,
            "app/items/route.py": (
                "from schema_models import Shared\n\n"
                "include_in_schema = True\n\n"
                "def POST(item: Shared) -> Shared:\n    return item\n"
            ),
            "app/internal/[key]/route.py": (
                "from schema_models import Secret, SecretReply, Shared\n\n"
                "include_in_schema = False\n\n"
                "def GET(key: str, verbose: bool = False) -> Shared:\n"
                "    return Shared(name=key)\n\n"
                "def POST(key: str, secret: Secret) -> SecretReply:\n"
                "    return SecretReply(ok=secret.token == key)\n"
            ),
        },
    )
    app = create_app(project / "app")
    client = make_client(app)

    schema = client("GET", "/openapi.json").json()
    assert list(schema["paths"]) == ["/items"]
    assert set(schema["components"]["schemas"]) == {"Shared"}
    assert "internal" not in json.dumps(schema)

    # Hidden is only about documentation: the route still serves requests.
    assert client("GET", "/internal/abc").json() == {"name": "abc"}
    reply = client("POST", "/internal/abc", json={"token": "abc"})
    assert reply.json() == {"ok": True}
    assert client("POST", "/internal/abc", json={}).status_code == 422
    assert client("DELETE", "/internal/abc").status_code == 405

    static = {f.path: f.content for f in plan_build(project).static_files}
    built = json.loads(static["openapi.json"])
    assert (built["paths"], built["components"]) == (
        schema["paths"],
        schema["components"],
    )

    # A deployed hidden function serves its route too.
    route_app = create_route_app(project / "app", "internal/[key]")
    assert make_client(route_app)("GET", "/internal/x").json() == {"name": "x"}


# @lat: [[tests#Schema with only hidden routes]]
def test_include_in_schema_only_hidden_routes(tmp_path, isolated_imports):
    project = tmp_path / "secret"
    write_project(
        project,
        {
            "schema_models.py": SCHEMA_MODELS,
            "app/route.py": (
                "from schema_models import Secret\n\n"
                "include_in_schema = False\n\n"
                "def POST(secret: Secret) -> Secret:\n    return secret\n"
            ),
        },
    )
    app = create_app(project / "app")
    assert app.openapi() == {
        "openapi": "3.1.0",
        "info": {"title": "Haumea", "version": "0.1.0"},
        "paths": {},
    }
    assert make_client(app)("POST", "/", json={"token": "t"}).json() == {"token": "t"}


# @lat: [[tests#Invalid include_in_schema]]
@pytest.mark.parametrize("value", ["'no'", "0", "None"])
def test_include_in_schema_must_be_bool(tmp_path, isolated_imports, value):
    project = tmp_path / "bad"
    write_project(
        project,
        {"app/route.py": f"include_in_schema = {value}\n\ndef GET():\n    return 1\n"},
    )
    with pytest.raises(TypeError, match="include_in_schema"):
        create_app(project / "app")


# @lat: [[tests#Lazy values#Sync lazy creates once]]
@pytest.mark.parametrize("value", [None, 0, "", False, [], object()])
def test_lazy_sync_creates_once(value):
    calls = []
    barrier = threading.Barrier(16)

    def factory():
        calls.append(1)
        time.sleep(0.01)  # widen the race window
        return value

    get = lazy(factory)

    def first_call():
        barrier.wait()
        return get()

    with ThreadPoolExecutor(16) as pool:
        results = list(pool.map(lambda _: first_call(), range(16)))
    assert all(result is value for result in results)
    assert get() is value
    assert len(calls) == 1


# @lat: [[tests#Lazy values#Async lazy creates once]]
@pytest.mark.parametrize("value", [None, 0, object()])
def test_lazy_async_creates_once(value):
    calls = []

    async def factory():
        calls.append(1)
        await asyncio.sleep(0.01)
        return value

    get = lazy(factory)
    assert inspect.iscoroutinefunction(get)

    async def main():
        return await asyncio.gather(*(get() for _ in range(16)))

    assert all(result is value for result in asyncio.run(main()))
    # The value outlives the event loop that created it.
    assert asyncio.run(get()) is value
    assert asyncio.run(main()) == [value] * 16
    assert len(calls) == 1


# @lat: [[tests#Lazy values#Failed creation is not cached]]
def test_lazy_failure_is_retried():
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("down")
        return "client"

    get = lazy(flaky)
    with pytest.raises(ConnectionError):
        get()
    assert get() == "client"
    assert get() == "client"
    assert len(attempts) == 2

    async_attempts = []

    async def async_flaky():
        async_attempts.append(1)
        await asyncio.sleep(0.01)
        if len(async_attempts) == 1:
            raise ConnectionError("down")
        return "pool"

    aget = lazy(async_flaky)

    async def main():
        return await asyncio.gather(*(aget() for _ in range(4)), return_exceptions=True)

    # Callers sharing the failed attempt all see its error...
    results = asyncio.run(main())
    assert all(isinstance(result, ConnectionError) for result in results)
    # ...and the next call starts a fresh one.
    assert asyncio.run(main()) == ["pool"] * 4
    assert len(async_attempts) == 2


# @lat: [[tests#Lazy values#Cancelled waiter keeps creation]]
def test_lazy_cancelled_waiter_does_not_cancel_creation():
    calls = []
    release = None

    async def factory():
        calls.append(1)
        await release.wait()
        return "pool"

    get = lazy(factory)

    async def main():
        nonlocal release
        release = asyncio.Event()
        first = asyncio.ensure_future(get())
        second = asyncio.ensure_future(get())
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        return await second

    assert asyncio.run(main()) == "pool"
    assert asyncio.run(get()) == "pool"
    assert len(calls) == 1


# @lat: [[tests#Lazy values#Lazy as a dependency]]
def test_lazy_as_dependency(tmp_path, isolated_imports):
    project = tmp_path / "clients"
    write_project(
        project,
        {
            "app/route.py": (
                "from typing import Annotated\n"
                "from haumea import Depends, lazy\n\n"
                "made = []\n\n"
                "def make_client(timeout: int = 5) -> dict:\n"
                "    made.append('sync')\n"
                "    return {'kind': 'sync', 'timeout': timeout}\n\n"
                "async def make_pool() -> dict:\n"
                "    made.append('async')\n"
                "    return {'kind': 'async'}\n\n"
                "get_client = lazy(make_client)\n"
                "get_pool = lazy(make_pool)\n\n"
                "def GET(client: dict = Depends(get_client),\n"
                "        pool: Annotated[dict, Depends(get_pool)] = None):\n"
                "    return {'client': client, 'pool': pool, 'made': made}\n\n"
                "async def POST(pool: dict = Depends(get_pool),\n"
                "               client: dict = Depends(get_client)):\n"
                "    return {'pool': pool, 'client': client, 'made': made}\n"
            ),
        },
    )
    app = create_app(project / "app")
    client = make_client(app)
    expected = {
        "client": {"kind": "sync", "timeout": 5},
        "pool": {"kind": "async"},
        "made": ["sync", "async"],
    }
    assert client("GET", "/?timeout=9").json() == expected
    assert client("POST", "/").json() == expected
    assert client("GET", "/").json() == expected
    # The factory's own parameters are not request inputs.
    operations = app.openapi()["paths"]["/"]
    assert all("parameters" not in op for op in operations.values())


# @lat: [[tests#Lazy values#Lazy exports]]
def test_lazy_exports():
    assert params_module.lazy is lazy
    get = lazy(lambda: 1)
    assert inspect.signature(get).parameters == {}
    assert not inspect.iscoroutinefunction(get)


# @lat: [[tests#Lazy values#Async lazy across event loops]]
def test_lazy_async_shared_across_event_loops():
    calls = []
    started = threading.Event()
    release = threading.Event()

    async def factory():
        calls.append(threading.get_ident())
        started.set()
        await asyncio.to_thread(release.wait, 5)
        return object()

    get = lazy(factory)

    joined = threading.Event()

    async def join():
        # Runs once get() below is suspended on the in-flight creation.
        asyncio.get_running_loop().call_soon(joined.set)
        return await get()

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(asyncio.run, get())
        assert started.wait(5)
        second = pool.submit(asyncio.run, join())
        assert joined.wait(5)
        release.set()
        assert first.result(5) is second.result(5)
    assert asyncio.run(get()) is first.result()
    assert len(calls) == 1


# @lat: [[tests#Lazy values#Async callable instance factory]]
def test_lazy_async_callable_instance(tmp_path, isolated_imports):
    project = tmp_path / "callable"
    write_project(
        project,
        {
            "app/route.py": (
                "from haumea import Depends, lazy\n\n"
                "class Pool:\n"
                "    calls = 0\n\n"
                "    async def __call__(self) -> dict:\n"
                "        Pool.calls += 1\n"
                "        return {'pool': Pool.calls}\n\n"
                "get_pool = lazy(Pool())\n\n"
                "def GET(pool: dict = Depends(get_pool)):\n"
                "    return {'pool': pool, 'calls': Pool.calls}\n"
            ),
        },
    )
    client = make_client(create_app(project / "app"))
    expected = {"pool": {"pool": 1}, "calls": 1}
    assert client("GET", "/").json() == expected
    assert client("GET", "/").json() == expected


def asyncio_errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "asyncio"]


# @lat: [[tests#Lazy values#Async factory raising synchronously]]
def test_lazy_async_factory_raising_synchronously():
    attempts = []

    def factory():
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("down")
        return asyncio.sleep(0, "pool")

    get = lazy(inspect.markcoroutinefunction(factory))

    async def main():
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(get(), 1)
        return await asyncio.wait_for(get(), 1)

    assert asyncio.run(main()) == "pool"
    assert len(attempts) == 2


# @lat: [[tests#Lazy values#Creation outlives its initiator]]
def test_lazy_creation_outlives_dropped_initiator(caplog):
    # Only the shared state can keep the creation task alive: its future is
    # weakly held here and referenced by nothing else.
    waiting = weakref.WeakSet()

    async def factory():
        future = asyncio.get_running_loop().create_future()
        waiting.add(future)
        return await future

    get = lazy(factory)

    async def main():
        first = asyncio.ensure_future(get())
        await asyncio.sleep(0)
        second = asyncio.ensure_future(get())
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert first.cancelled()
        del first
        gc.collect()
        assert len(waiting) == 1
        for future in waiting:
            future.set_result("pool")
        return await asyncio.wait_for(second, 1)

    assert asyncio.run(main()) == "pool"
    gc.collect()
    assert asyncio_errors(caplog) == []


# @lat: [[tests#Lazy values#Abandoned waiter after failure]]
def test_lazy_abandoned_waiter_after_failure(caplog):
    gate = threading.Event()

    async def factory():
        await asyncio.to_thread(gate.wait, 5)
        raise ConnectionError("late")

    get = lazy(factory)

    async def main():
        abandoned = asyncio.ensure_future(get())
        active = asyncio.ensure_future(get())
        await asyncio.sleep(0)
        abandoned.cancel()
        with pytest.raises(asyncio.CancelledError):
            await abandoned
        gate.set()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(active, 5)
        del abandoned
        await asyncio.sleep(0)

    asyncio.run(main())
    gc.collect()
    assert asyncio_errors(caplog) == []


SECURE_ITEMS = """\
from haumea import Depends, HTTPBearer, HTTPException, raises

bearer = HTTPBearer()


@raises(401, "Invalid token")
def current_user(token: str = Depends(bearer)) -> str:
    if token != "secret":
        raise HTTPException(401, "Invalid token")
    return "scott"


def GET() -> list[str]:
    return ["public"]


@raises(403, "Not allowed")
def POST(name: str, user: str = Depends(current_user)) -> str:
    return f"{user}:{name}"
"""


# @lat: [[tests#Explicit security and responses]]
def test_explicit_security_and_responses(tmp_path, isolated_imports):
    project = tmp_path / "secure"
    write_project(
        project,
        {
            "pyproject.toml": '[project]\nname = "secure"\nversion = "0"\n',
            "app/items/route.py": SECURE_ITEMS,
            "app/broken/route.py": (
                "from haumea import Depends, HTTPBearer\n\n"
                "def explode(token: str = Depends(HTTPBearer())) -> str:\n"
                "    raise RuntimeError('called during schema generation')\n\n"
                "def GET(value: str = Depends(explode)) -> str:\n"
                "    return value\n"
            ),
            "app/internal/route.py": (
                "from haumea import Depends, HTTPBearer, raises\n\n"
                "include_in_schema = False\n\n"
                "@raises(401, 'Internal only')\n"
                "def GET(token: str = Depends(HTTPBearer('internal'))) -> str:\n"
                "    return token\n"
            ),
        },
    )
    client = make_client(create_app(project / "app"))
    schema = client("GET", "/openapi.json").json()

    assert schema["components"] == {
        "securitySchemes": {"HTTPBearer": {"type": "http", "scheme": "bearer"}}
    }
    items = schema["paths"]["/items"]
    assert "security" not in items["get"]
    assert list(items["get"]["responses"]) == ["200"]

    post = items["post"]
    assert post["security"] == [{"HTTPBearer": []}]
    assert [(p["name"], p["in"]) for p in post["parameters"]] == [("name", "query")]
    assert {code: r["description"] for code, r in post["responses"].items()} == {
        "200": "Successful Response",
        "401": "Invalid token",
        "403": "Not allowed",
        "422": "Validation Error",
    }

    # Declarations are read, not executed: the raising dependency is
    # documented, and its scheme's runtime 401 is not, since it is undeclared.
    broken = schema["paths"]["/broken"]["get"]
    assert broken["security"] == [{"HTTPBearer": []}]
    assert list(broken["responses"]) == ["200"]
    assert "internal" not in json.dumps(schema)

    static = {f.path: f.content for f in plan_build(project).static_files}
    built = json.loads(static["openapi.json"])
    assert (built["paths"], built["components"]) == (
        schema["paths"],
        schema["components"],
    )

    assert client("GET", "/items").json() == ["public"]
    ok = client("POST", "/items?name=x", headers={"Authorization": "Bearer secret"})
    assert ok.json() == "scott:x"
    wrong = client("POST", "/items?name=x", headers={"Authorization": "Bearer no"})
    assert (wrong.status_code, wrong.json()) == (401, {"detail": "Invalid token"})


# @lat: [[tests#HTTP bearer credentials]]
@pytest.mark.parametrize(
    ("authorization", "token"),
    [
        (None, None),
        ("", None),
        ("Bearer", None),
        ("Bearer ", None),
        ("Basic dXNlcjpwYXNz", None),
        ("Bearertoken", None),
        ("Bearer abc.def", "abc.def"),
        ("bearer abc", "abc"),
        ("BEARER  abc ", "abc"),
    ],
)
def test_http_bearer_credentials(authorization, token):
    app = Haumea(docs=False)
    bearer = HTTPBearer()

    def handler(value: str = Depends(bearer)) -> str:
        return value

    app.add_route("/", handler, methods=["GET"])
    headers = {} if authorization is None else {"Authorization": authorization}
    response = make_client(app)("GET", "/", headers=headers)
    if token is None:
        assert response.status_code == 401
        assert response.json() == {"detail": "Not authenticated"}
        assert response.headers["www-authenticate"] == "Bearer"
    else:
        assert response.json() == token


# @lat: [[tests#Declared responses must be errors]]
@pytest.mark.parametrize("status", [200, 204, 399, 600])
def test_raises_rejects_non_error_statuses(status):
    with pytest.raises(ValueError, match="status"):
        raises(status, "nope")


def declared_descriptions(handler) -> dict[str, str]:
    app = Haumea()
    app.add_route("/", handler, methods=["GET"])
    responses = app.openapi()["paths"]["/"]["get"]["responses"]
    return {code: r["description"] for code, r in responses.items()}


# @lat: [[tests#Declared response precedence]]
def test_declared_response_precedence():
    @raises(401, "outer")
    @raises(401, "inner")
    @raises(403, "only")
    def stacked() -> None:
        return None

    assert declared_descriptions(stacked) == {
        "200": "Successful Response",
        "401": "outer",
        "403": "only",
    }

    @raises(401, "nested")
    @raises(403, "nested")
    @raises(404, "nested")
    def nested() -> None:
        return None

    def first(value: None = Depends(nested)) -> None:
        return value

    @raises(401, "direct")
    @raises(403, "direct")
    def second() -> None:
        return None

    @raises(403, "handler")
    def handler(a: None = Depends(first), b: None = Depends(second)) -> None:
        return None

    # The handler beats its dependencies, and a direct dependency beats a
    # nested one even when the nested one is in an earlier parameter.
    assert declared_descriptions(handler) == {
        "200": "Successful Response",
        "401": "direct",
        "403": "handler",
        "404": "nested",
    }


# @lat: [[tests#Declared response statuses must be integers]]
@pytest.mark.parametrize("status", [401.5, "401", True, None])
def test_raises_rejects_non_integer_statuses(status):
    with pytest.raises(TypeError, match="status"):
        raises(status, "nope")


# @lat: [[tests#HTTP bearer scheme names]]
@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("HTTPBearer", True),
        ("api.v1-token_2", True),
        ("", False),
        ("my bearer", False),
        ("a/b", False),
        ("tokén", False),
        ("a:b", False),
    ],
)
def test_http_bearer_scheme_names(name, valid):
    if not valid:
        with pytest.raises(ValueError, match="name"):
            HTTPBearer(name)
        return
    bearer = HTTPBearer(name)

    def handler(token: str = Depends(bearer)) -> str:
        return token

    app = Haumea()
    app.add_route("/", handler, methods=["GET"])
    assert list(app.openapi()["components"]["securitySchemes"]) == [name]
