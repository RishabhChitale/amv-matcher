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
INSTALL: dict = {"status": "idle", "progress": 0.0, "message": "", "error": None}   # mirror of the latest install job
INSTALL_JOBS: dict = {}      # job id -> install job
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
        job.update(progress=round(float(frac), 3), message=msg,
                   stage="downloading_weights" if msg == S.WEIGHTS_MSG else "separating")
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
               "progress": 0.0, "message": "Starting", "stage": "starting", "error": None, "stems": None, "stem_names": S.STEM_NAMES[provider],
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


PACK_VERSION = "1"        # bump when requirements-stems.txt changes: older packs are rebuilt
MIN_INSTALL_BYTES = 3 * 1024 ** 3
STAGE_ORDER = ("starting", "venv", "downloading", "installing", "verifying", "done")


def _read_marker() -> dict | None:
    try:
        d = json.loads((S.VENV_DIR / PACK_VERSION_FILE).read_text())
        return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001 - missing / garbled
        return None


def _complete() -> bool:
    """A finished pack: marker (written LAST by the installer) with the current version.  A dev
    .venv-stems (not frozen) with demucs in it also counts, it was built by hand."""
    m = _read_marker()
    if m is not None:
        return m.get("version") == PACK_VERSION
    return not S.paths_frozen() and S.demucs_installed()


def pack_status() -> dict:
    """{installed, version, python_path, size_mb, source, half_installed, weights_cached}: pure detection.
    source: 'pack' (frozen app's stems-pack dir), 'venv' (dev .venv-stems) or None.
    half_installed: a venv dir exists but has no marker / another pack version (the next install rebuilds it).
    weights_cached: htdemucs weights already downloaded (else the first separation downloads them)."""
    m = _read_marker()
    installed = bool(S.demucs_installed() and _complete())
    half = S.VENV_DIR.exists() and not _complete()
    size_mb = 0.0
    if installed or half:
        try:
            size_mb = round(sum(f.stat().st_size for f in S.VENV_DIR.rglob("*") if f.is_file()) / 1024 ** 2, 1)
        except OSError:
            size_mb = 0.0
    return {"installed": installed, "version": (m or {}).get("version", "dev") if installed else None,
            "python_path": str(S.demucs_python()) if installed else None, "size_mb": size_mb,
            "source": ("pack" if S.paths_frozen() else "venv") if installed else None,
            "half_installed": bool(half), "weights_cached": bool(S.weights_cached())}


# ---------------------------------------------------------------- installer
def _bundled(name: str) -> Path:
    base = getattr(sys, "_MEIPASS", None)
    return (Path(base) / name) if base and (Path(base) / name).is_file() else S.ROOT / name


def _upd(job: dict, **kw):
    job.update(kw)
    if INSTALL.get("id") == job["id"]:
        INSTALL.update(kw)


def _parse_pip(line: str, state: dict):
    """-> (stage, message) from one pip output line, or None.  Counts distinct 'Collecting' packages."""
    if line.startswith("Collecting "):
        pkg = re.split(r"[ =<>!~\[;(]", line[11:].strip(), maxsplit=1)[0]
        state["pkg"] = pkg
        state["n"] = state.get("n", 0) + 1
        return "downloading", f"Downloading {pkg}"
    if line.startswith(("Downloading ", "Using cached ")):
        return "downloading", f"Downloading {state.get('pkg', 'packages')}"
    if line.startswith("Installing collected packages"):
        return "installing", "Installing packages"
    if line.startswith("Successfully installed"):
        return "installing", "Packages installed"
    return None


def _finish_cancel(job: dict):
    winsafe.safe_rmtree(S.VENV_DIR, S.VENV_DIR.parent)
    _upd(job, status="cancelled", message="Cancelled", error=None)


