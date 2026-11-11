"""THE BOOM: audio drops and visual booms, each a ranked list with confidence.

Audio boom = the sub-bass / kick RE-ENTRY after a build or breakdown, not the loudest point:
a jump of the low band (< LOW_HZ) over the next ~2 s against the ~4 s before, with a strong
onset at the boom itself, boosted by a preceding cue (rising riser / filter sweep in the
spectral centroid, a near-silent gap, or a low-band breakdown). Drum/bass stems are used when
the song already has cached stems (source "stems"), otherwise the mix (source "mix").

Visual boom = a surge of SUBJECT motion (flow residual after the camera fit, see flow.py)
against its local baseline, a white flash, a zoom punch or a whip pan. Hard cuts alone are NOT
booms: cuts are the sync chain's job; a boom is the frame where the picture hits.

Everything is deterministic (fixed windows, no sampling) and cheap: the audio side reuses the
STFT magnitude, the visual side reuses the flow pass and per-frame brightness.
"""
from __future__ import annotations

import numpy as np
from scipy.ndimage import maximum_filter1d, median_filter, uniform_filter1d
from scipy.signal import find_peaks

LOW_HZ = 120.0
AFTER_S = 2.0           # low band measured over [t, t+AFTER_S)
BEFORE_S = 4.0          # ... against [t-BEFORE_S, t-GAP_S)
GAP_S = 0.25
JUMP_FULL_DB = 24.0     # low-band jump that alone counts as a full-confidence re-entry
FLOOR_DB = 40.0         # the low band is floored this far under its median: digital silence is not -100 dB
MIN_JUMP_DB = 3.0
AUDIO_SEP_S = 8.0       # booms are phrase events, not beats
SNAP_S = 0.12           # snap to the strongest onset within this
AUDIO_MAX = 8
AUDIO_MIN_CONF = 0.25
BOOM_VERSION = 2        # bump when audio booms change: cached song analyses recompute them

# F6e classes (docs/TIMELINE-LOG-FORMAT.md booms[].class), from the user's 2026-11-11 review.
# Levels are dB against the song's p90 low band (0.5 s smoothed); profiles are 24 log bands.
CLS_DEEP_DB = -15.0     # low band over the 8 s before <= this: a breakdown before it -> "drop"
CLS_BODY_DB = 6.0       # 4 s after-profile this far from the median boom profile -> odd one out, "boom"
CLS_INIT_CONF = 0.8     # initial boom (groove / beat entry): first strong non-drop before the first drop
CLS_INIT_JUMP = 20.0
CLS_ENCORE_DIST = 3.5   # encore: after the main drop, sounds like it (profile distance) ...
CLS_ENCORE_JUMP = 12.0   # ... jumps in hard ...
CLS_ENCORE_BRK = -28.0  # ... and re-enters like a drop, even with no build-up (spliced audio)
CLS_MAIN_FRAC = 0.75    # main drop = the FIRST drop ranking >= this x the best drop
LEAD_BACK_S = 1.2       # lead-in snap: the hit can start up to this before the low-band peak ...
LEAD_MIN_S = 0.12       # ... and at least this before it (else the onset snap already has it)

VIS_HZ = 10.0
VIS_BASE_S = 3.0        # running-median baseline half-window
VIS_Z = 3.0
VIS_RATIO = 2.0
VIS_SEP_S = 0.6
FLASH_RISE = 35.0       # gray levels over the 0.5 s median
FLASH_MIN = 120.0
ZOOM_PUNCH = 0.45       # |log-scale| per s
PUNCH_MAX_S = 0.6
MERGE_S = 0.4
VIS_MAX = 40
VIS_MIN_CONF = 0.2


def _db(x):
    return 10.0 * np.log10(np.maximum(x, 1e-10))


