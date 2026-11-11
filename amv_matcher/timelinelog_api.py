"""F6d endpoints: timeline-log export/import and project bookmarks. Spec: docs/TIMELINE-LOG-FORMAT.md.

Exports read cached analyses only (never start a heavy analysis); a track not analysed yet has
`detected: null`. Media are identified by duration/size/content hash, never by file name.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from . import project as P
from . import timelinelog as TL

router = APIRouter()


def _guard(fn):
    try:
        return fn()
    except (P.ProjectError, TL.LogError) as e:
        raise HTTPException(400, str(e))
    except KeyError:
        raise HTTPException(404, "Bookmark not found")
    except FileNotFoundError:
        raise HTTPException(404, "Project file not found")
    except OSError as e:
        raise HTTPException(400, f"Could not access the project file: {e.strerror or e}")


def _media_paths(data: dict):
    """(video_path, song_path, audio_role) from project data; role 'own' = the video's embedded audio."""
    vp = (data.get("video") or {}).get("path") or None
    sp = data.get("in_use") or None
    if isinstance(sp, dict):
        sp = sp.get("path")
    role = None
    if sp:
        role = "song"
        try:
            from . import server
            if os.path.dirname(os.path.abspath(sp)) == os.path.abspath(str(server.EXTRACTED)):
                role = "own"
        except Exception:
            pass
    return vp, sp, role


def _cached(vp, sp):
    from . import workers
    video = song = None
    if vp and os.path.exists(vp):
        try:
            from . import server
            video = server.VIDEOS.get(str(os.path.abspath(vp))) or server.VIDEOS.get(vp)
        except Exception:
            video = None
        video = video or workers._video_cached(vp)
    if sp and os.path.exists(sp):
        song = workers._song_cached(sp)
    return video, song


def build_for(data: dict) -> dict:
    d = P.normalize(data)
    vp, sp, role = _media_paths(d)
    video, song = _cached(vp, sp)
    return TL.build(d, video=video, song=song, video_path=vp if vp and os.path.exists(vp) else None,
                    song_path=sp if sp and os.path.exists(sp) else None, audio_role=role)


def _respond(log: dict, fmt: str):
    if fmt == "csv":
        return PlainTextResponse(TL.to_csv(log), media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": 'attachment; filename="timeline-log.csv"'})
    if fmt != "json":
        raise HTTPException(400, "format must be json or csv")
    return log


# ---------------------------------------------------------------- export / import
class ExportBody(BaseModel):
    data: dict
    format: str = "json"


class ImportBody(BaseModel):
    log: Any                       # the log object, or its JSON / CSV text
    format: str = "json"           # json | csv
    project: Optional[str] = None  # .amvproj path: bookmarks are written into it
    data: Optional[dict] = None    # or: unsaved project data (returned with the bookmarks in it)
    mode: str = "replace"          # replace | merge (by bookmark id)


@router.get("/api/timeline-log")
def timeline_log(project: str, format: str = "json"):
    return _guard(lambda: _respond(build_for(P.load(project)), format))


@router.post("/api/timeline-log")
def timeline_log_unsaved(body: ExportBody):
    return _guard(lambda: _respond(build_for(body.data), body.format))


@router.post("/api/timeline-log/import")
def timeline_log_import(body: ImportBody):
    def run():
        if body.format == "csv":
            if not isinstance(body.log, str):
                raise TL.LogError("a CSV log must be sent as text")
            log = TL.parse_csv(body.log)
        elif body.format == "json":
            log = TL.parse(body.log)
        else:
            raise TL.LogError("format must be json or csv")
        if body.project:
            d = P.load(body.project)
        elif body.data is not None:
            d = P.normalize(body.data)
        else:
            raise TL.LogError("send project (a path) or data")
        TL.import_into(d, log, body.mode)
        vp, sp, _ = _media_paths(d)
        match = TL.media_match(log, vp if vp and os.path.exists(vp) else None,
                               sp if sp and os.path.exists(sp) else None)
        if body.project:
            d = P.save(body.project, d)
        return {"ok": True, "bookmarks": TL.list_bookmarks(d), "perfect_sync_example": bool(d.get("perfect_sync_example")),
                "media_match": match, "data": d if not body.project else None}
    return _guard(run)


# ---------------------------------------------------------------- bookmarks CRUD (in the project file)
class BookmarkBody(BaseModel):
    project: str
    track: str
    t: float
    type: str = "other"
    note: str = ""


class BookmarkPatch(BaseModel):
    project: str
    track: Optional[str] = None
    t: Optional[float] = None
    type: Optional[str] = None
    note: Optional[str] = None


class FlagBody(BaseModel):
    project: str
    value: bool


def _edit(path, fn):
    def run():
        d = P.load(path)
        out = fn(d)
        d = P.save(path, d)
        return {"ok": True, "result": out, "bookmarks": TL.list_bookmarks(d),
                "perfect_sync_example": bool(d.get("perfect_sync_example"))}
    return _guard(run)


@router.get("/api/project/bookmarks")
def bookmarks_list(project: str, track: Optional[str] = None):
    def run():
        d = P.load(project)
        return {"bookmarks": TL.list_bookmarks(d, track), "types": list(TL.BOOKMARK_TYPES),
                "perfect_sync_example": bool(d.get("perfect_sync_example"))}
    return _guard(run)


@router.post("/api/project/bookmarks")
def bookmarks_add(body: BookmarkBody):
    return _edit(body.project, lambda d: TL.add_bookmark(d, body.track, body.t, body.type, body.note))


@router.patch("/api/project/bookmarks/{bid}")
def bookmarks_update(bid: str, body: BookmarkPatch):
    return _edit(body.project, lambda d: TL.update_bookmark(d, bid, track=body.track, t=body.t,
                                                            type=body.type, note=body.note))


@router.delete("/api/project/bookmarks/{bid}")
def bookmarks_delete(bid: str, project: str):
    def rm(d):
        if not TL.delete_bookmark(d, bid):
            raise KeyError(bid)
        return {"deleted": bid}
    return _edit(project, rm)


@router.post("/api/project/perfect-sync")
def perfect_sync_flag(body: FlagBody):
    def setf(d):
        d["perfect_sync_example"] = bool(body.value)
        return {"perfect_sync_example": d["perfect_sync_example"]}
    return _edit(body.project, setf)
