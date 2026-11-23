"""Offline, immutable Fabric source: the template, validation and explicit environment bindings.

Ported from the FDE SDK `shifde_fabric/source.py` (1fd902b) without the Ontology item type. The
template is the SDK Lakehouse and Notebook; the notebook's first cell is generated and exits with
the release digest, which the verification job reports back as its exit value.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from importlib.resources import files as resource_files
from typing import Any  # Any: decoded notebook and definition JSON
from uuid import UUID

from shifde.adapters.native.contracts import FabricItem, FabricSolutionSpec, Target, digest

ADAPTER_VERSION = "0.1.0"
ORIGIN = "https://api.fabric.microsoft.com"
COLLECTIONS = {"Lakehouse": "lakehouses", "Notebook": "notebooks"}
FABRIC_PROFILE = {"adapter": "0.1.0", "api": "v1", "packager": "fabric-source/v1"}
TOOLCHAIN_DIGEST = digest(FABRIC_PROFILE)
PLACEHOLDER = "00000000-0000-4000-8000-000000000000"
DEFINITION_MAX = 16 * 1024 * 1024 - 4096
_WORKSPACE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,99}$")


def fabric_step(key: str, phase: str) -> str:
    value = key + "-" + phase
    if len(value) <= 64:
        return value
    return key[:16] + "-" + hashlib.sha256(key.encode()).hexdigest()[:16] + "-" + phase


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def ordered(spec: FabricSolutionSpec) -> list[FabricItem]:
    result: list[FabricItem] = []
    visited: set[str] = set()
    by_key = {item.key: item for item in spec.items}

    def visit(item: FabricItem) -> None:
        if item.key in visited:
            return
        for key in item.depends_on:
            visit(by_key[key])
        result.append(item)
        visited.add(item.key)

    for item in spec.items:
        visit(item)
    return result


def root(spec: FabricSolutionSpec) -> str:
    return "" if spec.solution_root == "." else spec.solution_root + "/"


def template_spec(release_digest: str) -> FabricSolutionSpec:
    return FabricSolutionSpec(
        items=[
            FabricItem(
                key="lakehouse",
                item_type="Lakehouse",
                definition_path="lake.json",
                name_binding="lakehouse_name",
            ),
            FabricItem(
                key="notebook",
                item_type="Notebook",
                definition_path="main.ipynb",
                depends_on=["lakehouse"],
                lakehouse="lakehouse",
                name_binding="notebook_name",
            ),
        ],
        release_digest=release_digest,
        verification_notebook="notebook",
    )


def exit_cell(release_digest: str) -> dict[str, Any]:
    """The generated first notebook cell: it exits with the release digest."""
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": [f'mssparkutils.notebook.exit("{release_digest}")'],
    }


def _template(name: str) -> bytes:
    return resource_files("shifde.adapters.native.fabric").joinpath("templates", name).read_bytes()


def scaffold_files(spec: FabricSolutionSpec) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for item in spec.items:
        path = root(spec) + item.definition_path
        if item.item_type == "Lakehouse":
            result[path] = _template("lake.json")
        else:
            notebook = json.loads(_template("main.ipynb"))
            notebook["cells"].insert(0, exit_cell(spec.release_digest))
            result[path] = (json.dumps(notebook, separators=(",", ":")) + "\n").encode()
    return result


def validate_source(files: dict[str, bytes], spec: FabricSolutionSpec) -> None:
    for name in files:
        if any(part.startswith(".") for part in name.split("/")):
            raise ValueError("hidden files are forbidden")
    allowed: set[str] = set()
    for item in spec.items:
        path = root(spec) + item.definition_path
        doc = json.loads(files[path])
        if not isinstance(doc, dict):
            raise ValueError("definition must be a JSON object")
        if item.item_type == "Lakehouse":
            if (
                set(doc) - {"description", "enableSchemas"}
                or not isinstance(doc.get("description", ""), str)
                or len(doc.get("description", "")) > 256
                or not isinstance(doc.get("enableSchemas", True), bool)
            ):
                raise ValueError("unsupported Lakehouse definition")
        else:
            if (
                doc.get("nbformat") != 4
                or not isinstance(doc.get("metadata"), dict)
                or not isinstance(doc.get("cells"), list)
            ):
                raise ValueError("invalid ipynb definition")
            if "dependencies" in doc["metadata"] and not isinstance(
                doc["metadata"]["dependencies"], dict
            ):
                raise ValueError("notebook dependencies must be an object")
            if any(
                not isinstance(c, dict)
                or c.get("cell_type") not in {"code", "markdown", "raw"}
                or not isinstance(c.get("source"), (str, list))
                or (
                    isinstance(c["source"], list)
                    and any(not isinstance(s, str) for s in c["source"])
                )
                for c in doc["cells"]
            ):
                raise ValueError("invalid notebook cells")
            if not doc["cells"] or doc["cells"][0] != exit_cell(spec.release_digest):
                raise ValueError("the first notebook cell must be the generated release exit cell")
        allowed.add(path)
    if set(files) != allowed:
        raise ValueError("source contains unmanaged files or overlapping item paths")
    for item in spec.items:
        rendered = definition(
            item,
            spec,
            files,
            PLACEHOLDER,
            {i.key: "Item" for i in spec.items},
            {i.key: PLACEHOLDER for i in spec.items},
        )
        if rendered:
            canonical_definition(rendered, item.item_type)
            if len(json_bytes(rendered)) > DEFINITION_MAX:
                raise ValueError("Fabric definition exceeds supported transport budget")


def bindings(spec: FabricSolutionSpec, target: Target) -> tuple[str, dict[str, str]]:
    """The target's workspace id and the display name bound to each item."""
    if target.host != ORIGIN:
        raise ValueError("Fabric requires the canonical public-cloud API origin")
    workspace = target.bindings.get(spec.workspace_binding, "")
    if not _WORKSPACE.fullmatch(workspace) or str(UUID(workspace)) != workspace:
        raise ValueError("Fabric workspace binding must be a lowercase hyphenated GUID")
    names: dict[str, str] = {}
    for item in spec.items:
        name = target.bindings.get(item.name_binding or item.key, "")
        if not _NAME.fullmatch(name):
            raise ValueError("Fabric item names must be ASCII identifiers under 100 characters")
        names[item.key] = name
    if len(set(names.values())) != len(names):
        raise ValueError("Fabric item display names must be distinct")
    return workspace, names


