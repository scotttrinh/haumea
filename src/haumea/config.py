"""Project configuration: the ``[tool.haumea]`` table of ``pyproject.toml``.

The file is parsed with ``tomllib`` and never imports user code, so the
build can read it before loading any route module.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

import msgspec

PYPROJECT = "pyproject.toml"

BASE_PATH_SEGMENT = re.compile(r"[A-Za-z0-9\-._~!+,;=@]+")
"""Allowed characters in one ``base_path`` segment: RFC 3986 unreserved
characters plus the sub-delimiters that are inert in HTML, JS, regexes,
Vercel route destinations and file names."""


def normalize_base_path(base_path: str) -> str:
    """``"api"``, ``"/api/"`` -> ``"/api"``; ``""`` and ``"/"`` -> ``""``.

    Every segment must match ``BASE_PATH_SEGMENT``, not be ``.`` or
    ``..`` and not end in ``.func``, so the prefix is a literal, unencoded
    URL path, a safe ``static/`` subdirectory and never a function
    directory name under ``functions/``.
    """
    stripped = base_path.strip("/")
    if not stripped:
        return ""
    for segment in stripped.split("/"):
        if (
            segment in (".", "..")
            or segment.endswith(".func")
            or not BASE_PATH_SEGMENT.fullmatch(segment)
        ):
            raise ValueError(f"invalid base_path {base_path!r}")
    return f"/{stripped}"


class HaumeaConfig(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Project-level settings. Unknown keys are an error."""

    base_path: str = ""
    """URL prefix for every route, doc and static file, normalized."""
    tree_shake: bool = True
    """Bundle only the distributions each function imports."""
    include: tuple[str, ...] = ()
    """Distribution names bundled into every function."""

    def __post_init__(self) -> None:
        normalized = normalize_base_path(self.base_path)
        msgspec.structs.force_setattr(self, "base_path", normalized)


def load_config(pyproject: str | Path) -> HaumeaConfig:
    """Read ``[tool.haumea]`` from ``pyproject``; defaults if the table is absent.

    Raises ``ValueError`` for unknown keys or invalid values.
    """
    pyproject = Path(pyproject)
    with pyproject.open("rb") as file:
        tool = tomllib.load(file).get("tool", {})
    error = f"invalid [tool.haumea] in {pyproject}"
    if not isinstance(tool, Mapping):
        raise ValueError(f"{error}: `tool` must be a table")
    table = tool.get("haumea", {})
    if not isinstance(table, Mapping):
        raise ValueError(f"{error}: `tool.haumea` must be a table")
    try:
        return msgspec.convert(table, HaumeaConfig)
    except msgspec.ValidationError as exc:
        raise ValueError(f"invalid [tool.haumea] in {pyproject}: {exc}") from exc


def project_base_path(app_dir: str | Path) -> str:
    """The ``base_path`` configured beside ``app_dir``, or ``""`` if the
    project has no ``pyproject.toml``.

    Symlinks in ``app_dir`` are not resolved first, so ``app -> src/app``
    reads the project root's ``pyproject.toml``, as ``plan_build`` does.
    """
    pyproject = Path(os.path.abspath(app_dir)).parent / PYPROJECT
    return load_config(pyproject).base_path if pyproject.is_file() else ""
