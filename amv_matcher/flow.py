"""One dense optical-flow pass per video: global CAMERA motion vs local SUBJECT motion.

FlowPass is fed the gray 160-px frames that video.analyze_video already decodes (no second
decode). Every `stride`-th frame pair (~FLOW_FPS pairs/s) gets a Farneback dense flow; a robust
similarity fit (pan dx/dy, zoom, roll) on a fixed grid of flow samples is the camera; the
per-pixel residual is the subject. Everything is deterministic: Farneback is a fixed numeric
pipeline and the fit is a plain IRLS (Tukey weights, fixed iteration count, no random sampling),
so the same frames give the same numbers on every run.

Units: pan in frame-widths per second (+x right, +y down), zoom and roll in log-scale / radians
per second (+zoom = zooming in), subject in frame-widths per second (mean residual speed over the
moving pixels).

summarize(...) turns the raw per-pair series into the result fields:
  camera_motion   4 Hz normalized camera-motion intensity + pan/zoom/whip flags + raw pan/zoom
  camera_events   [{start, end, kind pan|tilt|zoom|whip, speed, smooth, direction}]
  subject_curve   10 Hz subject-motion speed (curves.HZ), for the energy curve and booms
"""
from __future__ import annotations

import cv2
import numpy as np
from scipy.ndimage import median_filter, uniform_filter1d

from . import curves

FLOW_FPS = 15.0          # flow pairs per second (stride = round(fps / FLOW_FPS))
GRID_STEP = 4            # px between fit samples
GRID_BORDER = 6          # px ignored at the frame edge (letterbox, flow edge artefacts)
IRLS_ITERS = 4
TUKEY_C = 3.0            # Tukey cutoff in robust sigmas
MIN_SIGMA_PX = 0.15
CAM_HZ = 4.0
CAM_REF = 0.3            # intensity = speed / (speed + CAM_REF): slow pan ~0.25, fast ~0.6, whip ~0.8
ZOOM_W = 0.5             # zoom rate (log-scale/s) -> pan-equivalent widths/s
ROLL_W = 0.3
PAN_MIN = 0.04           # widths/s: below this the camera is "still"
ZOOM_MIN = 0.08          # log-scale/s
WHIP_MIN = 1.5           # widths/s
WHIP_MAX_S = 0.8         # a whip is a short burst
EVENT_MIN_S = 0.5        # a pan/zoom event lasts at least this long (whips are exempt)
MIN_INLIERS = 0.35       # fit quality below this: the frame is mostly subject, camera = unknown (0)
CUT_GUARD_S = 0.04       # pairs whose span touches a cut (+- this) are invalid


def _grid(h, w):
    ys = np.arange(GRID_BORDER, h - GRID_BORDER, GRID_STEP)
    xs = np.arange(GRID_BORDER, w - GRID_BORDER, GRID_STEP)
    gx, gy = np.meshgrid(xs, ys)
    return gy.ravel(), gx.ravel()


def fit_similarity(px, py, dx, dy, w):
    """Robust 4-dof fit of d = [[a, -b], [b, a]] @ p + t on centred, width-normalized points.
    Returns (tx_px, ty_px, a, b, residual_px[], inlier_frac). a ~ scale-1, b ~ rotation (rad)."""
    x = px / w
    y = py / w
    n = len(x)
    A = np.zeros((2 * n, 4))
    A[0::2, 0], A[0::2, 1], A[0::2, 2] = x, -y, 1.0
    A[1::2, 0], A[1::2, 1], A[1::2, 3] = y, x, 1.0
    rhs = np.empty(2 * n)
    rhs[0::2], rhs[1::2] = dx / w, dy / w      # displacements in widths, like the points
    wt = np.ones(n)
    p = np.zeros(4)
    for _ in range(IRLS_ITERS):
        W = np.repeat(wt, 2)
        Aw = A * W[:, None]
        M = A.T @ Aw
        if np.linalg.cond(M) > 1e12:
            break
        p = np.linalg.solve(M, Aw.T @ rhs)
        r = rhs - A @ p
        rm = np.hypot(r[0::2], r[1::2]) * w
        sig = max(1.4826 * float(np.median(rm)), MIN_SIGMA_PX)
        u = rm / (TUKEY_C * sig)
        wt = np.where(u < 1.0, (1.0 - u * u) ** 2, 0.0)
        if wt.sum() < 8:
            wt = np.ones(n)
            break
    r = rhs - A @ p
    rm = np.hypot(r[0::2], r[1::2]) * w
    sig = max(1.4826 * float(np.median(rm)), MIN_SIGMA_PX)
    inl = float(np.mean(rm < TUKEY_C * sig))
    a, b, tx, ty = (float(v) for v in p)
    return tx * w, ty * w, a, b, rm, inl


