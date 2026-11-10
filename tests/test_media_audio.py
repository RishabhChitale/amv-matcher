"""extract_audio keeps sample timing across an undecodable audio packet (silence fills the gap)."""
import av
import numpy as np
import soundfile as sf

from amv_matcher.media import extract_audio

SR = 44100
CLICKS = (0.5, 1.5, 2.5, 3.2)  # the corrupt packet (~2.32 s) sits between the 2nd and 3rd


def _make_src(path):
    y = np.zeros(SR * 4, np.float32)
    for t in CLICKS:
        i = int(t * SR)
        y[i:i + 200] = 0.9
    st = (np.stack([y, y]) * 32767).astype(np.int16)
    o = av.open(str(path), "w")
    a = o.add_stream("aac", rate=SR)
    a.layout = "stereo"
    fr = av.AudioFrame.from_ndarray(np.ascontiguousarray(st.T.reshape(1, -1)), format="s16", layout="stereo")
    fr.sample_rate = SR
    for pk in a.encode(fr):
        o.mux(pk)
    for pk in a.encode(None):
        o.mux(pk)
    o.close()


def _corrupt(src, dst, index):
    c = av.open(str(src))
    ins = c.streams.audio[0]
    o = av.open(str(dst), "w")
    outs = o.add_stream_from_template(ins)
    for i, p in enumerate(x for x in c.demux(ins) if x.size):
        if i == index:
            q = av.Packet(bytes([0xFF]) * p.size)
            q.pts, q.dts, q.duration, q.time_base = p.pts, p.dts, p.duration, p.time_base
            p = q
        p.stream = outs
        o.mux(p)
    o.close()
    c.close()


def _click_times(wav):
    d, sr = sf.read(str(wav))
    idx = np.where(np.abs(d[:, 0]) > 0.3)[0]
    first = [idx[j] for j in range(len(idx)) if j == 0 or idx[j] - idx[j - 1] > 1000]
    return np.array(first) / sr


def test_corrupt_packet_keeps_later_click_timing(tmp_path):
    good, bad = tmp_path / "good.mp4", tmp_path / "bad.mp4"
    _make_src(good)
    _corrupt(good, bad, 100)
    tg = _click_times(extract_audio(good, tmp_path / "g"))
    tb = _click_times(extract_audio(bad, tmp_path / "b"))
    assert len(tg) == len(tb) == len(CLICKS)
    # clicks after the corrupt packet land where they do in the clean decode, within 1 ms
    assert np.max(np.abs(tg - tb)) < 0.001, (tg, tb)
    # and spacing is the authored spacing across the gap
    assert abs((tb[2] - tb[1]) - 1.0) < 0.001 and abs((tb[3] - tb[0]) - 2.7) < 0.001
