# decode.py integration (patch description; NOT applied)

`amv_matcher/decode.py` offers `open_frames(path, width, fps, start, end, hw, pix_fmt)` and `probe_video(path)`.
Measured on this Mac (M-series, VideoToolbox): hardware decode is **slower** than threaded software for
1080p60 H.264 when we downscale to 160/960 px (see below), so every call site below passes `hw="off"`.
`hw="auto"` is correct for other machines/codecs but should only be made the default after a re-benchmark
(tools/bench_decode.py). Software path: `thread_type=AUTO, thread_count=0`.

## 1. amv_matcher/video.py `analyze_video` (lines ~165-200) — the bottleneck
Today: `cv2.VideoCapture` (single decode thread, 47 s of `read`) + full-res `cv2.resize` per frame (40 s) = 92 s
for video A (136.8 s, 1080p60; MAX_FPS=60 so every frame is used, step=1). Replace the capture/resize/gray part:

```python
from . import decode
...
with decode.open_frames(path, width=160, fps=MAX_FPS, hw="off", pix_fmt="gray") as fs:
    for t, g in fs:                       # g: uint8 (h160, 160); swscale AREA straight from the YUV planes
        gray = g.astype(np.float32)
        (rest of the loop body unchanged: diffs / bright / thumbs from `gray`)
fps = ...   # keep: src_fps/step semantics => use probe_video(path)["fps"]; with fps=MAX_FPS >= src rate frames pass through
```
- Drop `cap`, `step`, grab/retrieve logic, `cv2.resize(frame, 160...)` and `cv2.cvtColor`; keep the 32x18 thumb resize.
- If `src_fps > MAX_FPS`, `fps=MAX_FPS` picks nearest-earlier frames by time (equivalent to `step` subsampling, not identical
  sampling instants): effective rate stays `MAX_FPS`; set `fps = MAX_FPS if src_fps > MAX_FPS else src_fps`.
- Keep the `ValueError("OpenCV could not open the video")` path: catch decode errors -> raise ValueError with the same wording
  ("could not open the video"), or fall back to the old cv2 path if `open_frames` raises on open.
- RISK: gray now comes from the Y plane scaled by swscale instead of BGR->GRAY of an INTER_AREA RGB resize. Values differ by a
  few levels (BT.601 vs full-range handling); cut/peak thresholds are MAD-relative so they should hold, but run
  `tools/real_suite.py` and the video tests before/after. If exact parity matters use `pix_fmt="bgr24"` + `cv2.cvtColor`
  (OpenCV channel order; costs ~+10 %).

## 2. amv_matcher/proxy.py `_encode` (lines ~70-86)
Decode + `frame.reformat(nw, nh, yuv420p)` already uses PyAV with `thread_type="AUTO"`. Only the decode loop changes:
```python
with decode.open_frames(vp, width=nw, height=nh, hw="off", pix_fmt="yuv420p") as fs:   # ndarray (nh*3/2, nw)
    ...  # needs VideoFrame.from_ndarray(img, format="yuv420p") per frame; pts = i, time_base = tb as today
```
Little gain (the libx264 encode dominates; reformat is already in swscale); only worth it for `thread_count=0` (set
`vs_in.thread_count = 0` — one line — instead, which is the real fix here). Recommended: do NOT migrate; just add `vs_in.thread_count = 0`.

## 3. amv_matcher/media.py `_export` non-copy path (lines ~205-214)
`vin.decode(vs_in)` -> `frame.to_ndarray(format="rgb24")` -> `from_ndarray` -> re-encode. Same advice: add
`vs_in.thread_type = "AUTO"; vs_in.thread_count = 0` after line ~155, and convert with `frame.reformat(width=vs.width,
height=vs.height, format="yuv420p")` (skips rgb round trip, ~2x less conversion work). `open_frames` is not needed
(it would drop the source timestamps the loop does not use anyway). Lines ~30 and ~41-64 are audio extraction: no change.

## Limits / risks
- HW decode only used when asked (`hw="videotoolbox"|"auto"`); coverage is per-codec (VideoToolbox: H.264/HEVC/ProRes,
  10-bit worked here via p010le; AV1/VP9 depend on the Mac). Mid-stream HW errors fall back to software without
  duplicating/skipping frames (tested with a mock).
- Windows `d3d11va`/`dxva2` and `cuda` paths are untested (no Windows machine); the code only selects them by name.
- `fast=True` (skip_frame=NONREF) is opt-in and drops frames; never used by the call sites above.
- No lowres decode (H.264 does not support it).
