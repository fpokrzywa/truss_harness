"""SHIFDE control-plane backend."""

from __future__ import annotations

import tomllib
from importlib import metadata
from pathlib import Path


def _read_version() -> str:
    """Single source of truth: `backend/pyproject.toml`. In a source checkout it is read directly
    (installed metadata can lag behind an edit until the next sync); an installed wheel has no
    pyproject next to it and reports its package metadata."""
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            if data.get("project", {}).get("name") == "shifde-backend":
                return str(data["project"]["version"])
        except (OSError, tomllib.TOMLDecodeError, KeyError):
            pass
    try:
        return metadata.version("shifde-backend")
    except metadata.PackageNotFoundError:  # pragma: no cover - neither source nor installed
        return "0.0.0"


__version__ = _read_version()
