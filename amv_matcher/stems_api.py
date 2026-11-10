"""Stems endpoints: separate (HPSS / Demucs) as a job, status, stem audio, stem features, installer."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import stems as S
from . import winsafe

router = APIRouter()
JOBS: dict = {}
LATEST: dict = {}            # (song key, provider) -> job id
INSTALL: dict = {"status": "idle", "progress": 0.0, "message": "", "error": None}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=1)   # demucs is heavy: one separation at a time
_install_lock = threading.Lock()


def _origin_guard(request: Request):
    from .server import _origin_guard as g   # lazy: server imports this module
    return g(request)


def _song(path: str):
    from .server import _safe_song
    return _safe_song(path)


class StemsRequest(BaseModel):
    song_path: str
    provider: str = "auto"


def _public(job: dict) -> dict:
    return {k: v for k, v in job.items() if not k.startswith("_")}


def _run(job: dict, song):
    def progress(frac, msg):
        job.update(progress=round(float(frac), 3), message=msg)
    try:
        res = S.separate(str(song), job["provider"], progress=progress, cancel=job["_cancel"],
                         proc_holder=job["_proc"])
        job.update(status="done", progress=1.0, message="Stems ready", stems=res["stems"],
                   stem_names=list(res["stems"]), seconds=round(time.time() - job["started"], 1))
    except S.Cancelled:
        job.update(status="cancelled", message="Cancelled")
    except Exception as e:  # noqa: BLE001
        job.update(status="error", error=f"{type(e).__name__}: {e}", message=str(e))


@router.post("/api/stems", dependencies=[Depends(_origin_guard)])
def start_stems(req: StemsRequest):
    song = _song(req.song_path)
    try:
        provider = S.resolve_provider(req.provider)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if provider == "demucs":
        if not S.demucs_installed():
            raise HTTPException(409, "Demucs is not installed")
        if not S.cached(song, "demucs") and S.free_bytes(S.CACHE_DIR) < S.MIN_FREE_BYTES:
            raise HTTPException(507, f"Not enough free disk space: {S.free_bytes(S.CACHE_DIR) / 1024 ** 3:.1f} GB free, 3 GB needed")
    key = (S.song_key(song), provider)
    with _lock:
        old = JOBS.get(LATEST.get(key))
        if old and old["status"] == "running":
            return _public(old)
        job = {"id": uuid.uuid4().hex[:12], "song_path": str(song), "provider": provider, "status": "running",
               "progress": 0.0, "message": "Starting", "error": None, "stems": None, "stem_names": S.STEM_NAMES[provider],
               "started": time.time(), "_cancel": threading.Event(), "_proc": {}}
        hit = S.cached(song, provider)
        if hit:
            job.update(status="done", progress=1.0, message="Stems ready (cached)", stems=hit["stems"], cached=True)
        JOBS[job["id"]] = job
        LATEST[key] = job["id"]
    if job["status"] == "running":
        _pool.submit(_run, job, song)
    return _public(job)


@router.post("/api/stems/cancel", dependencies=[Depends(_origin_guard)])
def cancel_stems(req: StemsRequest):
    song = _song(req.song_path)
    n = 0
    for prov in S.STEM_NAMES:
        j = JOBS.get(LATEST.get((S.song_key(song), prov)))
        if j and j["status"] == "running":
            j["_cancel"].set()
            p = j["_proc"].get("proc")
            if p and p.poll() is None:
                winsafe.kill_tree(p)
            n += 1
    return {"cancelled": n}


@router.get("/api/stems/status")
def stems_status(song_path: str):
    song = _song(song_path)
    key = S.song_key(song)
    jobs = [JOBS[LATEST[(key, p)]] for p in S.STEM_NAMES if (key, p) in LATEST]
    running = next((j for j in jobs if j["status"] == "running"), None)
    caps = {"demucs_installed": S.demucs_installed(), "free_gb": round(S.free_bytes(S.CACHE_DIR) / 1024 ** 3, 1),
            "install": _public(INSTALL)}
    have = {p: S.cached(song, p) for p in S.STEM_NAMES}
    best = have["demucs"] or have["hpss"]
    out = {"capabilities": caps, "available": {p: list(h["stems"]) for p, h in have.items() if h}}
    if running:
        out.update(running=_public(running), state="running")
    elif best:
        out.update(state="ready", provider=best["provider"], stems=list(best["stems"]))
    else:
        last = jobs[-1] if jobs else None
        out.update(state="none", last=_public(last) if last else None)
    return out


def _pick_provider(song, provider):
    if provider:
        return S.resolve_provider(provider)
    for p in ("demucs", "hpss"):
        if S.cached(song, p):
            return p
    raise HTTPException(404, "No stems separated for this song yet")


@router.get("/api/stem-audio")
def stem_audio(song_path: str, stem: str, provider: str = ""):
    song = _song(song_path)
    prov = _pick_provider(song, provider)
    hit = S.cached(song, prov)
    if not hit or stem not in hit["stems"]:
        raise HTTPException(404, "Unknown stem")
    return FileResponse(hit["stems"][stem], media_type="audio/wav")


@router.get("/api/stem-features")
def stem_features(song_path: str, provider: str = ""):
    song = _song(song_path)
    prov = _pick_provider(song, provider)
    return S.features_for(song, prov)


# ---------------------------------------------------------------- python / pack detection
PY_MIN, PY_MAX = (3, 9), (3, 12)          # torch 2.2.x wheels exist for these
PACK_VERSION_FILE = "pack.json"


def _py_version(cmd) -> tuple | None:
    """(major, minor) of the interpreter `cmd` (list), or None if it doesn't run / isn't Python 3."""
    try:
        r = winsafe.run(list(cmd) + ["-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                        capture_output=True, text=True, timeout=15)
    except Exception:  # noqa: BLE001 - missing exe, timeout, permission
        return None
    m = re.fullmatch(r"(\d+)\.(\d+)", (r.stdout or "").strip()) if r.returncode == 0 else None
    return (int(m.group(1)), int(m.group(2))) if m else None


def find_system_python() -> list | None:
    """Command (argv prefix) of a system Python 3.9-3.12 to build the stems venv with, else None.
    Never returns sys.executable when frozen (that is the app exe).  macOS: python3.x / python3;
    Windows: `py -3.x` launcher then python/python3 on PATH; others: python3 on PATH."""
    cands: list = []
    if winsafe._is_win():
        for minor in (11, 10, 12, 9):
            cands.append(["py", f"-3.{minor}"])
        cands += [["py", "-3"], ["python"], ["python3"]]
    else:
        for minor in (11, 10, 12, 9):
            cands.append([f"python3.{minor}"])
        cands += [["python3"], ["python"]]
        if sys.platform == "darwin":
            cands += [["/usr/bin/python3"], ["/opt/homebrew/bin/python3"], ["/usr/local/bin/python3"]]
    if not getattr(sys, "frozen", False):
        cands.insert(0, [sys.executable])
    seen = set()
    for c in cands:
        if c[0] in seen or (shutil.which(c[0]) is None and not Path(c[0]).is_file()):
            continue
        seen.add(c[0] if len(c) == 1 else " ".join(c))
        v = _py_version(c)
        if v and PY_MIN <= v <= PY_MAX:
            return c
    return None


def pack_status() -> dict:
    """{installed, version, python_path, size_mb, source}: pure detection, no downloads.
    source: 'pack' (frozen app's stems-pack dir), 'venv' (dev .venv-stems) or None."""
    py = S.demucs_python()
    installed = S.demucs_installed()
    version = None
    size_mb = 0.0
    if installed:
        try:
            version = json.loads((S.VENV_DIR / PACK_VERSION_FILE).read_text()).get("version")
        except Exception:  # noqa: BLE001 - no/garbled manifest
            version = None
        try:
            size_mb = round(sum(f.stat().st_size for f in S.VENV_DIR.rglob("*") if f.is_file()) / 1024 ** 2, 1)
        except OSError:
            size_mb = 0.0
    return {"installed": bool(installed), "version": version,
            "python_path": str(py) if installed else None, "size_mb": size_mb,
            "source": ("pack" if S.paths_frozen() else "venv") if installed else None}


# ---------------------------------------------------------------- installer
def _install_job():
    steps = 0
    try:
        INSTALL.update(status="running", progress=0.02, message="Creating environment", error=None)
        if not S.demucs_python().is_file():
            base = find_system_python() if getattr(sys, "frozen", False) else [sys.executable]
            if not base:
                raise RuntimeError("No system Python 3.9-3.12 found. Install Python from python.org, "
                                   "then try again (or drop a stems pack into the app data folder).")
            winsafe.run(base + ["-m", "venv", str(S.VENV_DIR)], check=True)
        req = S.ROOT / "requirements-stems.txt"
        proc = winsafe.popen([str(S.demucs_python()), "-m", "pip", "install", "--no-input", "-r", str(req)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        tail = []
        for line in proc.stdout:
            line = line.strip()
            if line.startswith(("Collecting", "Downloading", "Installing", "Using cached")):
                steps += 1
                INSTALL.update(progress=round(min(0.95, 0.05 + steps / 60), 3), message=line[:90])
            if line:
                tail = (tail + [line])[-6:]
        if proc.wait() != 0:
            raise RuntimeError("pip failed: " + " | ".join(tail[-3:]))
        if not S.demucs_installed():
            raise RuntimeError("Install finished but demucs is missing")
        INSTALL.update(status="done", progress=1.0, message="Demucs installed")
    except Exception as e:  # noqa: BLE001
        INSTALL.update(status="error", error=f"{type(e).__name__}: {e}", message=str(e))


@router.post("/api/stems/install", dependencies=[Depends(_origin_guard)])
def install_demucs():
    if S.demucs_installed():
        INSTALL.update(status="done", progress=1.0, message="Demucs already installed")
        return _public(INSTALL)
    if S.paths_frozen() and not find_system_python():
        raise HTTPException(501, S.PACK_MISSING_MSG + f" (drop a stems pack into {S.VENV_DIR})")
    free = S.free_bytes(S.ROOT)
    if free < S.MIN_FREE_BYTES:
        raise HTTPException(507, f"Not enough free disk space: {free / 1024 ** 3:.1f} GB free, 3 GB needed")
    with _install_lock:
        if INSTALL["status"] == "running":
            return _public(INSTALL)
        INSTALL.update(status="running", progress=0.0, message="Starting", error=None)
    threading.Thread(target=_install_job, daemon=True).start()
    return _public(INSTALL)


@router.get("/api/stems/install")
def install_status():
    d = _public(INSTALL)
    d["installed"] = S.demucs_installed()
    if S.paths_frozen() and not d["installed"]:
        d["message"] = S.PACK_MISSING_MSG
        d["frozen"] = True
    return d
