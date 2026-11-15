from __future__ import annotations

import argparse
import os
import sys

from haumea.build import DEFAULT_PLATFORM, DEFAULT_PYTHON, build


def dev_app():
    """App factory for ``haumea dev``; uvicorn re-imports it on reload."""
    from haumea import create_app

    return create_app("app", public_dir="public")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="haumea")
    commands = parser.add_subparsers(dest="command", required=True)

    dev = commands.add_parser("dev", help="run the app locally with reload")
    dev.add_argument("--host", default="127.0.0.1")
    dev.add_argument("--port", type=int, default=8000)

    build_cmd = commands.add_parser("build", help="emit .vercel/output")
    build_cmd.add_argument("--python", default=DEFAULT_PYTHON)
    build_cmd.add_argument(
        "--platform",
        default=DEFAULT_PLATFORM,
        help="uv --python-platform for vendored dependencies; "
        "'native' targets this machine",
    )

    args = parser.parse_args(argv)

    if args.command == "dev":
        import uvicorn

        sys.path.insert(0, os.getcwd())
        uvicorn.run(
            "haumea.cli:dev_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=True,
            reload_dirs=[os.getcwd()],
        )
    elif args.command == "build":
        platform = None if args.platform == "native" else args.platform
        output = build(".", python=args.python, platform=platform)
        print(f"Build output written to {os.path.relpath(output)}")
        print("Deploy with: vercel deploy --prebuilt")


if __name__ == "__main__":
    main()
