#!/usr/bin/env python3
"""F22 trajectory numbers per real case, by case id only (stats, dynamic range, follow r, ramps).

Usage: .venv/bin/python tools/f22_traj.py B D E F [--songs own|all] [--no-sync] [--json out.json]
       AMV_F22=1 .venv/bin/python tools/f22_traj.py ...      # flag on: totals include the sub-score

Cases come from <app data dir>/real_cases.json (NOT in git), same format and song tokens as
tools/real_suite.py ("@own", "@own:<id>", or a song file). Song files are labelled song1, song2, ...
by their position in the case; no file name or path is ever printed.

Per pair it prints:
  raw   the coupling profile at the scorer's alignment (own audio at offset 0 when it covers the
        video, otherwise score()'s best offset): follow r, lag, dyn_video / dyn_song, ratio, peak,
        sub_score, and the scorer's total
  sync  the same block from auto_sync's final["trajectory"] (output time, after retime), the
        auto-sync total, and the ramps into the strongest visual booms (video vs song rise, match)
The profile is always computed; AMV_F22=1 only changes whether totals include the sub-score.
"""
from __future__ import annotations

import argparse, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from amv_matcher import media, paths, trajectory as T     # noqa: E402
from amv_matcher.audio import analyze_song                 # noqa: E402
from amv_matcher.autosync import auto_sync                 # noqa: E402
from amv_matcher.score import score, score_at              # noqa: E402
from amv_matcher.video import analyze_video                # noqa: E402

ORIGINAL_SLACK_S = 0.5
KEYS = ("follow", "lag_s", "dyn_video", "dyn_song", "ratio", "peak", "sub_score")


def _own(vpath):
    wav = media.extract_audio(vpath, paths.CACHE / "extracted")
    return str(wav) if wav else None


def _tokens(case, which):
    out, n = [], 0
    for tok in case.get("songs") or ["@own"]:
        if tok.startswith("@own"):
            lab = "own" if tok == "@own" else "own:" + tok[5:]
        else:
            n += 1
            lab = f"song{n}"
        if which == "all" or tok == "@own":
            out.append((tok, lab))
    return out


def _line(tag, d):
    if not d:
        return f"  {tag:4s} (no overlap)"
    return f"  {tag:4s} " + "  ".join(f"{k}={d.get(k)}" for k in KEYS)


def run(case, tok, lab, videos, sync=True, budget=20.0):
    video = analyze_video(case["video"])
    sp = _own(case["video"]) if tok == "@own" else _own(videos[tok[5:]]) if tok.startswith("@own:") else tok
    song = analyze_song(sp)
    own = tok == "@own"
    if own and song["duration"] >= float(video["duration"]) - ORIGINAL_SLACK_S:
        sc = score_at(song, video, 0.0)
    else:
        sc = score(song, video, allow_negative=True)
    off = float(sc.get("best_offset_s") or 0.0)
    row = {"case": case["id"], "pair": lab, "flag": T.F22_ENABLED, "offset": round(off, 2),
           "total": sc["total_pct"], "raw": T.traj_stats(song, video, off)}
    if sync:
        r = auto_sync(song, video, target=100, budget_s=budget, original=own)
        tj = r["final"].get("trajectory") or {}
        row.update(sync_total=r["final"]["total_pct"], sync_offset=round(float(r["plan"].get("offset") or 0), 2),
                   sync={k: tj.get(k) for k in KEYS}, ramps=tj.get("ramps") or [])
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("ids", nargs="+", help="case ids, e.g. B D E F")
    ap.add_argument("--cases", default=str(paths.DATA / "real_cases.json"))
    ap.add_argument("--songs", choices=("own", "all"), default="own")
    ap.add_argument("--no-sync", action="store_true", help="skip auto_sync (raw alignment only)")
    ap.add_argument("--budget", type=float, default=20.0)
    ap.add_argument("--json", help="also write the rows to this JSON file")
    a = ap.parse_args()
    cases = {c["id"]: c for c in json.loads(Path(a.cases).read_text())["cases"]}
    videos = {k: c["video"] for k, c in cases.items()}
    rows = []
    print(f"F22 trajectory (AMV_F22={'1' if T.F22_ENABLED else 'off'}, reference={T.REFERENCE})")
    for cid in a.ids:
        if cid not in cases:
            print(f"{cid}: unknown case id")
            continue
        for tok, lab in _tokens(cases[cid], a.songs):
            try:
                row = run(cases[cid], tok, lab, videos, not a.no_sync, a.budget)
            except Exception as e:                       # message only: never a path
                print(f"{cid} {lab}: error {type(e).__name__}")
                continue
            rows.append(row)
            print(f"{cid} {lab}: total={row['total']} offset={row['offset']}"
                  + (f"  sync_total={row['sync_total']} sync_offset={row['sync_offset']}" if "sync" in row else ""))
            print(_line("raw", row["raw"]))
            if "sync" in row:
                print(_line("sync", row["sync"]))
                for rp in row["ramps"]:
                    print(f"    ramp boom_t={rp['boom_t']} strength={rp['strength']} video={rp['video_ramp']} "
                          f"song={rp['song_ramp']} match={rp['match']}")
    if a.json:
        Path(a.json).write_text(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
