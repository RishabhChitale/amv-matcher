"""DaVinci Resolve export bundle (FCPXML 1.9 + AMV Pump Fusion macro + audio + how-to).

NOT TESTED IN RESOLVE (none available here); only structurally validated (see tests/test_resolve.py).

Bundle folder `<video-stem>__<song-stem>_resolve/`:
  timeline.fcpxml   original video as N asset-clips (one per constant-speed stretch of the retime
                    warp; speed via <timeMap>), song as audio clips (one per splice segment, on
                    alternating lanes so neighbours overlap by a short crossfade), markers
                    'beat' / 'DROP' / 'CUT' (FCPXML has no marker colour, so names only)
  AMV_Pump.drfx     Fusion macro (installable template)
  AMV_Pump_plan.setting   the same macro with pump keyframes baked for THIS plan
  audio/            audio the timeline references (relative paths)
  HOW-TO-IMPORT.txt

FCPXML sources: Apple "FCPXML Reference" (asset-clip, timeMap/timept, marker, adjust-volume/fadeIn);
version 1.9 is what Resolve writes on export. Whether free Resolve 20/21 imports timeMap speeds,
fades or lane overlaps exactly as intended is UNVERIFIED.
"""
from __future__ import annotations

import json
import shutil
import zipfile
from fractions import Fraction
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import quoteattr
from xml.etree import ElementTree as ET

import numpy as np

from . import media
from .pump import pump_keyframes

XFADE_S = 0.04           # crossfade between spliced audio clips
MAX_BEAT_MARKERS_PER_BAR = 1
DROP_MATCH_S = 0.15
_FPS_TABLE = {Fraction(24000, 1001): (1001, 24000), Fraction(30000, 1001): (1001, 30000),
              Fraction(60000, 1001): (1001, 60000)}

# U11c speed-ramp export. Resolve import of <timeMap> is untested (no Resolve on the dev Mac), so two
# conventions for where the source offset lives are kept behind RAMP_ANCHOR:
#   "absolute-values": asset-clip start="0s", timept values are absolute source times
#   "relative-values": asset-clip start=<first source frame>, timept values count from 0
# They are identical while the ramp starts at source 0 (always true for speedramp plans).
RAMP_ANCHOR = "absolute-values"
RAMP_MODE_DEFAULT = "stepped"     # "smooth" = smooth2 timepts with ease times (opt-in)

SETTING_DIR_MAC = "~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Templates/Edit/Effects/"
SETTING_DIR_WIN = r"%AppData%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Templates\Edit\Effects" + "\\"


def setting_dir(platform=None) -> str:
    """Resolve effects-template dir for the platform; %AppData% / ~ are expanded at use time (W-19)."""
    import os
    import sys
    if (platform or sys.platform) == "win32":
        return os.path.expandvars(SETTING_DIR_WIN)
    return os.path.expanduser(SETTING_DIR_MAC)


# ------------------------------------------------------------------ time helpers
def snap_fps(fps) -> Fraction:
    f = float(fps)
    for n in (24000, 30000, 60000):
        if abs(f - n / 1001) < 0.005:
            return Fraction(n, 1001)
    return Fraction(f).limit_denominator(1001)


def frame_dur(fps) -> tuple:
    fr = snap_fps(fps)
    return _FPS_TABLE.get(fr, (fr.denominator, fr.numerator))


def tstr(frames: int, fd) -> str:
    return "0s" if frames == 0 else "%d/%ds" % (frames * fd[0], fd[1])


def to_frames(sec, fps) -> int:
    return int(round(float(sec) * float(snap_fps(fps))))


def _tsec(sec: float, fd, fps) -> str:
    """Seconds -> rational string on the frame grid."""
    return tstr(to_frames(sec, fps), fd)