def _win_mean(x, lo, hi):
    """Mean of x over [i+lo, i+hi) for every i (clipped to the array), via cumulative sums."""
    n = len(x)
    c = np.concatenate([[0.0], np.cumsum(x)])
    i = np.arange(n)
    a = np.clip(i + lo, 0, n)
    b = np.clip(i + hi, 0, n)
    cnt = np.maximum(b - a, 1)
    return (c[b] - c[a]) / cnt, (b - a)


def _p95(x):
    return float(np.percentile(x, 95)) + 1e-9 if len(x) else 1.0


def audio_booms(S, freqs, sr, hop, onset_env, low_S=None, source="mix", beat_grid=None):
    """Ranked audio booms [{t, conf, jump_db, onset, cue, kind}] plus the index of the main one.

    S: |STFT| (freq x frames) of the mix; low_S: optional |STFT| of the drums+bass stems used for
    the low-band measure. onset_env: librosa onset strength on the same hop."""
    fps = sr / hop
    L = low_S if low_S is not None else S
    low = _db((L[freqs < LOW_HZ] ** 2).sum(axis=0))
    m = min(len(low), S.shape[1], len(onset_env))
    if m < int(fps * (BEFORE_S + AFTER_S)):
        return {"booms": [], "main": None, "source": source}
    low = uniform_filter1d(low[:m], size=max(1, int(0.05 * fps)), mode="nearest")
    low = np.maximum(low, float(np.median(low)) - FLOOR_DB)
    env = np.asarray(onset_env[:m], dtype=float)
    env_n = np.clip(env / _p95(env), 0, 1.5)
    A, B, G = int(AFTER_S * fps), int(BEFORE_S * fps), int(GAP_S * fps)
    after, _ = _win_mean(low, 0, A)
    before, nb = _win_mean(low, -B, -G)
    # breakdown cue: the quietest second before the boom
    q1 = _win_mean(low, -int(1.0 * fps), -G)[0]
    jump = after - before
    jump_q = after - np.minimum(before, q1)
    # full-band loudness for the silence cue
    tot = _db((S[:, :m] ** 2).sum(axis=0))
    tot = np.maximum(tot, float(np.median(tot)) - FLOOR_DB)
    tot_before = _win_mean(tot, -int(1.0 * fps), -G)[0]
    tot_after, _ = _win_mean(tot, 0, int(0.5 * fps))
    silence = np.clip((tot_after - tot_before - 6.0) / 24.0, 0, 1)
    # riser / filter sweep: centroid (or high band) rising over the 4 s before
    pw = S[:, :m] ** 2
    cent = (freqs[:, None] * pw).sum(axis=0) / (pw.sum(axis=0) + 1e-10)
    lc = np.log2(np.maximum(cent, 50.0))
    c_late = _win_mean(lc, -int(1.0 * fps), -G)[0]
    c_early = _win_mean(lc, -B, -int(2.5 * fps))[0]
    riser = np.clip((c_late - c_early) / 1.5, 0, 1)          # +1.5 octaves = full cue
    breakdown = np.clip((before - q1 + jump_q - jump) / 18.0, 0, 1)
    cue = np.maximum.reduce([riser, silence, breakdown])
    # onset at the boom: the strongest onset within +-SNAP_S
    k = max(1, int(SNAP_S * fps))
    env_loc = maximum_filter1d(env_n, size=2 * k + 1, mode="nearest")
    jfull = np.clip((np.maximum(jump, jump_q) - MIN_JUMP_DB) / (JUMP_FULL_DB - MIN_JUMP_DB), 0, 1)
    score = np.where(np.maximum(jump, jump_q) >= MIN_JUMP_DB,
                     0.55 * jfull + 0.25 * np.clip(env_loc, 0, 1) + 0.20 * cue, 0.0)
    # only where the full before-window exists and the after-window is mostly inside the song
    score[:B] = 0.0
    score[m - int(0.5 * A):] = 0.0
    pk, _ = find_peaks(score, height=AUDIO_MIN_CONF, distance=max(1, int(AUDIO_SEP_S * fps)))
    beats = np.asarray(beat_grid or [], dtype=float)
    out = []
    for i in pk:
        a, b = max(0, i - k), min(m, i + k + 1)
        j = a + int(np.argmax(env[a:b]))               # snap to the hit
        t = j / fps
        if len(beats):
            nb_i = int(np.argmin(np.abs(beats - t)))
            if abs(beats[nb_i] - t) <= 0.08:
                t = float(beats[nb_i])
        out.append({"t": round(float(t), 3), "conf": round(float(min(1.0, score[i])), 3),
                    "jump_db": round(float(max(jump[i], jump_q[i])), 2),
                    "onset": round(float(min(1.0, env_loc[i])), 3),
                    "cue": round(float(cue[i]), 3),
                    "kind": "drop", "_rank": float(score[i])})
    out.sort(key=lambda d: (-d["_rank"], d["t"]))   # rank by the unclipped score: conf saturates at 1
    out = out[:AUDIO_MAX]
    out.sort(key=lambda d: d["t"])
    main = _classify(out, S[:, :m], freqs, fps, L, env, beats) if out else None
    for d in out:
        del d["_rank"]
    return {"booms": out, "main": main, "source": source}


