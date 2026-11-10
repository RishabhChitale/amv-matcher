"""FCPXML <timeMap> for speed ramps (prototype, NOT TESTED IN RESOLVE; no Resolve on this Mac).

Turns rufflet's speed ramps (speedramp.py: points [{t_src, speed, ease_in_s, ease_out_s}] or a sampled warp
[[src_s, out_s], ...]) into a list of <timept> dicts + the XML for one <timeMap>.

FCPXML timeMap (Apple DTD, FCPXML v1.8): <timeMap frameSampling preservesPitch> holds <timept time value
interp inTime outTime>. `time` = position on the clip's own (output) timeline, `value` = position in the
source media. interp is smooth2 (default) | linear | smooth (deprecated). inTime/outTime are the ease widths
at a point and are only used with the smooth interps.

Two modes:
  smooth   one timept per speed point at (out_time(t_src) -> t_src), interp=smooth2, inTime/outTime =
           the point's ease widths in OUTPUT seconds. Between points the slope is the plateau speed.
           A warp with no points is sampled every `knot_s` output seconds (an approximation).
  stepped  fallback: the warp is sampled on an output grid of `step_frames` frames, interp=linear, so the
           clip plays at constant speed for each step (continuous, speed jumps between steps).
           `stepped_clips` gives the same thing as separate clips for resolve.video_clips-style export.

The map is anchored at the clip start: first timept is 0s -> 0s, so the asset-clip should carry
start="0s". A 21.1 import report (ReCut PR 83, see docs below) says Resolve lost the source offset when the
timeMap held absolute source times with a non-zero start.

Audio: Resolve cannot ramp audio smoothly through FCPXML, so bake the stretched song to a WAV
(temporamp.stretch_warped, as resolve.prepare_audio does for the constant tempo ratio) and reference it
at speed 1.

Nothing here edits resolve.py; import snap_fps/frame_dur/tstr/to_frames from it."""
from __future__ import annotations

from typing import Optional
from xml.etree import ElementTree as ET

import numpy as np

from .resolve import frame_dur, snap_fps, to_frames, tstr

INTERPS = ("smooth2", "linear", "smooth")          # FCPXML DTD v1.8 enumeration (smooth deprecated)
SAMPLING = ("floor", "nearest-neighbor", "frame-blending", "optical-flow-classic", "optical-flow")
MODES = ("smooth", "stepped")


def _as_warp(points_or_warp, vd: Optional[float], ease: str):
    """-> (warp ndarray [[src, out]], points list or None)."""
    if points_or_warp is None or len(points_or_warp) == 0:
        raise ValueError("empty speed ramp")
    first = points_or_warp[0]
    if isinstance(first, dict):
        from . import speedramp as SR
        if vd is None:
            raise ValueError("video duration is required for speed points")
        w, _ = SR.warp_from_points(points_or_warp, float(vd), ease)
        return np.asarray(w, float), list(points_or_warp)
    w = np.asarray(points_or_warp, float)
    if w.ndim != 2 or w.shape[1] != 2 or len(w) < 2:
        raise ValueError("warp must be [[src_s, out_s], ...] with at least 2 rows")
    if np.any(np.diff(w[:, 0]) < 0) or np.any(np.diff(w[:, 1]) < 0):
        raise ValueError("warp must be non-decreasing")
    return w, None


def _out_at(warp, src_s):
    return float(np.interp(src_s, warp[:, 0], warp[:, 1]))


def _src_at(warp, out_s):
    return float(np.interp(out_s, warp[:, 1], warp[:, 0]))


def _tp(out_f: int, src_f: int, fd, interp: str, in_f: int = 0, out_ease_f: int = 0) -> dict:
    d = {"time": tstr(out_f, fd), "value": tstr(src_f, fd), "interp": interp, "_t": out_f, "_v": src_f}
    if interp != "linear":
        if in_f > 0:
            d["inTime"] = tstr(in_f, fd)
        if out_ease_f > 0:
            d["outTime"] = tstr(out_ease_f, fd)
    return d


def _dedupe(tps: list) -> list:
    """Strictly increasing output frames; later point wins on a collision (frame grid is coarse)."""
    out = []
    for t in tps:
        if out and t["_t"] <= out[-1]["_t"]:
            out[-1] = t if t["_t"] == out[-1]["_t"] else out[-1]
            continue
        out.append(t)
    return out


