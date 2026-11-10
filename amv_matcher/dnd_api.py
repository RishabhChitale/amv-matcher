"""Developer 'Do not disturb' flag: <app data>/dnd.json {on, since, by}. tools/relaunch_app.sh reads it and waits
instead of quitting/replacing the app while it is on."""
from __future__ import annotations

import time

from fastapi import APIRouter
from pydantic import BaseModel

from . import project as P

router = APIRouter()


class DndBody(BaseModel):
    on: bool


def _file():
    return P.APP_DATA / "dnd.json"


def read_dnd() -> dict:
    import json
    try:
        d = json.loads(_file().read_text(encoding="utf-8"))
        return {"on": bool(d.get("on")), "since": d.get("since"), "by": d.get("by")}
    except (OSError, ValueError, AttributeError):
        return {"on": False, "since": None, "by": None}


@router.get("/api/dnd")
def get_dnd():
    return read_dnd()


@router.post("/api/dnd")
def set_dnd(body: DndBody):
    d = {"on": body.on, "since": int(time.time()), "by": "user"}
    P._atomic_json(_file(), d)
    return d
