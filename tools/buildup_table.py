"""Ground-truth before/after for build_pct (score only). Media by case id only.
Usage: .venv/bin/python tools/buildup_table.py [--w W_BUILD]"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import real_suite as rs                                    # noqa: E402
from amv_matcher import paths, buildup                     # noqa: E402
from amv_matcher.score import score, score_at              # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--w", type=float, default=None)
    a = ap.parse_args()
    cases = json.loads((paths.DATA / "real_cases.json").read_text())["cases"]
    rs.OTHER_VIDEOS.update({c["id"]: c["video"] for c in cases})
    w = buildup.W_BUILD if a.w is None else a.w
    print(f"W_BUILD={w}\ncase pair kind before after build launch inten nL nB")
    for c in cases:
        v = rs.video_of(c["video"])
        for tok in c["songs"]:
            kind = rs.kind_of(tok)
            sp = rs.own_audio(c["video"]) if tok == "@own" else (rs.own_audio(rs.OTHER_VIDEOS[tok[5:]]) if kind == "cross" else tok)
            song = rs.song_of(sp)
            fn = (lambda: score_at(song, v, 0.0)) if (kind == "own" and song["duration"] >= float(v["duration"]) - rs.ORIGINAL_SLACK_S) \
                else (lambda: score(song, v, allow_negative=True))
            buildup.W_BUILD = 0.0
            b = fn()
            buildup.W_BUILD = w
            s = fn()
            name = {"own": "own", "cross": "cross", "song": "S1"}[kind]
            print(f"{c['id']:3} {tok if kind=='cross' else name:8} {kind:6} {b['total_pct']:4} {s['total_pct']:4} "
                  f"{s.get('build_pct','-')} {s.get('launch_pct','-')} {s.get('intensity_pct','-')} {s.get('n_launches','-')} {s.get('n_builds','-')}")


if __name__ == "__main__":
    main()