def build_timemap(points_or_warp, fps, mode: str = "smooth", *, vd: Optional[float] = None,
                  ease: str = "smoothstep", step_frames: int = 6, knot_s: float = 1.0) -> list:
    """List of timept dicts (keys time, value, interp, optional inTime/outTime; `_t`/`_v` hold the frame ints).

    points_or_warp: speedramp points (needs `vd`, source duration s) or a warp [[src_s, out_s], ...]."""
    if mode not in MODES:
        raise ValueError("mode must be one of %s" % (MODES,))
    warp, points = _as_warp(points_or_warp, vd, ease)
    fd = frame_dur(fps)
    f = float(snap_fps(fps))
    src_end = float(warp[-1, 0])
    out_end = float(warp[-1, 1])
    n_out = max(1, to_frames(out_end, fps))
    tps = [_tp(0, 0, fd, "linear" if mode == "stepped" else "smooth2")]

    if mode == "stepped":
        step = max(1, int(step_frames))
        for of in list(range(step, n_out, step)) + [n_out]:
            sf = to_frames(_src_at(warp, of / f), fps)
            tps.append(_tp(of, sf, fd, "linear"))
        return _dedupe(tps)

    if points is not None:                                    # one timept per speed point
        prev = float(points[0]["speed"])
        for p in points[1:]:
            ts = float(p["t_src"])
            sp = float(p["speed"])
            of = to_frames(_out_at(warp, ts), fps)
            ein = to_frames(float(p.get("ease_in_s", 0.0)) / max(0.5 * (prev + sp), 1e-6), fps)
            eout = to_frames(float(p.get("ease_out_s", 0.0)) / max(0.5 * (prev + sp), 1e-6), fps)
            tps.append(_tp(of, to_frames(ts, fps), fd, "smooth2", ein, eout))
            prev = sp
    else:                                                     # sampled warp: knots every knot_s output s
        k = max(1, to_frames(knot_s, fps))
        for of in range(k, n_out, k):
            tps.append(_tp(of, to_frames(_src_at(warp, of / f), fps), fd, "smooth2"))
    tps.append(_tp(n_out, to_frames(src_end, fps), fd, "smooth2"))
    return _dedupe(tps)


def stepped_clips(points_or_warp, fps, *, vd: Optional[float] = None, ease: str = "smoothstep",
                  step_frames: int = 6) -> list:
    """Stepped mode as separate constant-speed clips [{src_f, dst_f, dur_f, speed}], the same shape as
    resolve.video_clips (speed = source frames per output frame)."""
    tps = build_timemap(points_or_warp, fps, "stepped", vd=vd, ease=ease, step_frames=step_frames)
    clips = []
    for a, b in zip(tps, tps[1:]):
        dur = b["_t"] - a["_t"]
        if dur <= 0:
            continue
        clips.append({"src_f": a["_v"], "dst_f": a["_t"], "dur_f": dur, "speed": (b["_v"] - a["_v"]) / dur})
    return clips


def timemap_xml(timepts: list, *, preserves_pitch: bool = False, frame_sampling: str = "floor") -> str:
    """<timeMap> element text for the timept list (private `_t`/`_v` keys are not written)."""
    if frame_sampling not in SAMPLING:
        raise ValueError("frame_sampling must be one of %s" % (SAMPLING,))
    rows = []
    for t in timepts:
        a = ['time="%s"' % t["time"], 'value="%s"' % t["value"], 'interp="%s"' % t["interp"]]
        for k in ("inTime", "outTime"):
            if k in t:
                a.append('%s="%s"' % (k, t[k]))
        rows.append("<timept %s/>" % " ".join(a))
    return '<timeMap frameSampling="%s" preservesPitch="%d">%s</timeMap>' % (
        frame_sampling, 1 if preserves_pitch else 0, "".join(rows))


def validate_timemap(xml_or_timepts) -> list:
    """Check against the DTD v1.8 declarations (timeMap/timept attlists) plus the anchoring and monotonic
    rules this module relies on. Returns [] if clean, else a list of problem strings."""
    xml = xml_or_timepts if isinstance(xml_or_timepts, str) else timemap_xml(xml_or_timepts)
    errs = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        return ["not well-formed XML: %s" % e]
    if root.tag != "timeMap":
        return ["root is not timeMap"]
    for k, v in root.attrib.items():
        if k == "frameSampling" and v not in SAMPLING or k == "preservesPitch" and v not in ("0", "1"):
            errs.append("bad timeMap %s=%r" % (k, v))
        if k not in ("frameSampling", "preservesPitch"):
            errs.append("unknown timeMap attribute %s" % k)
    pts = list(root)
    if not pts:
        errs.append("timeMap has no timept")
    prev_t = prev_v = None
    for i, e in enumerate(pts):
        if e.tag != "timept":
            errs.append("child %d is %s, not timept" % (i, e.tag))
            continue
        for k in e.attrib:
            if k not in ("time", "value", "interp", "inTime", "outTime"):
                errs.append("timept %d: unknown attribute %s" % (i, k))
        for k in ("time", "value"):
            if k not in e.attrib:
                errs.append("timept %d: missing %s" % (i, k))
        interp = e.get("interp", "smooth2")
        if interp not in INTERPS:
            errs.append("timept %d: bad interp %r" % (i, interp))
        if interp == "linear" and ("inTime" in e.attrib or "outTime" in e.attrib):
            errs.append("timept %d: inTime/outTime are only used with smooth interps" % i)
        try:
            t, v = (_frac(e.get("time")), _frac(e.get("value")))
        except Exception:
            errs.append("timept %d: unparseable time/value" % i)
            continue
        if i == 0 and (t != 0 or v != 0):
            errs.append("first timept must be 0s -> 0s (clip-start anchored)")
        if prev_t is not None and t <= prev_t:
            errs.append("timept %d: time not strictly increasing" % i)
        if prev_v is not None and v < prev_v:
            errs.append("timept %d: value goes backwards (reverse playback is not produced)" % i)
        prev_t, prev_v = t, v
    return errs


def _frac(s: str):
    from fractions import Fraction
    s = s.strip()
    if not s.endswith("s"):
        raise ValueError(s)
    return Fraction(s[:-1])
