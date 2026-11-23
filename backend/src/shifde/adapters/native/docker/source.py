"""The `docker.compose/v1` bundle: constants and validation of `Dockerfile`, `compose.yaml`,
`release.json` and `app/**`.

Validation runs at validate and build (the target-free rules) and again at plan and apply (the
target rules too). Refusals are fixed text naming the rule and never echo file content.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any  # Any: YAML and JSON documents

import yaml

from shifde.adapters.native.contracts import Target

ADAPTER_VERSION = "0.1.0"
# python:3.12-slim index digest, read from registry-1.docker.io on 2026-11-23.
BASE_IMAGE = (
    "python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
)
RELEASE_LABEL = "org.shifde.release_digest"  # on the image
TARGET_LABEL = "org.shifde.target_id"  # on the compose service
ENV_FILE_DIR = "/etc/shifde-apps"
PROJECT = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
PORT_MIN, PORT_MAX = 1024, 65535
CONTAINER_PORT = 8000

FILES = frozenset({"Dockerfile", "compose.yaml", "release.json"})
SERVICE_KEYS = frozenset(
    {
        "image",
        "env_file",
        "environment",
        "ports",
        "read_only",
        "tmpfs",
        "cap_drop",
        "security_opt",
        "restart",
        "healthcheck",
        "labels",
    }
)
ENVIRONMENT_KEYS = frozenset({"DATABRICKS_WAREHOUSE_ID", "SERVING_ENDPOINT"})
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_DIRECTIVE = re.compile(r"^#\s*(syntax|escape|check)\s*=", re.IGNORECASE)
_RUN_FLAGS = ("--mount", "--network", "--security")
INSTRUCTIONS = frozenset(
    {
        "FROM",
        "LABEL",
        "RUN",
        "COPY",
        "WORKDIR",
        "ENV",
        "USER",
        "EXPOSE",
        "CMD",
        "ENTRYPOINT",
        "HEALTHCHECK",
    }
)


class BundleInvalid(ValueError):
    """A bundle rule failed; `code` names the rule."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Bundle:
    release_digest: str
    service: dict[str, Any]
    environment: dict[str, str]
    labels: dict[str, str]


def image_tag(project: str, release_digest: str) -> str:
    return f"shifde-{project}:{release_digest[:12]}"


def target_project_port(target: Target) -> tuple[str, int]:
    """The `project` and `port` bindings of a Docker target, validated."""
    project = target.bindings.get("project", "")
    port = target.bindings.get("port", "")
    if not PROJECT.fullmatch(project):
        raise ValueError("the project binding is missing or invalid")
    if not port.isascii() or not port.isdigit() or not PORT_MIN <= int(port) <= PORT_MAX:
        raise ValueError("the port binding is missing or outside 1024..65535")
    return project, int(port)


def _logical_lines(text: str) -> list[str]:
    """Dockerfile lines with continuations joined as Docker joins them: blank and comment lines
    inside a continuation are dropped, blank lines elsewhere dropped, other comments kept."""
    lines: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#") or not line:
            if not pending and line:
                lines.append(line)
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        line = (pending + line).strip()
        pending = ""
        if line:
            lines.append(line)
    if pending.strip():
        lines.append(pending.strip())
    return lines


def dockerfile_labels(text: str) -> dict[str, str]:
    """The `key=value` labels the Dockerfile's `LABEL` instructions set."""
    labels: dict[str, str] = {}
    for line in _logical_lines(text):
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].upper() == "LABEL":
            try:
                tokens = shlex.split(parts[1])
            except ValueError:
                raise BundleInvalid("dockerfile_label", "a Dockerfile LABEL is malformed") from None
            for token in tokens:
                key, sep, value = token.partition("=")
                if sep:
                    labels[key] = value
    return labels


def _leading_flags(tokens: list[str]) -> list[str]:
    """The `--flag` tokens before an instruction's arguments, as Docker parses them."""
    flags: list[str] = []
    for token in tokens:
        if not token.startswith("--"):
            break
        flags.append(token)
    return flags


def _validate_dockerfile(text: str, release_digest: str) -> None:
    froms = 0
    for line in _logical_lines(text):
        if line.startswith("#"):
            if _DIRECTIVE.match(line):
                raise BundleInvalid(
                    "dockerfile_directive", "Dockerfile parser directives are refused"
                )
            continue
        parts = line.split()
        instruction = parts[0].upper()
        flags = [token.lower() for token in _leading_flags(parts[1:])]
        if instruction not in INSTRUCTIONS:
            raise BundleInvalid(
                "dockerfile_instruction",
                "the Dockerfile uses an instruction outside the allow-list",
            )
        if any("<<" in token for token in parts[1:]):
            raise BundleInvalid("dockerfile_heredoc", "Dockerfile heredocs are refused")
        if instruction == "FROM":
            froms += 1
            if parts[1:] != [BASE_IMAGE]:
                raise BundleInvalid("dockerfile_from", "the Dockerfile FROM must be the base image")
        elif instruction == "RUN" and any(flag.startswith(_RUN_FLAGS) for flag in flags):
            raise BundleInvalid(
                "dockerfile_run_flag",
                "Dockerfile RUN mount, network and security flags are refused",
            )
        elif instruction == "COPY" and any(flag.startswith("--from") for flag in flags):
            raise BundleInvalid("dockerfile_copy_from", "Dockerfile COPY --from is refused")
    if froms != 1:
        raise BundleInvalid("dockerfile_from", "the Dockerfile must have exactly one FROM")
    if dockerfile_labels(text).get(RELEASE_LABEL) != release_digest:
        raise BundleInvalid(
            "dockerfile_label", "the Dockerfile must label the image with the release digest"
        )