# ------------------------------------------------------------------ plan -> clips
def video_clips(plan: dict, video_duration: float, fps) -> list:
    """Constant-speed video clips [{src_f, dst_f, dur_f, speed}] in frames at `fps`.
    No retime -> one clip at speed 1. Output frames tile [0, out_frames) exactly."""
    rt = (plan or {}).get("retime")
    warp = rt.get("warp") if rt else None
    if not warp or len(warp) < 2:
        n = to_frames(video_duration, fps)
        return [{"src_f": 0, "dst_f": 0, "dur_f": n, "speed": 1.0}]
    w = [(float(a), float(b)) for a, b in warp]
    groups = []   # merged (v0, o0, v1, o1, speed)
    for (v0, o0), (v1, o1) in zip(w, w[1:]):
        if o1 - o0 <= 1e-9 or v1 - v0 <= 1e-9:
            continue
        sp = (v1 - v0) / (o1 - o0)
        if groups and abs(groups[-1][4] - sp) < 1e-4 and abs(groups[-1][2] - v0) < 1e-6:
            g = groups[-1]
            groups[-1] = (g[0], g[1], v1, o1, (v1 - g[0]) / (o1 - g[1]))
        else:
            groups.append((v0, o0, v1, o1, sp))
    clips, prev_end = [], 0
    for i, (v0, o0, v1, o1, sp) in enumerate(groups):
        d0 = prev_end if i else to_frames(o0, fps)
        d1 = to_frames(o1, fps)
        if d1 <= d0:
            continue
        clips.append({"src_f": to_frames(v0, fps), "dst_f": d0, "dur_f": d1 - d0,
                      "speed": 1.0 if abs(sp - 1.0) < 1e-4 else round(sp, 6)})
        prev_end = d1
    return clips or [{"src_f": 0, "dst_f": 0, "dur_f": to_frames(video_duration, fps), "speed": 1.0}]


def ramp_plan(plan: dict):
    """(plan', ramp retime dict) when the plan carries a speed ramp (retime_ramps, else retime, with
    >= 2 `points` and a `warp`); plan' has retime replaced by it. Otherwise (plan, None)."""
    plan = plan or {}
    for k in ("retime_ramps", "retime"):
        rt = plan.get(k)
        if rt and len(rt.get("points") or []) >= 2 and len(rt.get("warp") or []) >= 2:
            return dict(plan, retime=rt), rt
    return plan, None


def tempo_ramp_warp(plan: dict):
    """Song tempo-ramp warp [[song_s, out_s]...] from plan['tempo_ramp'], or None."""
    tr = (plan or {}).get("tempo_ramp")
    w = tr.get("warp") if isinstance(tr, dict) else None
    return w if w and len(w) >= 2 else None


def ramp_timemap(rt: dict, video_duration: float, fps, mode=None) -> dict:
    """{xml, pts, n_out} for one clip carrying the whole ramp (see RAMP_ANCHOR)."""
    from . import resolve_ramps as RR
    mode = mode or rt.get("resolve_mode") or RAMP_MODE_DEFAULT
    src = rt["points"] if mode == "smooth" else rt["warp"]
    pts = RR.build_timemap(src, fps, mode, vd=float(video_duration))
    s0 = pts[0]["_v"]     # 0 for speedramp plans, so both RAMP_ANCHOR conventions emit the same XML
    return {"xml": RR.timemap_xml(pts), "pts": pts, "n_out": pts[-1]["_t"], "s0": s0}


def out_duration_of(plan: dict, video_duration: float) -> float:
    rt = (plan or {}).get("retime")
    if rt and rt.get("warp"):
        return float(rt["warp"][-1][1])
    return float(rt["out_duration"]) if rt and rt.get("out_duration") else float(video_duration)


def audio_pieces(plan: dict, out_duration: float) -> list:
    """Audio clips [{src_start, dst_start, dur}] in seconds; src is song-as-heard (tempo applied)
    time. Lead silence (negative src_start / negative offset) becomes a later timeline position."""
    segs = (plan or {}).get("segments")
    out = []
    if segs:
        for s in sorted(segs, key=lambda s: s["dst_start"]):
            a, b, d = float(s["src_start"]), float(s["src_end"]), float(s["dst_start"])
            if a < 0:
                d, a = d - a, 0.0
            if d >= out_duration or b <= a:
                continue
            dur = min(b - a, out_duration - d)
            if dur > 1e-6:
                out.append({"src_start": a, "dst_start": d, "dur": dur})
        return out
    off = float((plan or {}).get("offset") or 0.0)
    lead = max(0.0, -off)
    a = max(0.0, off)
    return [{"src_start": a, "dst_start": lead, "dur": max(0.0, out_duration - lead)}]


