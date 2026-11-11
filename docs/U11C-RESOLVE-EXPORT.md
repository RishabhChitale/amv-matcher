# U11c: Resolve (FCPXML) export of speed and tempo ramps

Applies only when the plan carries ramps; constant-speed and step-plan exports are byte-identical to before.

## Plan keys (see U11C-RAMP-FORMAT.md)
- `plan["retime_ramps"]` (preferred) else `plan["retime"]`, used as a ramp when it has a `warp` and at least 2 `points`.
- `plan["tempo_ramp"]["warp"]` (optional, not produced yet): song seconds per output second.

## Video
One `asset-clip` (start 0, duration = ramp `out_duration`) with a `<timeMap>`. Time points are strictly increasing in output time; values are source times.
- `stepped` (default, `RAMP_MODE_DEFAULT`): timepts from the sampled `warp`.
- `smooth` (opt-in until someone confirms it in a real Resolve; the smooth/inTime/outTime timemap is unverified there, while frame-sampled linear points give the same smooth look and are the safe import): timepts at each ramp boundary from `points` with smooth interpolation. Select with `rt["resolve_mode"]` or `build_fcpxml(ramp_mode=...)`.
- Markers have source times interpolated through the timept list; connected-audio offsets stay in output time (assumed to use the output timebase; unverified in Resolve).

## Anchoring (`RAMP_ANCHOR`, unverified without a real Resolve)
`absolute-values` (default) or `relative-values` (clip start = first source frame, values from 0). The two are identical while the ramp starts at source 0.

## Audio
`prepare_audio_ramp` bakes a stereo `<stem>_tempoR.wav` at speed 1 (`temporamp.stretch_warped` per channel), so the timeline audio needs no retime.

## Validation
`resolve_ramps.validate_timemap` (DTD checker); tests in `tests/test_resolve_u11c.py`.