def _bands(S, freqs, n=24):
    edges = np.geomspace(40.0, min(11000.0, float(freqs[-1])), n + 1)
    return _db(np.stack([(S[(freqs >= a) & (freqs < b)] ** 2).sum(axis=0)
                         for a, b in zip(edges[:-1], edges[1:])]))


def _classify(out, S, freqs, fps, L, env, beats):
    """F6e: give every boom a class (drop / initial_boom / encore / boom), snap it to the start of
    the hit and return the main boom's time. Mutates `out` (sorted by t)."""
    m = S.shape[1]
    Bd = _bands(S, freqs)
    # features at the detected (pre-snap) frame
    raw = _db((L[freqs < LOW_HZ, :m] ** 2).sum(axis=0))
    ref = float(np.percentile(raw, 90))
    low = uniform_filter1d(raw, max(1, int(0.5 * fps)), mode="nearest")
    seg = lambda x, a, b: x[max(0, a):max(max(0, a) + 1, min(len(x), b))]
    prof = []
    for d in out:
        i = int(round(d["t"] * fps))
        d["low_before"] = round(float(seg(raw, i - int(8 * fps), i - int(GAP_S * fps)).mean() - ref), 1)
        d["low_after"] = round(float(seg(raw, i, i + int(4 * fps)).mean() - ref), 1)
        d["break_db"] = round(float(seg(low, i - int(2.5 * fps), i - int(0.1 * fps)).min() - ref), 1)
        prof.append(seg(Bd.T, i, i + int(4 * fps)).mean(axis=0))
    P = np.stack(prof)
    # lead-in snap: the frame where the whole spectrum jumps (flux) at a strong onset
    D = np.r_[0.0, np.maximum(np.diff(Bd, axis=1), 0).mean(axis=0)]
    Ds = uniform_filter1d(D, 3, mode="nearest")
    p99, e95 = float(np.percentile(Ds, 99)), float(np.percentile(env, 95))
    env_loc = maximum_filter1d(env, size=5, mode="nearest")
    for d in out:
        d["t0"] = d["t"]
        if p99 <= 0:
            continue
        i = int(round(d["t"] * fps))
        a, z = max(0, i - int(LEAD_BACK_S * fps)), max(0, i - int(LEAD_MIN_S * fps))
        hit = np.nonzero((Ds[a:z] >= p99) & (env_loc[a:z] >= e95))[0]
        if len(hit):
            t = (a + int(hit[0])) / fps
            if len(beats):
                nb_i = int(np.argmin(np.abs(beats - t)))
                if abs(beats[nb_i] - t) <= 0.08:
                    t = float(beats[nb_i])
            d["lead_in"] = round(float(d["t"] - t), 3)
            d["t"] = round(float(t), 3)
    med = np.median(P, axis=0)
    body = np.abs(P - med).mean(axis=1) if len(out) >= 3 else np.zeros(len(out))
    for d, b in zip(out, body):
        odd = b > CLS_BODY_DB
        i = int(round(d["t0"] * fps))
        # a breakdown removes bass that was there: a bass-free intro before the first beat entry is no drop
        had_bass = bool(i > int(8 * fps)) and float(low[:i - int(8 * fps)].max() - ref) > CLS_DEEP_DB
        d["class"] = "boom" if odd else ("drop" if d["low_before"] <= CLS_DEEP_DB and had_bass else None)
        d["build"] = None
        d["_odd"] = bool(odd)
    drops = [k for k, d in enumerate(out) if d["class"] == "drop"]
    if drops:
        best = max(out[k]["_rank"] for k in drops)
        mk = next(k for k in drops if out[k]["_rank"] >= CLS_MAIN_FRAC * best)
    else:
        mk = max(range(len(out)), key=lambda k: (out[k]["_rank"], -out[k]["t"]))
        if out[mk]["class"] is None and out[mk]["low_before"] <= CLS_DEEP_DB:
            out[mk]["class"] = "drop"          # intro -> drop with nothing earlier: still the drop
            drops = [mk]
    first_drop = out[drops[0]]["t"] if drops else float("inf")
    next_drop = next((k for k in drops if k > mk), len(out))
    encore = False   # at most one: the first gapped repeat of the main drop before the next drop
    # initial boom: the hardest strong unclassified hit before the first drop (groove / beat entry)
    pre = [k for k, d in enumerate(out) if d["class"] is None and d["t"] < first_drop
           and (d["conf"] >= CLS_INIT_CONF or d["jump_db"] >= CLS_INIT_JUMP)]
    init = max(pre, key=lambda k: (out[k]["jump_db"], -out[k]["t"])) if pre else -1
    for k, d in enumerate(out):
        if d["class"] is not None:
            continue
        if k == init:
            d["class"] = "initial_boom"
        elif k > mk and drops and k < next_drop and not encore \
                and d["jump_db"] >= CLS_ENCORE_JUMP and d["break_db"] <= CLS_ENCORE_BRK \
                and float(np.abs(P[k] - P[mk]).mean()) <= CLS_ENCORE_DIST:
            encore = True
            d["class"] = "encore"       # heuristic: a drop again, may have no build-up
        else:
            d["class"] = "boom"
    for d in out:
        del d["_odd"], d["t0"]
    return out[mk]["t"]


