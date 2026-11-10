"""Runs inside the stems venv (.venv-stems): htdemucs on a stereo 44.1 kHz wav -> one wav per stem.

usage: python _demucs_runner.py in.wav out_dir
Prints 'PROGRESS <0..1>' lines on stdout; weights are cached under TORCH_HOME (set by the parent).
"""
import re
import sys
from pathlib import Path


def main(src, out_dir):
    import numpy as np
    import soundfile as sf
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    torch.set_num_threads(max(1, torch.get_num_threads()))
    print("PROGRESS 0.02 loading-model", flush=True)
    model = get_model("htdemucs")
    model.eval()
    wav, sr = sf.read(src, dtype="float32", always_2d=True)   # (n, ch)
    assert sr == model.samplerate, (sr, model.samplerate)
    x = torch.from_numpy(np.ascontiguousarray(wav.T))
    if x.shape[0] == 1:
        x = x.repeat(2, 1)
    ref = x.mean(0)
    x = (x - ref.mean()) / (ref.std() + 1e-8)
    # apply_model reports progress through tqdm on stderr; hook it so the parent sees fractions
    import demucs.apply as ap

    class _Bar:
        def __init__(self, it, *a, **k):
            self.it = list(it)

        def __iter__(self):
            n = max(1, len(self.it))
            for i, v in enumerate(self.it):
                print("PROGRESS %.3f separating" % (0.05 + 0.93 * i / n), flush=True)
                yield v

    ap.tqdm.tqdm = _Bar
    with torch.no_grad():
        out = apply_model(model, x[None], device="cpu", split=True, overlap=0.25, shifts=0,
                          progress=True, num_workers=0)[0]
    out = out * ref.std() + ref.mean()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, st in zip(model.sources, out):
        a = st.numpy().T
        sf.write(str(out_dir / f"{name}.wav"), np.clip(a, -1, 1), sr, subtype="PCM_16")
    print("PROGRESS 1.0 done", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
