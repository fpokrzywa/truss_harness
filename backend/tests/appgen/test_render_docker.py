"""SP8-1: until SP8-9 no app generator renders a `docker.compose/v1` release. `GENERATOR_VERSIONS`
names its version (the one its seal carries), and `render` refuses it by name whatever its manifest
carries; the Databricks and Snowflake versions and renders are the ones from before."""

from __future__ import annotations

import hashlib
from typing import Any  # Any: JSON documents

import pytest

from shifde.appgen.render import GENERATOR_VERSIONS, GeneratorVersionUnsupported, render
from shifde.packaging.canonical import digest
from tests.appgen.rendered import (
    DOCKER_SEALS,
    SAMPLE_DIGEST,
    SAMPLE_MANIFEST,
    SAMPLE_RESOLVED,
    SNOWFLAKE_DIGEST,
    SNOWFLAKE_MANIFEST,
    SNOWFLAKE_RESOLVED,
    on_docker,
)
from tests.appgen.test_render import GOLDEN_DATABRICKS

# the Snowflake render of the SP6 fixture before SP8 (DECISIONS B-4071)
GOLDEN_SNOWFLAKE = "e9c9bbeb3f957bbbf96d09fb16169c3a5f3e850f3dd18f9dbfd7b96a2a05a2fd"


def _golden(rendered: dict[str, bytes]) -> str:
    """The sha256 of the sorted names and contents, each after its length (DECISIONS B-3999)."""
    golden = hashlib.sha256()
    for name, content in sorted(rendered.items()):
        if not name.startswith("app/static/"):
            for part in (name.encode("utf-8"), content):
                golden.update(len(part).to_bytes(8, "big") + part)
    return golden.hexdigest()


@pytest.mark.parametrize("app", DOCKER_SEALS.values(), ids=DOCKER_SEALS)
@pytest.mark.parametrize("base", [SAMPLE_MANIFEST, SNOWFLAKE_MANIFEST], ids=["dbx", "sf"])
def test_a_docker_release_never_renders(base: dict[str, Any], app: Any) -> None:
    manifest = {**base}
    on_docker(app)(manifest)
    with pytest.raises(GeneratorVersionUnsupported) as caught:
        render(manifest, digest(manifest), SAMPLE_RESOLVED)
    assert caught.value.status == 409 and caught.value.code == "generator_version_unsupported"
    assert str(caught.value) == "no app generator renders a Docker release"


def test_docker_adds_a_version_and_changes_no_other_version_or_render() -> None:
    assert GENERATOR_VERSIONS == {
        "databricks.app/v1": "shifde.appgen/1.0",
        "snowflake.native-app/v1": "shifde.appgen.snowflake/1.0",
        "docker.compose/v1": "shifde.appgen.docker/1.0",
    }
    databricks = render(SAMPLE_MANIFEST, SAMPLE_DIGEST, SAMPLE_RESOLVED)
    assert _golden(databricks) == GOLDEN_DATABRICKS
    snowflake = render(SNOWFLAKE_MANIFEST, SNOWFLAKE_DIGEST, SNOWFLAKE_RESOLVED)
    assert _golden(snowflake) == GOLDEN_SNOWFLAKE