def _merge(cands):
    """Merge candidates within MERGE_S; the stronger keeps its kind, every kind is listed."""
    cands = sorted(cands, key=lambda c: (-c["conf"], c["t"], c["kind"]))
    kept = []
    for c in cands:
        hit = next((k for k in kept if abs(k["t"] - c["t"]) <= MERGE_S), None)
        if hit is None:
            kept.append({**c, "kinds": [c["kind"]]})
        elif c["kind"] not in hit["kinds"]:
            hit["kinds"].append(c["kind"])
            hit["conf"] = round(min(1.0, hit["conf"] + 0.1 * c["conf"]), 3)
    kept.sort(key=lambda c: c["t"])
    return kept


def visual_booms(subject, subject_hz, bright, fps, flow_series=None, camera_events=None,
                 duration=None):
    """Ranked visual booms [{t, conf, kind motion|flash|zoom_punch|whip_pan, kinds, strength}]."""
    cands = []
    # 1. subject-motion surge vs the local baseline
    s = np.asarray(subject, dtype=float)
    if len(s) >= int(4 * subject_hz):
        base = median_filter(s, size=int(2 * VIS_BASE_S * subject_hz) | 1, mode="nearest")
        d = s - base
        mad = 1.4826 * float(np.median(np.abs(d - np.median(d))))
        scale = max(mad, 0.02 * (float(np.percentile(s, 95)) + 1e-6), 1e-4)
        z = d / scale
        ok = (z >= VIS_Z) & (s >= VIS_RATIO * np.maximum(base, 1e-4))
        sc = np.where(ok, z, 0.0)
        pk, _ = find_peaks(sc, height=VIS_Z, distance=max(1, int(VIS_SEP_S * subject_hz)))
        for i in pk:
            ratio = s[i] / max(base[i], 1e-4)
            conf = 0.6 * min(1.0, (z[i] - VIS_Z) / 9.0 + 0.2) + 0.4 * min(1.0, (ratio - 1.0) / 4.0)
            cands.append({"t": round(float(i) / float(subject_hz), 3), "conf": round(float(conf), 3),
                          "kind": "motion", "strength": round(float(s[i]), 3)})
    # 2. flash: brightness jumps far over the last 0.5 s
    b = np.asarray(bright, dtype=float)
    if len(b) > 4:
        w = max(3, int(0.5 * fps))
        pad = np.concatenate([np.full(w, b[0]), b])
        prior = np.median(np.lib.stride_tricks.sliding_window_view(pad, w)[:len(b)], axis=1)  # frames before i
        rise = b - prior
        pk, _ = find_peaks(np.where((rise >= FLASH_RISE) & (b >= FLASH_MIN), rise, 0.0),
                           height=FLASH_RISE, distance=max(1, int(VIS_SEP_S * fps)))
        for i in pk:
            cands.append({"t": round(float(i) / float(fps), 3), "conf": round(float(min(1.0, 0.3 + rise[i] / 120.0)), 3),
                          "kind": "flash", "strength": round(float(rise[i]), 1)})
    # 3. zoom punch: a short, fast zoom burst
    if flow_series is not None and len(flow_series.get("t", [])):
        t = np.asarray(flow_series["t"], dtype=float)
        zr = np.asarray(flow_series["zoom"], dtype=float)
        inl = np.asarray(flow_series["inliers"], dtype=float)
        rate = float(flow_series["rate"])
        az = np.where(inl >= 0.35, np.abs(zr), 0.0)
        az = median_filter(az, size=3, mode="nearest") if len(az) >= 3 else az
        pk, props = find_peaks(az, height=ZOOM_PUNCH, distance=max(1, int(VIS_SEP_S * rate)),
                               width=(None, max(1.0, PUNCH_MAX_S * rate)), rel_height=0.5)
        for i in pk:
            cands.append({"t": round(float(t[i]), 3), "conf": round(float(min(1.0, 0.25 + az[i] / 2.0)), 3),
                          "kind": "zoom_punch", "strength": round(float(az[i]), 3)})
    # 4. whip pans (flow.camera_events): the boom is the fastest pair of the whip
    for e in camera_events or []:
        if e.get("kind") == "whip":
            cands.append({"t": round(0.5 * (e["start"] + e["end"]), 3),
                          "conf": round(float(min(1.0, 0.3 + e["peak"] / 4.0)), 3),
                          "kind": "whip_pan", "strength": e["peak"]})
    if duration is not None:
        cands = [c for c in cands if 0.0 <= c["t"] <= float(duration)]
    merged = [c for c in _merge(cands) if c["conf"] >= VIS_MIN_CONF]
    if len(merged) > VIS_MAX:
        merged = sorted(sorted(merged, key=lambda c: -c["conf"])[:VIS_MAX], key=lambda c: c["t"])
    return merged


