#!/usr/bin/env python3
"""Real-media regression suite: scores the user's own videos/songs numerically and writes a report.

Usage: .venv/bin/python tools/real_suite.py [--render] [--structure] [--no-discover] [--cases PATH]
                                            [--report docs/real-suite-latest.md] [--budget 20]

Cases live in <app data dir>/real_cases.json (NOT in git):
  {"cases": [{"id": "A", "video": "/abs/video.mp4",
              "songs": ["@own", "/abs/song.mp3", "@own:B"],
              "expect": {"own_min": 60, "cross_max": 40, "song_min": 50}}]}
Song tokens: "@own" = the video's own soundtrack (kind own), "@own:<id>" = another case video's
soundtrack on this video (kind cross), anything else is a song file (kind song).
Expect keys: own_min / song_min (alias songS1_like_min) are minimum total %, cross_max a maximum.
Discover mode adds uploads/songs not yet in cases as 'exploratory' cases (scores only, no pass/fail).
Videos are only ever referred to by case id in output; file names/paths of videos are never printed.
Exit code 1 on any failure. Numeric only: never looks at frames.
"""
from __future__ import annotations

import argparse, hashlib, json, sys, tempfile, time, traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from amv_matcher import media, paths                       # noqa: E402
from amv_matcher.audio import AUDIO_EXTS, analyze_song     # noqa: E402
from amv_matcher.autosync import auto_sync                 # noqa: E402
from amv_matcher.score import score, score_at              # noqa: E402
from amv_matcher.video import VIDEO_EXTS, analyze_video    # noqa: E402

ORIGINAL_SLACK_S = 0.5
EXTRACTED = paths.CACHE / "extracted"
UPLOADS = paths.UPLOADS
SEED_VIDEO_MATCH = {"A": "videoA"}     # id -> substring of the video file name (never printed)

_VIDEOS: dict = {}
_SONGS: dict = {}
_SECRETS: list = []                     # strings scrubbed from any printed/written text


def scrub(s) -> str:
    s = str(s)
    for secret, repl in _SECRETS:
        s = s.replace(secret, repl)
    return s


def video_of(path: str) -> dict:
    if path not in _VIDEOS:
        _VIDEOS[path] = analyze_video(path)
    return _VIDEOS[path]


def song_of(path: str) -> dict:
    if path not in _SONGS:
        _SONGS[path] = analyze_song(path)
    return _SONGS[path]


def list_media(folder: Path, exts) -> list:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in exts and not p.name.startswith("."))


def seed_cases() -> list:
    """First-run seed: video A (name contains 'videoA') and the other uploaded video as B."""
    vids = list_media(UPLOADS / "video", VIDEO_EXTS)
    songs = [str(p) for p in list_media(UPLOADS / "song", AUDIO_EXTS)]
    a = [v for v in vids if SEED_VIDEO_MATCH["A"] in v.name.lower()]
    b = [v for v in vids if v not in a]
    cases = []
    if a:
        cases.append({"id": "A", "video": str(a[0]), "songs": ["@own"] + songs + (["@own:B"] if b else []),
                      "expect": {}})
    if b:
        cases.append({"id": "B", "video": str(b[0]), "songs": ["@own"] + songs + (["@own:A"] if a else []),
                      "expect": {}})
    return cases


def vid_label(path: str) -> str:
    return "V" + hashlib.sha1(path.encode()).hexdigest()[:6]


def discover(cases: list) -> list:
    """Uploaded videos/songs not yet in any case become exploratory cases (no expectations)."""
    known_v = {str(Path(c["video"]).resolve()) for c in cases}
    known_s = {str(Path(s).resolve()) for c in cases for s in c["songs"] if not s.startswith("@")}
    extra = []
    vids = list_media(UPLOADS / "video", VIDEO_EXTS)
    songs = list_media(UPLOADS / "song", AUDIO_EXTS)
    new_songs = [str(s) for s in songs if str(s.resolve()) not in known_s]
    for v in vids:
        if str(v.resolve()) not in known_v:
            extra.append({"id": vid_label(str(v)), "video": str(v), "exploratory": True,
                          "songs": ["@own"] + [str(s) for s in songs], "expect": {}})
    for c in cases:
        if new_songs:
            extra.append({"id": c["id"] + "+new", "video": c["video"], "exploratory": True,
                          "songs": new_songs, "expect": {}})
    return extra


