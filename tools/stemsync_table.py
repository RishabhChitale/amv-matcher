"""Ground-truth before/after table for stem-aware scoring (score only, no auto_sync). Media by case id only.
Usage: .venv/bin/python tools/stemsync_table.py [--weight W]"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import real_suite as rs                                    # noqa: E402
from amv_matcher import paths, stems, stemsync             # noqa: E402
from amv_matcher.score import score, score_at              # noqa: E402


def feats(song_path):
    for prov in ("demucs", "hpss"):
        try:
            if stems.cached(song_path, prov):
                return stems.features_for(song_path, prov), prov
        except Exception:
            pass
    try:
        stems.separate(song_path, provider="hpss")
        return stems.features_for(song_path, "hpss"), "hpss"
    except Exception as e:
        return None, f"none({type(e).__name__})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weight", type=float, default=None)
    a = ap.parse_args()
    if a.weight is not None:
        stemsync.QUIET_STEM_WEIGHT = a.weight
    cases = json.loads((paths.DATA / "real_cases.json").read_text())["cases"]
    rs.OTHER_VIDEOS.update({c["id"]: c["video"] for c in cases})
    print(f"QUIET_STEM_WEIGHT={stemsync.QUIET_STEM_WEIGHT}")
    print("case pair   kind   before  after  quiet_sync(beats->stems)  nTargets  stems")
    for c in cases:
        v = rs.video_of(c["video"])
        for tok in c["songs"]:
            kind = rs.kind_of(tok)
            sp = rs.own_audio(c["video"]) if tok == "@own" else (rs.own_audio(rs.OTHER_VIDEOS[tok[5:]]) if kind == "cross" else tok)
            song = rs.song_of(sp)
            f, prov = feats(sp)
            tg = stemsync.stem_targets(song, f) if f else []
            fn = (lambda **k: score_at(song, v, 0.0, **k)) if (kind == "own" and song["duration"] >= float(v["duration"]) - rs.ORIGINAL_SLACK_S) \
                else (lambda **k: score(song, v, allow_negative=True, **k))
            b, s = fn(), fn(stem_targets=tg)
            name = {"own": "own", "cross": "cross", "song": "S1"}[kind]
            print(f"{c['id']:4} {tok if kind=='cross' else name:8} {kind:6} {b['total_pct']:6.1f} {s['total_pct']:6.1f}  "
                  f"{s.get('quiet_sync_beats','-')}->{s.get('quiet_sync','-')} ev={s.get('quiet_events','-')}  {len(tg):4}  {prov}")


if __name__ == "__main__":
    main()