# --- boom alignment sub-score (score._pack) -------------------------------------------------
W_BOOM = 0.10           # bonus weight in the total (never punishes, like buildup.blend_total)
BOOM_IN_TOTAL = False   # F6e gate: boom_pct is reported in the breakdown only, not folded into total_pct,
                        # until recall vs the user's reviewed bookmarks is good (tools/boom_pr.py)
BOOM_SIGMA_S = 0.08     # credit = conf_v * gaussian(err / BOOM_SIGMA_S); +-80 ms is a hit
BOOM_MATCH_S = 0.08
BOOM_MAIN_W = 2.0       # the main audio boom counts twice


def boom_stats(song, video, off, lead=0.0):
    """How well the song's audio booms land on the video's visual booms at offset `off`
    (song_time = video_time + off; an event at video t meets the beat at t + off + lead).
    Returns None when no audio boom falls inside the video, else
    {boom_pct 0..100, boom {audio_t, video_t, vis_t, err_ms, matched}, n_booms, n_matched}."""
    ab = song.get("booms") or []
    if not ab:
        return None
    vd = float(video["duration"])
    main = song.get("main_boom")
    tv, w, is_main = [], [], []
    for b in ab:
        t = float(b["t"]) - off - lead
        if 0.0 <= t <= vd:
            m = main is not None and abs(float(b["t"]) - float(main)) < 1e-6
            tv.append(t)
            w.append(float(b.get("conf", 1.0)) * (BOOM_MAIN_W if m else 1.0))
            is_main.append(m)
    if not tv:
        return None
    tv, w = np.asarray(tv), np.asarray(w)
    vb = video.get("visual_booms") or []
    if vb:
        vt = np.asarray([float(v["t"]) for v in vb])
        vc = np.asarray([float(v.get("conf", 1.0)) for v in vb])
        j = np.argmin(np.abs(vt[None, :] - tv[:, None]), axis=1)
        err = vt[j] - tv
        credit = vc[j] * np.exp(-0.5 * (err / BOOM_SIGMA_S) ** 2)
    else:
        j, err, credit = None, np.full(len(tv), np.inf), np.zeros(len(tv))
    pct = 100.0 * float((w * credit).sum() / max(w.sum(), 1e-9))
    k = is_main.index(True) if True in is_main else int(np.argmax(w))
    fin = np.isfinite(err[k])
    info = {"audio_t": round(float(tv[k] + off + lead), 3), "video_t": round(float(tv[k]), 3),
            "vis_t": round(float(vt[j[k]]), 3) if fin else None,
            "err_ms": round(1000.0 * float(err[k]), 1) if fin else None,
            "matched": bool(fin and abs(err[k]) <= BOOM_MATCH_S), "main": bool(is_main[k])}
    return {"boom_pct": round(pct, 1), "boom": info, "n_booms": int(len(tv)),
            "n_matched": int((np.abs(err) <= BOOM_MATCH_S).sum())}


