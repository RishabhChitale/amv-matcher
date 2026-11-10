"""HTTP routes for the in-place media library (see media_lib.py)."""
from __future__ import annotations

import urllib.parse
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import medialib as lib

router = APIRouter()


class PathBody(BaseModel):
    path: str


def _guard(fn, *a):
    try:
        return fn(*a)
    except PermissionError:
        raise HTTPException(403, "Path is outside the media roots")
    except ValueError:
        raise HTTPException(400, "Bad path")
    except (NotADirectoryError, FileNotFoundError):
        raise HTTPException(404, "Not found")
    except Exception as e:  # undecodable media etc.
        raise HTTPException(422, f"{type(e).__name__}: {e}")


@router.get("/api/media/roots")
def get_roots():
    return {"roots": lib.roots_info()}


@router.post("/api/media/roots")
def add_root(body: PathBody):
    try:
        lib.add_root(body.path)
    except (NotADirectoryError, OSError, ValueError, RuntimeError):
        raise HTTPException(400, "Not an existing directory")
    return {"roots": lib.roots_info()}


@router.delete("/api/media/roots")
def del_root(body: PathBody):
    lib.remove_root(body.path)
    return {"roots": lib.roots_info()}


@router.get("/api/fs/list")
def fs_list(path: str):
    return _guard(lib.list_dir, path)


@router.get("/api/media/probe")
def media_probe(path: str):
    out = dict(_guard(lib.probe, path))
    try:
        from .mediameta import download_source
        out["download"] = download_source(str(lib.safe_path(path)))
    except Exception:
        out["download"] = None
    return out


def _allow(path: str) -> None:
    from . import server
    fn = getattr(server, "allow_path", None)
    (fn or lib.allow_path)(path)


@router.post("/api/media/link")
def media_link(body: PathBody):
    p = _guard(lib.safe_path, body.path)
    kind = lib.kind_of(p)
    if not p.is_file() or kind is None:
        raise HTTPException(404, "Not a media file")
    _allow(str(p))
    return {"path": str(p), "name": p.name, "size": p.stat().st_size, "kind": kind}


@router.get("/api/thumb")
def media_thumb(path: str, t: float = None, w: int = 240):
    out = _guard(lib.thumb, path, t, w)
    return FileResponse(out, media_type="image/jpeg")


@router.get("/api/filmstrip")
def media_filmstrip(path: str, n: int = 12, w: int = 160):
    n = max(1, min(int(n), 60))
    dur = _guard(lib.probe, path).get("duration") or 0.0
    frames = []
    for i in range(n):
        t = round(dur * (i + 0.5) / n, 3)
        q = urllib.parse.urlencode({"path": path, "t": t, "w": w})
        frames.append({"t": t, "url": f"/api/thumb?{q}"})
    return {"frames": frames}
