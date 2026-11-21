"""Lazy Pydantic detection without importing ``pydantic``.

Checks ``sys.modules.get("pydantic")`` so ``pydantic`` is never imported unless
user code has already loaded it.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any


def pydantic_module() -> ModuleType | None:
    """Return the loaded ``pydantic`` module, or ``None`` if not imported."""
    module = sys.modules.get("pydantic")
    return module if isinstance(module, ModuleType) else None


def is_pydantic_model(annotation: Any) -> bool:
    """Return whether ``annotation`` is a ``pydantic.BaseModel`` subclass."""
    if not isinstance(annotation, type):
        return False
    pydantic = pydantic_module()
    if pydantic is None:
        return False
    base_model = getattr(pydantic, "BaseModel", None)
    return isinstance(base_model, type) and issubclass(annotation, base_model)