def _install_job(job: dict | None = None):
    if job is None:      # direct call (tests / scripts)
        job = _new_install_job()
    cancel = job["_cancel"]
    state: dict = {}
    t0 = time.time()
    try:
        _upd(job, status="running", stage="venv", progress=0.02, message="Creating environment", error=None)
        base = find_system_python() if getattr(sys, "frozen", False) else [sys.executable]
        if not base:
            raise RuntimeError("No system Python 3.9-3.12 found. Install Python from python.org, "
                               "then try again (or drop a stems pack into the app data folder).")
        if S.VENV_DIR.exists():              # half-installed / other pack version: rebuild from scratch
            if not winsafe.safe_rmtree(S.VENV_DIR, S.VENV_DIR.parent):
                raise RuntimeError(f"Could not remove the old environment at {S.VENV_DIR}")
        S.VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
        winsafe.run(base + ["-m", "venv", str(S.VENV_DIR)], check=True)
        if cancel.is_set():
            return _finish_cancel(job)
        py = S.venv_python(S.VENV_DIR)
        cmd = [str(py), "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
               "-r", str(_bundled("requirements-stems.txt"))]
        cons = _bundled("constraints-stems-macos.txt")
        total = 20
        if sys.platform == "darwin" and cons.is_file() and _py_version(base) == (3, 9):
            cmd += ["-c", str(cons)]
            total = max(total, sum(1 for ln in cons.read_text().splitlines() if ln.strip() and not ln.startswith("#")))
        _upd(job, stage="downloading", progress=0.05, message="Starting download")
        kw = {} if winsafe._is_win() else {"start_new_session": True}
        proc = winsafe.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kw)
        job["_proc"]["proc"] = proc
        tail = []
        for line in proc.stdout:
            if cancel.is_set():
                winsafe.kill_tree(proc)
                break
            line = line.strip()
            if not line:
                continue
            tail = (tail + [line])[-6:]
            r = _parse_pip(line, state)
            if r:
                stage, msg = r
                frac = 0.88 if stage == "installing" else 0.05 + 0.80 * min(1.0, state.get("n", 0) / total)
                _upd(job, stage=stage, progress=round(frac, 3), message=msg, package=state.get("pkg"))
        rc = proc.wait()
        if cancel.is_set():
            return _finish_cancel(job)
        if rc != 0:
            raise RuntimeError("pip failed: " + " | ".join(tail[-3:]))
        _upd(job, stage="verifying", progress=0.93, message="Verifying install")
        r = winsafe.run([str(py), "-I", "-c",
                         "import torch, demucs; from importlib.metadata import version as v; "
                         "print('torch', torch.__version__); print('demucs', v('demucs'))"],
                        capture_output=True, text=True, timeout=300)
        if cancel.is_set():
            return _finish_cancel(job)
        out = (r.stdout or "").strip()
        if r.returncode != 0:
            raise RuntimeError("Verification failed: " + " | ".join(((r.stderr or out).strip().splitlines() or ["?"])[-3:]))
        vers = dict(ln.split(" ", 1) for ln in out.splitlines() if " " in ln)
        pyv = _py_version(base)
        marker = {"version": PACK_VERSION, "torch": vers.get("torch"), "demucs": vers.get("demucs"),
                  "python": "%d.%d" % pyv if pyv else None, "installed_at": int(time.time())}
        tmp = S.VENV_DIR / (PACK_VERSION_FILE + ".tmp")
        tmp.write_text(json.dumps(marker))
        winsafe.replace_retry(tmp, S.VENV_DIR / PACK_VERSION_FILE)       # marker LAST
        _upd(job, status="done", stage="done", progress=1.0, message="Demucs installed",
             torch=vers.get("torch"), demucs=vers.get("demucs"), seconds=round(time.time() - t0, 1))
    except Exception as e:  # noqa: BLE001
        if cancel.is_set():
            return _finish_cancel(job)
        _upd(job, status="error", error=f"{type(e).__name__}: {e}", message=str(e))


def _new_install_job() -> dict:
    job = {"id": uuid.uuid4().hex[:12], "status": "running", "stage": "starting", "progress": 0.0,
           "message": "Starting", "error": None, "package": None, "started": time.time(),
           "_cancel": threading.Event(), "_proc": {}}
    INSTALL_JOBS[job["id"]] = job
    INSTALL.clear()
    INSTALL.update(_public(job))
    return job


def _spawn(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


@router.post("/api/stems/install", dependencies=[Depends(_origin_guard)])
def install_demucs():
    """Start the pack install job.  200 job (status running | done if already installed), 409 if one is
    running, 501 frozen without system Python, 507 under 3 GB free."""
    with _install_lock:
        cur = INSTALL_JOBS.get(INSTALL.get("id"))
        if cur and cur["status"] == "running":
            raise HTTPException(409, "An install is already running")
        if S.demucs_installed() and _complete():
            return {"id": None, "status": "done", "stage": "done", "progress": 1.0,
                    "message": "Demucs already installed", "error": None}
        if S.paths_frozen() and not find_system_python():
            raise HTTPException(501, S.PACK_MISSING_MSG + f" (drop a stems pack into {S.VENV_DIR})")
        free = S.free_bytes(S.VENV_DIR.parent)
        if free < MIN_INSTALL_BYTES:
            raise HTTPException(507, f"Not enough free disk space: {free / 1024 ** 3:.1f} GB free, 3 GB needed")
        job = _new_install_job()
    _spawn(_install_job, job)
    return _public(job)


@router.post("/api/stems/install/{job_id}/cancel", dependencies=[Depends(_origin_guard)])
def cancel_install(job_id: str):
    job = INSTALL_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown install job")
    was_running = job["status"] == "running"
    if was_running:
        job["_cancel"].set()
        p = job["_proc"].get("proc")
        if p and p.poll() is None:
            winsafe.kill_tree(p)
    return {"cancelled": was_running, "job": _public(job)}


@router.get("/api/stems/install/{job_id}")
def install_job_status(job_id: str):
    job = INSTALL_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown install job")
    return _public(job)


@router.get("/api/stems/pack")
def pack_info():
    return pack_status()


@router.get("/api/stems/install")
def install_status():
    d = _public(INSTALL)
    d["installed"] = S.demucs_installed()
    d["pack"] = pack_status()
    if S.paths_frozen() and not d["installed"] and d.get("status") in (None, "idle"):
        d["message"] = S.PACK_MISSING_MSG
        d["frozen"] = True
    return d
