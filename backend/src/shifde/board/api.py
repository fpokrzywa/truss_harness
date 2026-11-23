"""Board routes (sub-project 7 design 7.2): the organization board, filtered by workspace and
project slugs and paged by project, and one project's board."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from shifde.board import schemas as s
from shifde.board.service import DEFAULT_PROJECTS, MAX_PROJECTS, BoardService
from shifde.tenancy.dependencies import CursorQuery, ScopeDep

router = APIRouter(prefix="/api/v1")
ProjectsLimit = Annotated[int, Query(ge=1, le=MAX_PROJECTS)]


def _board(scope: ScopeDep) -> BoardService:
    return BoardService(scope)


Board = Annotated[BoardService, Depends(_board)]


@router.get("/orgs/{org}/board", tags=["board"])
def org_board(
    svc: Board,
    workspace: Annotated[str | None, Query()] = None,
    project: Annotated[str | None, Query()] = None,
    limit: ProjectsLimit = DEFAULT_PROJECTS,
    cursor: CursorQuery = None,
) -> s.BoardOut:
    return svc.org_board(workspace=workspace, project=project, limit=limit, cursor=cursor)


@router.get("/orgs/{org}/workspaces/{ws}/projects/{proj}/board", tags=["board"])
def project_board(svc: Board, ws: str, proj: str) -> s.BoardOut:
    return svc.project_board(ws, proj)
