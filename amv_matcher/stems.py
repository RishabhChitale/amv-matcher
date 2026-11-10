"""Stem separation (HPSS quick split, or Demucs htdemucs in its own venv) and per-stem features.

separate(song_path, provider) -> {"provider", "stems": {name: wav_path}}, cached in
.cache/stems/<sha12>/<provider>/. stem_features(stems) -> energy curves, onsets, vocal phrase
starts, 1 s section textures and quiet_detail (soft events inside low-energy sections).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
from . import paths as _paths
from . import winsafe
CACHE_DIR = _paths.CACHE / "stems"           # module var so tests can redirect it
VENV_DIR = _paths.STEMS_PACK if _paths.FROZEN else ROOT / ".venv-stems"
PACK_MISSING_MSG = "Stems pack not installed: quick split only"
# Demucs weights (~80 MB) are downloaded on the FIRST separation, not at install time.  The runner is
# started with TORCH_HOME=this dir so they land in the app-data cache (<app data>/cache/torch), never in
# ~/.cache/torch; they are shared by every pack version and survive a pack rebuild.
TORCH_HOME = _paths.CACHE / "torch"
WEIGHTS_MSG = "Downloading Demucs weights (first time only)"
SR = 44100
ENERGY_HZ = 10
MIN_FREE_BYTES = 3 * 1024 ** 3
STEM_NAMES = {"hpss": ["drums", "music"], "demucs": ["drums", "bass", "vocals", "other"]}
FEATURES_VERSION = 1
LEAKED_TEMP: list = []     # temp paths that could not be removed (locked on Windows); reported, not silent


def paths_frozen() -> bool:
    return _paths.FROZEN


class Cancelled(Exception):
    pass


def song_key(path) -> str:
    p = Path(path).resolve()
    st = p.stat()
    # W-15: case/separator-normalised on Windows (identical to str(p) on POSIX; one-time cache miss on Windows)
    return hashlib.sha1(f"{winsafe.norm_case_path(p)}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:12]


def _cleanup_dir(d: Path, root: Path) -> bool:
    ok = winsafe.safe_rmtree(d, root)
    if not ok and Path(d).exists():
        LEAKED_TEMP.append(str(d))
        log.warning("could not remove temp dir %s", d)
    return ok


def _cleanup_file(f: Path, root: Path) -> bool:
    ok = winsafe.safe_unlink(f, root)
    if not ok and Path(f).exists():
        LEAKED_TEMP.append(str(f))
        log.warning("could not remove temp file %s", f)
    return ok


def stem_dir(path, provider: str) -> Path:
    return CACHE_DIR / song_key(path) / provider


def venv_python(venv_dir=None) -> Path:
    """Interpreter path inside a venv by platform layout: Scripts/python.exe (Windows) or bin/python."""
    v = Path(venv_dir or VENV_DIR)
    return v / "Scripts" / "python.exe" if winsafe._is_win() else v / "bin" / "python"


def demucs_python() -> Path:
    win = VENV_DIR / "Scripts" / "python.exe"
    return win if win.is_file() else VENV_DIR / "bin" / "python"


def weights_cached() -> bool:
    """htdemucs weights (*.th) already in the app-data cache (TORCH_HOME/hub/checkpoints)."""
    d = TORCH_HOME / "hub" / "checkpoints"
    try:
        return d.is_dir() and any(d.glob("*.th"))
    except OSError:
        return False


def demucs_installed() -> bool:
    py = demucs_python()
    return py.is_file() and (any((VENV_DIR / "lib").glob("python*/site-packages/demucs/__init__.py"))
                                or (VENV_DIR / "Lib" / "site-packages" / "demucs" / "__init__.py").is_file())


def free_bytes(path=None) -> int:
    target = Path(path or ROOT)
    while not target.exists():
        target = target.parent
    return shutil.disk_usage(target).free


def resolve_provider(provider: str) -> str:
    if provider == "auto":
        return "demucs" if demucs_installed() else "hpss"
    if provider not in STEM_NAMES:
        raise ValueError(f"Unknown provider {provider!r}")
    return provider


def cached(song_path, provider: str):
    d = stem_dir(song_path, provider)
    files = {n: d / f"{n}.wav" for n in STEM_NAMES[provider]}
    if all(f.is_file() for f in files.values()):
        return {"provider": provider, "stems": {n: str(f) for n, f in files.items()}}
    return None


def _load_stereo(path, sr=SR) -> np.ndarray:
    y, _ = librosa.load(str(path), sr=sr, mono=False)
    y = np.atleast_2d(y).astype(np.float32)
    if y.shape[0] == 1:
        y = np.vstack([y, y])
    return y[:2]


def _write(path: Path, y: np.ndarray, sr=SR):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.clip(y.T, -1, 1), sr, subtype="PCM_16")


def separate_hpss(song_path, out: Path, progress=None, cancel=None) -> dict:
    """Harmonic/percussive split via one soft mask from the mono mix, applied to each channel."""
    y = _load_stereo(song_path)
    if progress:
        progress(0.1, "Splitting harmonic / percussive")
    n_fft, hop = 2048, 512
    mono = y.mean(0)
    S = librosa.stft(mono, n_fft=n_fft, hop_length=hop)
    H, P = librosa.decompose.hpss(np.abs(S), margin=2.0)
    h2, p2 = H ** 2, P ** 2
    mh = h2 / (h2 + p2 + 1e-12)
    mp = 1.0 - mh
    outs = {"drums": [], "music": []}
    for ch in range(2):
        if cancel and cancel.is_set():
            raise Cancelled()
        Sc = librosa.stft(y[ch], n_fft=n_fft, hop_length=hop)
        outs["drums"].append(librosa.istft(Sc * mp, hop_length=hop, length=y.shape[1]))
        outs["music"].append(librosa.istft(Sc * mh, hop_length=hop, length=y.shape[1]))
        if progress:
            progress(0.5 + 0.4 * (ch + 1) / 2, "Splitting harmonic / percussive")
    for n, chs in outs.items():
        _write(out / f"{n}.wav", np.vstack(chs))
    return {"provider": "hpss", "stems": {n: str(out / f"{n}.wav") for n in outs}}


def separate_demucs(song_path, out: Path, progress=None, cancel=None, proc_holder=None) -> dict:
    if not demucs_installed():
        raise RuntimeError("Demucs is not installed (run the installer or pip install -r requirements-stems.txt)")
    free = free_bytes(CACHE_DIR)
    if free < MIN_FREE_BYTES:
        raise RuntimeError(f"Not enough free disk space: {free / 1024 ** 3:.1f} GB free, 3 GB needed")
    out.mkdir(parents=True, exist_ok=True)
    src = out / "_input.wav"
    if progress:
        progress(0.01, "Decoding song")
    _write(src, _load_stereo(song_path))
    TORCH_HOME.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, TORCH_HOME=str(TORCH_HOME), PYTHONUNBUFFERED="1", OMP_NUM_THREADS=str(os.cpu_count() or 4),
               AMV_DEMUCS_JOBS=os.environ.get("AMV_DEMUCS_JOBS", "4"))   # 4 worker processes: 162 s vs 755 s on a 233 s song, identical stems
    runner = Path(__file__).with_name("_demucs_runner.py")
    tmp = out / "_tmp"
    _cleanup_dir(tmp, out)
    need_weights = not weights_cached()
    if need_weights and progress:
        progress(0.02, WEIGHTS_MSG)
    # own session/process group (POSIX) so kill_tree reaches demucs' worker grandchildren; Windows: taskkill /T
    kw = {} if winsafe._is_win() else {"start_new_session": True}
    proc = winsafe.popen([str(demucs_python()), "-I", str(runner), str(src), str(tmp)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, **kw)
    if proc_holder is not None:
        proc_holder["proc"] = proc
    tail = []
    try:
        for line in proc.stdout:
            if cancel and cancel.is_set():
                winsafe.kill_tree(proc)
                raise Cancelled()
            line = line.strip()
            if line.startswith("PROGRESS "):
                parts = line.split()
                if progress:
                    loading = len(parts) > 2 and parts[2] == "loading-model"
                    msg = (WEIGHTS_MSG if need_weights else "Loading Demucs model") if loading \
                        else "Separating with Demucs (CPU)"
                    progress(float(parts[1]), msg)
            elif line:
                tail.append(line)
                tail = tail[-8:]
        rc = proc.wait()
    finally:
        if proc.poll() is None:
            winsafe.kill_tree(proc)
            proc.wait()
    if cancel and cancel.is_set():
        _cleanup_dir(tmp, out)
        raise Cancelled()
    if rc != 0:
        raise RuntimeError("Demucs failed: " + " | ".join(tail[-3:]))
    for n in STEM_NAMES["demucs"]:
        winsafe.replace_retry(tmp / f"{n}.wav", out / f"{n}.wav")
    _cleanup_dir(tmp, out)
    _cleanup_file(src, out)
    return {"provider": "demucs", "stems": {n: str(out / f"{n}.wav") for n in STEM_NAMES["demucs"]}}


def separate(song_path, provider: str = "auto", progress=None, cancel=None, proc_holder=None) -> dict:
    """Separate (or load from cache). progress(frac, message)."""
    provider = resolve_provider(provider)
    hit = cached(song_path, provider)
    if hit:
        return hit
    out = stem_dir(song_path, provider)
    if provider == "hpss":
        return separate_hpss(song_path, out, progress, cancel)
    return separate_demucs(song_path, out, progress, cancel, proc_holder)


# ---------------------------------------------------------------- features
def _mono_rms(path, hz=ENERGY_HZ):
    y, sr = sf.read(str(path), dtype="float32", always_2d=True)
    m = y.mean(1)
    win = int(sr / hz)
    n = len(m) // win
    if n == 0:
        return np.zeros(1), m, sr
    rms = np.sqrt((m[: n * win].reshape(n, win) ** 2).mean(1))
    return rms, m, sr


def _norm(x):
    x = np.asarray(x, dtype=float)
    p = np.percentile(x, 95) if len(x) else 0.0
    return x / p if p > 1e-9 else np.zeros_like(x)


def _runs(mask, min_len):
    out, start = [], None
    for i, v in enumerate(list(mask) + [False]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start >= min_len:
                out.append((start, i))
            start = None
    return out


def stem_features(stems: dict, window_s: float = 1.0, quiet_pct: float = 30.0, per_stem_events: int = 10) -> dict:
    """stems: {name: wav_path}. Everything is in seconds on the stems' shared timeline."""
    names = list(stems)
    per, rms_raw, mono22 = {}, {}, {}
    n_min = None
    for n in names:
        rms, m, sr = _mono_rms(stems[n])
        rms_raw[n] = rms
        mono22[n] = librosa.resample(m, orig_sr=sr, target_sr=22050) if sr != 22050 else m
        n_min = len(rms) if n_min is None else min(n_min, len(rms))
    duration = float(sf.info(stems[names[0]]).duration)
    for n in names:
        rms_raw[n] = rms_raw[n][:n_min]
    # section energy: sum of all stems, 1-s smoothing
    total = sum(rms_raw[n] for n in names)
    k = max(1, int(ENERGY_HZ * window_s))
    total_s = np.convolve(total, np.ones(k) / k, mode="same")
    # quiet sections: below the quiet_pct percentile of the smoothed total, at least 2 s long
    thr = np.percentile(total_s, quiet_pct)
    quiet_mask = total_s <= thr
    quiet_runs = _runs(quiet_mask, 2 * ENERGY_HZ)
    quiet_sections = [{"start": round(a / ENERGY_HZ, 2), "end": round(b / ENERGY_HZ, 2)} for a, b in quiet_runs]

    onset_env_cache = {}
    for n in names:
        env = librosa.onset.onset_strength(y=mono22[n], sr=22050, hop_length=512)
        onset_env_cache[n] = env
        times_f = librosa.onset.onset_detect(onset_envelope=env, sr=22050, hop_length=512, backtrack=False)
        t = librosa.frames_to_time(times_f, sr=22050, hop_length=512)
        per[n] = {"energy_curve": [round(float(v), 4) for v in _norm(rms_raw[n])],
                  "onsets": [round(float(v), 3) for v in t],
                  "onset_strength": [round(float(env[f] / (np.percentile(env, 95) + 1e-9)), 3) for f in times_f]}

    # vocal phrase starts
    vocal_starts = []
    if "vocals" in stems:
        v = _norm(rms_raw["vocals"])
        active = v > 0.12
        i, silent_since = 0, 0
        was_silent = ENERGY_HZ * 0.6
        run = 0
        for idx, a in enumerate(active):
            if a:
                if run >= was_silent:
                    vocal_starts.append(round(idx / ENERGY_HZ, 2))
                run = 0
            else:
                run += 1
        # the very first phrase counts if the song opens with silence of any length >= 0.6 s
        # (handled above); a vocal active from t=0 is not a "start after silence"
    # textures: per-window active stems
    wn = max(1, int(ENERGY_HZ * window_s))
    n_win = n_min // wn
    levels = {}
    for n in names:
        p95 = np.percentile(rms_raw[n], 95) if len(rms_raw[n]) else 0.0
        w = rms_raw[n][: n_win * wn].reshape(n_win, wn).mean(1) if n_win else np.zeros(0)
        levels[n] = w / p95 if p95 > 1e-9 else np.zeros(n_win)
    textures = []
    for i in range(n_win):
        act = [n for n in names if levels[n][i] > 0.15]
        miss = [n for n in names if n not in act]
        if not act:
            label = "silence"
        elif not miss:
            label = "+".join(act) + ", all stems"
        else:
            label = "+".join(act) + ", no " + "+".join(miss)
        textures.append({"t": round(i * window_s, 2), "active": act, "label": label})
    sections, cur = [], None
    for tx in textures:
        if cur and cur["active"] == tx["active"]:
            cur["end"] = round(tx["t"] + window_s, 2)
        else:
            cur = {"start": tx["t"], "end": round(tx["t"] + window_s, 2), "active": tx["active"], "label": tx["label"]}
            sections.append(cur)

    # quiet_detail: strongest soft onsets of each stem inside the quiet sections
    quiet_detail = []
    for n in names:
        env = onset_env_cache[n]
        p95 = np.percentile(env, 95) + 1e-9
        t = np.array(per[n]["onsets"])
        s = np.array(per[n]["onset_strength"])
        keep = []
        for ti, si in zip(t, s):
            if any(a / ENERGY_HZ <= ti <= b / ENERGY_HZ for a, b in quiet_runs):
                # skip events of stems that are effectively silent right there
                idx = min(int(ti * ENERGY_HZ), n_min - 1)
                if per[n]["energy_curve"][idx] > 0.0005:
                    keep.append((float(si), float(ti)))
        keep.sort(reverse=True)
        picked = []
        for si, ti in keep:
            if all(abs(ti - pt) >= 0.25 for _, pt in picked):
                picked.append((si, ti))
            if len(picked) >= per_stem_events:
                break
        quiet_detail += [{"t": round(ti, 3), "stem": n, "strength": round(si, 3)} for si, ti in picked]
    quiet_detail.sort(key=lambda e: e["t"])
    return {"version": FEATURES_VERSION, "duration": round(duration, 3), "energy_hz": ENERGY_HZ,
            "stems": per, "vocal_phrase_starts": vocal_starts, "textures": textures,
            "texture_sections": sections, "quiet_sections": quiet_sections, "quiet_detail": quiet_detail}


_feat_lock = threading.Lock()


def features_for(song_path, provider: str) -> dict:
    """Cached features for an already separated (song, provider)."""
    hit = cached(song_path, provider)
    if not hit:
        raise FileNotFoundError("stems not separated yet")
    f = stem_dir(song_path, provider) / "features.json"
    with _feat_lock:
        try:
            d = json.loads(f.read_text())
            if d.get("version") == FEATURES_VERSION:
                return d
        except Exception:
            pass
        d = stem_features(hit["stems"])
        d["provider"] = provider
        f.write_text(json.dumps(d))
        return d