def blend_total(total, stats):
    """Fold boom_pct into the total as a bonus, never below the input. Off unless BOOM_IN_TOTAL."""
    if stats is None or W_BOOM <= 0 or not BOOM_IN_TOTAL:
        return total
    return max(total, (1.0 - W_BOOM) * total + W_BOOM * stats["boom_pct"])


def map_song_booms(song, fn):
    """{booms, main_boom} with every time passed through fn (monotone time map); {} when the song
    carries no booms. Used by the song transforms (tempo.apply_tempo, temporamp.apply_tempo_warp)."""
    if song.get("booms") is None:
        return {}
    m = song.get("main_boom")
    return {"booms": [dict(b, t=round(float(fn(float(b["t"]))), 3)) for b in song["booms"]],
            "main_boom": None if m is None else round(float(fn(float(m))), 3)}


def splice_song_booms(song, segments):
    """{booms, main_boom} remapped through splice segments (src [a, b) -> dst d0); a boom cut out is
    dropped, a repeated one appears twice; the main boom is its first kept copy (None if cut)."""
    if song.get("booms") is None:
        return {}
    m = song.get("main_boom")
    out, main = [], None
    for sg in segments:
        a, b, d0 = float(sg["src_start"]), float(sg["src_end"]), float(sg["dst_start"])
        for x in song["booms"]:
            t = float(x["t"])
            if a <= t < b:
                nt = round(t - a + d0, 3)
                out.append(dict(x, t=nt))
                if main is None and m is not None and abs(t - float(m)) < 1e-6:
                    main = nt
    out.sort(key=lambda d: d["t"])
    return {"booms": out, "main_boom": main}
