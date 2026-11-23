"""The deterministic Databricks App source of a sealed release (SP5 design 6.3).

SP5-5 fills this module: `render(manifest, release_digest, resolved) -> dict[str, bytes]`, pure
(no database, network or service imports; the "appgen.render is pure" import contract). Until then
it holds only the generator version a `/2` manifest records (design 6.2).
"""

from __future__ import annotations

GENERATOR_VERSION = "shifde.appgen/1.0"
