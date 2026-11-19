import asyncio
import json
import subprocess
import sys
import warnings
from pathlib import Path

import httpx
import pytest

from haumea import (
    HaumeaConfig,
    Request,
    create_app,
    create_route_app,
    load_config,
)
from haumea import build as build_module
from haumea.build import build, emit_build, plan_build
from haumea.cli import main as cli_main
from haumea.config import normalize_base_path
from haumea.openapi import swagger_html
from haumea.routing import discover_routes

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
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["header", "authorization"]

    response = client("PUT", "/api/items/7", json=item, headers={"Authorization": "x"})
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


# @lat: [[tests#Import tracer and closure]]
def test_trace_and_closure_edge_cases(tmp_path, isolated_imports, monkeypatch, capsys):
    from haumea.build import index_store

    def make_dist(
        name: str,
        module: str,
        *,
        requires: list[str] | None = None,
        extra_files: dict[str, str] | None = None,
        top_level: str | None = None,
    ) -> dict[str, str]:
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
