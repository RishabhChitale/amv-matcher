"""Synthetic fixtures generated in code (no copyrighted media)."""
import cv2
import numpy as np
import soundfile as sf

SR = 22050


def make_click_track(path, bpm, seconds=30.0, offset=0.0):
    y = np.zeros(int(SR * seconds), dtype=np.float32)
    n = int(0.03 * SR)
    t = np.arange(n) / SR
    click = (np.sin(2 * np.pi * 1000 * t) * np.exp(-t * 150)).astype(np.float32)
    period = 60.0 / bpm
    k = 0
    while offset + k * period < seconds - 0.05:
        i = int((offset + k * period) * SR)
        y[i:i + n] += click[: len(y) - i][:n]
        k += 1
    sf.write(str(path), y * 0.9, SR)


def make_video(path, seconds=10.0, fps=30, jump_every=0.5, size=(320, 240)):
    w, h = size
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, size)
    assert vw.isOpened(), "OpenCV could not open a VideoWriter"
    positions = [(40, 40), (240, 160), (200, 40), (60, 150)]
    for f in range(int(seconds * fps)):
        seg = int((f / fps) / jump_every + 1e-6)
        x, y = positions[seg % len(positions)]
        img = np.zeros((h, w, 3), np.uint8)
        cv2.rectangle(img, (x, y), (x + 40, y + 40), (255, 255, 255), -1)
        vw.write(img)
    vw.release()


def make_burst_video(path, calm=5.0, burst=5.0, fps=30, size=(320, 240)):
    """Static for `calm` s, then a rapidly jumping square for `burst` s."""
    w, h = size
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, size)
    assert vw.isOpened(), "OpenCV could not open a VideoWriter"
    pos = [(40, 40), (240, 160), (200, 40), (60, 150)]
    for f in range(int((calm + burst) * fps)):
        t = f / fps
        img = np.zeros((h, w, 3), np.uint8)
        if t < calm:
            cv2.rectangle(img, (40, 40), (80, 80), (255, 255, 255), -1)
        else:
            x, y = pos[int((t - calm) / 0.2) % 4]
            cv2.rectangle(img, (x, y), (x + 40, y + 40), (255, 255, 255), -1)
        vw.write(img)
    vw.release()


def make_quiet_loud(path, quiet=5.0, loud=5.0, loud_first=False, seed=0):
    """Near-silent for `quiet` s and a loud bass + noise burst for `loud` s (order selectable)."""
    rng = np.random.default_rng(seed)
    nq, nl = int(quiet * SR), int(loud * SR)
    tl = np.arange(nl) / SR
    loud_part = (0.6 * np.sin(2 * np.pi * 70 * tl) + 0.3 * rng.standard_normal(nl)).astype(np.float32)
    quiet_part = (0.002 * rng.standard_normal(nq)).astype(np.float32)
    y = np.concatenate([loud_part, quiet_part] if loud_first else [quiet_part, loud_part])
    sf.write(str(path), np.clip(y, -1, 1), SR)


def make_drop(path, bpm=120, seconds=12.0, drop_at=6.0, seed=0):
    """Quiet clicks until `drop_at`, then loud clicks plus a 60 Hz thump on every beat (a clear drop)."""
    rng = np.random.default_rng(seed)
    y = np.zeros(int(SR * seconds), dtype=np.float32)
    n = int(0.03 * SR)
    t = np.arange(n) / SR
    click = (np.sin(2 * np.pi * 1000 * t) * np.exp(-t * 150)).astype(np.float32)
    nt = int(0.25 * SR)
    tt = np.arange(nt) / SR
    thump = (np.sin(2 * np.pi * 60 * tt) * np.exp(-tt * 8)).astype(np.float32)
    period = 60.0 / bpm
    k = 0
    while k * period < seconds - 0.3:
        b = k * period
        i = int(b * SR)
        if b < drop_at - 1e-6:
            y[i:i + n] += 0.08 * click
        else:
            y[i:i + n] += 0.9 * click
            y[i:i + nt] += 0.9 * thump[: len(y) - i][:nt]
        k += 1
    y += (0.002 * rng.standard_normal(len(y))).astype(np.float32)
    sf.write(str(path), np.clip(y, -1, 1), SR)


