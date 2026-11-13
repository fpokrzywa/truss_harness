"""Not yet ported from SETH 3ba0e06; see docs/build/STATUS.md.

Placeholder import: keeps the import-linter `ignore_imports` entry
`shifde.secrets.** -> shifde.secrets.models` matched until the port replaces this module
(DECISIONS B-103)."""

from shifde.secrets import models

__all__ = ["models"]
