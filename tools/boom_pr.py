"""F6e: precision/recall of detected booms vs the user's bookmarks, from exported timeline logs.

    python tools/boom_pr.py LOG.json [LOG2.json|LOG.csv ...] [--audio-tol 0.05] [--video-tol 0.08] [--json]

Each log is an F6d export (docs/TIMELINE-LOG-FORMAT.md). A CSV export holds bookmarks only, so it needs
the matching JSON export for the detected side: pass `--detected JSON` (applied to every CSV given).
Prints per log and pooled numbers; media are identified by the log's hash prefix, never by name."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from amv_matcher import timelinelog as TL  # noqa: E402


def load(path, detected=None):
    text = Path(path).read_text(encoding="utf-8")
    if str(path).lower().endswith(".csv"):
        lg = TL.parse_csv(text)
        if detected:
            d = TL.parse(Path(detected).read_text(encoding="utf-8"))
            for tr in TL.TRACKS:
                lg[tr]["detected"] = d[tr].get("detected")
        return lg
    return TL.parse(text)


def label(lg, i):
    h = lambda tr: ((lg[tr].get("media") or {}).get("hash") or "")[:8] or "?"
    return f"log{i + 1} v:{h('video')} a:{h('audio')}"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--audio-tol", type=float, default=TL.AUDIO_TOL_S)
    ap.add_argument("--video-tol", type=float, default=TL.VIDEO_TOL_S)
    ap.add_argument("--detected")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    res, tot = [], {"audio": [0, 0, 0], "video": [0, 0, 0]}
    for i, p in enumerate(a.logs):
        lg = load(p, a.detected)
        r = TL.evaluate(lg, a.audio_tol, a.video_tol)
        r["log"] = label(lg, i)
        res.append(r)
        for tr in ("audio", "video"):
            tot[tr][0] += r[tr]["gt"]
            tot[tr][1] += r[tr]["detected"]
            tot[tr][2] += r[tr]["hits"]
    pooled = {tr: TL._pr(*v) for tr, v in tot.items()}
    if a.json:
        print(json.dumps({"logs": res, "pooled": pooled}, indent=1))
        return 0
    f = lambda x: "-" if x is None else f"{x:.2f}"
    for r in res:
        au, v = r["audio"], r["video"]
        print(f"{r['log']}: audio R {f(au['recall'])} P {f(au['precision'])} ({au['hits']}/{au['gt']} gt, "
              f"{au['detected']} det) class {au['class_hits']}/{au['class_total']} main_hit {au['main_hit']} "
              f"missed {au['missed']} | video R {f(v['recall'])} P {f(v['precision'])} "
              f"({v['hits']}/{v['gt']}) missed {v['missed']}")
        if au["confusion"]:
            print("   class confusion:", ", ".join(au["confusion"]))
    print(f"pooled (tol audio {a.audio_tol} s, video {a.video_tol} s): "
          + " | ".join(f"{tr} R {f(p['recall'])} P {f(p['precision'])} ({p['hits']}/{p['gt']})"
                       for tr, p in pooled.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