def definition(
    item: FabricItem,
    spec: FabricSolutionSpec,
    files: dict[str, bytes],
    workspace: str,
    names: dict[str, str],
    ids: dict[str, str],
) -> dict[str, Any] | None:
    """The native definition of a Notebook (bound to its Lakehouse); a Lakehouse has none."""
    if item.item_type != "Notebook":
        return None
    doc = copy.deepcopy(json.loads(files[root(spec) + item.definition_path]))
    if item.lakehouse:
        doc["metadata"].setdefault("dependencies", {})["lakehouse"] = {
            "default_lakehouse": ids[item.lakehouse],
            "default_lakehouse_name": names[item.lakehouse],
            "default_lakehouse_workspace_id": workspace,
            "known_lakehouses": [{"id": ids[item.lakehouse]}],
        }
    return {"format": "ipynb", "parts": [part("notebook-content.ipynb", json_bytes(doc))]}


def part(path: str, data: bytes) -> dict[str, str]:
    return {"path": path, "payloadType": "InlineBase64", "payload": base64.b64encode(data).decode()}


def canonical_definition(value: dict[str, Any], item_type: str) -> str:
    """The digest of a Notebook definition's authored content (cells and dependencies)."""
    if item_type != "Notebook":
        raise ValueError("only Notebook items carry a definition")
    parts = value["parts"]
    if len(parts) > 1000 or len({p["path"] for p in parts}) != len(parts):
        raise ValueError("invalid native definition parts")
    result: dict[str, Any] = {}
    for p in parts:
        if p["path"] == ".platform":
            continue
        if p["payloadType"] != "InlineBase64":
            raise ValueError("unsupported native payload type")
        if "notebook" in result:
            raise ValueError("Notebook definition must contain exactly one content part")
        doc = json.loads(base64.b64decode(p["payload"], validate=True))
        metadata = doc.get("metadata", {})
        # Fabric may add execution outputs and cell ids; compare authored cells and dependencies.
        result["notebook"] = {
            "cells": [
                {
                    "cell_type": c["cell_type"],
                    "source": "".join(c["source"]),
                    "metadata": c.get("metadata", {}),
                }
                for c in doc["cells"]
            ],
            "lakehouse": metadata.get("dependencies", {}).get("lakehouse"),
            "dependencies": {
                k: v for k, v in metadata.get("dependencies", {}).items() if v is not None
            },
            "kernelspec": metadata.get("kernelspec"),
            "kernel_info": metadata.get("kernel_info"),
            "language_info": metadata.get("language_info"),
        }
    if not result:
        raise ValueError("native definition content is missing")
    return digest(result)
