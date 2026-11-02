"""Projects: create, rename, delete, and file chats under them.

Deleting a project deletes the folder, never the chats; see
`store.projects.delete`. That is the one behaviour here worth stating twice.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, Field

from app.api.deps import ID_PATTERN, DbDep, UserDep
from app.store import chats, projects

router = APIRouter()

ProjectId = Annotated[str, Path(pattern=ID_PATTERN)]


class NameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@router.get("/api/projects")
async def list_projects(db: DbDep, user: UserDep) -> dict:
    return {"projects": await projects.list_projects(db, user=user)}


@router.post("/api/projects")
async def create_project(body: NameRequest, db: DbDep, user: UserDep) -> dict:
    return await projects.create(db, body.name, user=user)


@router.patch("/api/projects/{project_id}")
async def rename_project(
    project_id: ProjectId, body: NameRequest, db: DbDep, user: UserDep
) -> dict:
    if not await projects.rename(db, project_id, body.name, user=user):
        raise HTTPException(status_code=404, detail="Unknown project")
    return {"ok": True, "name": body.name.strip()}


@router.delete("/api/projects/{project_id}")
async def delete_project(project_id: ProjectId, db: DbDep, user: UserDep) -> dict:
    """Delete the project. Its chats stay, unfiled."""
    if not await projects.delete(db, project_id, user=user):
        raise HTTPException(status_code=404, detail="Unknown project")
    return {"ok": True}


@router.get("/api/projects/{project_id}/chats")
async def project_chats(project_id: ProjectId, db: DbDep, user: UserDep) -> dict:
    if await projects.get(db, project_id, user=user) is None:
        raise HTTPException(status_code=404, detail="Unknown project")
    return {"chats": await chats.list_chats(db, project_id=project_id, user=user)}