def make_video_with_audio(path, wav, seconds=10.0, fps=30, jump_every=0.5, size=(320, 240),
                          vcodec="libx264", acodec="aac"):
    """Jumping-square video muxed with `wav` as its soundtrack, written with PyAV."""
    import av
    w, h = size
    out = av.open(str(path), "w")
    vs = out.add_stream(vcodec, rate=fps)
    vs.width, vs.height = w, h
    vs.pix_fmt = "yuvj420p" if vcodec == "mjpeg" else "yuv420p"
    if vcodec == "libx264":
        vs.options = {"preset": "ultrafast", "crf": "18"}
    a = out.add_stream(acodec, rate=44100)
    a.layout = "stereo"
    pos = [(40, 40), (240, 160), (200, 40), (60, 150)]
    for f in range(int(seconds * fps)):
        x, y = pos[int((f / fps) / jump_every + 1e-6) % 4]
        img = np.zeros((h, w, 3), np.uint8)
        img[y:y + 40, x:x + 40] = 255
        for p in vs.encode(av.VideoFrame.from_ndarray(img, format="bgr24")):
            out.mux(p)
    for p in vs.encode(None):
        out.mux(p)
    data, sr = sf.read(str(wav), dtype="int16")
    data = data[: int(seconds * sr)]
    fmt = "s16" if acodec.startswith("pcm") else "fltp"
    rs = av.AudioResampler(format=fmt, layout="stereo", rate=44100,
                           frame_size=None if acodec.startswith("pcm") else 1024)
    fr = av.AudioFrame.from_ndarray(data.reshape(1, -1), format="s16", layout="mono")
    fr.sample_rate = sr
    for r in rs.resample(fr) + rs.resample(None):
        for p in a.encode(r):
            out.mux(p)
    for p in a.encode(None):
        out.mux(p)
    out.close()


def make_peaks_video(path, seconds=30.0, peaks=(10.0, 25.0), peak_len=4.0, fps=30, size=(320, 240)):
    """Static square, with a rapidly jumping square during each (start, start+peak_len) peak."""
    w, h = size
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, size)
    assert vw.isOpened(), "OpenCV could not open a VideoWriter"
    pos = [(40, 40), (240, 160), (200, 40), (60, 150)]
    for f in range(int(seconds * fps)):
        t = f / fps
        img = np.zeros((h, w, 3), np.uint8)
        pk = next((p for p in peaks if p <= t < p + peak_len), None)
        x, y = (40, 40) if pk is None else pos[int((t - pk) / 0.2) % 4]
        cv2.rectangle(img, (x, y), (x + 40, y + 40), (255, 255, 255), -1)
        vw.write(img)
    vw.release()


def make_loud_windows(path, bpm=120, seconds=60.0, loud=((40.0, 48.0),), seed=0):
    """Quiet clicks, with loud clicks plus a 60 Hz thump on every beat inside each (start, end) window."""
    rng = np.random.default_rng(seed)
    y = np.zeros(int(SR * seconds), dtype=np.float32)
    n = int(0.03 * SR)
    t = np.arange(n) / SR
    click = (np.sin(2 * np.pi * 1000 * t) * np.exp(-t * 150)).astype(np.float32)
    nt = int(0.25 * SR)
    tt = np.arange(nt) / SR
    thump = (np.sin(2 * np.pi * 60 * tt) * np.exp(-tt * 8)).astype(np.float32)
    period = 60.0 / bpm
    k = 0
    while k * period < seconds - 0.3:
        b = k * period
        i = int(b * SR)
        if any(a <= b < e for a, e in loud):
            y[i:i + n] += 0.9 * click
            y[i:i + nt] += 0.9 * thump[: len(y) - i][:nt]
        else:
            y[i:i + n] += 0.08 * click
        k += 1
    y += (0.002 * rng.standard_normal(len(y))).astype(np.float32)
    sf.write(str(path), np.clip(y, -1, 1), SR)


def make_video_jumps(path, jump_times, seconds=20.0, fps=30, size=(320, 240)):
    """Square jumps at the given times (s); between them it holds still."""
    w, h = size
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, size)
    assert vw.isOpened(), "OpenCV could not open a VideoWriter"
    positions = [(40, 40), (240, 160), (200, 40), (60, 150)]
    times = sorted(jump_times)
    for f in range(int(seconds * fps)):
        t = f / fps + 1e-6
        seg = sum(1 for j in times if j <= t)
        x, y = positions[seg % len(positions)]
        img = np.zeros((h, w, 3), np.uint8)
        cv2.rectangle(img, (x, y), (x + 40, y + 40), (255, 255, 255), -1)
        vw.write(img)
    vw.release()


def slow_section_jump_times(seconds=20.0):
    """0.5 s between jumps, 0.625 s between 5 and 15 s (a known slow section)."""
    out, t = [], 0.0
    while t < seconds:
        out.append(round(t, 4))
        t += 0.625 if 5.0 <= t < 15.0 else 0.5
    return out
