"""The Snowflake CLI pin (SP6 design 6.2 and 9.4): `CLI_VERSION`, `tools/snowflake-cli.in` and the
hashed lock `tools/snowflake-cli.txt` agree on 3.33.0, every locked requirement carries a sha256
hash, and no Snowflake distribution enters the backend's dependencies."""

from __future__ import annotations

import re
from pathlib import Path

from shifde.adapters.native.snowflake import source
from shifde.adapters.native.snowflake.source import CLI_VERSION

ROOT = Path(__file__).resolve().parents[4]
TOOLS = ROOT / "tools"


def test_the_constant_names_the_pin() -> None:
    assert CLI_VERSION == "3.33.0"
    assert "adapters/native/databricks/source.py" in (source.__doc__ or "")
    assert "tools/snowflake-cli.in" in (source.__doc__ or "")


def test_the_input_file_pins_the_same_version() -> None:
    lines = [
        line.strip()
        for line in (TOOLS / "snowflake-cli.in").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert lines == [f"snowflake-cli=={CLI_VERSION}"]


def test_every_locked_requirement_is_hashed() -> None:
    text = (TOOLS / "snowflake-cli.txt").read_text(encoding="utf-8")
    assert text.startswith("# Copied verbatim from the FDE SDK at 1fd902b")
    blocks = re.split(r"\n(?=[A-Za-z0-9])", text.split("\n", 1)[1])
    requirements = [b for b in blocks if re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*==", b)]
    assert len(requirements) > 10
    for block in requirements:
        assert "--hash=sha256:" in block, block.splitlines()[0]
        assert all(
            re.fullmatch(r"--hash=sha256:[a-f0-9]{64}( \\)?", line.strip())
            for line in block.splitlines()[1:]
            if line.strip().startswith("--hash")
        )
    pinned = [b for b in requirements if b.startswith("snowflake-cli==")]
    assert len(pinned) == 1 and pinned[0].startswith(f"snowflake-cli=={CLI_VERSION} \\")


def test_no_snowflake_distribution_enters_the_backend() -> None:
    for name in ("pyproject.toml", "uv.lock"):
        text = (ROOT / "backend" / name).read_text(encoding="utf-8")
        for distribution in ("snowflake-cli", "snowflake-connector", "snowflake-snowpark"):
            assert distribution not in text, (name, distribution)