# ------------------------------------------------------------------ markers
def build_markers(song_heard: dict, plan: dict, video: dict, clips: list, out_duration: float, fps) -> list:
    """[(frame, name, completed)] sorted. Times are OUTPUT-timeline frames.
    beat: <= 1 per bar (every 4th beat from the first beat after the first drop-free start, or
    every beat if that is already <= 1 per bar), DROP at drops, CUT at the video's cut_times
    mapped through the retime warp."""
    segs = audio_pieces(plan, out_duration)

    def song_to_out(t):   # first piece containing song time t
        for p in segs:
            if p["src_start"] - 1e-9 <= t <= p["src_start"] + p["dur"] + 1e-9:
                return p["dst_start"] + (t - p["src_start"])
        return None

    nf = sum(c["dur_f"] for c in clips)
    beats = [float(b) for b in (song_heard or {}).get("beat_times", [])]
    drops = [float(d) for d in (song_heard or {}).get("drops", [])]
    ev = {}
    for i, b in enumerate(beats):
        if i % 4:      # one marker per bar (4/4 assumed)
            continue
        if any(abs(b - d) <= DROP_MATCH_S for d in drops):
            continue
        t = song_to_out(b)
        if t is not None:
            ev.setdefault(to_frames(t, fps), "beat")
    for d in drops:
        t = song_to_out(d)
        if t is not None:
            ev[to_frames(t, fps)] = "DROP"
    cuts = [float(c) for c in (video or {}).get("cut_times", [])]
    warp = ((plan or {}).get("retime") or {}).get("warp")
    if cuts:
        if warp:
            from .retime import _wmap
            cuts = _wmap(warp, cuts).tolist()
        for t in cuts:
            f = to_frames(t, fps)
            if ev.get(f) != "DROP":
                ev[f] = "CUT"
    return [(f, n) for f, n in sorted(ev.items()) if 0 <= f < nf]