class FlowPass:
    """Feed every decoded frame (uint8 gray) with feed(); read .series() after the last one."""

    def __init__(self, fps: float, flow_fps: float = FLOW_FPS):
        self.fps = float(fps)
        self.stride = max(1, int(round(self.fps / flow_fps)))
        self.rate = self.fps / self.stride          # pairs per second
        self.i = -1
        self.prev = None
        self.grid = None
        self.rows = []      # (t, tx, ty, zoom, roll, subject, subject_p90, inliers, mag)

    def feed(self, gray: np.ndarray):
        self.i += 1
        if self.i % self.stride:
            return
        g = gray if gray.dtype == np.uint8 else np.clip(gray, 0, 255).astype(np.uint8)
        if self.prev is not None:
            self._pair(self.prev, g)
        self.prev = g

    def _pair(self, a, b):
        h, w = a.shape[:2]
        if self.grid is None or self.grid[2] != (h, w):
            gy, gx = _grid(h, w)
            self.grid = (gy, gx, (h, w), gx - w / 2.0, gy - h / 2.0)
        gy, gx, _, cx, cy = self.grid
        fl = cv2.calcOpticalFlowFarneback(a, b, None, 0.5, 4, 13, 3, 5, 1.1, 0)
        dx = fl[gy, gx, 0].astype(np.float64)
        dy = fl[gy, gx, 1].astype(np.float64)
        tx, ty, za, rb, rm, inl = fit_similarity(cx, cy, dx, dy, float(w))
        k = self.rate / float(w)                     # px per pair -> widths per second
        moving = rm[rm > np.percentile(rm, 75)] if len(rm) >= 8 else rm
        self.rows.append((
            self.i / self.fps,
            tx * k, ty * k,
            float(np.log1p(max(za, -0.9))) * self.rate, rb * self.rate,
            float(moving.mean()) * k if len(moving) else 0.0,
            float(np.percentile(rm, 90)) * k,
            inl,
            float(np.hypot(dx, dy).mean()) * k,
        ))

    def series(self) -> dict:
        cols = ["t", "pan_x", "pan_y", "zoom", "roll", "subject", "subject_p90", "inliers", "mag"]
        arr = np.array(self.rows, dtype=float).reshape(-1, len(cols))
        out = {c: arr[:, j] for j, c in enumerate(cols)}
        out["rate"] = self.rate
        return out


def _valid_mask(t, rate, cut_times):
    """False for pairs whose span (t - 1/rate, t] touches a cut: flow across a cut is garbage."""
    ok = np.ones(len(t), dtype=bool)
    if not len(cut_times) or not len(t):
        return ok
    c = np.asarray(cut_times, dtype=float)
    lo = t - 1.0 / rate - CUT_GUARD_S
    hi = t + CUT_GUARD_S
    j = np.searchsorted(c, lo, side="left")
    for i in range(len(t)):
        if j[i] < len(c) and c[j[i]] <= hi[i]:
            ok[i] = False
    return ok


def _fill(x, ok):
    """Linear interpolation over invalid samples (edges hold the nearest valid value)."""
    x = np.asarray(x, dtype=float).copy()
    if ok.all() or not ok.any():
        return x if ok.any() else np.zeros_like(x)
    idx = np.arange(len(x))
    x[~ok] = np.interp(idx[~ok], idx[ok], x[ok])
    return x


def _resample(t, x, hz, duration):
    n = max(1, int(np.ceil(duration * hz)))
    if not len(t):
        return np.zeros(n)
    grid = (np.arange(n) + 0.5) / hz
    return np.interp(grid, t, x)


def _runs(mask):
    out, start = [], None
    for i, m in enumerate(mask):
        if m and start is None:
            start = i
        elif not m and start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, len(mask)))
    return out


def camera_speed(s):
    """Pan-equivalent camera speed (widths/s) per pair; 0 where the fit is unreliable."""
    sp = np.hypot(s["pan_x"], s["pan_y"]) + ZOOM_W * np.abs(s["zoom"]) + ROLL_W * np.abs(s["roll"])
    return np.where(s["inliers"] >= MIN_INLIERS, sp, 0.0)


