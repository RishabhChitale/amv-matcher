"""PyAV helpers: embedded-audio extraction, audio rendering (splice/trim) and mp4 export.

PyAV's wheel bundles the ffmpeg libraries, so no system ffmpeg is needed. OpenCV stays the
frame-analysis library; PyAV is used only here.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Callable, Optional

import av
import numpy as np
import soundfile as sf

SR = 44100
COPYABLE_VIDEO = {"h264", "hevc", "mpeg4", "av1", "vp9"}   # codecs we remux into mp4 as-is


# ---------------------------------------------------------------- extraction
def _extract_key(path: Path) -> str:
    st = path.stat()
    return hashlib.sha256(f"{path.resolve()}|{int(st.st_mtime)}|{st.st_size}".encode()).hexdigest()[:12]


def has_audio_stream(video_path) -> bool:
    with av.open(str(video_path)) as c:
        return len(c.streams.audio) > 0


def extract_audio(video_path, out_dir: Path) -> Optional[Path]:
    """Decode the first audio stream to out_dir/<sha12>.wav (44.1 kHz stereo). None if no audio."""
    vp = Path(video_path)
    out = Path(out_dir) / f"{_extract_key(vp)}.wav"
    if out.is_file() and out.stat().st_size > 44:
        return out
    chunks = []
    with av.open(str(vp)) as c:
        if not c.streams.audio:
            return None
        rs = av.AudioResampler(format="s16", layout="stereo", rate=SR)
        for frame in c.decode(audio=0):
            for f in rs.resample(frame):
                chunks.append(f.to_ndarray())
        for f in rs.resample(None):
            chunks.append(f.to_ndarray())
    if not chunks:
        return None
    # packed s16 stereo -> shape (1, n*2); interleaved L R
    data = np.concatenate([a.reshape(-1) for a in chunks]).reshape(-1, 2)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.wav")
    sf.write(str(tmp), data, SR, subtype="PCM_16")
    os.replace(tmp, out)
    return out