def own_audio(vpath: str):
    wav = media.extract_audio(vpath, EXTRACTED)
    return str(wav) if wav else None


def kind_of(token: str) -> str:
    if token == "@own":
        return "own"
    return "cross" if token.startswith("@own:") else "song"


def clip_plan(plan: dict, secs: float) -> dict:
    """Cut a plan to its first `secs` output seconds (warp pairs are [src, out])."""
    p = json.loads(json.dumps(plan))
    rt = p.get("retime") or {}
    warp = rt.get("warp")
    if warp:
        keep = [w for w in warp if w[1] <= secs]
        if not keep or keep[-1][1] < secs:
            nxt = next((w for w in warp if w[1] > secs), None)
            if nxt and keep:
                a, b = keep[-1], nxt
                f = (secs - a[1]) / max(1e-9, b[1] - a[1])
                keep.append([a[0] + f * (b[0] - a[0]), secs])
            else:
                keep.append([secs, secs])
        rt["warp"], rt["out_duration"] = keep, secs
        p["retime"] = rt
    return p


def do_render(vpath: str, song_path: str, plan: dict, secs: float = 5.0) -> dict:
    from amv_matcher import render
    with tempfile.TemporaryDirectory(prefix="amv-suite-") as d:
        t = time.time()
        r = render.render_plan(vpath, clip_plan(plan, secs), None, Path(d) / "x.mp4",
                               interp="blend", song_path=song_path)
        return {"seconds": round(time.time() - t, 1), "frames": r.get("frames"),
                "out_duration": r.get("out_duration")}


def run_pair(case: dict, token: str, args) -> dict:
    vpath = case["video"]
    kind = kind_of(token)
    row = {"case": case["id"], "pair": token, "kind": kind, "error": None}
    t0 = time.time()
    try:
        video = video_of(vpath)
        if token == "@own":
            sp = own_audio(vpath)
        elif kind == "cross":
            other = OTHER_VIDEOS[token[5:]]
            sp = own_audio(other)
        else:
            sp = token
        if not sp:
            raise RuntimeError("no audio track")
        song = song_of(sp)
        t1 = time.time()
        if kind == "own" and song["duration"] >= float(video["duration"]) - ORIGINAL_SLACK_S:
            sc = score_at(song, video, 0.0)
        else:
            sc = score(song, video, allow_negative=True)
        row.update(total=sc["total_pct"], sync=sc["sync_pct"], arc=sc["arc_pct"], tempo=sc["tempo_pct"],
                   tight=(sc.get("tightness") or {}).get("tight_pct"), score_s=round(time.time() - t1, 1))
        if args.structure:
            row["sections"] = len(song.get("sections") or [])
        t2 = time.time()
        try:
            r = auto_sync(song, video, target=100, budget_s=args.budget, original=(kind == "own"))
            f = r["final"]
            row.update(natural=f.get("natural_total_pct"), after=f["total_pct"], after_tight=f.get("tight_pct"), sync_s=round(time.time() - t2, 1))
            if args.render:
                try:
                    row["render"] = do_render(vpath, sp, r["plan"])
                except Exception as e:
                    row["render"] = {"error": f"{type(e).__name__}: {e}"[:80]}
        except Exception as e:
            row["autosync_error"] = f"{type(e).__name__}: {e}"[:80]
    except Exception as e:
        row["error"] = f"{type(e).__name__}: {e}"[:120]
        if args.verbose:
            traceback.print_exc()
    row["total_s"] = round(time.time() - t0, 1)
    return row


def check(case: dict, row: dict) -> str:
    """'' = pass, otherwise the failure reason; 'n/a' when no expectation applies."""
    if case.get("exploratory"):
        return "explore"
    if row["error"]:
        return "FAIL error"
    exp, kind, total = case.get("expect") or {}, row["kind"], row["total"]
    if kind == "own" and "own_min" in exp:
        return "pass" if total >= exp["own_min"] else f"FAIL own {total}<{exp['own_min']}"
    if kind == "cross" and "cross_max" in exp:
        return "pass" if total <= exp["cross_max"] else f"FAIL cross {total}>{exp['cross_max']}"
    if kind == "song":
        lo = exp.get("song_min", exp.get("songS1_like_min"))
        if lo is not None:
            return "pass" if total >= lo else f"FAIL song {total}<{lo}"
    return "n/a"


