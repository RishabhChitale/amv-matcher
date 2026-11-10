# U11c: speed-ramp plan format (for render.py and the Resolve/FCPXML exporter)

Source: `amv_matcher/speedramp.py` (`plan_ramps(...)`, flag-style, nothing imports it yet).
Convention everywhere: **speed = source seconds per output second** (1.0 = real time, 1.2 = plays 20% faster).

## What a plan contains

| key | meaning |
|---|---|
| `warp` | `[[src_s, out_s], ...]` sampled at `WARP_HZ = 20` (monotonic in both columns, starts `[0, 0]`, ends `[video_duration, out_duration]`). Same contract as `retime.plan_retime()["warp"]` and `retime.apply_warp`. **Renderer: use this as-is** (linear interpolation between samples is within ~1 ms of the true curve). |
| `points` | `[{t_src, speed, ease_in_s, ease_out_s}, ...]`. **Resolve/FCPXML: build speed keyframes from this.** |
| `curve` | `[[src_s, out_s, speed], ...]` at 4 Hz, for UI plotting only. |
| `out_duration` | output length in seconds. |
| `ease`, `max_speed_dev`, `max_accel` | the settings used (`smoothstep`, 0.25, 0.5 by default). |

## Meaning of `points`

- Point 0 is `t_src = 0` with no ease; its `speed` is the first plateau.
- Point k (k >= 1) starts plateau k at `t_src` (a boundary in **source** time). The speed eases from plateau k-1 to plateau k over source time `[t_src - ease_in_s, t_src + ease_out_s]` (ease_in_s == ease_out_s today). Outside the ease windows the speed is constant (a plateau).
- Ease shape is `smoothstep(x) = x*x*(3-2x)` with x = (t - a)/(b - a) over the window [a, b], unless `ease` says otherwise (`linear`, `cosine`, `smootherstep` also exist). Inside a window: speed(t) = prev + (speed - prev) * ease(x).
- Eases never overlap. Optimizer plans (`mode="coord"`) keep each ease within 45% of the space to its neighbours; anchored plans (`mode="anchored"`) put a short constant "hold" around each event anchor and ease across the gap between holds.
- Output time of a source time t: `out(t) = integral from 0 to t of 1/speed(u) du`. `warp` is exactly this, so any consumer can recompute it.

## Consumers

**render.py (gligar):** feed `plan["warp"]` where the step plan's `warp` goes today (`retime.apply_warp` + the existing warp-based frame/audio resampling). Audio: `stretch_warped` over the same warp. Because the speed changes continuously, per-section constant-speed shortcuts (one `atempo` per section) would audibly step; use the warp-based stretch.

**resolve.py / FCPXML (krokorok):** FCPXML `<timeMap>` takes `<timept time value interp>` entries: `time` = clip-local source time, `value` = retimed time, i.e. the `[src_s, out_s]` pairs of the warp (check the exact attribute orientation against the existing step-plan export). Use a smooth interpolation (`interp="smooth2"`) instead of `linear`; with linear interps Resolve shows a speed step at every timept. Suggested density: one timept per ramp boundary (src = `t_src - ease_in_s`, `t_src`, `t_src + ease_out_s`, mapped through `warp`) plus the clip ends, which gives ~15-100 timepts for a 2-minute edit. Not verified in Resolve here, so treat the `interp` choice as a suggestion to test.

## Limits a consumer can rely on

- `speed` in `[1 - max_speed_dev, 1 + max_speed_dev]` = `[0.75, 1.25]` by default.
- `|d speed / d out_s| <= max_accel` (measured on a 0.1 s grid by `stats(warp)["max_accel"]`; step plans measure ~1.0 with instant speed jumps of ~0.13).
- `stats(warp)` returns `{max_jump, max_accel, speed_tv}`; a ramp plan has `max_jump <= 0.01` (no instant steps).

Tempo ramps for the song (`temporamp.py`) use the same warp/points shape with `speed` = song seconds per output second within +-10%.
