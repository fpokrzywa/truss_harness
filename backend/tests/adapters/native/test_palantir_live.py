"""The opt-in live validation of the Palantir Marketplace adapter. It waits on the owner: there is
no Foundry stack yet, so the test skips unconditionally and reads no file or environment variable.
The integration stays `experimental` (`LIVE_VALIDATED = False`).
"""

from __future__ import annotations

import pytest


@pytest.mark.live
def test_palantir_real_adapter_against_a_development_stack() -> None:
    pytest.skip("live validation waiting on owner input: no Palantir Foundry stack")
