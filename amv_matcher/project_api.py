"""Project endpoints: save / load / recent / relink / autosave for .amvproj files."""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import project as P

router = APIRouter()


class SaveBody(BaseModel):
    path: str
    data: dict


class PathBody(BaseModel):
    path: str


class RelinkBody(BaseModel):
    entry: dict
    roots: Optional[list] = None


class AutosaveBody(BaseModel):
    data: dict


def _guard(fn):
    try:
        return fn()
    except P.ProjectError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError:
        raise HTTPException(404, "Project file not found")
    except OSError as e:
        raise HTTPException(400, f"Could not access the project file: {e.strerror or e}")


@router.post("/api/project/save")
def project_save(body: SaveBody):
    def run():
        d = P.save(body.path, body.data)
        P.add_recent(body.path, d["name"])
        return {"ok": True, "path": str(P._check_ext(body.path)), "data": d}
    return _guard(run)


@router.post("/api/project/load")
def project_load(body: PathBody):
    def run():
        d = P.load(body.path)
        P.add_recent(body.path, d["name"])
        return {"data": d, "missing": [e for e in P._media_entries(d) if e.get("missing")]}
    return _guard(run)


@router.get("/api/project/recent")
def project_recent():
    return {"recent": P.recent()}


@router.post("/api/project/relink")
def project_relink(body: RelinkBody):
    roots = body.roots
    if not roots:   # default: the folder the file used to live in (if it still exists)
        old = os.path.dirname(str(body.entry.get("path", "")).replace("\\", "/"))
        roots = [old] if old else []
    return {"candidates": P.relink(body.entry, roots)}


@router.post("/api/project/autosave")
def project_autosave(body: AutosaveBody):
    def run():
        return {"ok": True, "path": str(P.autosave(body.data))}
    return _guard(run)
