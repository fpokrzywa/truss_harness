"""The platform-administrator installation overview (SETH 3ba0e06 `installation/api.py`).

SETH's public branding routes are not ported (B-1101). Not mounted here: the `app` port includes
`router` (docs/build/carry_forward.md, as B-501).
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from shifde.core.deps import SessionDep, SettingsDep
from shifde.identity.dependencies import AuthDep
from shifde.installation import schemas, service

router = APIRouter(prefix="/api/v1/platform", tags=["platform"])


@router.get("/installation")
def installation(
    auth: AuthDep, session: SessionDep, settings: SettingsDep, request: Request
) -> schemas.InstallationOut:
    """Effective installation configuration; platform administrators only. No secrets."""
    return service.installation_overview(
        session, settings, auth.principal, request.app.state.features.keys()
    )