# ------------------------------------------------------------------ FCPXML
def build_fcpxml(video_path: Path, video_duration: float, fps, width: int, height: int,
                 plan: dict, song_heard: dict, video: dict, audio_rel: str, audio_dur: float,
                 name: str = "AMV Matcher", ramp_mode: Optional[str] = None) -> str:
    """`video_path` is the ORIGINAL video (absolute file URI); `audio_rel` is the bundle-relative
    path of the wav the audio clips read (song-as-heard)."""
    q = quoteattr
    fr = snap_fps(fps)
    fd = frame_dur(fr)
    plan, ramp_rt = ramp_plan(plan)
    clips = video_clips(plan, video_duration, fr)
    tm = None
    if ramp_rt:
        tm = ramp_timemap(ramp_rt, video_duration, fr, ramp_mode)
        clips = [{"src_f": 0, "dst_f": 0, "dur_f": tm["n_out"], "speed": 1.0}]
    nf = sum(c["dur_f"] for c in clips)
    out_dur = nf / float(fr)
    vf = to_frames(video_duration, fr)
    pieces = audio_pieces(plan, out_dur)
    marks = build_markers(song_heard, plan, video, clips, out_dur, fr)
    uri = Path(video_path).resolve().as_uri()
    vname = Path(video_path).stem
    aname = Path(audio_rel).stem
    xf = to_frames(XFADE_S, fr)

    def mk_xml(c):
        """markers inside a clip use the clip's source timebase: start + local*speed."""
        o = []
        for f, n in marks:
            if c["dst_f"] <= f < c["dst_f"] + c["dur_f"]:
                if tm:
                    st = int(round(np.interp(f, [t["_t"] for t in tm["pts"]], [t["_v"] for t in tm["pts"]])))
                else:
                    st = c["src_f"] + int(round((f - c["dst_f"]) * c["speed"]))
                done = ' completed="0"' if n == "DROP" else ""
                o.append('<marker start="%s" duration="%s" value="%s"%s/>' % (tstr(st, fd), tstr(1, fd), n, done))
        return o

    parts = []
    for i, c in enumerate(clips):
        sp = c["speed"]
        attrs = 'ref="r2" offset="%s" name=%s start="%s" duration="%s" format="r1" tcFormat="NDF"' % (
            tstr(c["dst_f"], fd), q(vname),
            tstr(tm["s0"] if tm and RAMP_ANCHOR == "relative-values" else c["src_f"], fd), tstr(c["dur_f"], fd))
        inner = []
        if tm:
            inner.append(tm["xml"])
        elif abs(sp - 1.0) > 1e-9:
            src_end = c["src_f"] + int(round(c["dur_f"] * sp))
            inner.append('<timeMap><timept time="0s" value="%s" interp="linear"/>'
                         '<timept time="%s" value="%s" interp="linear"/></timeMap>'
                         % (tstr(c["src_f"], fd), tstr(c["dur_f"], fd), tstr(src_end, fd)))
        inner += mk_xml(c)
        # connected audio clips attach to the video clip whose span holds their start
        for j, p in enumerate(pieces):
            d_f = to_frames(p["dst_start"], fr)
            lastclip = i == len(clips) - 1
            if not (c["dst_f"] <= d_f < c["dst_f"] + c["dur_f"] or (j == 0 and i == 0 and d_f < 0)
                    or (lastclip and d_f >= c["dst_f"] + c["dur_f"])):
                continue
            s_f = to_frames(p["src_start"], fr)
            dur_f = max(1, min(to_frames(p["dur"], fr), nf - d_f))
            lane = -1 if j % 2 == 0 else -2
            # offset of a connected clip is in the parent's source timebase
            local = int(round((d_f - c["dst_f"]) * sp))
            fades = ""
            if len(pieces) > 1 and xf > 0:
                fi = '<fadeIn type="easeIn2" duration="%s"/>' % tstr(xf, fd) if j > 0 else ""
                fo = '<fadeOut type="easeOut2" duration="%s"/>' % tstr(xf, fd) if j < len(pieces) - 1 else ""
                if fi or fo:
                    fades = '<adjust-volume><param name="amount">%s%s</param></adjust-volume>' % (fi, fo)
            inner.append('<asset-clip ref="r3" lane="%d" offset="%s" name=%s start="%s" duration="%s" audioRole="music">%s</asset-clip>'
                         % (lane, tstr(c["src_f"] + local, fd), q("%s %d" % (aname, j + 1)), tstr(s_f, fd),
                            tstr(dur_f, fd), fades))
        parts.append("            <asset-clip %s>\n              %s\n            </asset-clip>" % (attrs, "\n              ".join(inner)))
    aud_f = to_frames(audio_dur, fr)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.9">
  <resources>
    <format id="r1" name="FFVideoFormat{height}p{int(round(float(fr)))}" frameDuration="{fd[0]}/{fd[1]}s" width="{width}" height="{height}"/>
    <asset id="r2" name={q(vname)} start="0s" duration="{tstr(vf, fd)}" hasVideo="1" format="r1" hasAudio="0">
      <media-rep kind="original-media" src={q(uri)}/>
    </asset>
    <asset id="r3" name={q(aname)} start="0s" duration="{tstr(aud_f, fd)}" hasAudio="1" audioSources="1" audioChannels="2" audioRate="48000">
      <media-rep kind="original-media" src={q(audio_rel)}/>
    </asset>
  </resources>
  <library>
    <event name={q(name)}>
      <project name={q(name)}>
        <sequence format="r1" duration="{tstr(nf, fd)}" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">
          <spine>
{chr(10).join(parts)}
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
'''


# ------------------------------------------------------------------ audio
def _tempo_wav(song_path, ratio: float, out: Path) -> None:
    """Whole song at `ratio` x speed (PyAV atempo, chained outside 0.5..2) -> float wav."""
    import av
    import soundfile as sf
    SR = media.SR
    g = av.filter.Graph()
    src = g.add_abuffer(sample_rate=SR, format="fltp", layout="stereo", time_base=Fraction(1, SR))
    chain, r = [], ratio
    while r > 2.0 + 1e-9:
        chain.append(2.0)
        r /= 2.0
    while r < 0.5 - 1e-9:
        chain.append(0.5)
        r /= 0.5
    chain.append(r)
    prev = src
    for v in chain:
        n = g.add("atempo", f"{v:.6f}")
        prev.link_to(n)
        prev = n
    sink = g.add("abuffersink")
    prev.link_to(sink)
    g.configure()
    rs = av.AudioResampler(format="fltp", layout="stereo", rate=SR)
    rs_out = av.AudioResampler(format="fltp", layout="stereo", rate=SR)
    chunks = []

    def drain():
        while True:
            try:
                fr = sink.pull()
            except (av.error.BlockingIOError, av.error.EOFError):
                return
            for o in rs_out.resample(fr):
                chunks.append(o.to_ndarray())

    with av.open(str(song_path)) as c:
        st = c.streams.audio[0]
        pts = 0
        for fr in c.decode(st):
            for r_ in rs.resample(fr):
                r_.pts, r_.time_base = pts, Fraction(1, SR)
                pts += r_.samples
                src.push(r_)
                drain()
    src.push(None)
    drain()
    y = np.concatenate(chunks, axis=1) if chunks else np.zeros((2, 0), np.float32)
    sf.write(str(out), y.T, SR, subtype="FLOAT")


def prepare_audio(song_path: Path, ratio: float, audio_dir: Path) -> tuple:
    """Write the song-as-heard into audio_dir. Returns (filename, duration_s).
    ratio==1: plain copy of the song file; else a tempo-stretched wav."""
    import soundfile as sf
    audio_dir.mkdir(parents=True, exist_ok=True)
    if abs(ratio - 1.0) < 1e-6:
        dst = audio_dir / Path(song_path).name
        shutil.copyfile(song_path, dst)
        try:
            dur = float(sf.info(str(dst)).duration)
        except Exception:
            import librosa
            dur = float(librosa.get_duration(path=str(dst)))
        return dst.name, dur
    dst = audio_dir / (Path(song_path).stem + "_tempo.wav")
    _tempo_wav(song_path, ratio, dst)
    return dst.name, float(sf.info(str(dst)).duration)


def prepare_audio_ramp(song_path: Path, warp, audio_dir: Path) -> tuple:
    """Tempo-ramped song baked to a stereo wav at speed 1 (temporamp.stretch_warped per channel).
    Returns (filename, duration_s)."""
    import soundfile as sf
    from .temporamp import stretch_warped
    audio_dir.mkdir(parents=True, exist_ok=True)
    y, sr = sf.read(str(song_path), always_2d=True, dtype="float32")
    chans = [stretch_warped(y[:, i], sr, warp) for i in range(min(2, y.shape[1]))]
    if len(chans) == 1:
        chans = chans * 2
    dst = audio_dir / (Path(song_path).stem + "_tempoR.wav")
    sf.write(str(dst), np.stack(chans, axis=1), sr, subtype="FLOAT")
    return dst.name, float(sf.info(str(dst)).duration)


# ------------------------------------------------------------------ pump .setting / .drfx
def build_pump_setting(song_heard: dict, plan: dict, out_dur: float, fps, width, height,
                       boom=40, decay=180, drops_only=False, shake=False) -> str:
    """Pump keyframes baked for this plan (output-timeline time, window start 0). The song used
    for event times is the song-as-heard re-timed through the plan's audio pieces."""
    pieces = audio_pieces(plan, out_dur)
    # pump_keyframes reads beat_times/drops/duration from a dict: remap them onto the output timeline
    def remap(ts):
        res = []
        for t in ts:
            for p in pieces:
                if p["src_start"] - 1e-9 <= t <= p["src_start"] + p["dur"] + 1e-9:
                    res.append(p["dst_start"] + (t - p["src_start"]))
                    break
        return res
    song_out = dict(song_heard)
    song_out["beat_times"] = remap(song_heard.get("beat_times", []))
    song_out["drops"] = remap(song_heard.get("drops", []))
    song_out["duration"] = out_dur
    kf = pump_keyframes(song_out, boom, decay, drops_only, float(snap_fps(fps)), duration=out_dur,
                        shake=shake, start=0.0)
    return _build_setting(kf, float(snap_fps(fps)), width, height, decay)


def _build_setting(keyframes, fps, width, height, decay_ms) -> str:
    fr = [int(round(k["t"] * fps)) for k in keyframes]
    curves = [("AMVPumpScale", (255, 80, 80), "scale"), ("AMVPumpBright", (255, 220, 80), "brightness"),
              ("AMVPumpDx", (80, 200, 255), "dx"), ("AMVPumpDy", (120, 255, 120), "dy")]
    splines = ""
    for n, c, key in curves:
        keys = "".join("\t\t\t\t\t\t[%d] = { %s, Flags = { Linear = true } },\n" % (f, repr(round(k[key], 4)))
                       for f, k in zip(fr, keyframes))
        splines += _SPLINE % dict(name=n, r=c[0], g=c[1], b=c[2], keys=keys)
    return _TEMPLATE % dict(splines=splines, w=float(width), h=float(height), decay=decay_ms)


_SPLINE = """				%(name)s = BezierSpline {
					SplineColor = { Red = %(r)d, Green = %(g)d, Blue = %(b)d },
					KeyFrames = {
%(keys)s					}
				},
"""

_TEMPLATE = """{
	Tools = ordered() {
		AMVPump = MacroOperator {
			Inputs = ordered() {
				MainInput1 = InstanceInput { SourceOp = "AMVPumpXf", Source = "Input", },
				Boom = InstanceInput { SourceOp = "AMVPumpCtrl", Source = "Boom", Default = 1, },
				Decay = InstanceInput { SourceOp = "AMVPumpCtrl", Source = "Decay", Default = %(decay)s, },
				Shake = InstanceInput { SourceOp = "AMVPumpCtrl", Source = "Shake", Default = 1, }
			},
			Outputs = {
				MainOutput1 = InstanceOutput { SourceOp = "AMVPumpBC", Source = "Output", }
			},
			ViewInfo = GroupInfo { Pos = { 0, 0 } },
			Tools = ordered() {
				AMVPumpCtrl = Custom {
					Inputs = {
						NumberIn1 = Input { SourceOp = "AMVPumpScale", Source = "Value", },
						NumberIn2 = Input { SourceOp = "AMVPumpBright", Source = "Value", },
						NumberIn3 = Input { SourceOp = "AMVPumpDx", Source = "Value", },
						NumberIn4 = Input { SourceOp = "AMVPumpDy", Source = "Value", },
					},
					ViewInfo = OperatorInfo { Pos = { 0, -60 } },
					UserControls = ordered() {
						Boom = { LINKS_Name = "Boom", LINKID_DataType = "Number", INPID_InputControl = "SliderControl", INP_Default = 1, INP_MinScale = 0, INP_MaxScale = 3, },
						Decay = { LINKS_Name = "Decay (ms, baked - regenerate to change)", LINKID_DataType = "Number", INPID_InputControl = "SliderControl", INP_Default = %(decay)s, INP_MinScale = 20, INP_MaxScale = 1000, },
						Shake = { LINKS_Name = "Shake", LINKID_DataType = "Number", INPID_InputControl = "SliderControl", INP_Default = 1, INP_MinScale = 0, INP_MaxScale = 3, }
					},
				},
				AMVPumpXf = Transform {
					Inputs = {
						Size = Input { Expression = "1 + (AMVPumpCtrl.NumberIn1 - 1) * AMVPumpCtrl.Boom", },
						Center = Input { Expression = "Point(0.5 + AMVPumpCtrl.NumberIn3 * AMVPumpCtrl.Shake / %(w)s, 0.5 - AMVPumpCtrl.NumberIn4 * AMVPumpCtrl.Shake / %(h)s)", },
					},
					ViewInfo = OperatorInfo { Pos = { 0, 0 } },
				},
				AMVPumpBC = BrightnessContrast {
					Inputs = {
						Gain = Input { Expression = "1 + (AMVPumpCtrl.NumberIn2 - 1) * AMVPumpCtrl.Boom", },
						Input = Input { SourceOp = "AMVPumpXf", Source = "Output", },
					},
					ViewInfo = OperatorInfo { Pos = { 0, 60 } },
				},
%(splines)s			},
		}
	},
	ActiveTool = "AMVPump"
}
"""


def build_drfx(setting_text: str, out: Path) -> Path:
    """Zip a .setting as Edit/Effects/AMV/AMV Pump.setting (Fusion template layout)."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Edit/Effects/AMV/AMV Pump.setting", setting_text)
    return out


HOWTO = """AMV Matcher -> DaVinci Resolve (free 20 / 21)          NOT TESTED IN RESOLVE by the app's author

Folder contents
  timeline.fcpxml         timeline: your ORIGINAL video as one clip per constant-speed section
                          (speed-changed sections can have Optical Flow switched on), the song as
                          audio clip(s), markers named beat / DROP / CUT (no colours possible)
  AMV_Pump.drfx           Fusion "AMV Pump" effect (generic: 3 sample keys)
  AMV_Pump_plan.setting   the same effect with pump keyframes baked for THIS timeline
  audio/                  the song as heard (tempo applied); the timeline reads it by relative path

1. Install the effect (do this with Resolve CLOSED)
   Windows: copy AMV_Pump_plan.setting into
              %AppData%\\Blackmagic Design\\DaVinci Resolve\\Support\\Fusion\\Templates\\Edit\\Effects\\
            (if that folder is missing: C:\\ProgramData\\Blackmagic Design\\DaVinci Resolve\\Fusion\\Templates\\Edit\\Effects\\)
   Mac:     copy AMV_Pump_plan.setting into
              ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Templates/Edit/Effects/
   (Make any missing folders. Alternative: double-click AMV_Pump.drfx. Best check afterwards:
    Effects Library > Templates > Edit > ... > Show Folder.)
2. Start Resolve, create/open a project, set it to the video's frame rate if asked.
3. File > Import > Timeline... and choose timeline.fcpxml. If asked, relink media: the video is the
   original file at its original location; the audio is audio/ in this folder.
4. Markers: beat (about one per bar), DROP, CUT (where the video's own cuts fall).
5. Effects Library > Effects (Edit) > AMV > "AMV Pump": drag onto the video clips (put it on each
   section clip, or make a Compound Clip of the video first). Adjust Boom / Shake on the Inspector.
   Decay is baked: regenerate the export to change it.
6. For smoother slow/fast sections: select a speed-changed clip, Inspector > Retime and Scaling >
   Retime Process = Optical Flow.

If something fails to import, send back: Resolve version, Free/Studio, the error text, a screenshot.
"""


# ------------------------------------------------------------------ orchestration
def _safe(s: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9._ ()\-]", "_", s).strip(" .")[:80] or "x"


def _write_lf(path: Path, text: str) -> None:
    """UTF-8 with LF newlines on every OS (write_text would emit CRLF on Windows), so bundle
    files are byte-identical across platforms."""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _dims(video_path: Path) -> tuple:
    import cv2
    cap = cv2.VideoCapture(str(video_path))
    try:
        w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1920), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1080)
    finally:
        cap.release()
    return (w or 1920), (h or 1080)


def export_bundle(video_path: Path, song_path: Path, plan: dict, song: dict, video: dict, out_root: Path,
                  boom: float = 40, decay: float = 180, drops_only: bool = False, shake: bool = False,
                  progress=None) -> dict:
    """Write the bundle folder under out_root and a zip next to it. `song`/`video` are the cached
    analysis dicts. Returns {folder, zip, clips, markers, out_duration}."""
    from .tempo import apply_tempo
    prog = progress or (lambda *a, **k: None)
    plan = plan or {}
    folder = Path(out_root) / f"{_safe(Path(video_path).stem)}__{_safe(Path(song_path).stem)}_resolve"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    fps = float(video.get("fps") or 30)
    width, height = _dims(video_path)
    ratio = float(plan.get("tempo_ratio") or 1.0)
    tw = tempo_ramp_warp(plan)
    if tw:
        from .temporamp import apply_tempo_warp
        heard = apply_tempo_warp(song, tw)
    else:
        heard = apply_tempo(song, ratio) if abs(ratio - 1.0) > 1e-9 else song
    prog("audio", 1)
    if tw:
        aname, adur = prepare_audio_ramp(Path(song_path), tw, folder / "audio")
    else:
        aname, adur = prepare_audio(Path(song_path), ratio, folder / "audio")
    plan_r, ramp_rt = ramp_plan(plan)
    if ramp_rt:
        out_dur = ramp_timemap(ramp_rt, float(video["duration"]), fps)["n_out"] / float(snap_fps(fps))
        clips = [None]
    else:
        clips = video_clips(plan, float(video["duration"]), fps)
        out_dur = sum(c["dur_f"] for c in clips) / float(snap_fps(fps))
    xml = build_fcpxml(video_path, float(video["duration"]), fps, width, height, plan, heard, video,
                       "audio/" + aname, adur)
    _write_lf(folder / "timeline.fcpxml", xml)
    prog("pump", 2)
    setting = build_pump_setting(heard, plan, out_dur, fps, width, height, boom, decay, drops_only, shake)
    _write_lf(folder / "AMV_Pump_plan.setting", setting)
    build_drfx(setting, folder / "AMV_Pump.drfx")
    _write_lf(folder / "HOW-TO-IMPORT.txt", HOWTO)
    prog("zip", 3)
    zpath = folder.parent / (folder.name + ".zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(folder.rglob("*")):
            if f.is_file():
                z.write(f, f"{folder.name}/{f.relative_to(folder).as_posix()}")
    root = ET.fromstring(xml)
    n_markers = len(root.findall(".//marker"))
    return {"folder": str(folder), "zip": str(zpath), "clips": len(clips), "markers": n_markers,
            "out_duration": round(out_dur, 4)}
