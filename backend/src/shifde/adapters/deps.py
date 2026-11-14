"""FastAPI dependency exposing the process-wide `IntegrationRuntime` (API layers only)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from shifde.adapters.runtime import IntegrationRuntime


def get_runtime(request: Request) -> IntegrationRuntime:
    runtime: IntegrationRuntime = request.app.state.integration
    return runtime


RuntimeDep = Annotated[IntegrationRuntime, Depends(get_runtime)]