def _string_map(value: Any, code: str, message: str) -> dict[str, str]:
    """A compose mapping (`{k: v}` or `["k=v"]`) as strings."""
    if value is None:
        return {}
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise BundleInvalid(code, message)
        return {key: "" if item is None else str(item) for key, item in value.items()}
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return dict(item.partition("=")[::2] for item in value)
    raise BundleInvalid(code, message)


def _validate_compose(data: bytes) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    try:
        document = yaml.safe_load(data)
    except yaml.YAMLError:
        raise BundleInvalid("compose_yaml", "compose.yaml is not valid YAML") from None
    if not isinstance(document, dict) or set(document) != {"services"}:
        raise BundleInvalid("compose_top_level", "compose.yaml may only hold services")
    services = document["services"]
    if not isinstance(services, dict) or len(services) != 1:
        raise BundleInvalid("compose_services", "compose.yaml must hold exactly one service")
    service = next(iter(services.values()))
    if not isinstance(service, dict) or not set(service) <= SERVICE_KEYS:
        raise BundleInvalid("compose_service_key", "the compose service uses a refused key")
    if (
        service.get("read_only") is not True
        or service.get("cap_drop") != ["ALL"]
        or service.get("security_opt") != ["no-new-privileges:true"]
    ):
        raise BundleInvalid(
            "compose_hardening",
            "the compose service must be read-only, drop all capabilities and set only "
            "no-new-privileges",
        )
    if "tmpfs" in service and service["tmpfs"] not in ("/tmp", ["/tmp"]):
        raise BundleInvalid("compose_tmpfs", "the compose tmpfs may only be /tmp")
    environment = _string_map(
        service.get("environment"), "compose_environment", "the compose environment is malformed"
    )
    if not set(environment) <= ENVIRONMENT_KEYS:
        raise BundleInvalid(
            "compose_environment", "the compose environment holds a refused variable"
        )
    labels = _string_map(service.get("labels"), "compose_labels", "compose labels are malformed")
    return service, environment, labels


def validate_bundle(files: Mapping[str, bytes]) -> Bundle:
    """The target-free rules; returns the parsed bundle."""
    for name in files:
        if any(part.startswith(".") for part in name.split("/")):
            raise BundleInvalid("bundle_hidden", "hidden files are refused")
        if name not in FILES and not name.startswith("app/"):
            raise BundleInvalid("bundle_file", "a file outside the bundle layout is refused")
    if not files.keys() >= FILES or "app/app.py" not in files:
        raise BundleInvalid("bundle_missing", "the bundle is missing a required file")
    try:
        record = json.loads(files["release.json"])
    except (ValueError, UnicodeDecodeError):
        record = None
    release_digest = record.get("digest") if isinstance(record, dict) else None
    if not isinstance(release_digest, str) or not _DIGEST.fullmatch(release_digest):
        raise BundleInvalid("release_record", "release.json must hold the release digest")
    try:
        dockerfile = files["Dockerfile"].decode("utf-8")
    except UnicodeDecodeError:
        raise BundleInvalid("dockerfile_encoding", "the Dockerfile is not UTF-8") from None
    _validate_dockerfile(dockerfile, release_digest)
    service, environment, labels = _validate_compose(files["compose.yaml"])
    return Bundle(release_digest, service, environment, labels)


def validate_for_target(bundle: Bundle, target: Target) -> tuple[str, int]:
    """The target rules (target label, image, env file and ports); returns project and port."""
    project, port = target_project_port(target)
    if bundle.labels.get(TARGET_LABEL) != str(target.id):
        raise PermissionError("the bundle belongs to another target")
    service = bundle.service
    if service.get("image") != image_tag(project, bundle.release_digest):
        raise BundleInvalid("compose_image", "the compose image is not the derived image tag")
    env_file = f"{ENV_FILE_DIR}/{project}.env"
    if service.get("env_file") not in ([env_file], env_file):
        raise BundleInvalid("compose_env_file", "the compose env_file is not the target's file")
    if service.get("ports") != [f"127.0.0.1:{port}:{CONTAINER_PORT}"]:
        raise BundleInvalid("compose_ports", "the compose port must be the loopback target port")
    return project, port
