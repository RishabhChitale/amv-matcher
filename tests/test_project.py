import json
import os
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from amv_matcher import project as P
from amv_matcher.project_api import router


@pytest.fixture
def app_data(tmp_path, monkeypatch):
    d = tmp_path / "appdata"
    monkeypatch.setattr(P, "APP_DATA", d)
    return d


@pytest.fixture
def client(app_data):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _media(tmp_path, name="v.mp4", body=b"x" * 100):
    f = tmp_path / name
    f.write_bytes(body)
    return P.media_entry(f)


def test_round_trip(tmp_path, app_data):
    v = _media(tmp_path)
    data = {"name": "My AMV", "video": v, "sources": [{**_media(tmp_path, "a.mp3"), "source": "song"}],
            "in_use": v["path"], "offset": -0.63, "effects": {"boom": 0.5}}
    p = tmp_path / "x.amvproj"
    P.save(p, data)
    d = P.load(p)
    assert d["name"] == "My AMV" and d["offset"] == -0.63 and d["effects"]["boom"] == 0.5
    assert d["video"]["path"] == v["path"] and not d["video"].get("missing")
    assert d["created"] and d["modified"]


def test_defaults_filled(tmp_path, app_data):
    p = tmp_path / "min.amvproj"
    p.write_text(json.dumps({"version": 1, "name": "m"}))
    d = P.load(p)
    assert d["splice"] == {"on": False, "max_cuts": 0}
    assert d["autosync"]["plan"] is None and d["autosync"]["lead_s"] == 0.06
    assert set(d["effects"]) >= {"boom", "decay", "drops_only", "shake"}
    assert d["export"]["quality"] and d["ui"] == {} and d["sources"] == []


def test_bad_version_and_extension(tmp_path, app_data):
    p = tmp_path / "f.amvproj"
    p.write_text(json.dumps({"version": 99}))
    with pytest.raises(P.ProjectError):
        P.load(p)
    with pytest.raises(P.ProjectError):
        P.save(tmp_path / "f.json", {})
    p.write_text("{not json")
    with pytest.raises(P.ProjectError):
        P.load(p)


def test_missing_and_changed_media_flagged(tmp_path, app_data):
    v, a = _media(tmp_path), _media(tmp_path, "a.mp3")
    p = tmp_path / "x.amvproj"
    P.save(p, {"video": v, "sources": [{**a, "source": "song"}]})
    os.remove(v["path"])
    (tmp_path / "a.mp3").write_bytes(b"changed size!")
    d = P.load(p)
    assert d["video"]["missing"] is True and d["sources"][0]["missing"] is True


def test_relink_finds_moved_file(tmp_path, app_data):
    v = _media(tmp_path, "clip.mp4", b"z" * 321)
    os.remove(v["path"])
    root = tmp_path / "newhome"
    (root / "deep" / "er").mkdir(parents=True)
    (root / "deep" / "er" / "clip.mp4").write_bytes(b"z" * 321)
    (root / "deep" / "clip.mp4").write_bytes(b"wrong size")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "clip.mp4").write_bytes(b"z" * 321)
    found = P.relink(v, [root])
    assert found == [str(root / "deep" / "er" / "clip.mp4")]
    assert P.relink(v, [tmp_path / "nope"]) == []


def test_recent_order_and_max(tmp_path, app_data):
    for i in range(12):
        P.save(tmp_path / f"p{i}.amvproj", {"name": f"p{i}"})
        P.add_recent(tmp_path / f"p{i}.amvproj", f"p{i}")
    r = P.recent()
    assert len(r) == 10 and r[0]["name"] == "p11" and r[-1]["name"] == "p2"
    P.add_recent(tmp_path / "p5.amvproj", "p5")
    assert P.recent()[0]["name"] == "p5" and len(P.recent()) == 10
    os.remove(tmp_path / "p5.amvproj")
    assert P.recent()[0]["missing"] is True and P.recent()[1]["missing"] is False


def test_atomic_write_keeps_old_on_crash(tmp_path, app_data, monkeypatch):
    p = tmp_path / "x.amvproj"
    P.save(p, {"name": "old"})
    before = p.read_bytes()

    def boom(*a, **k):
        raise OSError("simulated crash")
    monkeypatch.setattr(P.os, "replace", boom)
    with pytest.raises(OSError):
        P.save(p, {"name": "new"})
    assert p.read_bytes() == before
    assert [f.name for f in tmp_path.iterdir() if f.suffix == ".tmp"] == []


def test_api(tmp_path, client, app_data):
    v = _media(tmp_path)
    p = str(tmp_path / "api.amvproj")
    r = client.post("/api/project/save", json={"path": p, "data": {"name": "A", "video": v}})
    assert r.status_code == 200
    r = client.post("/api/project/load", json={"path": p})
    assert r.status_code == 200 and r.json()["data"]["name"] == "A" and r.json()["missing"] == []
    assert client.get("/api/project/recent").json()["recent"][0]["path"] == str((tmp_path / "api.amvproj").resolve())
    os.remove(v["path"])
    assert len(client.post("/api/project/load", json={"path": p}).json()["missing"]) == 1
    assert client.post("/api/project/save", json={"path": str(tmp_path / "bad.txt"), "data": {}}).status_code == 400
    assert client.post("/api/project/load", json={"path": str(tmp_path / "none.amvproj")}).status_code == 404
    r = client.post("/api/project/relink", json={"entry": v, "roots": [str(tmp_path)]})
    assert r.status_code == 200 and r.json()["candidates"] == []
    r = client.post("/api/project/autosave", json={"data": {"name": "../evil/name"}})
    assert r.status_code == 200
    ap = r.json()["path"]
    assert os.path.dirname(ap) == str(app_data / "autosave") and ap.endswith(".amvproj") and os.path.isfile(ap)