def summarize(s: dict, cut_times, duration: float) -> dict:
    """Result fields from a FlowPass.series(): camera_motion (4 Hz), camera_events, subject_curve (10 Hz)."""
    t = s["t"]
    rate = float(s["rate"])
    duration = float(duration)
    ok = _valid_mask(t, rate, cut_times)
    pan_x, pan_y = _fill(s["pan_x"], ok), _fill(s["pan_y"], ok)
    zoom, roll = _fill(s["zoom"], ok), _fill(s["roll"], ok)
    inl = _fill(s["inliers"], ok)
    subj = _fill(s["subject"], ok)
    # light 3-pair median: one bad Farneback pair must not read as a whip
    if len(t) >= 3:
        pan_x, pan_y, zoom, roll = (median_filter(v, size=3, mode="nearest") for v in (pan_x, pan_y, zoom, roll))
    ss = {"pan_x": pan_x, "pan_y": pan_y, "zoom": zoom, "roll": roll, "inliers": inl}
    speed = camera_speed(ss)
    pan = np.where(inl >= MIN_INLIERS, np.hypot(pan_x, pan_y), 0.0)

    c_speed = _resample(t, speed, CAM_HZ, duration)
    c_pan = _resample(t, pan, CAM_HZ, duration)
    c_px = _resample(t, pan_x, CAM_HZ, duration)
    c_py = _resample(t, pan_y, CAM_HZ, duration)
    c_zoom = _resample(t, np.where(inl >= MIN_INLIERS, zoom, 0.0), CAM_HZ, duration)
    curve = c_speed / (c_speed + CAM_REF)

    events = camera_events(t, pan_x, pan_y, zoom, inl, ok, rate)
    n4 = len(curve)
    f_pan, f_zoom, f_whip = np.zeros(n4, int), np.zeros(n4, int), np.zeros(n4, int)
    for e in events:
        a, b = int(np.floor(e["start"] * CAM_HZ)), int(np.ceil(e["end"] * CAM_HZ))
        arr = f_whip if e["kind"] == "whip" else f_zoom if e["kind"] == "zoom" else f_pan
        arr[max(0, a):min(n4, max(b, a + 1))] = 1

    subject10 = _resample(t, subj, curves.HZ, duration)
    return {
        "camera_motion": {
            "hz": CAM_HZ,
            "curve": [round(float(v), 3) for v in curve],
            "pan": f_pan.tolist(), "zoom": f_zoom.tolist(), "whip": f_whip.tolist(),
            "pan_x": [round(float(v), 3) for v in c_px],
            "pan_y": [round(float(v), 3) for v in c_py],
            "zoom_rate": [round(float(v), 3) for v in c_zoom],
            "speed": [round(float(v), 3) for v in c_speed],
            "pan_speed": [round(float(v), 3) for v in c_pan],
        },
        "camera_events": events,
        "subject_curve": [round(float(v), 4) for v in subject10],
        "subject_hz": curves.HZ,
        "flow_hz": round(rate, 3),
    }


def camera_events(t, pan_x, pan_y, zoom, inl, ok, rate):
    """Contiguous camera moves: whip (short fast burst), pan/tilt (sustained translation), zoom."""
    good = (inl >= MIN_INLIERS) & ok
    pan = np.hypot(pan_x, pan_y)
    out = []
    for kind, mask in (("whip", good & (pan >= WHIP_MIN)),
                       ("pan", good & (pan >= PAN_MIN) & (pan < WHIP_MIN)),
                       ("zoom", good & (np.abs(zoom) >= ZOOM_MIN))):
        # bridge single-pair gaps
        m = mask.copy()
        for i in range(1, len(m) - 1):
            if not m[i] and m[i - 1] and m[i + 1]:
                m[i] = True
        for a, b in _runs(m):
            start, end = float(t[a] - 1.0 / rate), float(t[b - 1])
            dur = end - start
            if kind == "whip" and dur > WHIP_MAX_S:
                kind_e = "pan"          # a long fast move is a fast pan, not a whip
            else:
                kind_e = kind
            if kind != "whip" and dur < EVENT_MIN_S:
                continue
            seg = slice(a, b)
            if kind == "zoom":
                sp = np.abs(zoom[seg])
                direction = "in" if float(np.mean(zoom[seg])) > 0 else "out"
            else:
                sp = pan[seg]
                mx, my = float(np.mean(pan_x[seg])), float(np.mean(pan_y[seg]))
                direction = ("right" if mx > 0 else "left") if abs(mx) >= abs(my) else ("down" if my > 0 else "up")
                if kind_e == "pan" and abs(my) > abs(mx):
                    kind_e = "tilt"
            mean_sp = float(np.mean(sp))
            # smoothness: 1 - normalized jerk (std of the speed change vs the mean speed)
            jerk = float(np.std(np.diff(sp))) if len(sp) > 2 else 0.0
            smooth = float(np.clip(1.0 - jerk / (mean_sp + 1e-6), 0.0, 1.0))
            out.append({"start": round(start, 3), "end": round(end, 3), "kind": kind_e,
                        "speed": round(mean_sp, 3), "peak": round(float(np.max(sp)), 3),
                        "smooth": round(smooth, 3), "direction": direction})
    out.sort(key=lambda e: (e["start"], e["kind"]))
    return out