def fmt(v):
    return "-" if v is None else (f"{v:g}" if isinstance(v, (int, float)) else str(v))


def label_pair(token: str) -> str:
    if token == "@own":
        return "own audio"
    if token.startswith("@own:"):
        return f"audio of {token[5:]}"
    return Path(token).name.split("-", 1)[-1].strip() if "-" in Path(token).name else Path(token).name


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cases", default=str(paths.DATA / "real_cases.json"))
    ap.add_argument("--report", default=str(Path(__file__).resolve().parent.parent / "docs" / "real-suite-latest.md"))
    ap.add_argument("--render", action="store_true", help="also render a 5 s blend excerpt per pair")
    ap.add_argument("--structure", action="store_true", help="also record the song-structure section count")
    ap.add_argument("--no-discover", action="store_true")
    ap.add_argument("--budget", type=float, default=20.0, help="auto-sync budget seconds")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cpath = Path(args.cases)
    if cpath.is_file():
        cases = json.loads(cpath.read_text())["cases"]
    else:
        cases = seed_cases()
        cpath.write_text(json.dumps({"cases": cases}, indent=2))
        print(f"seeded {cpath.name} with {len(cases)} case(s)")
    for c in cases:
        _SECRETS.append((c["video"], f"<video {c['id']}>"))
        _SECRETS.append((Path(c["video"]).name, f"<video {c['id']}>"))
    global OTHER_VIDEOS
    OTHER_VIDEOS = {c["id"]: c["video"] for c in cases}
    run = list(cases)
    if not args.no_discover:
        extra = discover(cases)
        for c in extra:
            _SECRETS.append((c["video"], f"<video {c['id']}>"))
            _SECRETS.append((Path(c["video"]).name, f"<video {c['id']}>"))
            OTHER_VIDEOS.setdefault(c["id"], c["video"])
        run += extra

    rows, fails = [], 0
    t_all = time.time()
    for c in run:
        for token in c["songs"]:
            row = run_pair(c, token, args)
            row["verdict"] = check(c, row)
            fails += row["verdict"].startswith("FAIL")
            rows.append(row)
            print(scrub(f"{row['case']:>8} {label_pair(token)[:28]:<28} total={fmt(row.get('total'))} "
                        f"after={fmt(row.get('after'))} {row['verdict']} {row['error'] or ''}"), flush=True)

    lines = ["# Real-media regression suite (latest run)", "",
             f"Run {time.strftime('%Y-%m-%d %H:%M')} · {len(rows)} pairs · {fails} failing · "
             f"{round(time.time() - t_all)} s. Numeric only; videos are identified by case id.", "",
             "| case | pair | total | sync | tight | arc | auto-sync (before→after) | verdict | score s | sync s |"
             + (" sections |" if args.structure else "") + (" render 5 s |" if args.render else ""),
             "|---|---|---|---|---|---|---|---|---|---|" + ("---|" if args.structure else "") + ("---|" if args.render else "")]
    for r in rows:
        ba = f"{fmt(r.get('total'))}→{fmt(r.get('after'))}" if r.get("after") is not None else (r.get("autosync_error") or "-")
        line = (f"| {r['case']} | {label_pair(r['pair'])} | {fmt(r.get('total'))} | {fmt(r.get('sync'))} | "
                f"{fmt(r.get('tight'))} | {fmt(r.get('arc'))} | {ba} | {r['verdict']} | "
                f"{fmt(r.get('score_s'))} | {fmt(r.get('sync_s'))} |")
        if args.structure:
            line += f" {fmt(r.get('sections'))} |"
        if args.render:
            rd = r.get("render") or {}
            line += f" {rd.get('error') or (str(rd.get('seconds')) + ' s' if rd else 'n/a')} |"
        rows_err = f" ERROR {r['error']}" if r["error"] else ""
        lines.append(scrub(line + rows_err))
    Path(args.report).write_text("\n".join(lines) + "\n")
    print(f"report: {args.report}  failing: {fails}")
    return 1 if fails else 0


OTHER_VIDEOS: dict = {}

if __name__ == "__main__":
    sys.exit(main())
